"""Follow TMDB works through the existing resource and library pipelines."""
import asyncio
import json
import logging
import re
import time
from datetime import date, datetime, timedelta
from pathlib import PurePosixPath
from typing import Literal
from urllib.parse import parse_qs, unquote, urlsplit
from zoneinfo import ZoneInfo

from fastapi import Depends, HTTPException, Path
from pydantic import BaseModel, ConfigDict, Field

from . import inventory, inventory_fs, share_service
from .cloud115 import CloudError
from .media import EPISODE_TAG, IDENTITY, KNOWN_VIDEOS, NeedsReview, episode_range, media_from_filename, normal_title, parse_name, source_of
from .offline_api import directory_config
from .resource_discovery import discover_resources
from .strm_options import share_effective_settings
from .transfer import shares


CHECK_INTERVAL = 1800
WAIT_INTERVAL = 21600
REVIEW_AFTER = 86400
PACK = re.compile(r'全\s*\d+\s*[集话話]|全集|全季|合集|剧集包|\b(?:complete|pack)\b|'
                  r'(?:[EePp]|\d)[\s]*[-–~～至到][\s]*(?:[EePp]\s*)?\d{1,3}(?![\d.pP])', re.I)
TITLE_CUT = re.compile(r'^[^\w\[\(【（]+', re.UNICODE)
SECOND_EPISODE = re.compile(r'(?i)(?<![a-z0-9])E(?:P)?[ ._-]?(\d{1,4})(?![a-z0-9])|第\s*(\d{1,4})\s*[集话話]')
MOVIE_PREVIEW = re.compile(r'(?i)枪版|槍版|预告|預告|试看片|'
                           r'\b(?:HD[ ._-]?(?:CAM|TS|TC)|CAM(?:RIP)?|TELESYNC|TELECINE|TRAILER|SAMPLE)\b')


class SubscriptionInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    share_action: Literal['share', 'transfer']
    backfill: bool = False
    enabled: bool = True
    auto_switch: bool = True
    minimum_resolution: Literal[0, 720, 1080, 2160] = 0
    preferred_sources: list[str] = Field(default_factory=list, max_length=16)


def meets_resolution(name, minimum):
    if not minimum:
        return True
    media = media_from_filename(name)
    return (media.get('height') or 0) >= minimum


def parse_date(value):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


async def work_details(engine, media_type, tmdb_id):
    details = await engine.tmdb(f'{media_type}/{tmdb_id}', detail_ttl=WAIT_INTERVAL)
    title_key = 'title' if media_type == 'movie' else 'name'
    if not isinstance(details, dict) or details.get('id') != tmdb_id or not details.get(title_key):
        raise CloudError('TMDB 未返回可核实的作品信息。', 'TMDB_UNAVAILABLE')
    return details


def regular_seasons(details):
    return [season for season in details.get('seasons') or []
            if isinstance(season, dict) and type(season.get('season_number')) is int
            and 1 <= season['season_number'] <= 99]


def inventory_snapshot(engine):
    roots = inventory_fs.resolve_roots(engine.root, engine.store)
    inventory_fs.check_missing_registrations(engine.root, roots, engine.store.db)
    rows = inventory_fs.scan(engine.root, roots, engine.store.db)
    return inventory.project(rows, engine.root, 'tmdb')


def work_names(details, media_type):
    keys = ('title', 'original_title') if media_type == 'movie' else ('name', 'original_name')
    return {normal_title(details.get(key)) for key in keys} - {''}


def inventory_work(snapshot, tmdb_id, media_type, names):
    works = [item for item in snapshot['items'] if item['kind'] == media_type]
    owners = [item for item in works if tmdb_id in item['tmdb_ids']]
    if (not snapshot['complete'] or len(owners) > 1
            or (owners and (owners[0]['tmdb_ids'] != [tmdb_id]
                            or not inventory.work_complete({**snapshot, 'items': works}, tmdb_id)))):
        raise NeedsReview('库存存在作品身份或编号冲突，请先在库存管理核对。')
    if not owners:
        for issue in snapshot['issues']:
            path = PurePosixPath(issue['path'])
            if any(int(match[1]) == tmdb_id for match in IDENTITY.finditer(str(path))):
                raise NeedsReview('库存中有这部作品的文件待核实。')
            if issue['reason'] == 'identity_unresolved':
                try:
                    hint = parse_name(path.name)
                except NeedsReview:
                    continue
                if hint['media_type'] == media_type and normal_title(hint['query']) in names:
                    raise NeedsReview('库存中有同名作品缺少 TMDB 标识。')
        return None
    return owners[0]


def release_hint(text, details, media_type, season, years, *, file_name=False):
    text = TITLE_CUT.sub('', str(text or '').strip())
    tags = list(EPISODE_TAG.finditer(text))
    if len(tags) > 1 or PACK.search(text):
        return None
    numbered = episode_range(text)
    if media_type == 'movie':
        if numbered or MOVIE_PREVIEW.search(text) or (file_name and not source_of(text)):
            return None
    else:
        if not numbered or numbered['invalid'] or numbered['first'] != numbered['last'] or not numbered['first']:
            return None
        if not numbered['explicit_season'] and numbered['season'] is None:
            if season != 1 or len(regular_seasons(details)) > 1:
                return None
        if any(int(first or second) != numbered['first'] for first, second in SECOND_EPISODE.findall(text)):
            return None
    ids = {int(match[1]) for match in IDENTITY.finditer(text)}
    if ids and ids != {details['id']}:
        return None
    try:
        parsed = parse_name(text + '.mkv')
    except NeedsReview:
        return None
    if (parsed['media_type'] != media_type or (media_type == 'tv' and parsed['season'] != season)
            or normal_title(parsed['query']) not in work_names(details, media_type)
            or (parsed['year'] and years and parsed['year'] not in years)):
        return None
    if media_type == 'movie' and file_name and not (ids or (parsed['year'] and parsed['year'] in years)):
        return None
    return parsed


async def matches_tmdb(engine, details, hint, cache):
    if hint['tmdb_id'] == details['id']:
        return True
    name = normal_title(hint['query'])
    movie = hint['media_type'] == 'movie'
    year = parse_date(details.get('release_date' if movie else 'first_air_date'))
    start_year = year.year if year and hint['year'] == year.year else None
    key = (name, start_year)
    if key not in cache:
        params = {'query': hint['query']}
        if start_year:
            params['primary_release_year' if movie else 'first_air_date_year'] = start_year
        result = await engine.tmdb('search/' + hint['media_type'], params)
        ids = {row.get('id') for row in result.get('results') or []
               if name in work_names(row, hint['media_type'])}
        cache[key] = result.get('total_pages') == 1 and ids == {details['id']}
    return cache[key]


def offline_filename(item):
    link = item['link']
    if item['link_type'] == 'magnet':
        names = parse_qs(urlsplit(link).query).get('dn', [])
        return names[0] if len(names) == 1 else ''
    parts = link.split('|')
    return unquote(parts[2]) if len(parts) >= 5 and parts[1].casefold() == 'file' else ''


def destination(store, row, kind):
    if kind == 'share':
        if row['share_action'] == 'share':
            return ''
        cfg = store.get('transfer_config', {}) or {}
        target = cfg.get('explore_cid') or cfg.get('target_cid')
    else:
        cfg = directory_config(store.get('offline_config', {}) or {})
        target = cfg.get(kind + '_cid')
    target = str(target or '')
    return target if re.fullmatch(r'[1-9]\d{0,23}', target) else ''


class MediaSubscriptions:
    def __init__(self, engine):
        self.engine = engine
        self.store = engine.store

    def update(self, row, state, message, *, interval=CHECK_INTERVAL, details=None):
        now = time.time()
        finished = state == 'completed'
        with self.store.transaction():
            self.store.db.execute(
                """UPDATE media_subscriptions SET state=?,message=?,last_check=?,next_check=?,enabled=?,
                     details=COALESCE(?,details)
                   WHERE media_type=? AND tmdb_id=? AND season=? AND enabled=1
                     AND share_action=? AND backfill=? AND next_check=?""",
                (state, message, now, 0 if finished else now + interval, not finished,
                 json.dumps(details) if details is not None else None,
                 row['media_type'], row['tmdb_id'], row['season'], row['share_action'], row['backfill'], row['next_check']))

    def attempts(self, row):
        return self.store.db.execute(
            """SELECT a.episode,a.inbox_id,a.link_key,a.kind,a.created,COALESCE(a.job_id,
                      CASE WHEN a.kind='share' AND json_valid(i.job_ids) THEN json_extract(i.job_ids,'$[0]') END) AS job_id,
                      i.status AS inbox_state,j.state AS job_state,
                      json_extract(j.config,'$.subscription_source_rejected') AS job_source_rejected
                 FROM media_subscription_attempts a LEFT JOIN resource_inbox i ON i.id=a.inbox_id
                 LEFT JOIN organize_jobs j ON j.id=COALESCE(a.job_id,
                      CASE WHEN a.kind='share' AND json_valid(i.job_ids) THEN json_extract(i.job_ids,'$[0]') END)
                 WHERE a.media_type=? AND a.tmdb_id=? AND a.season=? ORDER BY a.episode""",
            (row['media_type'], row['tmdb_id'], row['season'])).fetchall()

    def enqueue(self, row, item, episode, target, *, secret=None, expected=None):
        key = (row['media_type'], row['tmdb_id'], row['season'])
        now = time.time()
        with self.store.transaction(immediate=True):
            if self.store.db.execute('SELECT 1 FROM media_subscription_rejected WHERE link_key=?',
                                     (item['key'],)).fetchone():
                return False
            current = self.store.db.execute(
                """SELECT enabled,share_action,backfill,next_check FROM media_subscriptions
                   WHERE media_type=? AND tmdb_id=? AND season=?""",
                key).fetchone()
            if not current or not current['enabled'] or any(
                    current[field] != row[field] for field in ('share_action', 'backfill', 'next_check')):
                return False
            existing = self.store.db.execute('SELECT id FROM resource_inbox WHERE dedupe_key=?',
                                             (item['key'],)).fetchone()
            existing_share = secret and self.store.db.execute(
                'SELECT 1 FROM share_strms WHERE share_code=? AND revoked=0 LIMIT 1', (secret['share_code'],)).fetchone()
            claim = self.store.db.execute(
                """INSERT OR IGNORE INTO media_subscription_attempts
                   (media_type,tmdb_id,season,episode,link_key,kind,expected_video,inbox_id,created)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (*key, episode, item['key'], item['link_type'], json.dumps(expected) if expected else '',
                 existing['id'] if existing else None, now))
            if not claim.rowcount:
                return False
            if existing or existing_share:
                return True
            if secret and row['share_action'] == 'share':
                job_id = share_service.enqueue_organize(self.engine, origin='subscription', secret=secret,
                                                        expected_video=expected)
                self.store.db.execute('UPDATE media_subscription_attempts SET job_id=? WHERE id=?',
                                      (job_id, claim.lastrowid))
            else:
                title = row['title'] if row['media_type'] == 'movie' else f"{row['title']} S{row['season']:02d}E{episode:02d}"
                result = self.engine.resources.add_many(item['link'], source='subscription',
                                                         external_id=str(claim.lastrowid), title=title,
                                                         target_cid=target, auto_organize=True)
                if len(result['items']) != 1:
                    raise ValueError('这个资源无法单独加入收件箱。')
                self.store.db.execute('UPDATE media_subscription_attempts SET inbox_id=? WHERE id=?',
                                      (result['items'][0], claim.lastrowid))
        return True

    def retire_rejected(self, row, needed):
        # Move only a definitively rejected resource out of the unique active
        # slot. Unknown outcomes keep their slot and require reconciliation.
        rejected = []
        with self.store.transaction(immediate=True):
            for attempt in self.attempts(row):
                if attempt['episode'] not in needed:
                    continue
                if not row['auto_switch'] or not attempt['job_source_rejected'] or attempt['job_state'] != 'failed':
                    continue
                self.store.db.execute(
                    '''INSERT INTO media_subscription_rejected
                       (link_key,media_type,tmdb_id,season,episode,inbox_id,job_id,created,rejected)
                       VALUES(?,?,?,?,?,?,?,?,?)''',
                    (attempt['link_key'], row['media_type'], row['tmdb_id'], row['season'], attempt['episode'],
                     attempt['inbox_id'], attempt['job_id'], attempt['created'], time.time()))
                self.store.db.execute('DELETE FROM media_subscription_attempts WHERE link_key=?',
                                      (attempt['link_key'],))
                rejected.append(attempt['episode'])
        history = self.store.db.execute(
            '''SELECT link_key FROM media_subscription_rejected
               WHERE media_type=? AND tmdb_id=? AND season=?''',
            (row['media_type'], row['tmdb_id'], row['season']))
        return {entry[0] for entry in history}, sorted(set(rejected))

    async def checked_share(self, item, details, row, years, episode, cache):
        secret, = shares(item['link'])
        allowed = set(share_effective_settings(self.store)['strm']['video_extensions'])
        extensions = sorted(KNOWN_VIDEOS | allowed)
        async with asyncio.timeout(16):
            videos = await share_service.list_videos(self.engine.cloud, secret, extensions)
        if len(videos) != 1:
            return None
        video = videos[0]
        if PurePosixPath(video['name']).suffix.casefold() not in allowed:
            return None
        hint = release_hint(PurePosixPath(video['name']).stem, details, row['media_type'], row['season'], years, file_name=True)
        if not hint or (hint['episode'] or 0) != episode or not await matches_tmdb(self.engine, details, hint, cache):
            return None
        return secret, {'path': list(PurePosixPath(video['path'], video['name']).parts),
                        'size': video['size'], 'extensions': extensions}

    async def series_targets(self, row, snapshot, details, today):
        engine = self.engine
        tmdb_id, season = row['tmdb_id'], row['season']
        seasons = regular_seasons(details)
        season_info = next((entry for entry in seasons if entry['season_number'] == season), {})
        season_air = parse_date(season_info.get('air_date') or (details.get('first_air_date') if season == 1 else None))
        if season_air and season_air > today + timedelta(days=1):
            self.update(row, 'waiting', f'预计 {season_air} 开播。', interval=WAIT_INTERVAL, details={})
            return
        try:
            season_data = await engine.tmdb(f'tv/{tmdb_id}/season/{season}', detail_ttl=CHECK_INTERVAL)
        except CloudError as exc:
            if not engine.tmdb_missing_error(exc):
                raise
            self.update(row, 'waiting', 'TMDB 尚未提供这一季的集数。', interval=WAIT_INTERVAL, details={})
            return
        if not isinstance(season_data, dict) or season_data.get('season_number') != season:
            raise ValueError('TMDB 返回的季号无法核实。')
        dates = {ep['episode_number']: parse_date(ep.get('air_date')) for ep in season_data.get('episodes') or []
                 if isinstance(ep, dict) and type(ep.get('episode_number')) is int and ep['episode_number'] > 0}
        aired = {number for number, day in dates.items() if day and day <= today}
        premiere = min(dates, default=0)
        if not aired and dates.get(premiere) == today + timedelta(days=1):
            aired.add(premiere)
        if not aired:
            self.update(row, 'waiting', '等待这一季开播或 TMDB 公布播出日期。', interval=WAIT_INTERVAL, details={})
            return
        work = inventory_work(await snapshot(), tmdb_id, 'tv', work_names(details, 'tv'))
        present = {ep for side in ('cloud', 'share') for entry in (work or {}).get(side, [])
                   if entry.get('season') == season for ep in entry['episodes']}
        if present - set(dates):
            raise NeedsReview('库存集号与 TMDB 本季编号不一致，请先核对。')
        aired.update(number for number, day in dates.items() if day is None and number < max(present, default=0))
        needed = {number for number in aired - present if row['backfill'] or number > max(present, default=0)}
        years = {value.year for value in (season_air, parse_date(details.get('first_air_date'))) if value}
        return present, needed, years, any(day and day > today for day in dates.values())

    async def movie_targets(self, row, snapshot, details, today):
        work = inventory_work(await snapshot(), row['tmdb_id'], 'movie', work_names(details, 'movie'))
        if work:
            self.update(row, 'completed', '已在片库确认，订阅完成。', details={})
            return None
        release = parse_date(details.get('release_date'))
        if not release or release > today + timedelta(days=1):
            message = f'预计 {release} 上映。' if release else '等待 TMDB 公布上映日期。'
            self.update(row, 'waiting', message, interval=WAIT_INTERVAL, details={})
            return None
        return set(), {0}, {release.year}, False

    async def check(self, row, snapshot):
        engine = self.engine
        tmdb_id, media_type, season = row['tmdb_id'], row['media_type'], row['season']
        movie = media_type == 'movie'
        zone = ZoneInfo(self.store.get('timezone') or 'Asia/Shanghai')
        today = datetime.now(zone).date()
        details = await work_details(engine, media_type, tmdb_id)
        targets = await (self.movie_targets if movie else self.series_targets)(row, snapshot, details, today)
        if targets is None:
            return
        present, needed, years, has_upcoming = targets
        rejected_keys, rejected = self.retire_rejected(row, needed)
        attempts = [entry for entry in self.attempts(row) if entry['episode'] not in present]
        pending = [entry['episode'] for entry in attempts if entry['episode'] in needed]
        attention = [entry['episode'] for entry in attempts if entry['episode'] in needed and (
            entry['inbox_state'] in ('failed', 'skipped', 'needs_review', 'duplicate')
            or entry['job_state'] in ('partial', 'cancelled', 'failed', 'needs_review', 'waiting_login')
            or (not entry['job_id'] and not entry['inbox_state'])
            or time.time() - entry['created'] >= REVIEW_AFTER)]
        progress = {'present': sorted(present), 'missing': sorted(needed),
                    'submitted': sorted(set(pending) - set(attention)),
                    'needs_review': attention, 'candidates': [], 'rejected': rejected}
        waiting = needed - set(pending)
        if not waiting:
            state = 'review' if attention else 'submitted' if pending else 'caught_up'
            message = {'review': '已有提交需要核查，请查看关联任务。',
                       'submitted': '正在等待落盘和库存确认。', 'caught_up': '当前订阅范围已追齐。'}[state]
            interval = WAIT_INTERVAL if state == 'caught_up' and not has_upcoming else CHECK_INTERVAL
            self.update(row, state, message, interval=interval, details=progress)
            return

        result = await discover_resources(self.store, engine.http, engine.telegram, details['title' if movie else 'name'])
        progress['sources'] = result['sources']
        if not result['sources'] or all(source.get('error') for source in result['sources']):
            self.update(row, 'error', '暂无可用搜索来源，请检查 PanSou 和 Telegram 搜索频道。', details=progress)
            return
        checked, submitted, cache = 0, [], {}
        items = [item for item in result['items'] if item['key'] not in rejected_keys]
        priorities = {name: i for i, name in enumerate(json.loads(row['preferred_sources']))}
        if priorities:
            items.sort(key=lambda item: min((priorities.get(name, len(priorities))
                                            for name in item.get('source_names', [])), default=len(priorities)))
        offset = json.loads(row['details']).get('search_offset', 0) % max(1, len(items))
        for index, item in enumerate(items[offset:] + items[:offset], offset):
            if checked >= 4:
                progress['search_offset'] = index % len(items)
                break
            hint = release_hint(item['title'], details, media_type, season, years)
            episode = (hint['episode'] or 0) if hint else None
            if episode not in waiting:
                marker = episode_range(item['title'])
                if episode is None and (movie or not marker or marker['season'] in (None, season)):
                    if len(progress['candidates']) < 8:
                        reason = '片名、年份或片源版本无法确认。' if movie else '作品、季集或单集范围无法确认。'
                        progress['candidates'].append({**item, 'review_reason': reason})
                continue
            target = destination(self.store, row, item['link_type'])
            checked += 1
            reason = '尚未配置该类型的入库目录。'
            if (item['link_type'] == 'share' and row['share_action'] == 'share') or target:
                reason = '作品或链接里的文件名未通过核对。'
                try:
                    verified = await matches_tmdb(engine, details, hint, cache)
                    secret = expected = None
                    if verified and item['link_type'] == 'share':
                        reason = '分享内容无法确认为这部电影的单个正片。' if movie else '分享内容无法确认为这一集的单集视频。'
                        share = await self.checked_share(item, details, row, years, episode, cache)
                        verified = share is not None
                        if share:
                            secret, expected = share
                    elif verified:
                        actual = release_hint(offline_filename(item), details, media_type, season, years, file_name=True)
                        verified = bool(actual and (actual['episode'] or 0) == episode
                                        and await matches_tmdb(engine, details, actual, cache))
                    if verified and row['minimum_resolution']:
                        filename = expected['path'][-1] if expected else offline_filename(item)
                        reason = '文件名无法确认达到订阅的最低分辨率。'
                        verified = meets_resolution(filename, row['minimum_resolution'])
                    if verified and self.enqueue(row, item, episode, str(target), secret=secret, expected=expected):
                        submitted.append(episode)
                        waiting.remove(episode)
                        if not waiting and movie:
                            progress['candidates'].clear()
                            break
                        continue
                except (CloudError, TimeoutError, NeedsReview, ValueError):
                    reason = '资源读取或提交未完成，请核查。'
            if len(progress['candidates']) < 8:
                progress['candidates'].append({**item, 'review_reason': reason})

        progress['submitted'] = sorted(set(pending + submitted) - set(attention))
        if attention or progress['candidates']:
            state, message = 'review', '候选或已提交资源需要核查。'
        elif progress['submitted']:
            state, message = 'submitted', '已提交资源，等待落盘和库存确认。'
        else:
            state, message = 'searching', '本轮暂未找到符合条件的资源。'
        self.update(row, state, message, details=progress)

    async def tick(self):
        rows = self.store.db.execute(
            """SELECT * FROM media_subscriptions WHERE enabled=1 AND next_check<=?
               ORDER BY next_check,tmdb_id,season LIMIT 3""", (time.time(),)).fetchall()
        stock = None

        async def snapshot():
            nonlocal stock
            if stock is None:
                stock = await asyncio.to_thread(inventory_snapshot, self.engine)
            return stock

        for row in rows:
            try:
                async with asyncio.timeout(90):
                    await self.check(row, snapshot)
            except NeedsReview as exc:
                self.update(row, 'review', str(exc), details={})
            except (CloudError, OSError, ValueError, TimeoutError) as exc:
                safe = str(exc) if isinstance(exc, (CloudError, ValueError)) else '搜索或库存核查暂时不可用。'
                self.update(row, 'error', safe[:200], details={})
            except Exception as exc:
                logging.getLogger(__name__).warning('影视订阅检查失败：%s', type(exc).__name__)
                self.update(row, 'error', '本轮检查失败，请查看日志。', details={})

    async def loop(self):
        while not self.engine.closing:
            try:
                await self.tick()
            except Exception:
                logging.getLogger(__name__).exception('影视订阅检查失败')
            await asyncio.sleep(30)

    def public(self, row):
        data = dict(row)
        data['details'] = json.loads(data['details'])
        data['auto_switch'] = bool(data['auto_switch'])
        data['preferred_sources'] = json.loads(data['preferred_sources'])
        fields = ('episode', 'job_id', 'inbox_id', 'created', 'job_state', 'inbox_state')
        data['attempts'] = [{key: attempt[key] for key in fields} for attempt in self.attempts(row)[-12:]]
        data['rejected_attempts'] = [dict(attempt) for attempt in self.store.db.execute(
            '''SELECT episode,inbox_id,job_id,rejected FROM media_subscription_rejected
               WHERE media_type=? AND tmdb_id=? AND season=? ORDER BY rejected DESC LIMIT 12''',
            (row['media_type'], row['tmdb_id'], row['season']))]
        return data


def install_subscriptions(app, store, admin):
    path = '/api/v1/explore/subscriptions'

    @app.get(path, dependencies=[Depends(admin)])
    async def list_subscriptions():
        service = app.state.organizer.subscriptions
        return {'items': [service.public(row) for row in store.db.execute(
            'SELECT * FROM media_subscriptions ORDER BY title,season')]}

    @app.get(path + '/seasons/{tmdb_id}', dependencies=[Depends(admin)])
    async def seasons(tmdb_id: int = Path(gt=0)):
        details = await work_details(app.state.organizer, 'tv', tmdb_id)
        choices = [{'number': row['season_number'], 'name': row.get('name') or '',
                    'air_date': row.get('air_date') or ''} for row in regular_seasons(details)]
        return {'title': details['name'], 'seasons': choices or [{'number': 1, 'name': '第 1 季', 'air_date': ''}]}

    async def save_work(media_type, tmdb_id, season, body):
        key = media_type, tmdb_id, season
        row = store.db.execute(
            'SELECT title FROM media_subscriptions WHERE media_type=? AND tmdb_id=? AND season=?', key).fetchone()
        title = row['title'] if row else ''
        if not title:
            details = await work_details(app.state.organizer, media_type, tmdb_id)
            if media_type == 'tv':
                options = {entry['season_number'] for entry in regular_seasons(details)} or {1}
                if season not in options:
                    raise HTTPException(422, '请从这部剧的季列表中选择。')
            title = details['title' if media_type == 'movie' else 'name']
        # The revision used by a running check changes even if the same settings are saved twice.
        next_check = time.time()
        with store.transaction():
            store.db.execute(
                """INSERT INTO media_subscriptions
                   (media_type,tmdb_id,season,title,enabled,share_action,backfill,next_check,
                    auto_switch,minimum_resolution,preferred_sources)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(media_type,tmdb_id,season) DO UPDATE SET
                     enabled=excluded.enabled,share_action=excluded.share_action,backfill=excluded.backfill,
                     next_check=excluded.next_check,state='pending',message='',auto_switch=excluded.auto_switch,
                     minimum_resolution=excluded.minimum_resolution,preferred_sources=excluded.preferred_sources""",
                (*key, title, body.enabled, body.share_action, media_type == 'tv' and body.backfill, next_check,
                 body.auto_switch, body.minimum_resolution,
                 json.dumps(list(dict.fromkeys(s.strip()[:60] for s in body.preferred_sources if s.strip())),
                            ensure_ascii=False)))
        return {'saved': True}

    def remove_work(media_type, tmdb_id, season):
        with store.transaction():
            store.db.execute('DELETE FROM media_subscriptions WHERE media_type=? AND tmdb_id=? AND season=?',
                             (media_type, tmdb_id, season))
        return {'removed': True}

    @app.put(path + '/movie/{tmdb_id}', dependencies=[Depends(admin)])
    async def save_movie(body: SubscriptionInput, tmdb_id: int = Path(gt=0)):
        return await save_work('movie', tmdb_id, 0, body)

    @app.delete(path + '/movie/{tmdb_id}', dependencies=[Depends(admin)])
    async def remove_movie(tmdb_id: int = Path(gt=0)):
        return remove_work('movie', tmdb_id, 0)

    @app.put(path + '/{tmdb_id}/{season}', dependencies=[Depends(admin)])
    async def save_subscription(body: SubscriptionInput, tmdb_id: int = Path(gt=0),
                                season: int = Path(ge=1, le=99)):
        return await save_work('tv', tmdb_id, season, body)

    @app.delete(path + '/{tmdb_id}/{season}', dependencies=[Depends(admin)])
    async def remove_subscription(tmdb_id: int = Path(gt=0), season: int = Path(ge=1, le=99)):
        return remove_work('tv', tmdb_id, season)
