import asyncio
import sqlite3

import httpx
import pytest

from strimkeep import tmdb_client
from strimkeep.cloud115 import CloudError


class Store:
    def __init__(self):
        self.db = sqlite3.connect(':memory:', check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute('CREATE TABLE tmdb_cache(key TEXT PRIMARY KEY,body TEXT,updated REAL)')

    def get(self, key, default=None):
        return 'test-key' if key == 'tmdb_key' else default


def test_retries_429_and_503_then_caches(monkeypatch):
    async def no_sleep(_seconds):
        return None
    monkeypatch.setattr(tmdb_client.asyncio, 'sleep', no_sleep)
    calls = []

    async def handler(request):
        calls.append(str(request.url))
        if len(calls) == 1:
            return httpx.Response(429, headers={'Retry-After': '0'}, request=request)
        if len(calls) == 2:
            return httpx.Response(503, request=request)
        return httpx.Response(200, json={'id': 42}, request=request)

    async def run():
        store = Store()
        client = tmdb_client.TmdbClient(store, retries=2)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            assert (await client.get(http, 'tv/42'))['id'] == 42
            assert len(calls) == 3
            # The successful response is cached; a second call must not hit TMDB.
            assert (await client.get(http, 'tv/42'))['id'] == 42
            assert len(calls) == 3
        store.db.close()
    asyncio.run(run())


def test_does_not_retry_4xx(monkeypatch):
    async def no_sleep(_seconds):
        return None
    monkeypatch.setattr(tmdb_client.asyncio, 'sleep', no_sleep)
    calls = []

    async def handler(request):
        calls.append(str(request.url))
        return httpx.Response(400, json={'status_code': 400}, request=request)

    async def run():
        store = Store()
        client = tmdb_client.TmdbClient(store, retries=2)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            with pytest.raises(CloudError) as error:
                await client.get(http, 'tv/42')
            assert error.value.code == 'TMDB_UNAVAILABLE'
            assert 'HTTP 400，GET tv/42' in str(error.value) and 'test-key' not in str(error.value)
            assert len(calls) == 1
        store.db.close()
    asyncio.run(run())


@pytest.mark.parametrize(('exception_type', 'reason'), [
    (httpx.ConnectTimeout, '连接超时'),
    (httpx.ReadTimeout, '等待响应超时'),
    (httpx.ConnectError, '连接异常'),
])
def test_tmdb_transport_error_identifies_failure_without_raw_url(exception_type, reason):
    async def run():
        def handler(request):
            raise exception_type(str(request.url), request=request)

        store = Store()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = tmdb_client.TmdbClient(store, retries=0)
            with pytest.raises(CloudError) as error:
                await client.get(http, 'tv/42/season/1')
            assert error.value.code == 'TMDB_UNAVAILABLE'
            assert reason in str(error.value) and 'GET tv/42/season/1' in str(error.value)
            assert 'test-key' not in str(error.value)
        store.db.close()

    asyncio.run(run())


def test_tmdb_cache_sql_runs_off_event_loop(monkeypatch):
    calls = []
    real_to_thread = asyncio.to_thread

    async def tracked(func, *args, **kwargs):
        calls.append(func)
        return await real_to_thread(func, *args, **kwargs)

    async def handler(request):
        return httpx.Response(200, json={'id': 42}, request=request)

    async def run():
        store = Store()
        monkeypatch.setattr(tmdb_client.asyncio, 'to_thread', tracked)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = tmdb_client.TmdbClient(store, retries=0)
            assert (await client.get(http, 'tv/42'))['id'] == 42
            assert (await client.get(http, 'tv/42'))['id'] == 42
        assert len(calls) >= 3, 'TMDB 缓存读写仍留在事件循环'
    asyncio.run(run())
