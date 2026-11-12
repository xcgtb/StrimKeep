import asyncio

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
        result = await share_service.check_alive(engine)
        assert result['dead_count'] == 0 and result['skipped'] == 1
        assert (engine.root / entry['strm_path']).exists()
        assert store.db.execute('SELECT revoked FROM share_strms WHERE id=?', (entry['id'],)).fetchone()[0] == 0
        await http.aclose(); store.close()
    asyncio.run(scenario())
