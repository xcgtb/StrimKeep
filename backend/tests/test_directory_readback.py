"""Real NAS directory /rb response and recovery without replaying video work."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from strimkeep.cleanup import EmptyDirectoryCleanup
from strimkeep.cloud115 import Cloud115, CloudError
from strimkeep.main import create_app
from strimkeep.organizer import Organizer
from strimkeep.recycle import match_record
from strimkeep.store import Store
from test_empty_directories import fixture
from test_organize import CONFIG, MemoryCloud, new_job


NAS_SOURCE = {'id': '3546308101340661627', 'parent_id': '3546036010171237849',
              'name': '海底总动员 (2003) {tmdb-12}', 'size': 0, 'is_dir': True}
NAS_INTENT = {'source': NAS_SOURCE, 'state': 'intent', 'at': 1792771952.4840376,
              'before_rids': ['3546310263571482388']}
NAS_ENTRY = {'id': '3546333984264815622', 'cid': NAS_SOURCE['parent_id'],
             'file_name': NAS_SOURCE['name'], 'file_size': '0', 'type': '2',
             'dtime': '1792771954', 'parent_name': '转存'}


def test_real_nas_type_two_is_directory_and_matches_the_saved_intent(tmp_path):
    async def scenario():
        store = Store(tmp_path, 'admin', 'test-password-1234')
        store.set_many({'cookie': 'UID=1_A1; CID=test; SEID=test'})
        cloud = Cloud115(store)
        await cloud.http.aclose()
        paths = []
        def respond(request):
            paths.append(request.url.path)
            assert request.method == 'GET'
            if request.url.path == '/rb':
                return httpx.Response(200, json={'state': True, 'count': 1, 'offset': 0, 'data': [NAS_ENTRY]})
            if request.url.path == '/files/get_info':
                assert request.url.params['file_id'] == NAS_SOURCE['id']
                return httpx.Response(200, json={'state': False, 'code': 20018})
            raise AssertionError('directory confirmation must not list the parent')
        cloud.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        try:
            entries = await cloud.recycle_files()
            assert entries[0].get('is_dir') is True
            assert entries[0]['id'] == '' and entries[0]['rid'] == NAS_ENTRY['id']
            assert match_record(NAS_SOURCE, NAS_INTENT, entries)['rid'] == NAS_ENTRY['id']
            intent = {**NAS_INTENT}
            await EmptyDirectoryCleanup(SimpleNamespace(cloud=cloud)).confirm(intent)
            assert intent['state'] == 'confirmed' and intent['rid'] == NAS_ENTRY['id']
            assert set(paths) == {'/rb', '/files/get_info'}
        finally:
            await cloud.close()
            store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('kind,is_dir', [('1', False), ('2', True), ('0', None), ('9', None)])
def test_rb_type_mapping_is_independent_of_other_api_enumerations(tmp_path, kind, is_dir):
    async def scenario():
        store = Store(tmp_path, 'admin', 'test-password-1234')
        store.set_many({'cookie': 'UID=1_A1; CID=test; SEID=test'})
        cloud = Cloud115(store)
        await cloud.http.aclose()
        cloud.http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
            200, json={'state': True, 'count': 1, 'offset': 0, 'data': [{**NAS_ENTRY, 'type': kind}]})))
        try:
            entries = await cloud.recycle_files()
            assert entries[0].get('is_dir') is is_dir
            if is_dir is not True:
                assert match_record(NAS_SOURCE, NAS_INTENT, entries) is None
        finally:
            await cloud.close()
            store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['parent', 'name', 'size', 'file-type', 'old-record', 'duplicate'])
def test_directory_match_still_requires_unique_new_record_and_exact_metadata(change):
    record = {'id': '', 'rid': NAS_ENTRY['id'], 'parent_id': NAS_SOURCE['parent_id'],
              'name': NAS_SOURCE['name'], 'size': 0, 'is_dir': True, 'deleted_at': NAS_ENTRY['dtime']}
    action = {**NAS_INTENT}
    if change == 'parent': record['parent_id'] = '999'
    elif change == 'name': record['name'] = 'another folder'
    elif change == 'size': record['size'] = 10
    elif change == 'file-type': record['is_dir'] = False
    elif change == 'old-record': action['before_rids'] = [record['rid']]
    if change == 'duplicate':
        with pytest.raises(CloudError):
            match_record(NAS_SOURCE, action, [record, {**record, 'rid': '999'}])
    else:
        assert match_record(NAS_SOURCE, action, [record]) is None


@pytest.mark.parametrize('interruption', ['none', 'login', 'pause-restart'])
def test_interrupted_immediate_cleanup_never_replays_video_work(tmp_path, interruption):
    """空目录在整理过程中即时回收；中断后恢复只补收尾，不重跑视频工作。

    回收已前移到 prune_empty_chain：正常路径内外两层一次收完；登录失效/暂停会停在
    已回收的那一层，外层留到下一轮。没有待恢复记录时，「重试空目录清理」必须明确拒绝，
    不能假装修复，也不能重复发回收请求或重跑视频处理。
    """
    async def scenario():
        store, cloud, http, engine, outer, inner, unrelated = await fixture(tmp_path)
        jid = new_job(store)
        real = cloud.recycle_empty_directory

        async def interrupted(cid, parent):
            await real(cid, parent)
            if str(cid) != str(inner):
                return
            if interruption == 'login':
                raise CloudError('登录失效。', 'LOGIN_INVALID', 401)
            engine.update_job(jid, state='paused')

        if interruption != 'none':
            cloud.recycle_empty_directory = interrupted
        await engine.run_job(jid)
        writes = [w for w in cloud.writes if w[0] == 'recycle-dir']
        if interruption == 'none':
            assert engine.job(jid)['state'] == 'completed', dict(engine.job(jid))
            assert writes == [('recycle-dir', inner), ('recycle-dir', outer)]
            assert json.loads(engine.job(jid)['config'])['empty_cleanup']['entries'] == []
            # 没有待恢复记录时，「重试空目录清理」明确拒绝，不假装修复也不重复回收。
            config = json.loads(engine.job(jid)['config'])
            config['cleanup_only'] = True
            engine.update_job(jid, config=json.dumps(config))
            await engine.run_job(jid)
            assert engine.job(jid)['state'] == 'needs_review', dict(engine.job(jid))
            assert '没有可单独恢复的目录清理记录' in engine.job(jid)['message']
            assert [w for w in cloud.writes if w[0] == 'recycle-dir'] == writes
        else:
            # 中断点落在即时清理链上：内层已回收，外层保留，等下一轮。
            assert writes == [('recycle-dir', inner)]
            assert inner not in cloud.nodes and outer in cloud.nodes
            assert engine.job(jid)['state'] == ('waiting_login' if interruption == 'login' else 'paused'),                 dict(engine.job(jid))
            before_plans = [tuple(row) for row in store.db.execute('SELECT id,state,plan FROM organize_items WHERE job_id=?', (jid,))]
            before_tokens = [tuple(row) for row in store.db.execute('SELECT * FROM playbacks')]
            before_files = {str(path): path.read_bytes() for path in (tmp_path/'strm').rglob('*') if path.is_file()}
            before_writes = list(cloud.writes)
            cloud.recycle_empty_directory = real
            if interruption == 'pause-restart':
                engine = Organizer(store, cloud, http, tmp_path/'strm')
            touched = []
            original_process = engine.process_item

            async def spy_process(*args, **kwargs):
                touched.append('process_item')
                return await original_process(*args, **kwargs)

            engine.process_item = spy_process
            await engine.run_job(jid)
            assert engine.job(jid)['state'] == 'completed', dict(engine.job(jid))
            assert touched == [], '恢复时不该重跑视频处理'
            assert cloud.writes == before_writes, '恢复时不该重复发回收请求'
            assert [tuple(row) for row in store.db.execute('SELECT id,state,plan FROM organize_items WHERE job_id=?', (jid,))] == before_plans
            assert [tuple(row) for row in store.db.execute('SELECT * FROM playbacks')] == before_tokens
            assert {str(path): path.read_bytes() for path in (tmp_path/'strm').rglob('*') if path.is_file()} == before_files
        await http.aclose()
        store.close()
    asyncio.run(scenario())


def test_delayed_directory_visibility_retries_reads_only():
    async def scenario():
        reads = 0
        record = {'id': '', 'rid': NAS_ENTRY['id'], 'parent_id': NAS_SOURCE['parent_id'],
                  'name': NAS_SOURCE['name'], 'size': 0, 'is_dir': True, 'deleted_at': NAS_ENTRY['dtime']}
        async def listing():
            nonlocal reads
            reads += 1
            return [] if reads < 3 else [record]
        async def children(parent): return []
        async def exists(fid): return False
        cloud = SimpleNamespace(recycle_files=listing, children=children, live_file_exists=exists)
        intent = {**NAS_INTENT}
        await EmptyDirectoryCleanup(SimpleNamespace(cloud=cloud)).confirm(intent)
        assert intent['state'] == 'confirmed' and reads == 3
    asyncio.run(scenario())

@pytest.mark.parametrize('live', [True, False, 'error'])
def test_directory_confirmation_uses_exact_live_id_without_parent_pagination(live):
    async def scenario():
        record = {'id':'', 'rid':NAS_ENTRY['id'], 'parent_id':NAS_SOURCE['parent_id'],
                  'name':NAS_SOURCE['name'], 'size':0, 'is_dir':True, 'deleted_at':NAS_ENTRY['dtime']}
        async def recycle_files(): return [record]
        async def children(parent):
            raise AssertionError('parent listing is unnecessary after exact retirement evidence')
        async def exists(fid):
            assert fid == NAS_SOURCE['id']
            if live == 'error': raise CloudError('存续检查失败', 'CLOUD_UNAVAILABLE')
            return live
        cloud = SimpleNamespace(recycle_files=recycle_files, children=children, live_file_exists=exists)
        intent = {**NAS_INTENT}
        cleanup = EmptyDirectoryCleanup(SimpleNamespace(cloud=cloud))
        if live is False:
            await cleanup.confirm(intent)
            assert intent['state'] == 'confirmed'
        else:
            with pytest.raises(CloudError): await cleanup.confirm(intent)
            assert intent['state'] == 'intent'
    asyncio.run(scenario())


def api_setup(tmp_path, monkeypatch):
    monkeypatch.setenv('ADMIN_USERNAME', 'admin')
    monkeypatch.setenv('ADMIN_PASSWORD', 'test-password-1234')
    # Test the authenticated enqueue contract independently of worker scheduling.
    monkeypatch.setattr(Organizer, 'start', lambda self: None)
    app = create_app(tmp_path/'data', tmp_path/'strm', MemoryCloud)
    app.state.store.set_many({'cookie': 'UID=1_A1; CID=test; SEID=test'})
    cfg = {**CONFIG, 'empty_cleanup': {'entries': [{**NAS_INTENT}], 'warning': '目录核查待重试。'}}
    jid = new_job(app.state.store, cfg)
    with app.state.store.db:
        app.state.store.db.execute("UPDATE organize_jobs SET state='partial',manifest=1,notify_state='accepted' WHERE id=?", (jid,))
        app.state.store.db.execute("INSERT INTO organize_items(job_id,file_id,original,state,plan) VALUES(?,?,?,'done',?)",
                                  (jid, '1000', json.dumps(NAS_SOURCE), json.dumps({'warnings': []})))
    return app, jid


def test_authenticated_cleanup_retry_only_queues_existing_task_and_hides_internal_state(tmp_path, monkeypatch):
    app, jid = api_setup(tmp_path, monkeypatch)
    with TestClient(app) as client:
        login = client.post('/api/v1/auth/login', json={'username': 'admin', 'password': 'test-password-1234'}).json()
        client.headers['X-CSRF-Token'] = login['csrf']
        before = [tuple(r) for r in app.state.store.db.execute('SELECT * FROM organize_items')]
        job = client.get('/api/v1/organize/jobs').json()['items'][0]
        assert job['can_retry_cleanup'] is True and 'empty_cleanup' not in job['config']
        result = client.post(f'/api/v1/organize/jobs/{jid}/retry-cleanup')
        assert result.status_code == 200 and result.json()['state'] == 'queued'
        assert result.json()['notify_state'] == 'accepted'
        assert 'cleanup_only' not in result.json()['config']
        config = json.loads(app.state.organizer.job(jid)['config'])
        assert config['cleanup_only'] is True and config['empty_cleanup']['entries'][0] == NAS_INTENT
        assert [tuple(r) for r in app.state.store.db.execute('SELECT * FROM organize_items')] == before
        assert client.post(f'/api/v1/organize/jobs/{jid}/retry-cleanup').status_code == 409


@pytest.mark.parametrize('reason', ['completed', 'cancelled', 'no-intent', 'repair-only', 'index-job', 'unfinished-video', 'no-manifest'])
def test_cleanup_retry_rejects_unrelated_or_unfinished_tasks(tmp_path, monkeypatch, reason):
    app, jid = api_setup(tmp_path, monkeypatch)
    with TestClient(app) as client:
        login = client.post('/api/v1/auth/login', json={'username': 'admin', 'password': 'test-password-1234'}).json()
        client.headers['X-CSRF-Token'] = login['csrf']
        config = json.loads(app.state.organizer.job(jid)['config'])
        if reason in ('completed', 'cancelled'):
            app.state.organizer.update_job(jid, state=reason)
        elif reason == 'no-intent': config.pop('empty_cleanup')
        elif reason == 'repair-only': config['repair_only'] = True
        elif reason == 'index-job': config['job_kind'] = 'archive_index'
        elif reason == 'unfinished-video':
            with app.state.store.db:
                app.state.store.db.execute("UPDATE organize_items SET state='archived' WHERE job_id=?", (jid,))
        elif reason == 'no-manifest': app.state.organizer.update_job(jid, manifest=0)
        elif reason == 'other-task': new_job(app.state.store)
        app.state.organizer.update_job(jid, config=json.dumps(config))
        before = dict(app.state.organizer.job(jid))
        assert client.post(f'/api/v1/organize/jobs/{jid}/retry-cleanup').status_code == 409
        assert dict(app.state.organizer.job(jid)) == before


def test_output_repair_after_cancelled_directory_retry_keeps_cloud_read_only_contract(tmp_path, monkeypatch):
    app, jid = api_setup(tmp_path, monkeypatch)
    with TestClient(app) as client:
        login = client.post('/api/v1/auth/login', json={'username': 'admin', 'password': 'test-password-1234'}).json()
        client.headers['X-CSRF-Token'] = login['csrf']
        config = json.loads(app.state.organizer.job(jid)['config'])
        config['cleanup_only'] = True
        app.state.organizer.update_job(jid, config=json.dumps(config), state='cancelled')
        result = client.post(f'/api/v1/organize/jobs/{jid}/retry-output')
        assert result.status_code == 200
        config = json.loads(app.state.organizer.job(jid)['config'])
        assert config['repair_only'] is True and 'cleanup_only' not in config
        assert config['empty_cleanup']['entries'][0] == NAS_INTENT
