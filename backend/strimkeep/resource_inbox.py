"""Durable resource inbox for manual, RSS and webhook ingestion.

The existing transfer/offline pipelines already know how to receive a link and
how to organize the result. This module adds the missing front door: every
source produces normalized inbox rows first, duplicates are rejected by a
stable key, and a worker submits rows through the existing pipelines.
"""
import asyncio
import hashlib
import hmac
import html
import ipaddress
import json
import re
import secrets
import time
import xml.etree.ElementTree as ET
from urllib.parse import urljoin, urlsplit

from fastapi import Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from .cloud115 import CloudError
from .offline_api import directory_config, organize_flag_for
from .transfer import shares

SOURCES = ('manual', 'rss', 'webhook', 'telegram', 'subscription')
STATUSES = ('pending', 'processing', 'queued', 'duplicate', 'needs_review', 'failed', 'skipped')
PROCESSING_LEASE = 300
MAX_FEED_BYTES = 2 * 1024 * 1024
MAX_FEED_REDIRECTS = 5
URL_RE = re.compile(r'(?i)(?:magnet:\?[^\s<>"\']+|ed2k://[^\s<>"\']+|(?:https?|ftp)://[^\s<>"\']+)')
MAGNET_HASH = re.compile(r'(?i)btih:([A-Za-z0-9]+)')
ED2K_HASH = re.compile(r'(?i)ed2k://\|file\|[^|]*\|\d+\|([0-9a-f]{32})\|')
MAX_ITEMS = 100


def _now():
    return time.time()


def _digest(value):
    return hashlib.sha1(str(value).encode('utf-8', 'replace')).hexdigest()


def _clean_url(value):
    return str(value or '').rstrip(').,;，。；）]>')


def _extract_urls(text):
    """Pull links out of plain text, RSS HTML and JSON payloads."""
    found = []
    for match in URL_RE.finditer(html.unescape(str(text or ''))):
        url = _clean_url(match.group(0))
        if url and url not in found:
            found.append(url)
    return found


def _link_key(url):
    low = str(url or '').lower()
    magnet = MAGNET_HASH.search(low)
    if magnet:
        return 'magnet:' + magnet.group(1).lower()
    ed2k = ED2K_HASH.search(low)
    if ed2k:
        return 'ed2k:' + ed2k.group(1).lower()
    return 'url:' + _digest(url)


def candidates(text):
    """Return (dedupe_key, url, kind) for every link in a piece of text."""
    found = []
    try:
        share_links = shares(text)
    except ValueError:
        share_links = []
    for secret in share_links:
        code = str(secret.get('share_code') or '')
        if not code:
            continue
        url = 'https://115.com/s/' + code
        if secret.get('receive_code'):
            url += '?password=' + str(secret['receive_code'])
        found.append(('share:' + code, url, 'share'))
    for url in _extract_urls(text):
        low = url.lower()
        if any(token in low for token in ('115.com/s/', '115cdn.com/s/', 'anxia.com/s/')):
            continue
        if low.startswith('magnet:'):
            kind = 'magnet'
        elif low.startswith('ed2k://'):
            kind = 'ed2k'
        elif low.startswith(('http://', 'https://', 'ftp://')):
            kind = 'direct'
        else:
            continue
        item = (_link_key(url), url, kind)
        if item not in found:
            found.append(item)
    return found


def _local(tag):
    return str(tag or '').rsplit('}', 1)[-1].lower()


def _child_text(node, names):
    for child in list(node):
        if _local(child.tag) in names:
            value = child.text
            if value is None:
                value = child.attrib.get('href') or child.attrib.get('url') or ''
            value = str(value or '').strip()
            if value:
                return value
    return ''


def feed_items(content):
    """Parse RSS 2.0 / Atom without adding a feedparser dependency."""
    root = ET.fromstring(content)
    items = []
    for node in root.iter():
        if _local(node.tag) not in ('item', 'entry'):
            continue
        title = _child_text(node, ('title',))
        link = _child_text(node, ('link', 'guid', 'id'))
        if not link:
            link = _child_text(node, ('url',))
        guid = _child_text(node, ('guid', 'id'))
        summary = _child_text(node, ('description', 'summary', 'content', 'encoded'))
        enclosure = ''
        for child in list(node):
            if _local(child.tag) == 'enclosure':
                enclosure = str(child.attrib.get('url') or '').strip()
                if enclosure:
                    break
        # RSS <link> is usually an article page, not a media URL. Only keep it
        # when it is a known share or a direct media/torrent file.
        text = '\n'.join(value for value in (title, summary, enclosure) if value)
        low_link = link.lower()
        if (any(token in low_link for token in ('115.com/s/', '115cdn.com/s/', 'anxia.com/s/'))
                or low_link.startswith(('magnet:', 'ed2k://'))
                or low_link.endswith(('.torrent', '.mkv', '.mp4', '.ts', '.iso'))):
            text = '\n'.join(value for value in (text, link) if value)
        if text:
            items.append({'title': title, 'link': link, 'guid': guid or link,
                          'text': text})
    return items


class ResourceConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')
    target_cid: str = Field(default='', max_length=24)
    target_label: str = Field(default='', max_length=500)
    auto_organize: bool = False


class InboxInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    text: str = Field(min_length=1, max_length=16000)
    title: str = Field(default='', max_length=300)
    target_cid: str = Field(default='', max_length=24)
    auto_organize: bool | None = None


class FeedInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(default='', max_length=100)
    url: str = Field(min_length=1, max_length=2000)
    enabled: bool = True
    interval: int = Field(default=300, ge=60, le=86400)
    target_cid: str = Field(default='', max_length=24)
    include: str = Field(default='', max_length=500)
    exclude: str = Field(default='', max_length=500)
    auto_organize: bool = False


class FeedImport(BaseModel):
    model_config = ConfigDict(extra='forbid')
    xml: str = Field(min_length=1, max_length=2_000_000)
    target_cid: str = Field(default='', max_length=24)
    auto_organize: bool = False


class ResourceInbox:
    """Worker-side queue. Web requests only insert rows; they never wait on 115."""
    INTERVAL = 3

    def __init__(self, engine):
        self.engine, self.store, self.http = engine, engine.store, engine.http
        self.task = None
        self.next_feed = 0.0
        self._pruned_at = 0.0
        # Old databases predate the feed health columns. The table exists here,
        # unlike the minimal fixture used by schema_migrations tests.
        columns = {row[1] for row in self.store.db.execute('PRAGMA table_info(resource_feeds)')}
        for name, ddl in (('failures', ' INTEGER NOT NULL DEFAULT 0'),
                          ('last_success', ' REAL NOT NULL DEFAULT 0'),
                          ('last_count', ' INTEGER NOT NULL DEFAULT 0')):
            if name not in columns:
                self.store.db.execute('ALTER TABLE resource_feeds ADD COLUMN %s%s' % (name, ddl))
        inbox_columns = {row[1] for row in self.store.db.execute('PRAGMA table_info(resource_inbox)')}
        if 'lease_until' not in inbox_columns:
            self.store.db.execute('ALTER TABLE resource_inbox ADD COLUMN lease_until REAL NOT NULL DEFAULT 0')
        self.store.db.commit()

    def config(self):
        return {**ResourceConfig().model_dump(), **(self.store.get('resource_config', {}) or {})}

    def public(self, row):
        value = dict(row)
        try:
            value['job_ids'] = json.loads(value.get('job_ids') or '[]')
        except (TypeError, ValueError):
            value['job_ids'] = []
        return value

    def add_many(self, text, source='manual', external_id='', title='', target_cid='',
                 auto_organize=False):
        """Insert normalized rows; return accepted/requeued/duplicate counts."""
        if source not in SOURCES:
            source = 'manual'
        found = candidates(text)
        if not found:
            raise ValueError('没有找到可接入的 115 分享、磁力、ED2K 或直链。')
        if len(found) > MAX_ITEMS:
            raise ValueError('单次最多接入 %d 个链接。' % MAX_ITEMS)
        now = _now()
        accepted = requeued = duplicates = 0
        items = []
        with self.store.transaction(immediate=True):
            for key, url, _kind in found:
                existing = self.store.db.execute(
                    'SELECT id,status FROM resource_inbox WHERE dedupe_key=?', (key,)).fetchone()
                if existing:
                    if source != 'subscription' and existing['status'] in ('failed', 'skipped', 'needs_review', 'duplicate'):
                        self.store.db.execute(
                            'UPDATE resource_inbox SET source=?,external_id=?,title=?,text=?,'
                            'target_cid=?,auto_organize=?,status=\'pending\',message=\'\','
                            'attempts=0,updated=? WHERE id=?',
                            (source, external_id, title or '', url, target_cid,
                             1 if auto_organize else 0, now, existing['id']))
                        requeued += 1
                        items.append(existing['id'])
                    else:
                        duplicates += 1
                    continue
                rowid = self.store.db.execute(
                    'INSERT INTO resource_inbox(source,external_id,dedupe_key,title,text,'
                    'target_cid,auto_organize,status,created,updated) '
                    "VALUES(?,?,?,?,?,?,?, 'pending',?,?)",
                    (source, external_id, key, title or '', url, target_cid,
                     1 if auto_organize else 0, now, now)).lastrowid
                accepted += 1
                items.append(rowid)
        return {'accepted': accepted, 'requeued': requeued, 'duplicates': duplicates,
                'items': items}

    def add_review(self, text, source='telegram', external_id='', title='',
                   target_cid='', message='需要人工核查。'):
        """Insert links as ``needs_review`` instead of submitting them."""
        result = self.add_many(text, source, external_id, title, target_cid, False)
        with self.store.db:
            for row_id in result['items']:
                self.store.db.execute(
                    "UPDATE resource_inbox SET status='needs_review',message=?,updated=? "
                    "WHERE id=? AND status='pending'", (message[:300], _now(), row_id))
        return result

    def _set(self, row_id, status, message='', *, attempts=None):
        values = [status, message, _now(), row_id]
        sql = 'UPDATE resource_inbox SET status=?,message=?,updated=?,lease_until=0 WHERE id=?'
        if attempts is not None:
            sql = ('UPDATE resource_inbox SET status=?,message=?,updated=?,attempts=?,lease_until=0 '
                   'WHERE id=?')
            values = [status, message, _now(), attempts, row_id]
        with self.store.db:
            self.store.db.execute(sql, values)

    async def process_row(self, row):
        subscription = row['source'] == 'subscription'
        target = str(row['target_cid'] or '').strip() or self.config().get('target_cid', '')
        auto = bool(row['auto_organize'])
        text = str(row['text'] or '')
        try:
            share_links = shares(text)
        except ValueError:
            share_links = []
        if share_links:
            # Share rows can fall back to the normal Web transfer directory.
            target = target or str((self.store.get('transfer_config', {}) or {}).get('target_cid', ''))
            if not target:
                self._set(row['id'], 'needs_review', '还没有设置 115 分享转存目录。')
                return
            options = {}
            if subscription:
                attempt = self.store.db.execute(
                    'SELECT expected_video FROM media_subscription_attempts WHERE inbox_id=?',
                    (row['id'],)).fetchone()
                if not attempt or not attempt['expected_video']:
                    self._set(row['id'], 'needs_review', '追更转存缺少已核对的视频信息。')
                    return
                options = {'expected_video': json.loads(attempt['expected_video']),
                           'update': f"subscription:{row['id']}"}
                self._set(row['id'], 'needs_review', '转存提交待核实，请查看任务中心。')
            try:
                ids = await self.engine.transfer.enqueue(text, target, auto, 'inbox', **options)
            except (ValueError, CloudError) as exc:
                self._fail(row, exc)
                return
            self._set(row['id'], 'queued', '转存任务 #%s 已入队。' % ','.join(map(str, ids)))
            with self.store.db:
                self.store.db.execute('UPDATE resource_inbox SET job_ids=? WHERE id=?',
                                      (json.dumps(ids), row['id']))
            self.store.event('resource', '资源收件箱 #%d：转存 %d 个分享 → %s，任务 %s。'
                             % (row['id'], len(ids), self._label(target), ids))
            return
        urls = _extract_urls(text)
        if not urls:
            self._set(row['id'], 'skipped', '内容里没有可提交的链接。')
            return
        url = urls[0]
        low = url.lower()
        kind = 'magnet' if low.startswith('magnet:') else ('ed2k' if low.startswith('ed2k://') else 'direct')
        cfg = directory_config(self.store.get('offline_config', {}) or {})
        if not str(row['target_cid'] or '').strip() and not self.config().get('target_cid'):
            if kind == 'magnet':
                target = cfg.get('magnet_cid') or cfg.get('target_cid') or target
            elif kind == 'ed2k':
                target = cfg.get('ed2k_cid') or cfg.get('target_cid') or target
            else:
                target = cfg.get('target_cid') or target
        if not target:
            self._set(row['id'], 'needs_review', '还没有设置 %s 的离线目录。' % kind)
            return
        organize = auto if auto else organize_flag_for(cfg, kind, 'inbox')
        if subscription:
            self._set(row['id'], 'needs_review', '离线提交待核实，请查看任务中心。')
        try:
            sid = await self.engine.offline.submit(
                [url], target, self._label(target), organize=organize, origin='inbox')
        except (ValueError, CloudError) as exc:
            self._fail(row, exc)
            return
        self._set(row['id'], 'queued', '离线提交 #%d 已入队。' % sid)
        self.store.event('resource', '资源收件箱 #%d：提交 %s → %s，离线 #%d。'
                         % (row['id'], kind, self._label(target), sid))

    def _fail(self, row, exc):
        attempts = int(row['attempts'] or 0) + 1
        code = getattr(exc, 'code', '')
        message = str(exc)[:300] or type(exc).__name__
        if code == 'OFFLINE_DUPLICATE':
            status = 'duplicate'
        elif code in ('LOGIN_REQUIRED', 'LOGIN_INVALID'):
            status = 'needs_review'
        else:
            status = 'failed'
        self._set(row['id'], status, message, attempts=attempts)
        self.store.event('resource', '资源收件箱 #%d 处理失败（%s）：%s'
                         % (row['id'], status, message[:120]))

    def _label(self, cid):
        return self.store.directory_label(cid, '') or str(cid)

    async def process_pending(self):
        now = _now()
        # A worker may have died after claiming a row.  Reclaim it only after
        # the lease expires so two workers never submit the same link.
        with self.store.db:
            self.store.db.execute(
                "UPDATE resource_inbox SET status='pending',message='处理超时，已重新排队。',"
                "lease_until=0,updated=? WHERE status='processing' AND lease_until<?",
                (now, now))
        rows = []
        with self.store.transaction(immediate=True):
            candidates = self.store.db.execute(
                "SELECT id FROM resource_inbox WHERE status='pending' ORDER BY id LIMIT 5").fetchall()
            for candidate in candidates:
                changed = self.store.db.execute(
                    "UPDATE resource_inbox SET status='processing',lease_until=?,updated=? "
                    "WHERE id=? AND status='pending'",
                    (now + PROCESSING_LEASE, now, candidate['id'])).rowcount
                if changed:
                    rows.append(self.store.db.execute(
                        'SELECT * FROM resource_inbox WHERE id=?', (candidate['id'],)).fetchone())
        for row in rows:
            try:
                await self.process_row(row)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # one bad row must not stop the inbox
                self._fail(row, exc)


    async def poll_feeds(self):
        now = _now()
        if now < self.next_feed:
            return
        self.next_feed = now + 60
        rows = self.store.db.execute(
            'SELECT * FROM resource_feeds WHERE enabled=1 AND next_run<=? '
            'ORDER BY next_run,id LIMIT 5', (now,)).fetchall()
        for row in rows:
            await self.poll_feed(row)

    async def _fetch_feed(self, url, headers):
        """Fetch a feed with a hard size cap and validated manual redirects."""
        current = str(url)
        for _ in range(MAX_FEED_REDIRECTS + 1):
            current = _validate_url(current)
            async with self.http.stream('GET', current, headers=headers, timeout=30,
                                        follow_redirects=False) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get('location', '')
                    if not location:
                        raise ValueError('RSS 重定向缺少目标地址。')
                    current = urljoin(current, location)
                    continue
                if response.status_code == 304:
                    return 304, response.headers, b''
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes(65536):
                    if len(body) + len(chunk) > MAX_FEED_BYTES:
                        raise ValueError('RSS 内容超过 2MB。')
                    body.extend(chunk)
                return response.status_code, response.headers, bytes(body)
        raise ValueError('RSS 重定向次数过多。')

    async def poll_feed(self, row):
        headers = {}
        if row['etag']:
            headers['If-None-Match'] = row['etag']
        if row['last_modified']:
            headers['If-Modified-Since'] = row['last_modified']
        now = _now()
        try:
            status, response_headers, content = await self._fetch_feed(str(row['url']), headers)
            if status == 304:
                self._feed_done(row, now, state='ok')
                return
            items = feed_items(content)
            include = str(row['include'] or '').casefold()
            exclude = str(row['exclude'] or '').casefold()
            accepted = duplicates = 0
            for item in items:
                haystack = (item['title'] + '\n' + item['text']).casefold()
                if include and include not in haystack:
                    continue
                if exclude and exclude in haystack:
                    continue
                try:
                    result = self.add_many(
                        item['text'], 'rss', item['guid'], item['title'],
                        str(row['target_cid'] or ''), bool(row['auto_organize']))
                except ValueError:
                    continue
                accepted += result['accepted'] + result['requeued']
                duplicates += result['duplicates']
            self._feed_done(row, now, state='ok', accepted=accepted,
                            etag=response_headers.get('etag', ''),
                            last_modified=response_headers.get('last-modified', ''))
            if accepted:
                self.store.event('resource', 'RSS「%s」新增 %d 条资源，重复 %d 条。'
                                 % (row['name'] or row['url'], accepted, duplicates))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._feed_done(row, now, state='error', error=(str(exc) or type(exc).__name__)[:300])

    def _feed_done(self, row, now, *, state, error='', accepted=0, etag=None,
                   last_modified=None):
        interval = max(60, int(row['interval'] or 300))
        failures = int(row['failures'] or 0)
        if state == 'ok':
            failures = 0
            next_run = now + interval
        else:
            failures += 1
            # Exponential backoff, capped at six hours. A broken feed must not
            # be retried every interval forever.
            next_run = now + min(6 * 3600, interval * (2 ** min(failures, 6)))
        values = [state, error, now, next_run, failures, row['id']]
        sql = ('UPDATE resource_feeds SET state=?,error=?,last_run=?,next_run=?,'
               'failures=? WHERE id=?')
        if state == 'ok':
            sql = ('UPDATE resource_feeds SET state=?,error=?,last_run=?,next_run=?,'
                   'failures=?,last_success=?,last_count=? WHERE id=?')
            values = [state, error, now, next_run, failures, now, int(accepted or 0),
                      row['id']]
        if etag is not None:
            sql = sql.replace(' WHERE id=?', ',etag=?,last_modified=? WHERE id=?')
            values = values[:-1] + [etag or '', last_modified or '', row['id']]
        with self.store.db:
            self.store.db.execute(sql, values)

    def prune(self, keep_days=90, keep_rows=5000):
        """Drop old terminal inbox rows; never touch pending/queued/review."""
        now = _now()
        cutoff = now - max(1, int(keep_days)) * 86400
        with self.store.db:
            protected = (" AND NOT EXISTS (SELECT 1 FROM media_subscription_attempts a"
                         " WHERE a.inbox_id=resource_inbox.id)")
            by_age = self.store.db.execute(
                "DELETE FROM resource_inbox WHERE status IN ('duplicate','skipped','failed')"
                " AND updated<?" + protected, (cutoff,)).rowcount
            total = self.store.db.execute(
                "SELECT count(*) FROM resource_inbox WHERE status IN"
                " ('duplicate','skipped','failed')" + protected).fetchone()[0]
            by_count = max(0, total - max(1, int(keep_rows)))
            if by_count:
                self.store.db.execute(
                    "DELETE FROM resource_inbox WHERE id IN (SELECT id FROM resource_inbox"
                    " WHERE status IN ('duplicate','skipped','failed')" + protected +
                    " ORDER BY id ASC LIMIT ?)", (by_count,))
        return {'by_age': by_age, 'by_count': by_count}

    async def loop(self):
        while not getattr(self.engine, 'closing', False):
            try:
                if _now() - self._pruned_at >= 3600:
                    self._pruned_at = _now()
                    self.prune()
            except Exception as exc:
                self.store.event('resource', '收件箱清理异常：%s' % type(exc).__name__)
            try:
                await self.process_pending()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.store.event('resource', '资源收件箱循环异常：%s' % type(exc).__name__)
            try:
                await self.poll_feeds()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.store.event('resource', 'RSS 轮询异常：%s' % type(exc).__name__)
            await asyncio.sleep(self.INTERVAL)

    def start(self):
        if self.task is None:
            self.task = asyncio.create_task(self.loop())

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None


def _validate_url(value):
    value = str(value or '').strip()
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError('地址无效。') from None
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or
            parsed.password or parsed.fragment or (port is not None and not 1 <= port <= 65535)):
        raise ValueError('请填写不含凭据和片段的 HTTP(S) 地址。')
    host = parsed.hostname.lower().rstrip('.')
    if host == 'localhost' or host.endswith('.localhost') or host in (
            'metadata', 'metadata.google.internal'):
        raise ValueError('RSS 地址不能指向本机或云元数据服务。')
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address and (address.is_loopback or address.is_link_local or address.is_unspecified
                    or address.is_multicast or address.is_reserved):
        raise ValueError('RSS 地址不能指向本机、链路本地或保留地址。')
    return value



def install_resources(app, store, admin):
    def service():
        return app.state.organizer.resources

    @app.get('/api/v1/resources/config', dependencies=[Depends(admin)])
    async def config():
        return {'config': service().config(),
                'webhook_token': store.meta_json('resource_webhook_token', '') or ''}

    @app.put('/api/v1/resources/config', dependencies=[Depends(admin)])
    async def save_config(body: ResourceConfig):
        if body.target_cid:
            if not body.target_cid.isdigit() or int(body.target_cid) <= 0:
                raise HTTPException(422, '请选择一个非根目录作为默认目录。')
            await app.state.cloud.directory(body.target_cid)
        store.set_many({'resource_config': body.model_dump()})
        return await config()

    @app.post('/api/v1/resources/webhook-token', dependencies=[Depends(admin)])
    async def rotate_webhook_token():
        previous = store.meta_json('resource_webhook_token', '') or ''
        token = secrets.token_urlsafe(24)
        if previous:
            # Keep the old token valid for one day so an external sender can
            # finish a batch while the user updates its configuration.
            store.set_meta('resource_webhook_previous_token', previous)
            store.set_meta('resource_webhook_previous_until', _now() + 86400)
        store.set_meta('resource_webhook_token', token)
        return {'token': token}

    @app.get('/api/v1/resources/inbox', dependencies=[Depends(admin)])
    async def inbox(status: str = 'all', source: str = '', before: int = Query(2**62, ge=1),
                    limit: int = Query(100, ge=1, le=300)):
        where, params = [], []
        if status and status != 'all':
            if status not in STATUSES:
                raise HTTPException(422, '未知的收件箱状态。')
            where.append('status=?')
            params.append(status)
        if source:
            if source not in SOURCES:
                raise HTTPException(422, '未知的资源来源。')
            where.append('source=?')
            params.append(source)
        where.append('id<?')
        params.append(before)
        rows = store.db.execute(
            'SELECT * FROM resource_inbox WHERE ' + ' AND '.join(where) +
            ' ORDER BY id DESC LIMIT ?', (*params, limit)).fetchall()
        counts = {row['status']: row['n'] for row in store.db.execute(
            'SELECT status,count(*) n FROM resource_inbox GROUP BY status')}
        return {'items': [service().public(row) for row in rows], 'counts': counts}

    @app.post('/api/v1/resources/inbox', dependencies=[Depends(admin)])
    async def add(body: InboxInput):
        cfg = service().config()
        target = body.target_cid or cfg.get('target_cid', '')
        if not target:
            raise HTTPException(422, '请先选择默认目录，或在本次提交里指定目录。')
        if not target.isdigit() or int(target) <= 0:
            raise HTTPException(422, '请选择一个非根目录。')
        await app.state.cloud.directory(target)
        auto = cfg.get('auto_organize', False) if body.auto_organize is None else body.auto_organize
        try:
            result = service().add_many(body.text, 'manual', '', body.title,
                                        target, bool(auto))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        return result

    @app.post('/api/v1/resources/inbox/{row_id}/retry', dependencies=[Depends(admin)])
    async def retry(row_id: int):
        row = store.db.execute('SELECT * FROM resource_inbox WHERE id=?', (row_id,)).fetchone()
        if not row:
            raise HTTPException(404, '收件箱条目不存在。')
        if row['status'] in ('queued', 'processing'):
            raise HTTPException(409, '这条已经入队或正在处理，不能重复提交。')
        with store.db:
            store.db.execute(
                "UPDATE resource_inbox SET status='pending',message='',attempts=0,updated=?,"
                "lease_until=0 WHERE id=?", (_now(), row_id))
        return service().public(store.db.execute(
            'SELECT * FROM resource_inbox WHERE id=?', (row_id,)).fetchone())

    @app.post('/api/v1/resources/inbox/{row_id}/skip', dependencies=[Depends(admin)])
    async def skip(row_id: int):
        row = store.db.execute('SELECT * FROM resource_inbox WHERE id=?', (row_id,)).fetchone()
        if not row:
            raise HTTPException(404, '收件箱条目不存在。')
        if row['status'] in ('queued', 'processing'):
            raise HTTPException(409, '这条已经入队或正在处理，不能跳过。')
        with store.db:
            store.db.execute(
                "UPDATE resource_inbox SET status='skipped',message='已手动跳过。',updated=?,"
                "lease_until=0 WHERE id=?", (_now(), row_id))
        return {'ok': True}

    @app.delete('/api/v1/resources/inbox/{row_id}', dependencies=[Depends(admin)])
    async def remove(row_id: int):
        row = store.db.execute('SELECT * FROM resource_inbox WHERE id=?', (row_id,)).fetchone()
        if not row:
            raise HTTPException(404, '收件箱条目不存在。')
        if row['status'] in ('queued', 'processing'):
            raise HTTPException(409, '这条已经入队或正在处理，不能删除。')
        with store.db:
            store.db.execute('DELETE FROM resource_inbox WHERE id=?', (row_id,))
        return {'ok': True}


    @app.get('/api/v1/resources/sources', dependencies=[Depends(admin)])
    async def sources():
        feeds = store.db.execute('SELECT * FROM resource_feeds ORDER BY id DESC').fetchall()
        try:
            channels = service().engine.channels.sources()
        except Exception:
            channels = []
        return {'feeds': [dict(row) for row in feeds],
                'channels': [{'name': row.get('name', ''), 'title': row.get('title', ''),
                              'state': row.get('state', ''), 'error': row.get('error', ''),
                              'checked': row.get('checked', 0), 'posts': row.get('posts', 0),
                              'failures': row.get('failures', 0),
                              'last_success': row.get('last_success', 0)}
                             for row in channels],
                'webhook_configured': bool(store.meta_json('resource_webhook_token', ''))}

    @app.post('/api/v1/resources/feeds/import', dependencies=[Depends(admin)])
    async def import_feed(body: FeedImport):
        cfg = service().config()
        target = body.target_cid or cfg.get('target_cid', '')
        if not target:
            raise HTTPException(422, '请先选择 RSS 落点，或在自动来源设置默认落点。')
        if not target.isdigit() or int(target) <= 0:
            raise HTTPException(422, '请选择一个非根目录。')
        await app.state.cloud.directory(target)
        try:
            items = feed_items(body.xml)
        except Exception:
            raise HTTPException(422, 'RSS XML 解析失败，请确认粘贴的是完整 RSS/Atom 内容。') from None
        accepted = duplicates = 0
        for item in items:
            try:
                result = service().add_many(
                    item['text'], 'rss', item['guid'], item['title'], target,
                    bool(body.auto_organize))
            except ValueError:
                continue
            accepted += result['accepted'] + result['requeued']
            duplicates += result['duplicates']
        return {'accepted': accepted, 'duplicates': duplicates, 'items': len(items)}

    @app.get('/api/v1/resources/feeds', dependencies=[Depends(admin)])
    async def feeds():
        rows = store.db.execute('SELECT * FROM resource_feeds ORDER BY id DESC').fetchall()
        return {'items': [dict(row) for row in rows]}

    def feed_url_exists(url, exclude=None):
        sql = 'SELECT 1 FROM resource_feeds WHERE url=?'
        params = [url]
        if exclude is not None:
            sql += ' AND id<>?'
            params.append(exclude)
        return store.db.execute(sql, params).fetchone() is not None

    @app.post('/api/v1/resources/feeds', dependencies=[Depends(admin)])
    async def add_feed(body: FeedInput):
        try:
            url = _validate_url(body.url)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        if feed_url_exists(url):
            raise HTTPException(409, '这个 RSS 地址已经添加过了。')
        if body.target_cid and (not body.target_cid.isdigit() or int(body.target_cid) <= 0):
            raise HTTPException(422, '请选择一个非根目录作为 RSS 落点。')
        now = _now()
        with store.db:
            row_id = store.db.execute(
                'INSERT INTO resource_feeds(name,url,enabled,interval,target_cid,include,exclude,'
                'auto_organize,next_run,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (body.name, url, 1 if body.enabled else 0, body.interval, body.target_cid,
                 body.include, body.exclude, 1 if body.auto_organize else 0,
                 now, now, now)).lastrowid
        return {'id': row_id}

    @app.put('/api/v1/resources/feeds/{feed_id}', dependencies=[Depends(admin)])
    async def save_feed(feed_id: int, body: FeedInput):
        try:
            url = _validate_url(body.url)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        if feed_url_exists(url, exclude=feed_id):
            raise HTTPException(409, '这个 RSS 地址已经添加过了。')
        if body.target_cid and (not body.target_cid.isdigit() or int(body.target_cid) <= 0):
            raise HTTPException(422, '请选择一个非根目录作为 RSS 落点。')
        with store.db:
            changed = store.db.execute(
                'UPDATE resource_feeds SET name=?,url=?,enabled=?,interval=?,target_cid=?,'
                'include=?,exclude=?,auto_organize=?,next_run=0,updated=? WHERE id=?',
                (body.name, url, 1 if body.enabled else 0, body.interval, body.target_cid,
                 body.include, body.exclude, 1 if body.auto_organize else 0,
                 _now(), feed_id)).rowcount
        if not changed:
            raise HTTPException(404, 'RSS 源不存在。')
        return {'ok': True}

    @app.delete('/api/v1/resources/feeds/{feed_id}', dependencies=[Depends(admin)])
    async def remove_feed(feed_id: int):
        with store.db:
            store.db.execute('DELETE FROM resource_feeds WHERE id=?', (feed_id,))
        return {'ok': True}

    @app.post('/api/v1/resources/feeds/{feed_id}/run', dependencies=[Depends(admin)])
    async def run_feed(feed_id: int):
        with store.db:
            changed = store.db.execute(
                'UPDATE resource_feeds SET next_run=0,updated=? WHERE id=?',
                (_now(), feed_id)).rowcount
        if not changed:
            raise HTTPException(404, 'RSS 源不存在。')
        return {'ok': True}

    @app.post('/api/v1/resources/webhook/{token}')
    async def webhook(token: str, request: Request):
        expected = store.meta_json('resource_webhook_token', '') or ''
        valid = bool(expected) and hmac.compare_digest(token, expected)
        if not valid:
            previous = store.meta_json('resource_webhook_previous_token', '') or ''
            until = float(store.meta_json('resource_webhook_previous_until', 0) or 0)
            valid = bool(previous) and until >= _now() and hmac.compare_digest(token, previous)
        if not valid:
            raise HTTPException(404, 'Not Found')
        raw = await request.body()
        if len(raw) > 1_000_000:
            raise HTTPException(413, 'Payload too large')
        try:
            payload = json.loads(raw.decode('utf-8')) if raw else {}
        except (UnicodeDecodeError, ValueError):
            payload = {'text': raw.decode('utf-8', 'replace')}
        items = payload if isinstance(payload, list) else [payload]
        accepted = duplicates = 0
        for item in items[:MAX_ITEMS]:
            if not isinstance(item, dict):
                continue
            text = '\n'.join(str(item.get(key) or '') for key in
                             ('text', 'message', 'description', 'content', 'magnetUrl', 'url'))
            text += '\n' + json.dumps(item, ensure_ascii=False)
            title = str(item.get('title') or item.get('name') or '')
            external = str(item.get('guid') or item.get('id') or item.get('external_id') or '')
            target = str(item.get('target_cid') or '')
            try:
                result = service().add_many(text, 'webhook', external, title, target, False)
            except ValueError:
                continue
            accepted += result['accepted'] + result['requeued']
            duplicates += result['duplicates']
        return {'accepted': accepted, 'duplicates': duplicates}
