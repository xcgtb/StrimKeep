"""Telegram 用户会话（MTProto）：用来读私密频道。

只读：get_entity / get_messages / iter_dialogs，不发送、不删除、不改频道权限。
- StringSession 用 store.crypto 加密后存 settings['telegram_session']
- api_id / api_hash / phone 同样加密存 settings['telegram_api']
- 登录中的临时客户端只存在内存，进程重启即作废（需要重新走一次验证码）

与公开预览页（channel_monitor.PREVIEW）产出同样的结构：
    {'title': str, 'messages': [{'id','text','links'}], 'has_more': bool}
所以下游的去重、解析、落盘、扫描授权完全复用。
"""
import asyncio

from telethon import TelegramClient, errors
from telethon.tl.types import MessageEntityTextUrl, MessageEntityUrl
from telethon.sessions import StringSession

SESSION_KEY = 'telegram_session'
# 这是设置项的键名，不是凭据本身；变量名带 SETTING 以免被密钥扫描误判。
API_SETTING = 'telegram_api'
FETCH_LIMIT = 20


class SessionError(RuntimeError):
    """会话不可用（未登录、被登出、网络不可达）。"""


class TelegramSession:
    def __init__(self, store):
        self.store = store
        self._client = None
        self._pending = None          # {'client','phone','hash','api_id','api_hash'}
        self._lock = asyncio.Lock()

    # ---------- 持久化 ----------
    def saved(self):
        return self.store.get(SESSION_KEY, '') or ''

    def api(self):
        return self.store.get(API_SETTING, {}) or {}

    def status(self):
        api = self.api()
        return {'logged_in': bool(self.saved()),
                'phone': api.get('phone', ''),
                'api_id': api.get('api_id', '')}

    # ---------- 客户端 ----------
    async def client(self):
        """返回已登录的客户端；没登录返回 None。"""
        if self._client is not None:
            return self._client
        saved, api = self.saved(), self.api()
        if not saved or not api.get('api_id') or not api.get('api_hash'):
            return None
        async with self._lock:
            if self._client is None:
                client = TelegramClient(StringSession(saved), int(api['api_id']), api['api_hash'])
                try:
                    await client.connect()
                    if not await client.is_user_authorized():
                        raise SessionError('Telegram 会话已失效，请重新登录')
                except SessionError:
                    await client.disconnect()
                    raise
                except Exception as exc:
                    await client.disconnect()
                    raise SessionError('Telegram 连接失败：%s' % str(exc)[:120]) from None
                self._client = client
        return self._client

    async def close(self):
        if self._client is not None:
            await self._client.disconnect()
            self._client = None


    # ---------- 登录 ----------
    async def send_code(self, api_id, api_hash, phone):
        """第一步：用 api_id/api_hash 申请验证码。"""
        api_id, api_hash, phone = str(api_id).strip(), str(api_hash).strip(), str(phone).strip()
        if not api_id.isdigit():
            raise SessionError('api_id 必须是数字（my.telegram.org 里获取）')
        if not api_hash:
            raise SessionError('api_hash 不能为空')
        if not phone.startswith('+') or not phone[1:].isdigit():
            raise SessionError('手机号要带国家码，例如 +8613800000000')
        if self._pending and self._pending.get('client'):
            try:
                await self._pending['client'].disconnect()
            except Exception:
                pass
        client = TelegramClient(StringSession(), int(api_id), api_hash)
        try:
            await client.connect()
            sent = await client.send_code_request(phone)
        except Exception as exc:
            await client.disconnect()
            raise SessionError('发送验证码失败：%s' % str(exc)[:120]) from None
        self._pending = {'client': client, 'phone': phone, 'hash': sent.phone_code_hash,
                         'api_id': api_id, 'api_hash': api_hash}
        self.store.event('telegram', '已发送 Telegram 登录验证码（%s）。' % phone)
        return {'sent': True}

    async def verify(self, code):
        """第二步：提交验证码。开了二步验证会返回 need_password。"""
        if not self._pending:
            raise SessionError('请先发送验证码')
        client, phone = self._pending['client'], self._pending['phone']
        try:
            await client.sign_in(phone=phone, code=str(code).strip(),
                                 phone_code_hash=self._pending['hash'])
        except errors.SessionPasswordNeededError:
            return {'need_password': True}
        except errors.PhoneCodeInvalidError:
            raise SessionError('验证码不正确') from None
        except errors.PhoneCodeExpiredError:
            raise SessionError('验证码已过期，请重新发送') from None
        except Exception as exc:
            raise SessionError('登录失败：%s' % str(exc)[:120]) from None
        await self._finish()
        return {'ok': True, 'need_password': False}

    async def password(self, password):
        """二步验证密码。"""
        if not self._pending:
            raise SessionError('请先发送验证码')
        try:
            await self._pending['client'].sign_in(password=str(password))
        except Exception as exc:
            raise SessionError('二步验证失败：%s' % str(exc)[:120]) from None
        await self._finish()
        return {'ok': True}

    async def _finish(self):
        client = self._pending['client']
        phone = self._pending['phone']
        api_id, api_hash = self._pending['api_id'], self._pending['api_hash']
        self.store.set_many({SESSION_KEY: client.session.save(),
                             API_SETTING: {'api_id': api_id, 'api_hash': api_hash, 'phone': phone}})
        self._client = client
        self._pending = None
        self.store.event('telegram', 'Telegram 用户会话已登录（%s）。' % phone)

    async def logout(self):
        if self._pending and self._pending.get('client'):
            try:
                await self._pending['client'].disconnect()
            except Exception:
                pass
        self._pending = None
        client = self._client
        self._client = None
        if client is not None:
            try:
                await client.log_out()
            except Exception:
                pass
        self.store.set_many({SESSION_KEY: '', API_SETTING: {}})
        self.store.event('telegram', 'Telegram 用户会话已退出。')

    # ---------- 读取 ----------
    async def dialogs(self):
        """已加入的频道（含私密），供前端勾选。"""
        client = await self.client()
        if client is None:
            raise SessionError('未登录 Telegram 用户会话')
        out = []
        try:
            async for dialog in client.iter_dialogs():
                entity = dialog.entity
                if getattr(dialog, 'is_channel', False):
                    kind = 'channel'
                elif getattr(dialog, 'is_group', False):
                    kind = 'group'
                elif getattr(entity, 'bot', False):
                    kind = 'bot'        # 只列机器人；普通联系人不在监控范围
                else:
                    continue
                username = getattr(entity, 'username', None) or ''
                out.append({'id': str(dialog.id), 'title': dialog.title or getattr(dialog, 'name', '') or '',
                            'username': username, 'private': not bool(username), 'kind': kind})
        except SessionError:
            raise
        except Exception as exc:
            raise SessionError('读取频道列表失败：%s' % str(exc)[:120]) from None
        order = {'channel': 0, 'group': 1, 'bot': 2}
        out.sort(key=lambda item: (order.get(item['kind'], 9), item['private'], item['title']))
        return out

    async def fetch(self, name, before=None):
        """读一个频道的最新消息，产出与预览页相同的结构。"""
        client = await self.client()
        if client is None:
            raise SessionError('未登录 Telegram 用户会话')
        try:
            raw_name = str(name).strip()
            # -100… 带负号，isdigit() 会判 False，这里去掉符号再判断
            target = int(raw_name) if raw_name.lstrip('-').isdigit() else raw_name
            try:
                entity = await client.get_entity(target)
            except ValueError:
                # 重启后实体缓存是空的：先列一遍会话把 access_hash 补上，再取一次。
                async for _ in client.iter_dialogs():
                    pass
                entity = await client.get_entity(target)
            messages = await client.get_messages(entity, limit=FETCH_LIMIT + 1,
                                                 max_id=int(before) if before else 0)
        except SessionError:
            raise
        except Exception as exc:
            raise SessionError('读取频道 %s 失败：%s' % (name, str(exc)[:120])) from None
        has_more = len(messages) > FETCH_LIMIT
        messages = list(reversed(messages[:FETCH_LIMIT]))
        rows = []
        for message in messages:
            text = message.message or ''
            # 超链接（显示文字 + 隐藏 URL）不在 text 里，要从 entities 取
            links = []
            for entity in (message.entities or []):
                if isinstance(entity, MessageEntityTextUrl) and entity.url:
                    links.append(entity.url)
                elif isinstance(entity, MessageEntityUrl):
                    links.append(text[entity.offset:entity.offset + entity.length])
            # 机器人基本把链接放在内联按钮里，正文是空的 —— 只取 entities 会一条都抓不到。
            markup = getattr(message, 'reply_markup', None)
            for markup_row in (getattr(markup, 'rows', None) or []):
                for button in (getattr(markup_row, 'buttons', None) or []):
                    url = getattr(button, 'url', None)
                    if url:
                        links.append(url)
            links = list(dict.fromkeys(links))      # 正文和按钮可能指向同一个链接
            if not text and not links:
                continue
            rows.append({'id': message.id, 'text': text, 'links': links})
        # 拿不到真实名字时回空，不要回退成 -100 开头的 ID：
        # 那个 ID 会被当成频道名存起来，后面再也换不回真名。
        title = (getattr(entity, 'title', '') or
                 ' '.join(filter(None, [getattr(entity, 'first_name', ''), getattr(entity, 'last_name', '')])) or
                 getattr(entity, 'username', '') or '')
        if not title:
            # 有些目标 get_entity 拿回来的对象不带名字，扫一遍会话按 ID 找。
            try:
                async for dialog in client.iter_dialogs():
                    if str(dialog.id) == str(target):
                        title = dialog.name or dialog.title or ''
                        break
            except Exception:
                pass
        return {'title': title,
                'messages': rows, 'has_more': has_more}
