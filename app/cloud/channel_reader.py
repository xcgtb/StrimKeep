"""Bounded MTProto reads. Sessions stay encrypted; cursors commit with inbox records."""
import asyncio
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from app.cloud import credentials, services, store, events
from app.cloud.errors import CloudError

DEFAULT = {"sealed": "", "pending": "", "expires": 0}
_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cloud-channels")
_future = None

# 节奏策略放在 pacing 里：纯计算、无依赖，可脱离 Telegram 单测。
from app.cloud.pacing import (  # noqa: E402  (放在常量之后，保持原有阅读顺序)
    FLOOD_MARGIN_SEC, ChannelFloodWait, flood_wait_seconds, next_poll_delay,
)


def get(account_id):
    cfg = services.get(account_id, "telegram_reader", DEFAULT)
    return {"revision": cfg["revision"], "connected": bool(cfg["config"]["sealed"]),
            "pending": bool(cfg["config"]["pending"] and cfg["config"]["expires"] > time.time())}


def _client(secret):
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    return TelegramClient(StringSession(secret.get("session", "")), int(secret["api_id"]), secret["api_hash"],
                          connection_retries=0, request_retries=0, flood_sleep_threshold=0, timeout=12)


def _run(coro):
    try:
        return asyncio.run(asyncio.wait_for(coro, 45))
    except CloudError:
        raise
    except Exception as exc:
        seconds = flood_wait_seconds(exc)
        if seconds:
            # 保留秒数往上抛：调度必须按真实等待时间退避，否则会被越罚越久。
            raise ChannelFloodWait(seconds) from None
        raise CloudError("Telegram 操作失败，请检查网络、登录信息或稍后重试；未自动重发验证码", 502) from None


async def _login(secret, action, code="", password=""):
    from telethon.errors import SessionPasswordNeededError
    client = _client(secret)
    try:
        await client.connect()
        if action == "code":
            sent = await client.send_code_request(secret["phone"])
            secret["phone_code_hash"] = sent.phone_code_hash
        elif action == "verify":
            try:
                await client.sign_in(phone=secret["phone"], code=code, phone_code_hash=secret["phone_code_hash"])
            except SessionPasswordNeededError:
                if not password:
                    return {**secret, "session": client.session.save(), "needs_password": True}
                await client.sign_in(password=password)
        elif action == "password":
            await client.sign_in(password=password)
        else:
            if not await client.is_user_authorized():
                raise CloudError("Telegram 会话未登录", 409)
        secret["session"] = client.session.save()
        return secret
    finally:
        await client.disconnect()


def login(account_id, body):
    action = body.get("action")
    expected = body.get("expected_revision")
    current = services.get(account_id, "telegram_reader", DEFAULT)
    if current["revision"] != expected:
        raise CloudError("Telegram 登录配置已变化，请刷新", 409)
    if action == "disconnect":
        patch = dict(DEFAULT)
    elif action in ("code", "import"):
        api_id, api_hash = str(body.get("api_id", "")), str(body.get("api_hash", ""))
        if not re.fullmatch(r"[1-9][0-9]{0,11}", api_id) or not re.fullmatch(r"[a-fA-F0-9]{32}", api_hash):
            raise CloudError("请填写 Telegram API ID 与32位 API Hash")
        secret = {"api_id": api_id, "api_hash": api_hash}
        if action == "code":
            phone = str(body.get("phone", ""))
            if not re.fullmatch(r"\+[0-9]{7,16}", phone):
                raise CloudError("手机号需包含国际区号，例如 +86…")
            secret["phone"] = phone
        else:
            session = body.get("session", "")
            if not isinstance(session, str) or not 100 < len(session) < 2000:
                raise CloudError("请填写有效的 Telethon StringSession")
            secret["session"] = session
        result = _run(_login(secret, "code" if action == "code" else "import"))
        patch = {"pending": credentials.seal(result), "expires": time.time()+600} if action == "code" else {"sealed": credentials.seal(result), "pending": "", "expires": 0}
    elif action == "verify":
        if not current["config"]["pending"] or current["config"]["expires"] < time.time():
            raise CloudError("验证码登录已过期，请重新获取", 409)
        secret = credentials.unseal(current["config"]["pending"])
        password, code = str(body.get("password", "")), str(body.get("code", ""))
        if len(password) > 256 or len(code) > 12:
            raise CloudError("验证码或两步验证密码过长")
        result = _run(_login(secret, "password" if secret.get("needs_password") else "verify", code, password))
        if result.get("needs_password") and not password:
            patch = {"pending": credentials.seal(result), "expires": time.time()+600}
        else:
            result.pop("needs_password", None)
            patch = {"sealed": credentials.seal(result), "pending": "", "expires": 0}
    else:
        raise CloudError("Telegram 登录操作无效")
    services.save(account_id, "telegram_reader", patch, expected, DEFAULT, lambda cfg, old: cfg)
    value = get(account_id)
    value["needs_password"] = bool(action == "verify" and result.get("needs_password") and not body.get("password"))
    return value


async def _messages(secret, rule, cursor, initialized):
    from telethon.utils import get_peer_id
    client = _client(secret)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise CloudError("Telegram 用户会话已失效，请重新登录", 409)
        address = rule["address"]
        entity = await client.get_entity(int(address) if address.startswith("-100") else address)
        if not getattr(entity, "broadcast", False):
            raise CloudError("目标不是可读频道", 409)
        # Initial zero means start at the current tip without importing old posts.
        limit = max(1, rule.get("history_limit", 0)) if not initialized else max(1, rule.get("history_limit", 20) or 20)
        options = {"limit": limit} if not initialized else {"min_id": cursor, "reverse": True, "limit": limit}
        messages = [m async for m in client.iter_messages(entity, **options)]
        tip = max([cursor, *[m.id for m in messages]])
        if not initialized and not rule.get("history_limit", 0):
            return [], tip, False
        result = []
        for m in sorted(messages, key=lambda v: v.id):
            entities = [{"type": "text_link", "url": e.url} for e in (m.entities or []) if getattr(e, "url", None)]
            for row in (getattr(getattr(m, 'reply_markup', None), 'rows', None) or [])[:20]:
                entities.extend({'type':'text_link','url':b.url} for b in (getattr(row,'buttons',None) or [])[:20] if isinstance(getattr(b,'url',None),str))
            result.append({"message_id": m.id, "date": int(m.date.timestamp()), "chat": {"id": get_peer_id(entity)}, "text": m.message or "", "entities": entities})
        # 取满上限说明后面可能还有未读：让调度立刻再跑一轮，而不是干等一个周期。
        backlog = bool(initialized and len(messages) >= limit)
        return result, tip, backlog
    finally:
        await client.disconnect()


def poll_one():
    from app.cloud import receiving
    now = time.time()
    with store.db() as con:
        row = con.execute("SELECT c.id,c.account_id,c.account_revision,c.revision,c.payload,s.cursor,s.initialized FROM cloud_channels c JOIN cloud_accounts a ON a.id=c.account_id LEFT JOIN cloud_channel_cursors s ON s.id=c.id WHERE json_extract(c.payload,'$.enabled')=1 AND json_extract(c.payload,'$.mode')='user' AND COALESCE(s.next_poll,0)<=? ORDER BY COALESCE(s.next_poll,0) LIMIT 1", (now,)).fetchone()
    if not row:
        return False
    rid, aid, arev, rev, raw, cursor, initialized = row
    rule = json.loads(raw)
    error = ""
    try:
        account = store.account(aid)
        cfg = services.get(aid, "telegram_reader", DEFAULT)
        if not account or account["revision"] != arev or cfg["account_revision"] != arev or not cfg["config"]["sealed"]:
            raise CloudError("账号授权或 Telegram 会话变化，请重新登录并保存频道规则", 409)
        events.emit('channel','正在读取频道：'+rule['name'])
        # 兼容旧签名（2 元组）：有些测试与早期调用方只返回 messages/tip。
        loaded = _run(_messages(credentials.unseal(cfg["config"]["sealed"]), rule, cursor or 0, bool(initialized)))
        if len(loaded) >= 3:
            messages, tip, backlog = loaded[0], loaded[1], bool(loaded[2])
        else:
            messages, tip, backlog = loaded[0], loaded[1], False
        token_scope = "user:"+rule["address"]
        with store.db() as con:
            current = con.execute("SELECT revision,payload FROM cloud_channels WHERE id=?", (rid,)).fetchone()
            session = con.execute("SELECT revision FROM cloud_service_settings WHERE account_id=? AND kind='telegram_reader'", (aid,)).fetchone()
            account = con.execute("SELECT revision FROM cloud_accounts WHERE id=?", (aid,)).fetchone()
            if not current or current[0] != rev or not json.loads(current[1])["enabled"] or not session or session[0] != cfg["revision"] or not account or account[0] != arev:
                return True
            position = con.execute('SELECT cursor,initialized FROM cloud_channel_cursors WHERE id=?', (rid,)).fetchone()
            if (position or (None, None)) != (cursor, initialized):
                return True
            for msg in messages:
                receiving.record_channel(con, rid, aid, arev, rev, rule, msg, token_scope)
            if messages:
                events.emit('channel','频道读取完成：'+rule['name']+' · '+str(len(messages))+' 条消息已检查',con=con)
            if backlog:
                events.emit('channel','频道仍有积压，立即继续：'+rule['name'],con=con)
            delay = next_poll_delay(default_seconds=rule.get("poll_seconds", 300), backlog=backlog)
            con.execute("INSERT INTO cloud_channel_cursors VALUES(?,?,1,?, '') ON CONFLICT(id) DO UPDATE SET cursor=excluded.cursor,initialized=1,next_poll=excluded.next_poll,last_error=''", (rid, tip, time.time()+delay))
            con.execute("INSERT INTO cloud_public_channel_status VALUES(?,?,?,?,0) ON CONFLICT(channel_id) DO UPDATE SET last_success=excluded.last_success,last_check=excluded.last_check,last_count=excluded.last_count,failures=0", (rid,time.time(),time.time(),len(messages)))
        return True
    except Exception as exc:
        failed_at = time.time()
        # 限流必须按服务端秒数退避；提前重试会把等待时间罚得更长。
        seconds = exc.seconds if isinstance(exc, ChannelFloodWait) else flood_wait_seconds(exc)
        if seconds:
            error = 'Telegram 限流，需等待 %d 秒后重试' % seconds
            delay = next_poll_delay(error_seconds=seconds)
            events.emit('channel',rule['name']+' · '+error,level='WARNING')
        else:
            error = str(exc) if isinstance(exc, CloudError) else "频道读取失败，请检查登录、权限和网络"
            delay = next_poll_delay()
            events.emit('channel',rule['name']+' · '+error,level='WARNING')
    with store.db() as con:
        current = con.execute('SELECT c.revision,c.payload,a.revision FROM cloud_channels c JOIN cloud_accounts a ON a.id=c.account_id WHERE c.id=?', (rid,)).fetchone()
        position = con.execute('SELECT cursor,initialized FROM cloud_channel_cursors WHERE id=?', (rid,)).fetchone()
        if not current or current[0] != rev or current[2] != arev or not json.loads(current[1]).get('enabled') or (position or (None,None)) != (cursor, initialized):
            return True
        con.execute("INSERT INTO cloud_public_channel_status VALUES(?,0,?,0,1) ON CONFLICT(channel_id) DO UPDATE SET last_check=excluded.last_check,failures=failures+1", (rid,failed_at))
        con.execute("INSERT INTO cloud_channel_cursors VALUES(?,0,0,?,?) ON CONFLICT(id) DO UPDATE SET next_poll=excluded.next_poll,last_error=excluded.last_error", (rid, failed_at+delay, error))
    return True


def drain(limit=3):
    """一次调度里连续处理多个到期频道。

    单个 tick 只读一个频道时，N 个频道的实际轮询周期会被拉长到 N 倍；
    这里按上限连跑，跑满即停，剩下的交给下一个 tick。
    """
    for _ in range(max(1, int(limit))):
        if not poll_one():
            return


def schedule():
    global _future
    if _future is None or _future.done():
        with store.db() as con:
            exists = con.execute("SELECT 1 FROM cloud_channels c JOIN cloud_accounts a ON a.id=c.account_id LEFT JOIN cloud_channel_cursors s ON s.id=c.id WHERE json_extract(c.payload,'$.enabled')=1 AND json_extract(c.payload,'$.mode')='user' AND COALESCE(s.next_poll,0)<=? LIMIT 1", (time.time(),)).fetchone()
        if exists:
            _future = _POOL.submit(drain)
