"""Emby 每小时后台预热：只读、失败不影响调度、可关闭。"""
import asyncio
from types import SimpleNamespace

import httpx

from strimkeep.emby import EmbyPrewarm
from strimkeep.store import Store

BASE = 'http://emby/emby'


class FakeResponse:
    def __init__(self, data, status=200):
        self._data, self.status_code = data, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError('boom', request=httpx.Request('GET', BASE),
                                        response=httpx.Response(self.status_code))

    def json(self):
        return self._data


class FakeHTTP:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    async def get(self, url, **kwargs):
        path = url[len(BASE):] if url.startswith(BASE) else url
        self.calls.append((path, kwargs.get('params')))
        if path in self.routes:
            payload = self.routes[path]
            return payload if isinstance(payload, FakeResponse) else FakeResponse(payload)
        return FakeResponse({}, 404)


def engine(store, http, connection=('http://emby/emby', 'key')):
    return SimpleNamespace(store=store, http=http, emby=SimpleNamespace(connection=lambda: connection))


ROUTES = {
    '/Users': [{'Id': 'u1'}, {'Id': 'u2'}],
    '/Users/u1/Items/Resume': {'Items': [{'Id': 'a'}, {'Id': 'b'}]},
    '/Users/u1/Items': {'Items': [{'Id': 'c'}]},
    '/Users/u2/Items/Resume': {'Items': []},
    '/Users/u2/Items': {'Items': [{'Id': 'd'}, {'Id': 'e'}, {'Id': 'f'}]},
    '/Items/Counts': {'MovieCount': 10, 'SeriesCount': 3},
}


def test_prewarm_counts_users_and_items(tmp_path):
    async def run():
        store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
        http = FakeHTTP(ROUTES)
        result = await EmbyPrewarm(engine(store, http)).tick()
        assert result == {'users': 2, 'resume': 2, 'latest': 4, 'libraries': 2, 'failed': 0}
        paths = [path for path, _ in http.calls]
        assert '/Users/u1/Items/Resume' in paths and '/Users/u2/Items' in paths
        assert any(row['message'].startswith('Emby 后台预热完成') for row in
                   store.db.execute("SELECT message FROM events WHERE kind='emby'"))
        store.close()
    asyncio.run(run())


def test_prewarm_runs_once_per_interval(tmp_path):
    async def run():
        store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
        http = FakeHTTP(ROUTES)
        prewarm = EmbyPrewarm(engine(store, http))
        assert await prewarm.tick() is not None
        calls = len(http.calls)
        assert await prewarm.tick() is None, '一小时内不该重复预热'
        assert len(http.calls) == calls
        assert await prewarm.tick(now=prewarm._last + 61 * 60) is not None
        store.close()
    asyncio.run(run())


def test_prewarm_can_be_switched_off(tmp_path):
    async def run():
        store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
        store.set_many({'emby_prewarm_minutes': 0})
        http = FakeHTTP(ROUTES)
        assert await EmbyPrewarm(engine(store, http)).tick() is None
        assert http.calls == []
        store.close()
    asyncio.run(run())


def test_prewarm_skips_when_emby_is_not_configured(tmp_path):
    async def run():
        store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
        http = FakeHTTP(ROUTES)
        assert await EmbyPrewarm(engine(store, http, connection=None)).tick() is None
        assert http.calls == []
        store.close()
    asyncio.run(run())


def test_prewarm_failure_is_recorded_not_raised(tmp_path):
    async def run():
        store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
        http = FakeHTTP({'/Users': FakeResponse({}, 500)})
        result = await EmbyPrewarm(engine(store, http)).tick()
        assert result['failed'] == 1 and result['users'] == 0
        store.close()
    asyncio.run(run())
