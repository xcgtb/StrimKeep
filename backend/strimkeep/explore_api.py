"""TMDB 影视探索：热门/分类浏览 + 入库状态角标（以 Emby 媒体库为准）。"""
import hashlib
import json
import time
from datetime import date, timedelta
from collections import OrderedDict

from fastapi import Depends, HTTPException, Query

SORTS = ('popular', 'trending', 'top_rated', 'newest')
MOVIE_ONLY = ('now_playing', 'upcoming')
TV_ONLY = ('airing_today', 'on_the_air')

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


def install_explore(app, store, admin):
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
        genre: int = Query(None, ge=0),
        year: int = Query(None, ge=1900, le=2100),
        region: str = Query(None, pattern='^[A-Z]{2}$'),
        page: int = Query(1, ge=1, le=500),
        q: str = Query(None, max_length=200),
        per_page: int = Query(40, ge=10, le=60),
    ):
        allowed = set(MOVIE_ONLY if kind == 'movie' else TV_ONLY)
        selected = {sort} if sort in allowed else set()
        if selected:
            sort = 'popular'
        if sort not in SORTS:
            raise HTTPException(422, '筛选条件不适用于当前作品类型。')
        if q and q.strip() and (selected or genre or year or region or sort != 'popular'):
            raise HTTPException(422, '名称搜索暂不支持组合筛选，请清除筛选条件后搜索。')
        if sort == 'trending' and (selected or genre or year or region):
            raise HTTPException(422, 'TMDB 趋势榜暂不支持组合筛选，请选择其他排序。')
        params = {'page': page}
        date_field = 'primary_release_date' if kind == 'movie' else 'first_air_date'
        if q and q.strip():
            path = 'search/' + kind
            params['query'] = q.strip()
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
            params['with_origin_country'] = region
        if genre:
            params['with_genres'] = genre
        if year:
            params['primary_release_year' if kind == 'movie' else 'first_air_date_year'] = year

        import asyncio, math
        # Translate arbitrary page sizes into TMDB's fixed 20-item pages.
        # Round offsets before fetching, then slice: 30-item pages otherwise
        # silently skip ten works at each boundary.
        offset = (page - 1) * per_page
        if offset >= 10000:
            return {'items': [], 'page': page, 'total_pages': min(500, math.ceil(10000 / per_page))}
        base = offset // 20 + 1
        skip = offset % 20
        pages_needed = min(501 - base, max(1, math.ceil((skip + per_page) / 20)))
        pages = await asyncio.gather(*(tmdb(path, {**params, 'page': base + i})
                                       for i in range(pages_needed)))
        merged = []
        for data in pages:
            merged += data.get('results') or []
        seen = set()
        results = []
        for item in merged[skip:skip + per_page]:
            if not isinstance(item, dict) or 'id' not in item or item['id'] in seen:
                continue
            seen.add(item['id'])
            results.append(item)
        total = pages[0].get('total_results')
        if type(total) is not int or total < 0:
            total = (pages[0].get('total_pages') or 1) * 20
        total_pages = max(1, min(500, math.ceil(min(total, 10000) / per_page)))

        # Emby 为准（在不在媒体库），集数与总集数只查当前页里在库的剧集
        emby_idx, emby_complete = await emby_tmdb_index()
        org_idx = organized_totals()
        def series_id(tid):
            value = emby_idx.get(('tv', tid))
            return value if isinstance(value, str) and value else None
        tv_sids = [(r['id'], series_id(r['id'])) for r in results
                   if isinstance(r, dict) and series_id(r['id'])]
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
                'title': r.get('title') or r.get('name') or '',
                'poster': r.get('poster_path') or '',
                'rating': r.get('vote_average') or 0,
                'year': (r.get('release_date') or r.get('first_air_date') or '')[:4],
                'badge': badge, 'ratio': ratio,
            })
        return {'items': items, 'page': page, 'total_pages': min(total_pages, 500)}
