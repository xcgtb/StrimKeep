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
    def __init__(self, store, count=None, retries=MAX_RETRIES):
        self.store = store
        self.count = count or (lambda _category: None)
        self.retries = max(0, int(retries))

    async def get(self, http, path, params=None, *, misses=None,
                  search_ttl=SEARCH_TTL, detail_ttl=DETAIL_TTL,
                  episode_ttl=EPISODE_TTL, missing_ttl=MISSING_TTL):
        key = self.store.get('tmdb_key')
        if not key:
            raise CloudError('缺少 TMDB API Key。', 'TMDB_UNAVAILABLE', 409)
        ttl = (episode_ttl if '/episode/' in path else
               search_ttl if path.startswith('search/') else detail_ttl)
        cache = cache_key(path, params)
        now = time.time()
        cached = self.store.db.execute('SELECT body,updated FROM tmdb_cache WHERE key=?', (cache,)).fetchone()
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
                    raise CloudError(f'TMDB 请求失败（HTTP {status}）。', 'TMDB_UNAVAILABLE', 502) from None
                await asyncio.sleep(_delay(attempt, exc.response))
            except (httpx.TimeoutException, httpx.HTTPError):
                if attempt >= self.retries:
                    raise CloudError('TMDB 请求失败（连接或响应超时）。', 'TMDB_UNAVAILABLE') from None
                await asyncio.sleep(_delay(attempt))
            except ValueError:
                raise CloudError('TMDB 请求失败（返回数据无效）。', 'TMDB_UNAVAILABLE') from None
        else:  # pragma: no cover - loop always breaks or raises; kept for defensive clarity
            raise CloudError('TMDB 请求失败（连接或响应超时）。', 'TMDB_UNAVAILABLE')

        if misses is not None:
            misses.pop(cache, None)
        with self.store.db:
            self.store.db.execute('INSERT OR REPLACE INTO tmdb_cache VALUES(?,?,?)',
                                  (cache, json.dumps(data, ensure_ascii=False), now))
            self.store.db.execute('DELETE FROM tmdb_cache WHERE updated<?', (now - 90 * 86400,))
        return data
