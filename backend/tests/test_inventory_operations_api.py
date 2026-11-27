import asyncio
import json
import sqlite3
import sys
import threading
import time
from contextlib import asynccontextmanager
from types import ModuleType, SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from strimkeep.inventory_operations_api import install_inventory_operations, inventory_delete_loop
from strimkeep.schema_migrations import INVENTORY_DISPATCH_DDL


def setup_app(root, monkeypatch, include_share=True, *, start_worker=True):
    relative = '分享影视库/剧集/Show {tmdb-12}/Season 01/Show.S01E01.1080p.strm'
    path = root / relative
    if include_share:
        path.parent.mkdir(parents=True)
        (path.parents[1] / 'tvshow.nfo').write_text('show', encoding='utf-8')
        (path.parents[1] / 'poster.jpg').write_text('poster', encoding='utf-8')
        path.write_text('http://example/play115/share/s1/a', encoding='utf-8')
    (root / 'cloud').mkdir()
    database_path = root / 'inventory-test.sqlite'
    db = sqlite3.connect(database_path, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.executescript('''CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
      CREATE TABLE organize_jobs(state TEXT);
      CREATE TABLE share_wash_intents(id TEXT);
      CREATE TABLE organized_files(file_id TEXT,playback_id TEXT,relative_path TEXT);
      CREATE TABLE share_strms(id TEXT,share_code TEXT,receive_code TEXT,file_id TEXT,pickcode TEXT,strm_path TEXT,token_hash TEXT,revoked INTEGER);
      CREATE TABLE output_registry(account_id TEXT,file_id TEXT,source TEXT,trail TEXT,playback_id TEXT,relative_path TEXT,files TEXT,active INTEGER);
      CREATE TABLE owned_outputs(relative_path TEXT,account_id TEXT,file_id TEXT,digest TEXT);''')
    db.execute(INVENTORY_DISPATCH_DDL)
    if include_share:
        db.execute('INSERT INTO share_strms VALUES (?,?,?,?,?,?,?,?)', ('s1', 'code', '', 'f1', '', relative, 'hash', 0))
    db.commit()
    settings = {'organize_config': {'library_dir_name': 'cloud'}}
    class TestStore:
        def __init__(self):
            self.local = threading.local()
            self.local.db = db
            self.root = root / 'state'

        @property
        def db(self):
            if not hasattr(self.local, 'db'):
                connection = sqlite3.connect(database_path, timeout=10)
                connection.row_factory = sqlite3.Row
                self.local.db = connection
            return self.local.db

        def get(self, key, default=None):
            return settings.get(key, default)

    store = TestStore()
    engine = SimpleNamespace(root=root, store=store, publication_gate=asyncio.Lock(), index=SimpleNamespace(account=lambda _: 'account'))
    share = ModuleType('strimkeep.share_service')
    share._verified_share_strm = lambda base, rel, sid: base / rel if sid == 's1' else None
    monkeypatch.setitem(sys.modules, 'strimkeep.share_service', share)
    @asynccontextmanager
    async def lifespan(app):
        task = None
        if start_worker:
            task = asyncio.create_task(inventory_delete_loop(store, app.state.inventory_run_delete, interval=.01))
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    app = FastAPI(lifespan=lifespan)
    app.state.organizer = engine
    app.state.strms = root
    def admin(): raise HTTPException(401)
    install_inventory_operations(app, store, admin)
    return app, admin, path, db


def test_single_cloud_library_can_plan_and_absence_is_revalidated(tmp_path, monkeypatch):
    app, admin, _, db = setup_app(tmp_path, monkeypatch, include_share=False)
    relative = 'cloud/Movie {tmdb-78}/Movie.strm'
    media = tmp_path / relative
    media.parent.mkdir()
    media.write_text('http://example/cloud', encoding='utf-8')
    checksum = 'a' * 64
    db.execute('INSERT INTO organized_files VALUES (?,?,?)', ('13', 'p1', relative))
    db.execute('INSERT INTO output_registry VALUES (?,?,?,?,?,?,?,1)',
               ('account', '13', '{}', '[]', 'p1', relative, json.dumps({relative: checksum})))
    db.execute('INSERT INTO owned_outputs VALUES (?,?,?,?)', (relative, 'account', '13', checksum))
    db.commit()
    with TestClient(app) as client:
        app.dependency_overrides[admin] = lambda: None
        request = {'mode': 'manual', 'selections': [{'kind': 'movie', 'tmdb_id': 78, 'side': 'cloud'}]}
        response = client.post('/api/v1/inventory/delete/plan', json=request)
        assert response.status_code == 200, response.text
        assert [action['side'] for action in response.json()['actions']] == ['cloud']
        (tmp_path / '分享影视库').mkdir()
        plan_id = response.json()['plan_id']
        assert client.post(f'/api/v1/inventory/delete/{plan_id}/execute').status_code == 200
        assert wait(client, plan_id)['state'] == 'failed'
        assert media.exists()
    db.close()


def wait(client, plan_id):
    for _ in range(100):
        result = client.get(f'/api/v1/inventory/delete/{plan_id}').json()
        if result['state'] not in ('starting', 'executing'):
            return result
        time.sleep(.02)
    raise AssertionError('plan did not finish')


def test_web_only_queues_and_worker_waits_for_running_publishers(tmp_path, monkeypatch):
    app, admin, path, db = setup_app(tmp_path, monkeypatch, start_worker=False)
    try:
        with TestClient(app) as client:
            app.dependency_overrides[admin] = lambda: None
            plan = client.post('/api/v1/inventory/delete/plan', json={
                'mode': 'manual', 'selections': [{'kind': 'tv', 'tmdb_id': 12, 'side': 'share'}]}).json()
            plan_id = plan['plan_id']
            assert client.post(f'/api/v1/inventory/delete/{plan_id}/execute').status_code == 200
            assert client.get(f'/api/v1/inventory/delete/{plan_id}').json()['state'] == 'starting'
            assert path.is_file()

        async def run():
            from strimkeep.inventory_plans import _load
            with db:
                db.execute("INSERT INTO organize_jobs VALUES('running')")
            task = asyncio.create_task(app.state.inventory_run_delete(plan_id))
            await asyncio.sleep(.04)
            assert path.is_file() and not task.done()
            async with app.state.organizer.publication_gate:
                with db:
                    db.execute("DELETE FROM organize_jobs")
                assert path.is_file() and not task.done()
            await asyncio.wait_for(task, 3)
            assert not path.exists()
            assert _load(db, plan_id)['state'] == 'completed'
        asyncio.run(run())
    finally:
        db.close()


@pytest.mark.parametrize('phase', ['cloud_recycled', 'strm_missing', 'uncertain', 'ledger_committed', 'keeper_changed'])
def test_inventory_resume_reconciles_cloud_receipt_once(tmp_path, monkeypatch, phase):
    from strimkeep import inventory_executor
    from strimkeep.inventory_operations_api import PlanRequest
    from strimkeep.inventory_plans import _load, pending_action, queue_plan, recover_dispatched_plans
    from strimkeep.store import Store
    from test_deep_delete import add_managed_output, FakeEngine, GuardedCloud, make_store, MANAGED_CONTENT, MANAGED_REL

    async def run():
        store = make_store(tmp_path)
        try:
            relative = MANAGED_REL.removesuffix('.mkv') + '.strm'
            with store.db:
                store.db.execute("UPDATE organized_files SET relative_path=? WHERE file_id='3001'", (relative,))
                store.db.execute("UPDATE playbacks SET relative_path=? WHERE file_id='3001'", (relative,))
                store.db.execute('DELETE FROM share_strms')
            source = add_managed_output(store, relative)
            store.set_many({'organize_config': {'library_dir_name': '影视媒体库'}})

            class InterruptedCloud(GuardedCloud):
                async def recycle(self, fid, parent):
                    await super().recycle(fid, parent)
                    raise asyncio.CancelledError()

            app = FastAPI()
            engine = FakeEngine(store, tmp_path / 'strm')
            engine.publication_gate = asyncio.Lock()
            engine.index = SimpleNamespace(account=lambda _: '1')
            engine.cloud = (GuardedCloud(source) if phase == 'ledger_committed'
                            else InterruptedCloud(source, keep=phase == 'uncertain'))
            app.state.organizer, app.state.strms = engine, engine.root
            install_inventory_operations(app, store, lambda: None)
            file = engine.root / relative
            file.parent.mkdir(parents=True)
            file.write_bytes(MANAGED_CONTENT)
            file.with_suffix('.nfo').write_text('sidecar', encoding='utf-8')
            keeper = engine.root / relative.replace('影视媒体库/', '分享影视库/', 1)
            if phase == 'keeper_changed':
                keeper.parent.mkdir(parents=True)
                keeper.write_bytes(b'keeper')
            plan = await app.state.inventory_build_delete_plan(PlanRequest(
                mode='manual', selections=[{'kind': 'tv', 'tmdb_id': 10, 'side': 'cloud'}]))
            pid = plan['plan_id']
            queue_plan(store.db, pid)
            def interrupt_after_ledger(*args, **kwargs):
                raise asyncio.CancelledError()

            with monkeypatch.context() as patch:
                if phase == 'ledger_committed':
                    patch.setattr(inventory_executor, 'record_progress', interrupt_after_ledger)
                with pytest.raises(asyncio.CancelledError):
                    await app.state.inventory_run_delete(pid)
            assert _load(store.db, pid)['state'] == 'executing'
            assert pending_action(store.db, pid) == (plan['actions'][0], phase == 'ledger_committed')
            assert file.is_file() == (phase != 'ledger_committed')
            if phase in ('strm_missing', 'keeper_changed'):
                file.unlink()
            if phase == 'keeper_changed':
                keeper.write_bytes(b'changed')
            store.close()
            store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
            engine.store = store
            engine.publication_gate = asyncio.Lock()
            app = FastAPI()
            app.state.organizer, app.state.strms = engine, engine.root
            install_inventory_operations(app, store, lambda: None)
            recover_dispatched_plans(store.db)
            await app.state.inventory_run_delete(pid)
            status = _load(store.db, pid)
            assert engine.cloud.recycled == [('3001', '77')]
            if phase == 'keeper_changed':
                assert status['state'] == 'needs_review' and keeper.read_bytes() == b'changed'
                assert store.db.execute("SELECT 1 FROM organized_files WHERE file_id='3001'").fetchone()
                assert file.with_suffix('.nfo').exists()
                return
            if phase == 'uncertain':
                assert status['state'] == 'needs_review' and file.is_file()
                assert pending_action(store.db, pid) == (plan['actions'][0], False)
                engine.cloud.live['3001'] = False
                queue_plan(store.db, pid)
                await app.state.inventory_run_delete(pid)
                status = _load(store.db, pid)
            assert status['state'] == 'completed' and status['progress'] == plan['actions']
            assert engine.cloud.recycled == [('3001', '77')]
            assert not file.exists() and not file.with_suffix('.nfo').exists()
            assert store.db.execute("SELECT 1 FROM organized_files WHERE file_id='3001'").fetchone() is None
        finally:
            store.close()
    asyncio.run(run())


def test_user_plan_and_isolated_share_deletion(tmp_path, monkeypatch):
    app, admin, path, db = setup_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        request = {'mode': 'manual', 'selections': [{'kind':'tv', 'tmdb_id':12, 'side':'share'}]}
        assert client.post('/api/v1/inventory/delete/plan', json=request).status_code == 401
        app.dependency_overrides[admin] = lambda: None
        result = client.post('/api/v1/inventory/delete/plan', json=request)
        assert result.status_code == 200, result.text
        plan = result.json()
        assert len(plan['actions']) == 1 and path.exists()
        assert client.post(f"/api/v1/inventory/delete/{plan['plan_id']}/execute").status_code == 200
        status = wait(client, plan['plan_id'])
        assert status['state'] == 'completed', status
        assert len(status['progress']) == 1 and not path.exists()
        assert not path.parents[1].exists()
        assert not list((tmp_path / 'state' / 'cleanup' / 'orphans').glob('*.tar'))
        history = client.get('/api/v1/inventory-plans').json()['items']
        assert history[0]['state'] == 'completed' and history[0]['done'] == 1
        assert 'snapshot' not in history[0] and 'claim_hash' not in history[0]
        assert db.execute('SELECT count(*) FROM share_strms').fetchone()[0] == 0
        assert client.post(f"/api/v1/inventory/delete/{plan['plan_id']}/execute").status_code == 409
    db.close()


def test_changed_source_after_plan_never_deletes(tmp_path, monkeypatch):
    app, admin, path, db = setup_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        app.dependency_overrides[admin] = lambda: None
        result = client.post('/api/v1/inventory/delete/plan', json={'mode':'manual', 'selections':[{'kind':'tv', 'tmdb_id':12, 'side':'share'}]})
        assert result.status_code == 200, result.text
        plan_id = result.json()['plan_id']
        db.execute("UPDATE share_strms SET file_id='different'")
        db.commit()
        client.post(f'/api/v1/inventory/delete/{plan_id}/execute')
        status = wait(client, plan_id)
        assert status['state'] == 'failed' and status['error'] and path.exists()
    db.close()


def test_running_organizer_blocks_plan_creation(tmp_path, monkeypatch):
    app, admin, path, db = setup_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        app.dependency_overrides[admin] = lambda: None
        db.execute("INSERT INTO organize_jobs VALUES('running')")
        db.commit()
        response = client.post('/api/v1/inventory/delete/plan', json={'mode':'manual', 'selections':[{'kind':'tv', 'tmdb_id':12, 'side':'share'}]})
        assert response.status_code == 409 and path.exists()
    db.close()


def test_governance_selection_executes_real_isolated_share_action(tmp_path, monkeypatch):
    import strimkeep.inventory_operations_api as api
    from strimkeep.inventory_api import install_inventory
    app, admin, path, db = setup_app(tmp_path, monkeypatch)
    def preview(projection, *args, **kwargs):
        return {'scan_complete': True, 'single': [], 'dual': [
            {'kind': 'tv', 'tmdb_id': 12, 'season': 1, 'winner': 'cloud',
             'action_kind': 'shr', 'action_reason': 'local_wins'}]}
    monkeypatch.setattr(api, 'governance_preview', preview)
    install_inventory(app, app.state.organizer.store, admin)
    with TestClient(app) as client:
        app.dependency_overrides[admin] = lambda: None
        scan = client.post('/api/v1/inventory/preview', json={'tie': 'cloud'})
        assert scan.status_code == 200, scan.text
        rev = scan.json()['scan_revision']
        request = {'mode': 'governance', 'scan_revision': rev, 'selections': [{'kind': 'tv', 'tmdb_id': 12,
                   'season': 1, 'winner': 'cloud', 'action_kind': 'shr', 'action_reason': 'local_wins'}]}
        stale = {**request, 'scan_revision': '0' * 64}
        assert client.post('/api/v1/inventory/delete/plan', json=stale).status_code == 409
        result = client.post('/api/v1/inventory/delete/plan', json=request)
        assert result.status_code == 200, result.text
        plan = result.json()
        assert [action['side'] for action in plan['actions']] == ['share']
        assert client.post(f"/api/v1/inventory/delete/{plan['plan_id']}/execute").status_code == 200
        status = wait(client, plan['plan_id'])
        assert status['state'] == 'completed' and len(status['progress']) == 1 and not path.exists()
    db.close()


def test_governance_rejects_inventory_changed_after_scan(tmp_path, monkeypatch):
    from strimkeep.inventory import project, registered_rows
    from strimkeep.inventory_api import inventory_config_value
    from strimkeep.inventory_revision import governance_revision
    from strimkeep.inventory_sources import source_fingerprints
    from strimkeep.quality import public_policy
    app, admin, path, db = setup_app(tmp_path, monkeypatch)
    store = app.state.organizer.store
    revision = governance_revision(project(registered_rows(store), tmp_path), public_policy(), inventory_config_value({}), source_fingerprints(db, registered_rows(store), 'account'))
    added = path.with_name('Show.S01E02.1080p.strm')
    added.write_text('http://example/play115/share/s2/a', encoding='utf-8')
    relative = added.relative_to(tmp_path).as_posix()
    db.execute('INSERT INTO share_strms VALUES (?,?,?,?,?,?,?,?)', ('s2', 'code', '', 'f2', '', relative, 'hash', 0))
    db.commit()
    with TestClient(app) as client:
        app.dependency_overrides[admin] = lambda: None
        response = client.post('/api/v1/inventory/delete/plan', json={'mode': 'governance', 'scan_revision': revision,
            'selections': [{'kind': 'tv', 'tmdb_id': 12, 'season': 1, 'winner': 'cloud', 'action_kind': 'shr'}]})
        assert response.status_code == 409
        assert '重新扫描' in response.text and path.exists() and added.exists()
    db.close()


def test_scan_without_cloud_account_still_lists_and_plans_share_side(tmp_path, monkeypatch):
    from strimkeep.inventory_api import install_inventory
    app, admin, _, db = setup_app(tmp_path, monkeypatch)
    app.state.organizer.index.account = lambda _: (_ for _ in ()).throw(ValueError('no account'))
    install_inventory(app, app.state.organizer.store, admin)
    with TestClient(app) as client:
        app.dependency_overrides[admin] = lambda: None
        response = client.post('/api/v1/inventory/preview', json={'tie': 'cloud'})
        payload = response.json()
        assert response.status_code == 200
        # 没有网盘账号不再让整页治理不可用：库存照常展示，只是网盘侧不能删。
        assert payload['scan_complete'] is True
        assert not any(row.get('reason') == 'source_unverified' for row in payload['blocked'])
        assert 'scan_revision' in payload
    db.close()


def test_unregistered_share_strm_can_be_cleaned_without_ledger(tmp_path, monkeypatch):
    """台账里没有这一行，也照样能清分享侧的本地 STRM（目录就是事实）。"""
    app, admin, path, db = setup_app(tmp_path, monkeypatch)
    db.execute('DELETE FROM share_strms')
    db.commit()
    with TestClient(app) as client:
        app.dependency_overrides[admin] = lambda: None
        plan = client.post('/api/v1/inventory/delete/plan',
                           json={'mode': 'manual',
                                 'selections': [{'kind': 'tv', 'tmdb_id': 12, 'side': 'share'}]})
        assert plan.status_code == 200, plan.text
        assert plan.json()['actions'][0]['id'].startswith('fs-')
        assert client.post("/api/v1/inventory/delete/%s/execute" % plan.json()['plan_id']).status_code == 200
        result = wait(client, plan.json()['plan_id'])
        assert result['state'] == 'completed', result
    assert not path.exists()
    assert db.execute('SELECT COUNT(*) FROM share_strms').fetchone()[0] == 0
    db.close()


def test_governance_revision_detects_replaced_strm(tmp_path, monkeypatch):
    from strimkeep.inventory import project, registered_rows
    from strimkeep.inventory_api import inventory_config_value
    from strimkeep.inventory_revision import governance_revision
    from strimkeep.inventory_sources import source_fingerprints
    from strimkeep.quality import public_policy
    app, _, path, db = setup_app(tmp_path, monkeypatch)
    store = app.state.organizer.store
    def revision():
        return governance_revision(project(registered_rows(store), tmp_path), public_policy(), inventory_config_value({}), source_fingerprints(db, registered_rows(store), 'account'))
    old = revision()
    path.write_text('http://example/replaced-stream', encoding='utf-8')
    assert revision() != old
    old = revision()
    db.execute("UPDATE share_strms SET file_id='different-source'")
    db.commit()
    assert revision() != old
    db.close()


def test_manual_scan_creates_plan_and_latest_read_does_not_scan(tmp_path, monkeypatch):
    import strimkeep.inventory_api as scan_api
    import strimkeep.inventory_operations_api as operations
    app, admin, path, db = setup_app(tmp_path, monkeypatch)
    store = app.state.organizer.store
    def meta_json(key, default=None):
        row = db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default
    store.meta_json = meta_json
    calls = []
    def preview(projection, *args, **kwargs):
        calls.append(1)
        return {'scan_complete': True, 'issues': [], 'blocked': [], 'single': [], 'dual': [
            {'kind': 'tv', 'tmdb_id': 12, 'season': 1, 'winner': 'cloud',
             'action_kind': 'shr', 'action_reason': 'local_wins'}]}
    monkeypatch.setattr(scan_api, 'governance_preview', preview)
    monkeypatch.setattr(operations, 'governance_preview', preview)
    scan_api.install_inventory(app, store, admin)
    with TestClient(app) as client:
        app.dependency_overrides[admin] = lambda: None
        assert client.get('/api/v1/inventory/last-scan').json()['result'] is None
        assert not calls
        scanned = client.post('/api/v1/inventory/preview', json={'tie':'cloud'}).json()
        plan = scanned['plan']
        assert plan['expires_in'] == 7200 and len(plan['actions']) == 1
        count = len(calls)
        assert client.get('/api/v1/inventory/last-scan').json()['result']['plan']['plan_id'] == plan['plan_id']
        assert len(calls) == count
        assert client.post(f"/api/v1/inventory/delete/{plan['plan_id']}/execute").status_code == 200
        assert wait(client, plan['plan_id'])['state'] == 'completed'
        assert client.get('/api/v1/inventory/last-scan').json()['result'] is None
        assert len(calls) == count and not path.exists()
    db.close()


def test_scheduled_scan_creates_plan_once_per_interval(tmp_path, monkeypatch):
    import asyncio
    import strimkeep.inventory_api as scan_api
    import strimkeep.inventory_operations_api as operations
    app, admin, _, db = setup_app(tmp_path, monkeypatch)
    store = app.state.organizer.store
    original_get = store.get
    store.get = lambda key, default=None: ({'scan_schedule_enabled':True, 'scan_interval_hours':6}
                                           if key == 'inventory_config' else original_get(key, default))
    def meta_json(key, default=None):
        row = db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default
    def set_meta(key, value):
        with db: db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', (key,json.dumps(value)))
    store.meta_json = meta_json; store.set_meta = set_meta; store.event = lambda *args: None
    calls = []
    def preview(projection, *args, **kwargs):
        calls.append(1)
        return {'scan_complete': True, 'issues': [], 'blocked': [], 'single': [], 'dual': [
            {'kind':'tv', 'tmdb_id':12, 'season':1, 'winner':'cloud', 'action_kind':'shr', 'action_reason':'local_wins'}]}
    monkeypatch.setattr(scan_api, 'governance_preview', preview)
    monkeypatch.setattr(operations, 'governance_preview', preview)
    scan_api.install_inventory(app, store, admin)
    async def stop(_): raise asyncio.CancelledError()
    async def run_once():
        monkeypatch.setattr(scan_api.asyncio, 'sleep', stop)
        try: await app.state.inventory_scan_loop()
        except asyncio.CancelledError: pass
    asyncio.run(run_once())
    saved = meta_json('inventory-last-scan')
    assert saved['result']['plan']['expires_in'] == 7200
    first_calls = len(calls)
    assert first_calls > 0 and meta_json('inventory-last-scheduled-run') > 0
    asyncio.run(run_once())
    assert len(calls) == first_calls
    db.close()


def test_scan_plan_keeps_both_explicit_special_cleanup_actions(tmp_path, monkeypatch):
    import strimkeep.inventory_api as scan_api
    app, admin, _, db = setup_app(tmp_path, monkeypatch)
    common = {'kind':'tv', 'tmdb_id':12, 'season':0}
    local = {**common, 'winner':'share', 'action_kind':'loc', 'action_reason':'special_delete_local'}
    share = {**common, 'winner':'cloud', 'action_kind':'shr', 'action_reason':'special_delete_share'}
    monkeypatch.setattr(scan_api, 'governance_preview', lambda *args, **kwargs: {
        'scan_complete':True, 'issues':[], 'blocked':[], 'single':[], 'dual':[local, share]})
    captured = []
    async def build(request, ttl, reuse=None):
        captured.extend(request.selections)
        return {'plan_id':'x','actions':[], 'expires_in':ttl}
    app.state.inventory_build_delete_plan = build
    scan_api.install_inventory(app, app.state.organizer.store, admin)
    with TestClient(app) as client:
        app.dependency_overrides[admin] = lambda: None
        response = client.post('/api/v1/inventory/preview', json={'tie':'cloud'})
        assert response.status_code == 200
        assert response.json()['plan_skipped_conflicts'] == 0
        assert {v.action_reason for v in captured} == {'special_delete_local','special_delete_share'}
    db.close()
