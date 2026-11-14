import asyncio
import time

from strimkeep import share_service
from strimkeep.cloud115 import CloudError
from test_phase2_share_pipeline import share_setup


def test_alive_check_never_deletes_replaced_strm_or_user_sidecars(tmp_path):
    async def scenario():
        store, cloud, http, engine, _ = share_setup(tmp_path)
        entries = share_service.generate(store, engine.root, {'share_code':'code','receive_code':''}, [
            {'id':'1','name':'bad.mkv','size':5000,'path':'Work/'},
            {'id':'2','name':'valid.mkv','size':5000,'path':'Work/'}])['created']
        paths = [engine.root / e['strm_path'] for e in entries]
        paths[0].write_text('https://example.invalid/user-content')
        sidecar = paths[0].parent / 'manual.nfo'
        sidecar.write_text('personal metadata')
        async def expired(*args): raise CloudError('失效', 'SHARE_NOT_FOUND', 404)
        cloud.share_url_download = expired
        async def cancelled(*args): return 'cancelled'
        cloud.share_availability = cancelled
        first = await share_service.check_alive(engine)
        assert first['dead_count'] == 0
        store.db.execute('UPDATE share_check_candidates SET first_at=?', (time.time()-301,))
        store.db.commit()
        result = await share_service.check_alive(engine)
        assert result['dead_count'] == 1 and result['skipped'] == 1
        assert paths[0].read_text() == 'https://example.invalid/user-content'
        assert not paths[1].exists()
        assert sidecar.read_text() == 'personal metadata'
        assert store.db.execute('SELECT revoked FROM share_strms WHERE id=?', (entries[0]['id'],)).fetchone()[0] == 0
        await http.aclose(); store.close()
    asyncio.run(scenario())


def test_alive_check_rechecks_row_after_concurrent_organize(tmp_path):
    async def scenario():
        store, cloud, http, engine, _ = share_setup(tmp_path)
        entry = share_service.generate(store, engine.root, {'share_code':'code','receive_code':''}, [
            {'id':'1','name':'one.mkv','size':5000,'path':'Work/'}])['created'][0]
        started, release = asyncio.Event(), asyncio.Event()
        async def expired(*args):
            started.set()
            await release.wait()
            raise CloudError('失效', 'SHARE_NOT_FOUND', 404)
        async def cancelled(*args): return 'cancelled'
        async def first_expired(*args): raise CloudError('失效', 'SHARE_NOT_FOUND', 404)
        cloud.share_availability = cancelled
        cloud.share_url_download = first_expired
        await share_service.check_alive(engine)
        store.db.execute('UPDATE share_check_candidates SET first_at=?', (time.time()-301,))
        store.db.commit()
        cloud.share_url_download = expired
        task = asyncio.create_task(share_service.check_alive(engine))
        await started.wait()
        source = engine.root / entry['strm_path']
        target = engine.root / '分享影视库' / 'new.strm'
        target.parent.mkdir(parents=True, exist_ok=True)
        async with engine.publication_gate:
            source.rename(target)
            with store.db:
                store.db.execute('UPDATE share_strms SET strm_path=? WHERE id=?', (target.relative_to(engine.root).as_posix(), entry['id']))
        release.set()
        result = await task
        assert result['dead_count'] == 0 and result['skipped'] == 1
        assert target.is_file()
        assert store.db.execute('SELECT revoked FROM share_strms WHERE id=?', (entry['id'],)).fetchone()[0] == 0
        await http.aclose(); store.close()
    asyncio.run(scenario())


def test_unidentified_move_refuses_replaced_or_moved_source(tmp_path):
    async def scenario():
        store, cloud, http, engine, _ = share_setup(tmp_path)
        entry = share_service.generate(store, engine.root, {'share_code':'code','receive_code':''}, [
            {'id':'1','name':'one.mkv','size':5000,'path':'Work/'}])['created'][0]
        row = store.db.execute('SELECT * FROM share_strms WHERE id=?', (entry['id'],)).fetchone()
        source = engine.root / entry['strm_path']
        source.write_text('https://example.invalid/user-content')
        await share_service._to_unident(engine, 1, row, None, '未识别')
        assert source.read_text() == 'https://example.invalid/user-content'
        assert store.db.execute('SELECT strm_path FROM share_strms WHERE id=?', (entry['id'],)).fetchone()[0] == entry['strm_path']
        assert not list((engine.root / share_service.UNIDENT_NAME).rglob('*.strm'))
        await http.aclose(); store.close()
    asyncio.run(scenario())


def test_alive_check_refuses_delete_if_internal_delete_record_fails(tmp_path, monkeypatch):
    from strimkeep import internal_delete
    monkeypatch.setattr(internal_delete, 'remember', lambda *args: None)
    async def scenario():
        store, cloud, http, engine, _ = share_setup(tmp_path)
        entry = share_service.generate(store, engine.root, {'share_code':'code','receive_code':''}, [
            {'id':'1','name':'one.mkv','size':5000,'path':'Work/'}])['created'][0]
        async def expired(*args): raise CloudError('失效', 'SHARE_NOT_FOUND', 404)
        cloud.share_url_download = expired
        async def cancelled(*args): return 'cancelled'
        cloud.share_availability = cancelled
        await share_service.check_alive(engine)
        store.db.execute('UPDATE share_check_candidates SET first_at=?', (time.time()-301,))
        store.db.commit()
        result = await share_service.check_alive(engine)
        assert result['dead_count'] == 0 and result['skipped'] == 1
        assert (engine.root / entry['strm_path']).exists()
        assert store.db.execute('SELECT revoked FROM share_strms WHERE id=?', (entry['id'],)).fetchone()[0] == 0
        await http.aclose(); store.close()
    asyncio.run(scenario())


def test_share_check_needs_separate_observations_and_resets_after_recovery(tmp_path):
    async def scenario():
        store, cloud, http, engine, _ = share_setup(tmp_path)
        entry = share_service.generate(store, engine.root, {'share_code':'code','receive_code':''},
            [{'id':'1','name':'one.mkv','size':5000,'path':'Work/'}])['created'][0]
        path = engine.root / entry['strm_path']
        async def expired(*args): raise CloudError('不存在', 'SHARE_NOT_FOUND', 404)
        async def cancelled(*args): return 'cancelled'
        cloud.share_url_download, cloud.share_availability = expired, cancelled
        assert (await share_service.check_alive(engine))['dead_count'] == 0
        assert (await share_service.check_alive(engine))['dead_count'] == 0
        assert path.exists()
        async def available(*args): return 'https://cdn.example/a', {}
        cloud.share_url_download = available
        assert (await share_service.check_alive(engine))['alive'] == 1
        assert store.db.execute('SELECT count(*) FROM share_check_candidates').fetchone()[0] == 0
        cloud.share_url_download = expired
        async def accessible(*args): return 'accessible'
        cloud.share_availability = accessible
        assert (await share_service.check_alive(engine))['dead_count'] == 0
        assert path.exists()
        await http.aclose(); store.close()
    asyncio.run(scenario())


def test_share_availability_accepts_only_explicit_cancel_code():
    from types import SimpleNamespace
    from strimkeep.cloud115 import Cloud115
    async def scenario():
        class Client: share_snap_app = object()
        class Fake:
            response = None
            def client(self): return Client()
            async def call(self, *args, **kwargs):
                if isinstance(self.response, Exception): raise self.response
                return self.response
        fake = Fake()
        for response, expected in [(CloudError('分享已取消','CLOUD_REJECTED',422,errno=4100010),'cancelled'),
                                   (CloudError('接口不存在','SHARE_NOT_FOUND',404),'unknown'),
                                   (CloudError('限流','RATE_LIMITED',503),'unknown'),
                                   ({'state':True,'data':{}},'accessible'),
                                   ({'state':True},'unknown')]:
            fake.response = response
            assert await Cloud115.share_availability(fake,'code','pwd') == expected
    asyncio.run(scenario())


def test_share_availability_uses_sdk_response_and_preserves_errno(tmp_path):
    from strimkeep.cloud115 import Cloud115
    from strimkeep.store import Store
    async def scenario():
        store=Store(tmp_path, 'admin', 'test-password-1234')
        cloud=Cloud115(store)
        class Client:
            response = None
            async def share_snap_app(self, payload, **kwargs):
                assert payload == {'share_code':'code', 'receive_code':'pwd', 'cid':0, 'limit':1, 'offset':0}
                assert kwargs['app'] == 'android'
                return self.response
        client=Client()
        cloud.client=lambda:client
        client.response={'state':False,'errno':4100010,'error':'分享已取消'}
        assert await cloud.share_availability('code','pwd') == 'cancelled'
        client.response={'state':False,'errno':10004,'error':'接口不存在'}
        assert await cloud.share_availability('code','pwd') == 'unknown'
        client.response={'state':True,'data':{}}
        assert await cloud.share_availability('code','pwd') == 'accessible'
        await cloud.close(); store.close()
    asyncio.run(scenario())
