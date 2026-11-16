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
            assert len(calls) == 1
        store.db.close()
    asyncio.run(run())
