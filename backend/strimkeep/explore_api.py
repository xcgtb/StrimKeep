"""TMDB 影视探索：热门/分类浏览 + 入库状态角标（以 Emby 媒体库为准）。"""
import asyncio
import hashlib
import json
import math
import re
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from collections import OrderedDict
from typing import Annotated

from fastapi import Depends, HTTPException, Query
from pydantic import Field

SORTS = ('popular', 'trending', 'top_rated', 'newest')
MOVIE_ONLY = ('now_playing', 'upcoming')
TV_ONLY = ('airing_today', 'on_the_air')
TMDB_PAGE_SIZE = 20
TMDB_PAGE_LIMIT = 500
RESULT_FIELDS = ('id', 'title', 'name', 'original_title', 'original_name',
                 'poster_path', 'vote_average', 'release_date', 'first_air_date')

# TMDB ID → {kind}，5 分钟缓存，避免每次浏览都拉全库
_emby_cache = {'at': 0, 'index': {}}


class _SeriesCountCache:
    """Bounded, expiring counts isolated by Emby connection and series ID."""
    def __init__(self, limit=512, ttl=600):
        self.limit = limit
        self.ttl = ttl
        self.entries = OrderedDict()

    def _expire(self, now):
        for key, (stamp, _) in list(self.entries.items()):
            if not 0 <= now - stamp < self.ttl:
                del self.entries[key]

    def get(self, key, now):
        self._expire(now)
        item = self.entries.get(key)
        if item is None:
            return None
        self.entries.move_to_end(key)
        return item[1]

    def put(self, key, value, now):
        self._expire(now)
        self.entries[key] = (now, value)
        self.entries.move_to_end(key)
        while len(self.entries) > self.limit:
            self.entries.popitem(last=False)


def _emby_identity(url, key):
    return hashlib.sha256((url + '\0' + key).encode()).hexdigest()


@dataclass
class _ExploreWindow:
    """A sequence of distinct works retained while the user turns adjacent pages."""
    next_page: int
    offset: int
    last_access: float
    last_page: int = TMDB_PAGE_LIMIT
    total: int | None = None
    items: list = field(default_factory=list)
    ids: set = field(default_factory=set)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def append(self, number, data):
        raw = data.get('results') or []
        reported_total = data.get('total_results')
        if type(reported_total) is int and reported_total >= 0:
            self.total = min(reported_total, TMDB_PAGE_LIMIT * TMDB_PAGE_SIZE)
            raw = raw[:max(0, self.total - (number - 1) * TMDB_PAGE_SIZE)]
        reported_pages = data.get('total_pages')
        if type(reported_pages) is int and reported_pages >= 0:
            self.last_page = min(reported_pages, TMDB_PAGE_LIMIT)
        elif self.total is not None:
            self.last_page = math.ceil(self.total / TMDB_PAGE_SIZE)
        elif len(raw) < TMDB_PAGE_SIZE:
            self.last_page = number
        for item in raw:
            if not isinstance(item, dict) or type(item.get('id')) is not int or item['id'] in self.ids:
                continue
            self.ids.add(item['id'])
            self.items.append({name: item[name] for name in RESULT_FIELDS if name in item})
        self.next_page = number + 1


class _ExplorePages:
    def __init__(self):
        self.windows = OrderedDict()

    async def collect(self, fetch, path, params, page, per_page):
        offset = (page - 1) * per_page
        max_results = TMDB_PAGE_LIMIT * TMDB_PAGE_SIZE
        if offset >= max_results:
            return [], min(page, math.ceil(max_results / per_page)), math.ceil(max_results / per_page), False, False
        key = (path, tuple(sorted(params.items())))
        now = time.monotonic()
        window = self.windows.get(key)
        if window is None or offset < window.offset or (
            not window.lock.locked() and (
                now - window.last_access > 300
                or offset > window.offset + len(window.items) + 12 * TMDB_PAGE_SIZE
            )
        ):
            # An unrelated client can jump deep into the result set without replaying hundreds of API pages.
            start = 1 if offset <= 12 * TMDB_PAGE_SIZE else offset // TMDB_PAGE_SIZE + 1
            window = self.windows[key] = _ExploreWindow(start, (start - 1) * TMDB_PAGE_SIZE, now)
        self.windows.move_to_end(key)
        while len(self.windows) > 8:
            self.windows.popitem(last=False)
        async with window.lock:
            window.last_access = time.monotonic()
            wanted = offset + per_page - window.offset
            pulled = 0
            while len(window.items) < wanted and window.next_page <= window.last_page:
                if pulled >= 24:
                    raise HTTPException(502, 'TMDB 返回的分页数据暂时不完整，请重试。')
                count = min(4, max(1, math.ceil((wanted - len(window.items)) / TMDB_PAGE_SIZE)))
                numbers = range(window.next_page, min(window.next_page + count, window.last_page + 1))
                pages = await asyncio.gather(*(fetch(path, {**params, 'page': n}) for n in numbers))
                pulled += len(pages)
                for number, data in zip(numbers, pages):
                    if number > window.last_page:
                        break
                    window.append(number, data)
            exhausted = window.next_page > window.last_page
            available = window.offset + len(window.items)
            estimate = window.total if window.total is not None else window.last_page * TMDB_PAGE_SIZE
            if exhausted and window.offset and not window.items and estimate <= window.offset:
                last = max(1, math.ceil(estimate / per_page))
            else:
                last = None
            total = available if exhausted else max(available, estimate)
            total_pages = max(1, min(TMDB_PAGE_LIMIT, math.ceil(total / per_page)))
            page = min(page, total_pages)
            start = (page - 1) * per_page - window.offset
            items = window.items[max(0, start):max(0, start + per_page)]
        if last is not None:
            return await self.collect(fetch, path, params, last, per_page)
        return items, page, total_pages, exhausted and not available, page < total_pages


def install_explore(app, store, admin):
    explore_pages = _ExplorePages()

    async def tmdb(path, params=None):
        key = store.get('tmdb_key')
        if not key:
            raise HTTPException(409, '请先保存 TMDB API Key。')
        import httpx
        try:
            response = await app.state.http.get('https://api.themoviedb.org/3/' + path,
                params={'api_key': key, 'language': 'zh-CN', **(params or {})})
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError):
            raise HTTPException(502, 'TMDB 请求失败，请检查 API Key 和网络。')

    async def emby_tmdb_index():
        """(类型, TMDB ID) → 条目 Id，以 Emby 媒体库为准（ProviderIds.Tmdb）。

        TMDB 的电影与剧集是两套命名空间，同一个数字可以既是电影又是剧集
        （例如 1480 同时是《历劫佳人》和《甜心俏佳人》）。只按数字索引时，
        后写入的电影会覆盖剧集的 sid，剧集角标就退化成「在库」。
        5 分钟缓存，避免每次探索都拉 9000+ 条。"""
        connection = app.state.organizer.emby.connection()
        if not connection:
            return {}, set()
        url, key = connection
        identity = _emby_identity(url, key)
        now = time.time()
        if (_emby_cache.get('identity') == identity and _emby_cache['at']
                and now - _emby_cache['at'] < 300):
            return _emby_cache['index'], set(_emby_cache.get('complete', ()))
        index, complete = {}, set()
        for item_type, kind in (('Series', 'tv'), ('Movie', 'movie')):
            try:
                response = await app.state.http.get(url + '/Items', headers={'X-Emby-Token': key},
                    params={'Recursive': 'true', 'IncludeItemTypes': item_type,
                            'Fields': 'ProviderIds', 'Limit': 20000})
                response.raise_for_status()
                data = response.json()
                items = data.get('Items')
                if not isinstance(items, list):
                    continue
                for item in items:
                    tmdb_id = (item.get('ProviderIds') or {}).get('Tmdb')
                    if not tmdb_id:
                        continue
                    try:
                        key_value = (kind, int(tmdb_id))
                    except (ValueError, TypeError):
                        continue
                    index[key_value] = item.get('Id') if kind == 'tv' else True
                # A truncated library can confirm presence, but not absence.
                total = data.get('TotalRecordCount')
                if total is None or (isinstance(total, int) and total <= len(items)):
                    complete.add(kind)
            except Exception:
                continue
        if complete == {'tv', 'movie'}:
            _emby_cache.update(at=now, index=index, identity=identity, complete=complete)
        return index, complete

    _unique_cache = _SeriesCountCache()

    async def series_unique_count(sid):
        """按 (季, 集) 去重后的真实集数。

        同一部剧在库里存在重复副本时（例如两套 STRM 产线各入库一次），Emby 会把
        两套分集都算进同一个 Series，集数会超过 TMDB 的总集数（16/8）。
        """
        connection = app.state.organizer.emby.connection()
        if not connection:
            return 0
        url, key = connection
        cache_key = (_emby_identity(url, key), sid)
        now = time.monotonic()
        cached = _unique_cache.get(cache_key, now)
        if cached is not None:
            return cached
        try:
            response = await app.state.http.get(url + '/Shows/%s/Episodes' % sid,
                headers={'X-Emby-Token': key},
                params={'Limit': 10000, 'Fields': 'ParentIndexNumber,IndexNumber'})
            response.raise_for_status()
            items = response.json().get('Items') or []
            total = len({(item.get('ParentIndexNumber'), item.get('IndexNumber'))
                         for item in items
                         if isinstance(item, dict) and item.get('IndexNumber') is not None})
        except Exception:
            total = 0
        _unique_cache.put(cache_key, total, time.monotonic())
        return total

    _eps_cache = _SeriesCountCache()

    async def series_episode_count(sid):
        """单部剧的已入库集数：/Shows/{sid}/Seasons 的 ChildCount 求和，缓存 10 分钟。"""
        connection = app.state.organizer.emby.connection()
        if not connection:
            return 0
        url, key = connection
        cache_key = (_emby_identity(url, key), sid)
        now = time.monotonic()
        cached = _eps_cache.get(cache_key, now)
        if cached is not None:
            return cached
        try:
            response = await app.state.http.get(url + '/Shows/%s/Episodes' % sid,
                headers={'X-Emby-Token': key}, params={'Limit': 0})
            response.raise_for_status()
            total = int(response.json().get('TotalRecordCount') or 0)
        except Exception:
            total = 0
        _eps_cache.put(cache_key, total, time.monotonic())
        return total

    def organized_totals():
        """organized_files 的 TMDB id → {'count': 已整理集数, 'total': TMDB 总集数}。
        作为 Emby 集数的补充：Emby 不返回每部剧的集数，但 organized_files 的 metadata
        里存了 number_of_episodes。"""
        index = {}
        for row in store.db.execute('SELECT metadata FROM organized_files'):
            try:
                m = json.loads(row[0])
                details = m.get('details', {})
                tid = details.get('id')
                if not tid or m.get('hint', {}).get('media_type') != 'tv':
                    continue
                entry = index.setdefault(tid, {'count': 0, 'total': 0})
                entry['count'] += 1
                entry['total'] = max(entry['total'], details.get('number_of_episodes') or 0)
            except Exception:
                continue
        return index

    @app.get('/api/v1/explore/genres', dependencies=[Depends(admin)])
    async def genres(kind: str = Query('movie', pattern='^(movie|tv)$')):
        data = await tmdb('genre/' + kind + '/list')
        return {'genres': data.get('genres', [])}

    @app.get('/api/v1/explore/list', dependencies=[Depends(admin)])
    async def explore_list(
        kind: str = Query('movie', pattern='^(movie|tv)$'),
        sort: str = Query('popular'),
        genre: list[Annotated[int, Field(ge=0)]] = Query([], max_length=32),
        year: int = Query(None, ge=1900, le=2100),
        region: list[Annotated[str, Field(pattern='^[A-Z]{2}$')]] = Query([], max_length=20),
        page: int = Query(1, ge=1, le=500),
        q: str = Query(None, max_length=200),
        per_page: int = Query(40, ge=10, le=60),
    ):
        genre = sorted(set(genre) - {0})
        region = sorted(set(region))
        allowed = set(MOVIE_ONLY if kind == 'movie' else TV_ONLY)
        selected = {sort} if sort in allowed else set()
        if selected:
            sort = 'popular'
        if sort not in SORTS:
            raise HTTPException(422, '筛选条件不适用于当前作品类型。')
        if q and q.strip() and (selected or genre or region or sort != 'popular'):
            raise HTTPException(422, '名称搜索请先清除类型 / 地区 / 榜单筛选，年份可以保留。')
        if sort == 'trending' and (selected or genre or year or region):
            raise HTTPException(422, 'TMDB 趋势榜暂不支持组合筛选，请选择其他排序。')
        if not store.get('tmdb_key'):
            raise HTTPException(409, '请先保存 TMDB API Key。')
        params = {}
        date_field = 'primary_release_date' if kind == 'movie' else 'first_air_date'
        query = (q or '').strip()
        if query:
            # 「沙丘 2021」「沙丘 (2021)」这类输入：把年份拆出来当筛选条件。
            # 整串丢给 TMDB 搜索基本必然 0 结果——现场反馈的「带年份就搜不到」。
            match = re.search(r'[\(（\[]?\s*((?:19|20)\d{2})\s*[\)）\]]?$', query)
            if match:
                trimmed = query[:match.start()].strip()
                if trimmed:
                    if year is None:
                        year = int(match.group(1))
                    query = trimmed
        if query:
            path = 'search/' + kind
            params['query'] = query
            params['include_adult'] = 'false'
        elif sort == 'trending':
            path = 'trending/' + kind + '/week'
        elif selected and not (region or genre or year):
            path = kind + '/' + next(iter(selected))
        else:
            path = 'discover/' + kind
            params['sort_by'] = {'popular': 'popularity.desc', 'top_rated': 'vote_average.desc',
                                 'newest': date_field + '.desc'}[sort]
            if sort == 'top_rated':
                params['vote_count.gte'] = 200
        if selected and path.startswith('discover/'):
            today = date.today()
            if kind == 'movie':
                if 'now_playing' in selected:
                    params['primary_release_date.gte'] = (today - timedelta(days=42)).isoformat()
                else:
                    params['primary_release_date.gte'] = today.isoformat()
                if 'upcoming' not in selected:
                    params['primary_release_date.lte'] = today.isoformat()
            else:
                params['air_date.gte'] = (today - timedelta(days=7) if 'on_the_air' in selected else today).isoformat()
                params['air_date.lte'] = today.isoformat()
        if region:
            params['with_origin_country'] = '|'.join(region)
        if genre:
            params['with_genres'] = '|'.join(map(str, genre))
        if year:
            params['primary_release_year' if kind == 'movie' else 'first_air_date_year'] = year

        year_key = 'primary_release_year' if kind == 'movie' else 'first_air_date_year'
        results, actual_page, total_pages, empty, has_more = await explore_pages.collect(tmdb, path, params, page, per_page)
        relaxed_year = bool(query and year and empty)
        if relaxed_year:
            relaxed = {key: value for key, value in params.items() if key != year_key}
            results, actual_page, total_pages, _, has_more = await explore_pages.collect(tmdb, path, relaxed, page, per_page)
        if not results:
            return {'items': [], 'page': actual_page, 'total_pages': total_pages, 'has_more': False,
                    'relaxed_year': relaxed_year}

        # Emby 为准（在不在媒体库），集数与总集数只查当前页里在库的剧集
        emby_idx, emby_complete = await emby_tmdb_index()
        org_idx = organized_totals()
        def series_id(tid):
            value = emby_idx.get(('tv', tid))
            return value if isinstance(value, str) and value else None
        tv_sids = [(r['id'], series_id(r['id'])) for r in results
                   if kind == 'tv' and series_id(r['id'])]
        async def _tv_total(tid):
            try:
                details = await app.state.organizer.tmdb('tv/%d' % tid)
                return details.get('number_of_episodes') or 0
            except Exception:
                return 0
        counts_list = await asyncio.gather(*(series_episode_count(sid) for _, sid in tv_sids))
        totals_list = await asyncio.gather(*(_tv_total(tid) for tid, _ in tv_sids))
        eps_counts = {tid: n for (tid, _), n in zip(tv_sids, counts_list)}
        tv_totals = {tid: n for (tid, _), n in zip(tv_sids, totals_list)}

        def total_of(tid):
            # TMDB 的实时总集数优先：我们记录的 total 是整理当时的快照，连载剧会过时。
            return tv_totals.get(tid) or (org_idx.get(tid) or {}).get('total') or 0

        # 计数超过总集数，说明库里同一部剧有重复副本；这时按 (季, 集) 去重。
        suspect = [(tid, sid) for tid, sid in tv_sids
                   if total_of(tid) and (eps_counts.get(tid) or 0) > total_of(tid)]
        if suspect:
            unique = await asyncio.gather(*(series_unique_count(sid) for _, sid in suspect))
            for (tid, _), value in zip(suspect, unique):
                eps_counts[tid] = value

        items = []
        for r in results:
            if not isinstance(r, dict) or 'id' not in r:
                continue
            tid = r['id']
            in_emby = (kind, tid) in emby_idx
            org = org_idx.get(tid)

            if not in_emby and kind not in emby_complete:
                badge, ratio = '状态未知', None
            elif not in_emby:
                # Emby 没有 = 未入库（即使 organized_files 有，STRM 可能还没扫进 Emby）
                badge, ratio = '未入库', None
            elif kind == 'movie':
                badge, ratio = '已入库', None
            elif kind == 'tv':
                # 已入库集数：Emby 逐剧统计（缓存 10 分钟）为准，organized_files 兜底
                count = eps_counts.get(tid) or (org or {}).get('count') or 0
                total = total_of(tid)
                if total and count >= total:
                    badge = '已完整'
                    ratio = '%d/%d' % (count, total) if count <= total else '%d 集' % count
                elif count > 0 and total:
                    badge, ratio = '连载中', '%d/%d' % (count, total)
                elif count > 0:
                    badge, ratio = '已入库', None
                else:
                    badge, ratio = '在库', None
            else:
                badge, ratio = '在库', None
            items.append({
                'id': tid, 'kind': kind,
                'title': r.get('title') or r.get('name') or r.get('original_title') or r.get('original_name') or '未命名',
                'poster': r.get('poster_path') or '',
                'rating': r.get('vote_average') or 0,
                'year': (r.get('release_date') or r.get('first_air_date') or '')[:4],
                'badge': badge, 'ratio': ratio,
            })
        return {'items': items, 'page': actual_page, 'total_pages': total_pages,
                'has_more': has_more, 'relaxed_year': relaxed_year}
