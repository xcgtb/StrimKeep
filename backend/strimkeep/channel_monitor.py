"""公开频道监控：读 Telegram 给游客看的 t.me/s/<频道> 预览页。

为什么不用 Bot API 的 channel_post：那要求 bot 是频道管理员，而资源频道基本
都不是自己的，这个前提不成立。公开频道有一份服务端渲染的预览页，未登录的
普通 HTTP 客户端就能读，还支持 ?before=<消息ID> 往回翻，所以监控别人的频道
既不需要 bot 权限，也不需要用户账号和 api_id/api_hash。

边界（写在代码里，别指望它万能）：
- 只适用于公开频道；私密频道/群组没有预览页。
- 预览只给最近的消息，翻页深度有限，所以首次接入只登记游标、不回溯历史。
- 拿不到编辑历史、评论、投票细节；帖子被删就永久消失。

每个频道可以单独配：是否启用、落库方式（免转存 STRM / 转存到 115 目录）、
正文正则（命中才收 / 命中就跳过）。全局可以配轮询间隔或 cron。
"""
import asyncio
import html as html_lib
import json
import re
import time
from datetime import datetime

from croniter import croniter

from .cloud115 import CloudError
from . import share_service
from .share_service import enqueue_organize
from .transfer import shares as parse_shares
from .message_templates import channel_notice

PREVIEW = 'https://t.me/s/{name}'
USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36')
CHANNEL_NAME = re.compile(r'[A-Za-z][A-Za-z0-9_]{3,31}')
MESSAGE_HEAD = re.compile(r'<div class="tgme_widget_message[^"]*"[^>]*data-post="([^"/]+)/(\d+)"')
TITLE = re.compile(r'<meta property="og:title" content="([^"]*)"')
TIME = re.compile(r'<time[^>]*datetime="([^"]+)"')
HREF = re.compile(r'href="([^"]+)"')
TAG = re.compile(r'<[^>]+>')
HAS_MORE = 'js-messages_more'
MAX_TEXT = 4000
RULE_DEFAULTS = {'enabled': True, 'share_mode': '', 'share_cid': '', 'share_label': '',
                 'offline_cid': '', 'offline_label': '', 'magnet_cid': None, 'magnet_label': None,
                 'ed2k_cid': None, 'ed2k_label': None, 'include': '', 'exclude': '', 'routes': [],
                 'organize_after': False, 'source': 'preview', 'history_pages': 1}
OFFLINE_PREFIX = ('magnet:', 'ed2k://')


def normalize_channel(value, strict=False):
    """把 @name / name / t.me/name / t.me/s/name 统一成 name；邀请链接一律拒绝。"""
    text = str(value or '').strip()
    text = re.sub(r'(?i)^(?:https?://)?(?:www\.)?t(?:elegram)?\.me/', '', text)
    text = re.sub(r'(?i)^s/', '', text)
    text = text.lstrip('@').split('?')[0].strip('/')
    if CHANNEL_NAME.fullmatch(text):
        return text
    # 私密频道没有 username：用用户会话读取时允许数字 id（含 -100 前缀）。
    if re.fullmatch(r'-?\d{6,}', text):
        return text
    if strict:
        raise ValueError('只支持公开频道用户名，例如 @channel 或 https://t.me/channel；邀请链接不行。')
    return ''


def normalize_rule(raw):
    """老配置里频道是纯字符串；落点从「单 mode + 单目录」迁到「分享 + 离线」两路。"""
    rule = dict(RULE_DEFAULTS)
    if isinstance(raw, str):
        rule['name'] = normalize_channel(raw)
    elif isinstance(raw, dict):
        rule.update({k: raw[k] for k in RULE_DEFAULTS if k in raw})
        rule['name'] = normalize_channel(raw.get('name'))
    else:
        return None
    source = raw if isinstance(raw, dict) else {}
    legacy_mode = str(source.get('mode') or '')
    legacy_target = str(source.get('target_cid') or '')
    legacy_label = str(source.get('target_label') or '')
    rule['enabled'] = bool(rule['enabled'])
    share_mode = str(rule.get('share_mode') or '')
    if not share_mode:
        # 老规则迁移：strm 只生成 STRM；transfer 转存到原目录；offline 原来不处理分享。
        share_mode = {'strm': 'strm', 'transfer': 'transfer', 'offline': 'transfer'}.get(legacy_mode, 'strm')
    if share_mode == 'none':
        share_mode = 'transfer'      # 旧「不处理分享」= 115 网盘落盘 + 空转存目录
    rule['share_mode'] = share_mode if share_mode in ('strm', 'transfer') else 'strm'
    rule['share_cid'] = str(rule.get('share_cid') or (legacy_target if legacy_mode == 'transfer' else ''))
    rule['share_label'] = str(rule.get('share_label') or (legacy_label if legacy_mode == 'transfer' else ''))
    rule['offline_cid'] = str(rule.get('offline_cid') or (legacy_target if legacy_mode == 'offline' else ''))
    rule['offline_label'] = str(rule.get('offline_label') or (legacy_label if legacy_mode == 'offline' else ''))
    for kind in ('magnet', 'ed2k'):
        # Legacy STRM mode skipped both types even if an old directory existed.
        if rule.get(kind+'_cid') is None:
            if rule['share_mode'] == 'strm':
                rule[kind+'_cid'] = ''
                rule[kind+'_label'] = ''
            else:
                # 模式 A：分享转存、磁力、ED2K 统一落到同一个 115 落盘目录。
                rule[kind+'_cid'] = rule['share_cid'] or rule['offline_cid']
                rule[kind+'_label'] = rule['share_label'] or rule['offline_label']
        else:
            rule[kind+'_cid'] = str(rule[kind+'_cid'] or '')
            rule[kind+'_label'] = str(rule.get(kind+'_label') or '')
    rule['organize_after'] = bool(rule.get('organize_after'))
    # 来源：preview=公开预览页（游客可看）；session=用户会话（可读私密频道）
    rule['source'] = 'session' if str(rule.get('source') or '') == 'session' else 'preview'
    try:
        rule['history_pages'] = max(1, min(20, int(rule.get('history_pages') or 1)))
    except (TypeError, ValueError):
        rule['history_pages'] = 1
    rule['include'] = str(rule['include'] or '')
    rule['exclude'] = str(rule['exclude'] or '')
    routes = []
    for raw_route in (rule.get('routes') or []):
        if not isinstance(raw_route, dict):
            continue
        cid = str(raw_route.get('target_cid') or '')
        keywords = str(raw_route.get('keywords') or '').strip()
        if not cid or not keywords:
            continue
        routes.append({'keywords': keywords, 'target_cid': cid,
                       'target_label': str(raw_route.get('target_label') or '') or cid})
    rule['routes'] = routes
    return rule if rule['name'] else None


def route_target(rule, text):
    """按关键词选目录：命中第一条分流规则就用它的目录，否则用频道默认目录。"""
    lowered = str(text or '').casefold()
    for route in rule.get('routes') or []:
        words = [w.strip().casefold() for w in str(route.get('keywords') or '').split(',') if w.strip()]
        if words and any(w in lowered for w in words):
            return route['target_cid'], route['target_label']
    return rule['target_cid'], rule['target_label']


def _plain(fragment):
    text = fragment.replace('<br/>', '\n').replace('<br>', '\n')
    return html_lib.unescape(TAG.sub('', text)).strip()


def parse_channel(page):
    """解析预览页：返回 {'title', 'messages': [{id, text, links, at}], 'has_more'}。"""
    title_match = TITLE.search(page)
    title = html_lib.unescape(title_match.group(1)) if title_match else ''
    parts = MESSAGE_HEAD.split(page)
    messages = []
    # parts = [前导, 频道名1, 消息ID1, 片段1, 频道名2, 消息ID2, 片段2, ...]
    for index in range(1, len(parts) - 2, 3):
        message_id = int(parts[index + 1])
        chunk = parts[index + 2]
        start = chunk.find('class="tgme_widget_message_text')
        if start >= 0:
            start = chunk.find('>', start) + 1
            end = chunk.find('tgme_widget_message_footer', start)
            body = chunk[start:end if end > 0 else len(chunk)]
        else:
            body = ''
        links = [html_lib.unescape(url) for url in HREF.findall(chunk)]
        time_match = TIME.search(chunk)
        messages.append({'id': message_id, 'text': _plain(body)[:MAX_TEXT],
                         'links': links, 'at': time_match.group(1) if time_match else ''})
    messages.sort(key=lambda item: item['id'])
    return {'title': title, 'messages': messages, 'has_more': HAS_MORE in page}


class ChannelMonitor:
    """按频道游标增量读预览页；两次请求之间留间隔，429 就退避。"""

    MIN_INTERVAL = 60
    GAP = 1.5              # 两次请求 t.me 之间至少隔这么久
    MAX_PAGES = 2          # 一次最多往回翻两页（40 条），够追上突发

    def __init__(self, engine):
        self.engine, self.store = engine, engine.store
        self.task = None
        self.clock = time.monotonic
        self.sleep = asyncio.sleep
        self._next_fetch = 0.0
        with self.store.db:
            self.store.db.execute('''CREATE TABLE IF NOT EXISTS channel_state(
                name TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '',
                last_message_id INTEGER, checked REAL NOT NULL DEFAULT 0,
                state TEXT NOT NULL DEFAULT 'new', error TEXT NOT NULL DEFAULT '',
                posts INTEGER NOT NULL DEFAULT 0, added REAL NOT NULL)''')
            self.store.db.execute('''CREATE TABLE IF NOT EXISTS channel_shares(
                share_code TEXT PRIMARY KEY, name TEXT NOT NULL,
                message_id INTEGER NOT NULL, job_id INTEGER, created REAL NOT NULL)''')
            self.store.db.execute('''CREATE TABLE IF NOT EXISTS channel_posts(
                name TEXT NOT NULL, message_id INTEGER NOT NULL,
                created REAL NOT NULL, PRIMARY KEY(name, message_id))''')
            self.store.db.execute('''CREATE TABLE IF NOT EXISTS channel_cancelled_shares(
                share_code TEXT PRIMARY KEY, created REAL NOT NULL)''')
            self.store.db.execute('''CREATE TABLE IF NOT EXISTS channel_share_attempts(
                name TEXT NOT NULL, message_id INTEGER NOT NULL, share_code TEXT NOT NULL,
                attempts INTEGER NOT NULL, skipped INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '', updated REAL NOT NULL,
                PRIMARY KEY(name, message_id, share_code))''')
            self.store.db.execute('''CREATE TABLE IF NOT EXISTS channel_backfill(
                name TEXT PRIMARY KEY, cursor INTEGER NOT NULL, before_id INTEGER NOT NULL,
                title TEXT NOT NULL, messages TEXT NOT NULL)''')
        # STRM jobs and their share claims are committed together. On restart,
        # replay posts beyond the durable cursor; already queued shares deduplicate.
        # Offline submissions may have reached 115, so never replay those here.
        with self.store.db:
            for rule in self.config()['channels']:
                if rule['share_mode'] == 'strm':
                    self.store.db.execute('''DELETE FROM channel_posts WHERE name=? AND message_id >
                        COALESCE((SELECT last_message_id FROM channel_state WHERE name=?), 0)''',
                        (rule['name'], rule['name']))

    def config(self):
        raw = self.store.get('channel_monitor') or {}
        channels, seen = [], set()
        for item in raw.get('channels') or []:
            rule = normalize_rule(item)
            if rule and rule['name'] not in seen:
                seen.add(rule['name'])
                channels.append(rule)
        try:
            interval = int(raw.get('interval') or 300)
        except (TypeError, ValueError):
            interval = 300
        cron = str(raw.get('cron') or '').strip()
        return {'enabled': bool(raw.get('enabled')), 'notify': bool(raw.get('notify', True)),
                'schedule_mode': 'cron' if raw.get('schedule_mode') == 'cron' and cron else 'interval',
                'interval': max(self.MIN_INTERVAL, min(86400, interval)), 'cron': cron,
                'channels': channels}

    def save(self, config):
        self.store.set_many({'channel_monitor': config})
        for rule in config['channels']:
            with self.store.db:
                self.store.db.execute('INSERT OR IGNORE INTO channel_state(name,added) VALUES(?,?)',
                                      (rule['name'], time.time()))
        self._mark_duplicates()
        return self.config()

    def _mark_duplicates(self):
        """同名频道只留一个：同一个频道的用户名和数字 ID 两种写法会重复抓取。

        现场：`ltxjasna`（用户名）和 `-1004419698318`（数字 ID）其实是同一个频道 ——
        频道原本公开（有用户名），改成私密后用户名失效、只剩数字 ID，于是又从用户
        会话列表加了一条。旧的用户名条目不会自己消失，只会每轮 302。

        按抓到的 title 判重，后加的标 duplicate_of。**正本要选能正常抓取的那条**：
        转私密后旧的用户名条目一直是 error，它 added 更早，按 added 排会把它当正本，
        反而把唯一能用的数字 ID 条目跳过。
        """
        cols = {row[1] for row in self.store.db.execute('PRAGMA table_info(channel_state)')}
        if 'duplicate_of' not in cols:
            with self.store.db:
                self.store.db.execute('ALTER TABLE channel_state ADD COLUMN duplicate_of TEXT')
        names = {rule['name'] for rule in self.config()['channels']}
        rows = self.store.db.execute(
            "SELECT name,title,duplicate_of FROM channel_state "
            "ORDER BY CASE WHEN state='error' THEN 1 ELSE 0 END, added").fetchall()
        seen, changed = {}, []
        with self.store.db:
            for row in rows:
                if row['name'] not in names:
                    # 已经从配置里移除：游标留着（误删恢复能续上），但不参与判重。
                    continue
                title = str(row['title'] or '').strip()
                owner = seen.get(title) if title else None
                if title and not owner:
                    seen[title] = row['name']
                if row['duplicate_of'] != owner:
                    self.store.db.execute('UPDATE channel_state SET duplicate_of=? WHERE name=?',
                                          (owner, row['name']))
                    if owner:
                        changed.append((row['name'], owner))
        # 只在判定变化时通知一次：每轮抓取都发会刷屏（0.9.15 踩过同样的坑）。
        for name, owner in changed:
            self.store.event('channel', '频道 %s 与 %s 是同一个频道，已跳过以免重复抓取。'
                             % (name, owner))
        return seen

    def rule(self, name):
        for item in self.config()['channels']:
            if item['name'] == name:
                return item
        return dict(RULE_DEFAULTS, name=name)

    def forget(self, name):
        with self.store.db:
            self.store.db.execute('DELETE FROM channel_state WHERE name=?', (name,))
            self.store.db.execute('DELETE FROM channel_posts WHERE name=?', (name,))
            self.store.db.execute('DELETE FROM channel_shares WHERE name=?', (name,))
            self.store.db.execute('DELETE FROM channel_backfill WHERE name=?', (name,))
        config = self.config()
        config['channels'] = [c for c in config['channels'] if c['name'] != name]
        self.store.set_many({'channel_monitor': config})
        return self.config()

    def _adopt_orphan(self, name, title):
        """同一个频道换了写法，把旧条目的跟进位置和已处理记录接过来。

        现场：频道从公开转私密后用户名被释放，用户在界面上删掉旧条目、再从会话列表
        重加数字 ID。移除只是从配置里拿掉，`channel_state` 还留着（误删恢复靠它），
        但账本是按名字记的 —— 新名字认不到旧账本，首轮当"首次接入"重扫一页，
        磁力/电驴又往 115 离线提交一遍。

        抓到的标题两种模式都有，就按标题认人：配置里已经没有、又抓过东西的同名旧行，
        就是同一个频道的上一条记录。搬完把旧行删掉，免得下轮再认一次。
        """
        title = str(title or '').strip()
        if not title:
            return None
        state = self._state(name)
        if state and state['last_message_id']:
            return None      # 自己已经抓出东西了，不用接续
        names = {rule['name'] for rule in self.config()['channels']}
        rows = self.store.db.execute(
            'SELECT name,last_message_id FROM channel_state '
            'WHERE title=? AND name<>? AND last_message_id IS NOT NULL',
            (title, name)).fetchall()
        orphans = [row for row in rows if row['name'] not in names]
        if not orphans:
            return None
        # 同名旧行有好几条时，挑跟进最远的那个。
        orphan = max(orphans, key=lambda row: row['last_message_id'])['name']
        old = self._state(orphan)
        with self.store.db:
            # 新名字下可能是上一轮留下的空账本，先清掉；主键撞上就跳过那条。
            self.store.db.execute('DELETE FROM channel_posts WHERE name=?', (name,))
            self.store.db.execute('UPDATE OR IGNORE channel_posts SET name=? WHERE name=?',
                                  (name, orphan))
            self.store.db.execute('UPDATE OR IGNORE channel_backfill SET name=? WHERE name=?',
                                  (name, orphan))
            self.store.db.execute(
                'UPDATE channel_state SET last_message_id=?, posts=?, title=? WHERE name=?',
                (old['last_message_id'], old['posts'], title, name))
            self.store.db.execute('DELETE FROM channel_state WHERE name=?', (orphan,))
        self.store.event('channel', '频道 %s 与已移除的 %s 是同一个频道，已接上原来的跟进位置'
                         '（消息 %s），不会重扫旧帖。' % (title, orphan, old['last_message_id']))
        return old['last_message_id']

    def sources(self):
        rows = {row['name']: dict(row) for row in self.store.db.execute('SELECT * FROM channel_state')}
        result = []
        for rule in self.config()['channels']:
            row = rows.get(rule['name']) or {'name': rule['name'], 'title': '', 'last_message_id': None,
                                             'checked': 0, 'state': 'new', 'error': '', 'posts': 0}
            result.append({**row, **{k: rule[k] for k in RULE_DEFAULTS}})
        return result

    def _state(self, name):
        row = self.store.db.execute('SELECT * FROM channel_state WHERE name=?', (name,)).fetchone()
        return dict(row) if row else None

    def _remember(self, name, **values):
        values['checked'] = time.time()
        with self.store.db:
            self.store.db.execute('UPDATE channel_state SET ' + ','.join(k + '=?' for k in values) + ' WHERE name=?',
                                  (*values.values(), name))

    def matches(self, rule, text):
        """正文正则：include 不命中就跳过，exclude 命中就跳过。写错了按不设规则处理。"""
        try:
            if rule['include'] and not re.search(rule['include'], text):
                return False
            if rule['exclude'] and re.search(rule['exclude'], text):
                return False
        except re.error:
            self.store.event('channel', '频道 %s 的正则无效，已按不设规则处理。' % rule['name'])
        return True

    async def _pace(self):
        wait = self._next_fetch - self.clock()
        if wait > 0:
            await self.sleep(wait)
        self._next_fetch = self.clock() + self.GAP

    async def _page(self, name, before=None):
        # 每一次请求 t.me 都过匀间隔，翻页也算，别把预览页当成自家接口。
        await self._pace()
        url = PREVIEW.format(name=name) + (('?before=%d' % before) if before else '')
        return await self.engine.http.get(url, headers={'User-Agent': USER_AGENT})

    async def refresh(self, name):
        """读一个频道，处理游标之后的新帖，返回本次入队的分享数。"""
        state = self._state(name)
        if not state:
            with self.store.db:
                self.store.db.execute('INSERT OR IGNORE INTO channel_state(name,added) VALUES(?,?)',
                                      (name, time.time()))
            state = self._state(name)
        cursor = state['last_message_id']
        backfill = self.store.db.execute('SELECT * FROM channel_backfill WHERE name=?', (name,)).fetchone()
        if backfill and backfill['cursor'] == cursor:
            pages = [json.loads(self.store.crypto.decrypt(backfill['messages'].encode()).decode())]
            oldest, title = backfill['before_id'], backfill['title']
        else:
            pages, oldest, title = [], None, ''
        has_more = False
        adopted = False
        try:
            rule_now = self.rule(name) or {}
            source = rule_now.get('source', 'preview')
            first_pages = max(1, min(20, int(rule_now.get('history_pages') or 1)))
            for attempt in range(self.MAX_PAGES):
                if source == 'session':
                    # 私密频道：用户会话读消息，产出结构与预览页一致
                    parsed = await self.engine.telegram.fetch(name, oldest)
                else:
                    response = await self._page(name, oldest)
                    if response.status_code == 429:
                        self._remember(name, state='backoff', error='Telegram 限流，稍后再试')
                        return 0
                    if response.status_code != 200:
                        self._remember(name, state='error', error='预览页返回 HTTP %d' % response.status_code)
                        return 0
                    parsed = parse_channel(response.text)
                title = parsed['title'] or title
                has_more = parsed['has_more']
                if not parsed['messages']:
                    error = ('会话里没有可读消息：机器人可能把链接放在按钮里，或你没有该群组的读取权限'
                             if source == 'session'
                             else '没有公开预览：频道不存在、未公开，或已被限制')
                    self._remember(name, title=title, state='error', error=error)
                    return 0
                if not adopted:
                    # 每轮只认一次：删了重加的频道，靠标题接上原来的游标和账本。
                    adopted = True
                    inherited = self._adopt_orphan(name, title)
                    if inherited is not None:
                        cursor = inherited
                        state = self._state(name) or state
                pages.insert(0, parsed['messages'])
                oldest = min(item['id'] for item in parsed['messages'])
                if cursor is None:
                    # 首次接入：按频道配置的页数回溯（默认 1 页 ≈ 20 条）
                    if attempt + 1 >= first_pages or not has_more:
                        break
                elif oldest <= cursor or not has_more or attempt == self.MAX_PAGES - 1:
                    break
            messages = [item for page in pages for item in page]
            # A full page budget without reaching the previous cursor must not
            # advance it: persist the fetched posts and continue next poll.
            if cursor is not None and oldest > cursor and has_more:
                with self.store.db:
                    self.store.db.execute('INSERT OR REPLACE INTO channel_backfill VALUES(?,?,?,?,?)',
                        (name, cursor, oldest, title, self.store.crypto.encrypt(json.dumps(messages, ensure_ascii=False).encode()).decode()))
                self._remember(name, title=title, state='backfill', error='频道积压，正在分批补读。')
                return 0
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._remember(name, state='error', error='读取失败：%s' % str(exc)[:120])
            return 0

        newest = max(item['id'] for item in messages)
        # 首次接入：回溯当前预览页（约 20 条）并按规则处理一遍，之后只跟进新帖。
        first = cursor is None
        cursor = 0 if first else cursor

        fresh = sorted({item['id']: item for item in messages if item['id'] > cursor}.values(), key=lambda item: item['id'])
        queued = 0
        processed_cursor = cursor
        failed = False
        for item in fresh:
            try:
                queued += await self.consume(name, title, item)
            except (ValueError, CloudError) as exc:
                # Keep this post available for the next poll; stop before it.
                with self.store.db:
                    self.store.db.execute('DELETE FROM channel_posts WHERE name=? AND message_id=?', (name, item['id']))
                self._remember(name, state='error', error='转存失败或任务入队失败，将重试：%s' % str(exc)[:120])
                failed = True
                break
            processed_cursor = item['id']
        # 本轮 consume 里写下的错误（比如转存失败）不能被收尾的 ready 覆盖掉。
        if failed and backfill:
            processed_cursor = cursor
        if not failed and processed_cursor == newest:
            with self.store.db:
                self.store.db.execute('DELETE FROM channel_backfill WHERE name=?', (name,))
        after = self._state(name) or {}
        error = after.get('error') if failed or after.get('error') != state['error'] else ''
        self._remember(name, title=title, last_message_id=processed_cursor,
                       state='error' if error else 'ready', error=error or '',
                       posts=state['posts'] + queued)
        if first:
            self.store.event('channel', '频道 %s 首次接入：回溯当前页 %d 条，按规则入队 %d 个。'
                             % (name, len(messages), queued))
        return queued

    async def consume(self, name, title, message):
        """一条帖子：认领消息 → 正则过滤 → 解析分享 → 去重 → 按频道方式入库。"""
        rule = self.rule(name)
        if not rule['enabled']:
            return 0
        with self.store.db:
            claimed = self.store.db.execute(
                'INSERT OR IGNORE INTO channel_posts(name,message_id,created) VALUES(?,?,?)',
                (name, message['id'], time.time())).rowcount
        if not claimed:
            return 0
        text = message['text'] + '\n' + '\n'.join(message['links'])
        if not self.matches(rule, text):
            return 0
        # 磁力 / 电驴与 115 分享分开处理：一条帖子可以两者都有。
        handled = 0
        urls = []
        for token in re.split(r'[\s<>"，。]+', text):
            if token.lower().startswith(OFFLINE_PREFIX) and token not in urls:
                urls.append(token)
        skipped_offline = []
        # 关键词分流对三种落盘方式都生效：命中就用分流目录；否则用磁力/电驴各自的目录，
        # 都没配时回退到统一离线目录（磁力与电驴可以共用一个目录）。
        route_cid, route_label = route_target(
            {'routes': rule['routes'], 'target_cid': '', 'target_label': ''}, text)
        for kind, prefix in (('magnet', 'magnet:'), ('ed2k', 'ed2k://')):
            group = [url for url in urls if url.lower().startswith(prefix)]
            if not group:
                continue
            if route_cid:
                cid, label = route_cid, route_label
            else:
                cid = rule[kind+'_cid']
                label = rule[kind+'_label'] or cid
            if cid:
                target_rule = {**rule, 'offline_cid': cid, 'offline_label': label}
                handled += await self._offline(name, title, message, target_rule, group)
            else:
                skipped_offline.extend(group)
        if skipped_offline:
            self.store.event('channel', '频道 %s：磁力或 ED2K 未设置目录，已跳过 %d 个链接。'
                             % (title or name, len(skipped_offline)))
        try:
            found = parse_shares(text)
        except ValueError:
            return handled
        if not found:
            return handled
        if rule['share_mode'] == 'transfer':
            route_cid, route_label = route_target(
                {'routes': rule['routes'], 'target_cid': rule['share_cid'],
                 'target_label': rule['share_label']}, text)
            if (route_cid, route_label) != (rule['share_cid'], rule['share_label']):
                rule = {**rule, 'share_cid': route_cid, 'share_label': route_label}
                self.store.event('channel', '频道 %s：关键词命中分流目录 %s。'
                                 % (title or name, route_label))
            if not rule['share_cid']:
                return handled
            return handled + await self._transfer(name, title, message, rule, found)
        return handled + await self._strm(name, title, message, rule, found)

    async def _offline(self, name, title, message, rule, urls):
        """磁力 / 电驴：只落盘到频道指定目录，不自动整理（整理交给目录扫描）。"""
        if not urls:
            return 0
        submitted = urls[:20]
        try:
            await self.engine.offline.submit(submitted, rule['offline_cid'],
                                             rule['offline_label'] or rule['offline_cid'],
                                             organize=bool(rule['organize_after']))
        except CloudError as exc:
            self._remember(name, state='error', error='离线提交失败：%s' % str(exc)[:120])
            self.store.event('channel', '频道 %s 离线提交结果未确认，需人工核查，避免重复提交：%s' % (title or name, str(exc)[:120]))
            if self.config()['notify']:
                kind = '磁力' if submitted[0].lower().startswith('magnet:') else 'ED2K'
                self._notify(name, title, message, [], 'offline_unknown',
                             self.store.directory_label(rule['offline_cid'], rule['offline_label']),
                             resource_kind=kind)
            return 0
        self.store.event('channel', '频道 %s：提交 %d 个磁力/电驴到离线下载（%s），只落盘。'
                         % (title or name, len(submitted), rule['offline_label'] or rule['offline_cid']))
        if self.config()['notify']:
            kind = '磁力' if submitted[0].lower().startswith('magnet:') else 'ED2K'
            self._notify(name, title, message, [], 'offline',
                         self.store.directory_label(rule['offline_cid'], rule['offline_label']),
                         count=len(submitted), resource_kind=kind, omitted=len(urls)-len(submitted))
        return len(submitted)

    async def _strm(self, name, title, message, rule, found):
        """免转存：只把分享里的视频写成待整理 STRM。

        与「影视整理」完全对齐：定时 5 分钟轮询（share_scan_tick）或手动触发接手，
        频道本身不建整理任务，也不改云端文件。
        """
        share_service.install(self.store)
        created, pending_error = 0, None
        for secret in found:
            code = str(secret.get('share_code') or '')
            if self.store.db.execute('SELECT 1 FROM channel_cancelled_shares WHERE share_code=?', (code,)).fetchone():
                continue
            attempt = self.store.db.execute(
                'SELECT attempts,skipped FROM channel_share_attempts WHERE name=? AND message_id=? AND share_code=?',
                (name, message['id'], code)).fetchone()
            if attempt and attempt['skipped']:
                continue
            try:
                videos = await share_service.list_videos(self.engine.cloud, secret)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # 明确已取消的分享无需重试；不能把登录、限流等临时故障当成取消。
                if isinstance(exc, CloudError) and exc.code in ('CLOUD_REJECTED', 'SHARE_NOT_FOUND') and '分享已取消' in str(exc):
                    with self.store.db:
                        self.store.db.execute('INSERT OR IGNORE INTO channel_cancelled_shares VALUES(?,?)', (code, time.time()))
                    self.store.event('channel', '频道 %s：分享 %s 已取消，已跳过，不再自动重试。' % (title or name, code))
                    continue
                tries = (attempt['attempts'] if attempt else 0) + 1
                skipped = tries >= 4  # 首次尝试 + 最多三次重试
                detail = str(exc)[:120] or type(exc).__name__
                with self.store.db:
                    self.store.db.execute(
                        'INSERT OR REPLACE INTO channel_share_attempts VALUES(?,?,?,?,?,?,?)',
                        (name, message['id'], code, tries, int(skipped), detail, time.time()))
                if skipped:
                    self.store.event('channel', '频道 %s：分享 %s 已尝试 %d 次仍失败，已跳过：%s' % (title or name, code, tries, detail))
                else:
                    self.store.event('channel', '频道 %s：分享 %s 读取失败（第 %d/4 次），稍后重试：%s' % (title or name, code, tries, detail))
                    if pending_error is None:
                        pending_error = exc if isinstance(exc, (CloudError, ValueError)) else CloudError('分享读取异常：' + type(exc).__name__)
                continue
            with self.store.db:
                self.store.db.execute('DELETE FROM channel_share_attempts WHERE name=? AND message_id=? AND share_code=?', (name, message['id'], code))
            result = share_service.generate(self.store, self.engine.root, secret, videos, origin='channel')
            created += len(result['created'])
        if created:
            self.store.event('channel', '频道 %s：生成 %d 条待整理 STRM（免转存），等定时/手动整理接手。'
                             % (title or name, created))
            if self.config()['notify']:
                self._notify(name, title, message, [], 'strm', count=created)
        if pending_error is not None:
            raise pending_error
        return created

    async def _transfer(self, name, title, message, rule, found):
        """转存模式：链接先落进 115 目录，再交给影视整理的定时扫描跟进。"""
        fresh = []
        for secret in found:
            with self.store.db:
                claimed = self.store.db.execute(
                    'INSERT OR IGNORE INTO channel_shares(share_code,name,message_id,created) VALUES(?,?,?,?)',
                    (secret['share_code'], name, message['id'], time.time())).rowcount
            if claimed:
                fresh.append(secret)
            else:
                existing = self.store.db.execute('SELECT name,message_id,job_id FROM channel_shares WHERE share_code=?',
                                                 (secret['share_code'],)).fetchone()
                if existing and existing['name'] == name and existing['message_id'] == message['id'] and existing['job_id'] is None:
                    fresh.append(secret)
        if not fresh:
            return 0
        links = ['https://115.com/s/' + secret['share_code'] +
                 ('?password=' + secret['receive_code'] if secret['receive_code'] else '')
                 for secret in fresh]
        try:
            # auto=False：转存完成后不立刻生成整理任务，交给影视整理的定时扫描接手。
            ids = await self.engine.transfer.enqueue('\n'.join(links), rule['share_cid'], bool(rule['organize_after']),
                                                     'channel', 'channel:%s:%s' % (name, message['id']))
        except (ValueError, CloudError) as exc:
            self._remember(name, state='error', error='转存失败：%s' % str(exc)[:120])
            self.store.event('channel', '频道 %s 转存失败：%s' % (title or name, str(exc)[:120]))
            raise
        if len(ids) != len(fresh):
            raise ValueError('分享与转存任务数量不一致，等待重新核对。')
        with self.store.db:
            for secret, job_id in zip(fresh, ids):
                self.store.db.execute('UPDATE channel_shares SET job_id=? WHERE share_code=?',
                                      (job_id, secret['share_code']))
        self.store.event('channel', '频道 %s：转存 %d 个分享到 %s，任务 %s。'
                         % (title or name, len(fresh), rule['share_cid'] or rule['share_label'], ids))
        if self.config()['notify']:
            self._notify(name, title, message, [(jid, '转存') for jid in ids], 'transfer',
                         self.store.directory_label(rule['share_cid'], rule['share_label']))
        return len(fresh)

    def _notify(self, name, title, message, queued, mode, target='', count=None, resource_kind='', omitted=0):
        heading,body=channel_notice(title or name, message.get('text',''), queued, mode, target, count, resource_kind, omitted)
        job_id = queued[0][0] if queued and isinstance(queued[0][0], int) else None
        task = asyncio.create_task(self.engine.notifications.publish(
            'channel:%s:%s:%s:%s' % (name, message['id'], mode, resource_kind), heading, body, job_id=job_id))
        self.engine.notice_tasks.add(task)
        task.add_done_callback(self.engine.notice_tasks.discard)

    async def tick(self, force=False):
        config = self.config()
        if not config['enabled'] and not force:
            return []
        results = []
        enabled = {rule['name'] for rule in config['channels'] if rule['enabled']}
        for rule in config['channels']:
            if not rule['enabled'] and not force:
                continue
            # 每抓一个就重算一次：title 是抓完才知道的，只在循环外算一次的话，
            # 后面频道的 title 要等到下一轮才生效（白多抓一轮）。
            self._mark_duplicates()
            owner = self._duplicate_owner(rule['name'])
            if owner and owner in enabled and not force:
                # 同一个频道的另一种写法（用户名 / 数字 ID），正本这轮会抓：跳过，避免重复抓取和转存。
                # 正本被禁用或已从配置里移除时不跳过，否则这个频道就谁都不抓了。
                continue
            results.append((rule['name'], await self.refresh(rule['name'])))
        self._mark_duplicates()   # 最后一个频道抓完才拿到 title，收尾再判一次
        return results

    def _duplicate_owner(self, name):
        row = self.store.db.execute('SELECT duplicate_of FROM channel_state WHERE name=?',
                                    (name,)).fetchone()
        return row['duplicate_of'] if row else None

    def next_delay(self):
        config = self.config()
        if config['schedule_mode'] == 'cron' and config['cron']:
            try:
                now = datetime.now()
                return max(30.0, (croniter(config['cron'], now).get_next(datetime) - now).total_seconds())
            except (ValueError, KeyError):
                pass
        return float(config['interval'])

    def start(self):
        if self.task is None:
            self.task = asyncio.create_task(self.loop())

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    async def loop(self):
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.store.event('channel', '频道轮询异常：%s' % str(exc)[:160])
            await self.sleep(self.next_delay())
