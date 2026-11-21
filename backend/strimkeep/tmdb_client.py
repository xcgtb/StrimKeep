"""TMDB HTTP client: cache, negative cache and bounded retries.

This is intentionally an I/O adapter, not part of the organizer state machine.
It keeps the exact error codes/messages the rest of the code already relies on.
"""
import asyncio
import json
import random
import time

import httpx

from .cloud115 import CloudError

SEARCH_TTL = 86400
DETAIL_TTL = 7 * 86400
EPISODE_TTL = 30 * 86400
MISSING_TTL = 3600

# 429/5xx 才重试；4xx 的鉴权/参数错误不重试，避免把无效请求放大。
RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_RETRIES = 2
BASE_DELAY = 0.5


def cache_key(path, params=None):
    """Cache key includes language; the same query in another language is another result."""
    extra = {k: str(v) for k, v in (params or {}).items() if k not in ('api_key',)}
    return path + ('?' + '&'.join(f'{k}={extra[k]}' for k in sorted(extra)) if extra else '')


def missing_error(exc):
    """TMDB「条目不存在」的两种历史写法：新代码 TMDB_NOT_FOUND，旧代码 404 + UNAVAILABLE。"""
    return getattr(exc, 'code', '') == 'TMDB_NOT_FOUND' or (
        getattr(exc, 'code', '') == 'TMDB_UNAVAILABLE' and getattr(exc, 'status', None) == 404)


def _retry_after(response):
    try:
        value = response.headers.get('Retry-After', '')
        return min(max(float(value), 0.0), 5.0)
    except (AttributeError, TypeError, ValueError):
        return 0.0


def _delay(attempt, response=None):
    server = _retry_after(response) if response is not None else 0.0
    return min(5.0, max(server, BASE_DELAY * (2 ** attempt) + random.uniform(0, 0.2)))


class TmdbClient:
    def __init__(self, store=None, count=None, retries=MAX_RETRIES):
        self.store = store
        self.count = count or (lambda _category: None)
        self.retries = max(0, int(retries))

    async def get(self, http, path, params=None, *, misses=None,
                  search_ttl=SEARCH_TTL, detail_ttl=DETAIL_TTL,
                  episode_ttl=EPISODE_TTL, missing_ttl=MISSING_TTL):
        key = await asyncio.to_thread(self.store.get, 'tmdb_key')
        if not key:
            raise CloudError('缺少 TMDB API Key。', 'TMDB_UNAVAILABLE', 409)
        ttl = (episode_ttl if '/episode/' in path else
               search_ttl if path.startswith('search/') else detail_ttl)
        cache = cache_key(path, params)
        now = time.time()
        cached = await asyncio.to_thread(
            lambda: self.store.db.execute(
                'SELECT body,updated FROM tmdb_cache WHERE key=?', (cache,)).fetchone())
        if cached and now - cached['updated'] < ttl:
            try:
                result = json.loads(cached['body'])
                self.count('缓存命中')
                return result
            except ValueError:
                pass
        if misses and misses.get(cache, 0) > time.monotonic():
            self.count('404 负缓存')
            raise CloudError('TMDB 中找不到该条目（GET %s → HTTP 404，%d 秒内不重试）。'
                             % (path, missing_ttl), 'TMDB_NOT_FOUND', 404)

        query = {'api_key': key, 'language': 'zh-CN', **(params or {})}
        for attempt in range(self.retries + 1):
            response = None
            try:
                self.count('网络请求')
                response = await http.get('https://api.themoviedb.org/3/' + path, params=query)
                response.raise_for_status()
                data = response.json()
                break
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status == 404:
                    if misses is not None:
                        if len(misses) > 512:
                            misses.clear()
                        misses[cache] = time.monotonic() + missing_ttl
                    raise CloudError('TMDB 中找不到该条目（GET %s → HTTP 404）。' % path,
                                     'TMDB_NOT_FOUND', 404) from None
                if status not in RETRY_STATUS or attempt >= self.retries:
                    raise CloudError(f'TMDB 请求失败（HTTP {status}，GET {path}）。', 'TMDB_UNAVAILABLE', 502) from None
                await asyncio.sleep(_delay(attempt, exc.response))
            except httpx.HTTPError as exc:
                if attempt >= self.retries:
                    if isinstance(exc, httpx.ConnectTimeout):
                        reason = '连接超时'
                    elif isinstance(exc, httpx.TimeoutException):
                        reason = '等待响应超时'
                    else:
                        reason = f'连接异常，{type(exc).__name__}'
                    raise CloudError(f'TMDB 请求失败（{reason}，GET {path}）。', 'TMDB_UNAVAILABLE') from None
                await asyncio.sleep(_delay(attempt))
            except ValueError:
                raise CloudError('TMDB 请求失败（返回数据无效）。', 'TMDB_UNAVAILABLE') from None
        else:  # pragma: no cover - loop always breaks or raises; kept for defensive clarity
            raise CloudError('TMDB 请求失败（连接或响应超时）。', 'TMDB_UNAVAILABLE')

        if misses is not None:
            misses.pop(cache, None)
        await asyncio.to_thread(self._save_cache, cache, data, now)
        return data

    def _save_cache(self, cache, data, now):
        with self.store.db:
            self.store.db.execute('INSERT OR REPLACE INTO tmdb_cache VALUES(?,?,?)',
                                  (cache, json.dumps(data, ensure_ascii=False), now))
            self.store.db.execute('DELETE FROM tmdb_cache WHERE updated<?', (now - 90 * 86400,))

    async def episode(self, tmdb, tv_id, season, number, *, season_unavailable,
                      episode_ttl=EPISODE_TTL):
        """单集缓存 → 季接口 → 单集接口；季接口不可用时短时间熔断。

        ``tmdb`` 由调用方传入（通常是 Organizer.tmdb），保留测试 monkeypatch 能力。
        """
        path = f'tv/{tv_id}/season/{season}/episode/{number}'
        cached = self.store.db.execute('SELECT body,updated FROM tmdb_cache WHERE key=?', (path,)).fetchone()
        if cached and time.time() - cached['updated'] < episode_ttl:
            try:
                data = json.loads(cached['body'])
                if isinstance(data, dict) and data.get('season_number') == season and data.get('episode_number') == number:
                    self.count('单集缓存命中')
                    return data
            except (TypeError, ValueError):
                pass
        season_path = f'tv/{tv_id}/season/{season}'
        unavailable_until = (season_unavailable or {}).get(season_path, 0)
        if time.monotonic() >= unavailable_until:
            try:
                data = await tmdb(season_path)
            except CloudError as exc:
                if exc.code not in ('TMDB_UNAVAILABLE', 'TMDB_NOT_FOUND'):
                    raise
                # 季接口不支持或暂时不可达时沿用原单集接口，不在每一集重复撞季接口。
                if season_unavailable is not None:
                    season_unavailable[season_path] = time.monotonic() + 60
            else:
                if isinstance(data, dict) and data.get('season_number') == season and isinstance(data.get('episodes'), list):
                    matches = [ep for ep in data['episodes'] if isinstance(ep, dict)
                               and ep.get('season_number') == season and ep.get('episode_number') == number]
                    required = ('id', 'name', 'overview', 'air_date', 'season_number', 'episode_number')
                    if len(matches) == 1 and matches[0].get('id') and all(key in matches[0] for key in required):
                        return matches[0]
        try:
            return await tmdb(path)
        except CloudError as exc:
            if exc.code == 'TMDB_NOT_FOUND':
                # TMDB 还没登记这一集（国产剧/短剧数据常常滞后于播出）：返回 None，
                # 让 build_plan 按文件名里的集号兜底归档，而不是整条转人工。
                return None
            raise

    async def search(self, tmdb, kind, query, year, attempts, variant, language=None):
        """一次 TMDB 搜索，并把尝试记进证据链。剧集不传年份，年份留到证据校验。"""
        params = {'query': query, 'include_adult': 'false'}
        if kind != 'tv' and year:
            params['year'] = year
        if language:
            params['language'] = language
        record = {'endpoint': 'search/' + kind, 'variant': variant,
                  'params': {k: v for k, v in params.items() if k != 'include_adult'}}
        try:
            data = await tmdb('search/' + kind, params)
        except Exception as exc:
            record.update({'candidates': 0, 'reason': (type(exc).__name__ + ':' + str(exc))[:60]})
            attempts.append(record)
            raise
        results = data.get('results') or []
        record.update({
            'candidates': len(results),
            'top': ' | '.join('%s(%s)#%s' % (r.get('name') or r.get('title') or '',
                                             (r.get('first_air_date') or r.get('release_date') or '')[:4],
                                             r.get('id')) for r in results[:3])})
        attempts.append(record)
        return results
