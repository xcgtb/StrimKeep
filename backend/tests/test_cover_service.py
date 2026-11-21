"""封面落地：取 Emby Primary/Backdrop 写 poster/fanart，Refresh 时序记录。"""
import asyncio
import time
from types import SimpleNamespace

import httpx
from fastapi import FastAPI

from strimkeep import commands, worker
from strimkeep.cover_service import CoverService, install_cover
from strimkeep.store import Store


class FakeDelivery:
    def __init__(self, url='http://emby/emby', key='k'):
        self._url, self._key = url, key
        self.store = None
        self.http = None

    def connection(self):
        return (self._url, self._key)


def make(tmp_path):
    store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
    store.set_many({'console_config': {'emby_strm_path': '/media'}})
    delivery = FakeDelivery()
    delivery.store = store
    service = CoverService(delivery, tmp_path / 'strm', store)
    return store, service, delivery


def test_cover_writes_missing_images_and_refreshes(tmp_path, monkeypatch):
    async def run():
        store, service, delivery = make(tmp_path)
        series_dir = tmp_path / 'strm' / '媒体库' / '剧集' / '某剧 (2026)'
        series_dir.mkdir(parents=True)
        done_dir = tmp_path / 'strm' / '媒体库' / '电影' / '老片 (2001)'
        done_dir.mkdir(parents=True)
        (tmp_path / 'strm' / '媒体库' / '剧集' / '无图 (2026)').mkdir(parents=True)
        (done_dir / 'poster.jpg').write_bytes(b'\xff\xd8old')
        items = [
            {'Id': '1', 'Type': 'Series', 'Name': '某剧',
             'Path': '/media/媒体库/剧集/某剧 (2026)',
             'ImageTags': {'Primary': 'tag'}, 'BackdropImageTags': ['b']},
            {'Id': '2', 'Type': 'Movie', 'Name': '老片',
             'Path': '/media/媒体库/电影/老片 (2001)/老片.strm',
             'ImageTags': {'Primary': 'tag'}, 'BackdropImageTags': []},
            {'Id': '3', 'Type': 'Series', 'Name': '无图',
             'Path': '/media/媒体库/剧集/无图 (2026)',
             'ImageTags': {}, 'BackdropImageTags': []},
        ]

        async def request(_http, method, url, **kwargs):
            if url.endswith('/Items') and method == 'GET':
                return httpx.Response(200, json={'Items': items},
                                      request=httpx.Request(method, url))
            if '/Images/Primary' in url:
                return httpx.Response(200, content=b'\xff\xd8fake',
                                      request=httpx.Request(method, url))
            if '/Images/Backdrop' in url:
                return httpx.Response(200, content=b'\xff\xd8bg',
                                      request=httpx.Request(method, url))
            if url.endswith('/Refresh'):
                return httpx.Response(204, request=httpx.Request(method, url))
            raise AssertionError(url)

        monkeypatch.setattr('strimkeep.cover_service.emby_request', request)
        summary = await service.sweep()
        assert summary['written'] == 1 and summary['skipped'] == 1 and summary['no_image'] == 1
        assert (series_dir / 'poster.jpg').read_bytes() == b'\xff\xd8fake'
        assert (series_dir / 'fanart.jpg').read_bytes() == b'\xff\xd8bg'
        assert (done_dir / 'poster.jpg').read_bytes() == b'\xff\xd8old'
        assert summary['timeline'] and summary['timeline'][0]['refresh_at']
        store.close()
    asyncio.run(run())


def test_daily_tick_runs_once_per_day(tmp_path, monkeypatch):
    async def run():
        store, service, delivery = make(tmp_path)
        calls = []

        async def fake_sweep():
            calls.append(1)
            return {'at': time.time(), 'timeline': []}

        monkeypatch.setattr(service, 'sweep', fake_sweep)
        import time as _t
        real = _t.localtime
        # 接 *args：store 写日志时调的是 localtime(now)，0 参数的 lambda 会 TypeError。
        monkeypatch.setattr(_t, 'localtime', lambda *args: time.struct_time(
            (2026, 11, 1, 1, 0, 0, 0, 0, 0)))
        await service.tick_daily()
        await service.tick_daily()               # 同日第二次不跑
        assert len(calls) == 1
        monkeypatch.setattr(_t, 'localtime', lambda *args: time.struct_time(
            (2026, 11, 1, 5, 0, 0, 0, 0, 0)))
        await service.tick_daily()               # 非 01 点不跑
        assert len(calls) == 1
        store.close()
    asyncio.run(run())


def test_split_web_queues_cover_and_reads_persisted_result(tmp_path, monkeypatch):
    async def run():
        store, service, _ = make(tmp_path)
        web_store = Store(store.root, 'admin', 'test-password-1234')
        app = FastAPI()
        app.state.store = web_store
        app.state.organizer = SimpleNamespace(web_only=True)
        install_cover(app, lambda: True)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            assert (await client.get('/api/v1/cover/status')).json() == {
                'running': False, 'queued': False, 'last': None, 'error': ''}
            assert (await client.post('/api/v1/cover/run')).status_code == 503
            store.set_meta('worker_heartbeat', {'at': time.time()})
            rebuild = commands.submit(store, 'strm_rebuild')
            first = await client.post('/api/v1/cover/run')
            assert first.status_code == 200 and first.json()['queued']
            assert (await client.get('/api/v1/cover/status')).json()['queued']
            assert (await client.post('/api/v1/cover/run')).status_code == 409
            summary = {'at': time.time(), 'scanned': 2, 'written': 1}
            stop = asyncio.Event()

            async def sweep():
                status = (await client.get('/api/v1/cover/status')).json()
                assert status['running'] and not status['queued']
                stop.set()
                return summary

            monkeypatch.setattr(service, 'sweep', sweep)
            await worker.command_loop(store, SimpleNamespace(cover=service), stop, (commands.COVER_SWEEP,))
            assert commands.get(store, rebuild)['state'] == 'queued'
            assert commands.get(store, first.json()['command_id'])['state'] == 'done'
            assert (await client.get('/api/v1/cover/status')).json() == {
                'running': False, 'queued': False, 'last': summary, 'error': ''}

            async def failed_sweep():
                stop.set()
                return {'error': 'Emby 未配置。'}

            monkeypatch.setattr(service, 'sweep', failed_sweep)
            stop.clear()
            assert (await client.post('/api/v1/cover/run')).status_code == 200
            await worker.command_loop(store, SimpleNamespace(cover=service), stop, (commands.COVER_SWEEP,))
            assert (await client.get('/api/v1/cover/status')).json() == {
                'running': False, 'queued': False, 'last': summary, 'error': 'Emby 未配置。'}
        web_store.close()
        store.close()
    asyncio.run(run())


def test_scheduled_cover_uses_same_queue_as_manual_cover(tmp_path, monkeypatch):
    async def run():
        store, service, _ = make(tmp_path)
        service.queue_daily = True
        monkeypatch.setattr(time, 'localtime', lambda *args: time.struct_time((2026, 11, 1, 1, 0, 0, 0, 0, 0)))
        manual = commands.submit(store, commands.COVER_SWEEP, unique=True)
        await service.tick_daily()
        assert commands.running(store, commands.COVER_SWEEP)['id'] == manual
        assert store.meta('cover_last_day') == ''
        commands.finish(store, manual, {})
        await service.tick_daily()
        daily = commands.running(store, commands.COVER_SWEEP)
        assert daily['id'] != manual
        assert store.meta('cover_last_day')
        commands.finish(store, daily['id'], {})
        await service.tick_daily()
        assert commands.running(store, commands.COVER_SWEEP) is None
        store.close()
    asyncio.run(run())
