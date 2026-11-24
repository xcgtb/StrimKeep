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
import json
import re
import secrets
import time
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from .cloud115 import CloudError
from .offline_api import directory_config, organize_flag_for
from .transfer import shares

SOURCES = ('manual', 'rss', 'webhook')
STATUSES = ('pending', 'queued', 'duplicate', 'needs_review', 'failed', 'skipped')
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


class ResourceInbox:
    """Worker-side queue. Web requests only insert rows; they never wait on 115."""
    INTERVAL = 3

    def __init__(self, engine):
        self.engine, self.store, self.http = engine, engine.store, engine.http
        self.task = None
        self.next_feed = 0.0

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
        with self.store.db:
            for key, url, _kind in found:
                existing = self.store.db.execute(
                    'SELECT id,status FROM resource_inbox WHERE dedupe_key=?', (key,)).fetchone()
                if existing:
                    if existing['status'] in ('failed', 'skipped', 'needs_review', 'duplicate'):
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


    def _set(self, row_id, status, message='', *, attempts=None):
        values = [status, message, _now(), row_id]
        sql = 'UPDATE resource_inbox SET status=?,message=?,updated=? WHERE id=?'
        if attempts is not None:
            sql = ('UPDATE resource_inbox SET status=?,message=?,updated=?,attempts=? '
                   'WHERE id=?')
            values = [status, message, _now(), attempts, row_id]
        with self.store.db:
            self.store.db.execute(sql, values)

    async def process_row(self, row):
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
            try:
                ids = await self.engine.transfer.enqueue(text, target, auto, 'inbox')
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
        rows = self.store.db.execute(
            "SELECT * FROM resource_inbox WHERE status='pending' ORDER BY id LIMIT 5").fetchall()
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

    async def poll_feed(self, row):
        headers = {}
        if row['etag']:
            headers['If-None-Match'] = row['etag']
        if row['last_modified']:
            headers['If-Modified-Since'] = row['last_modified']
        now = _now()
        try:
            response = await self.http.get(str(row['url']), headers=headers,
                                           timeout=30, follow_redirects=True)
            if response.status_code == 304:
                self._feed_done(row, now, state='ok')
                return
            response.raise_for_status()
            items = feed_items(response.content[:2_000_000])
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
            self._feed_done(row, now, state='ok', etag=response.headers.get('etag', ''),
                            last_modified=response.headers.get('last-modified', ''))
            if accepted:
                self.store.event('resource', 'RSS「%s」新增 %d 条资源，重复 %d 条。'
                                 % (row['name'] or row['url'], accepted, duplicates))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._feed_done(row, now, state='error', error=(str(exc) or type(exc).__name__)[:300])

    def _feed_done(self, row, now, *, state, error='', etag=None, last_modified=None):
        values = [state, error, now, now + max(60, int(row['interval'] or 300)), row['id']]
        sql = 'UPDATE resource_feeds SET state=?,error=?,last_run=?,next_run=? WHERE id=?'
        if etag is not None:
            sql = ('UPDATE resource_feeds SET state=?,error=?,last_run=?,next_run=?,'
                   'etag=?,last_modified=? WHERE id=?')
            values = [state, error, now, now + max(60, int(row['interval'] or 300)),
                      etag or '', last_modified or '', row['id']]
        with self.store.db:
            self.store.db.execute(sql, values)

    async def loop(self):
        while not getattr(self.engine, 'closing', False):
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
        token = secrets.token_urlsafe(24)
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
        if row['status'] == 'queued':
            raise HTTPException(409, '这条已经入队，不能重复提交。')
        with store.db:
            store.db.execute(
                "UPDATE resource_inbox SET status='pending',message='',attempts=0,updated=? WHERE id=?",
                (_now(), row_id))
        return service().public(store.db.execute(
            'SELECT * FROM resource_inbox WHERE id=?', (row_id,)).fetchone())

    @app.post('/api/v1/resources/inbox/{row_id}/skip', dependencies=[Depends(admin)])
    async def skip(row_id: int):
        row = store.db.execute('SELECT * FROM resource_inbox WHERE id=?', (row_id,)).fetchone()
        if not row:
            raise HTTPException(404, '收件箱条目不存在。')
        with store.db:
            store.db.execute(
                "UPDATE resource_inbox SET status='skipped',message='已手动跳过。',updated=? WHERE id=?",
                (_now(), row_id))
        return {'ok': True}

    @app.delete('/api/v1/resources/inbox/{row_id}', dependencies=[Depends(admin)])
    async def remove(row_id: int):
        with store.db:
            store.db.execute('DELETE FROM resource_inbox WHERE id=?', (row_id,))
        return {'ok': True}


    @app.get('/api/v1/resources/feeds', dependencies=[Depends(admin)])
    async def feeds():
        rows = store.db.execute('SELECT * FROM resource_feeds ORDER BY id DESC').fetchall()
        return {'items': [dict(row) for row in rows]}

    @app.post('/api/v1/resources/feeds', dependencies=[Depends(admin)])
    async def add_feed(body: FeedInput):
        try:
            url = _validate_url(body.url)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
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
        if not expected or not hmac.compare_digest(token, expected):
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
