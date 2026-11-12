"""Upgrade regression checks for retired features and shared media state."""
import asyncio
import json
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

from strimkeep import share_service
from strimkeep.main import create_app
from strimkeep.organizer import Organizer
from strimkeep.store import Store
from test_app import FakeCloud

TABLES = ('mapping_entries', 'mapping_works', 'mapping_snapshots', 'governance_plans')
SETTINGS = ('mapping_config', 'governance_config', 'governance_policy')


def seed(store):
    share_service.install(store)
    store.set_many({**{key: {'enabled': True} for key in SETTINGS},
                    'cookie': 'UID=123; CID=keep', 'console_config': {'emby_strm_path': '/media'},
                    'upgrade_policy': {'mode': 'upgrade'}})
    with store.db:
        for table in TABLES:
            store.db.execute(f'CREATE TABLE {table}(id INTEGER PRIMARY KEY)')
            store.db.execute(f'INSERT INTO {table} VALUES(1)')
        for key in ('mapping_revision', 'governance_last_scan'):
            store.db.execute('INSERT INTO meta VALUES(?,?)', (key, '123'))
        for kind in ('mapping', 'mapping_rebuild', 'governance', 'governance_execute', 'organize'):
            store.db.execute('INSERT INTO task_schedules(kind,next_due) VALUES(?,123)', (kind,))
            for state in ('queued', 'running', 'paused', 'waiting_login', 'completed'):
                jid = store.db.execute('INSERT INTO organize_jobs(state,config,created,updated) VALUES(?,?,1,1)',
                                       (state, json.dumps({'job_kind': kind}))).lastrowid
                store.db.execute("INSERT INTO organize_items(job_id,file_id,original) VALUES(?,'f','{}')", (jid,))
                store.db.execute("INSERT INTO notifications(at,level,job_id,event_key,title,message) VALUES(1,'info',?,?,'t','m')", (jid, f'job:{jid}'))
                store.db.execute("INSERT INTO emby_deliveries(job_id,state,payload,next_due,deadline) VALUES(?,'pending','{}',1,2)", (jid,))
        store.db.execute("INSERT INTO playbacks VALUES('p','f','pick','movie','movie.strm','hash',0,1)")
        store.db.execute("INSERT INTO share_strms(id,share_code,file_id,name,strm_path,token_hash,revoked,created) VALUES('s','share','sf','movie','share.strm','hash',1,1)")
        store.db.execute("INSERT INTO share_revocations VALUES('share','sf','governance-plan:1',1)")
        store.db.execute("INSERT INTO output_registry VALUES('123','f','{}','[]','p','cipher','movie.strm','[]',1,1)")
        store.db.execute("INSERT INTO owned_outputs VALUES('movie.strm','123','f','hash')")
        store.db.execute("INSERT INTO organized_files VALUES('f',11,11,'p','movie.strm','{}')")


def test_upgrade_cleanup_is_idempotent_and_preserves_media_state(tmp_path):
    store = Store(tmp_path, 'admin', 'test-password-1234')
    seed(store)
    keep_tables = ('playbacks', 'share_strms', 'share_revocations', 'output_registry', 'owned_outputs', 'organized_files')
    snapshots = {t: [tuple(r) for r in store.db.execute(f'SELECT * FROM {t}')] for t in keep_tables}
    strm = tmp_path / 'existing.strm'
    strm.write_text('http://example/play/p?token=preserved')
    for _ in range(2):
        store.remove_retired_modules()
        names = {r[0] for r in store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert not set(TABLES) & names
        assert all(store.get(key) == '' for key in SETTINGS)
        assert store.meta('mapping_revision') == store.meta('governance_last_scan') == ''
        assert store.db.execute('SELECT count(*) FROM organize_jobs').fetchone()[0] == 5
        assert all(json.loads(r[0])['job_kind'] == 'organize' for r in store.db.execute('SELECT config FROM organize_jobs'))
        for t in ('organize_items', 'notifications', 'emby_deliveries'):
            assert store.db.execute(f'SELECT count(*) FROM {t}').fetchone()[0] == 5
        assert [r[0] for r in store.db.execute('SELECT kind FROM task_schedules')] == ['organize']
        for table, rows in snapshots.items():
            assert [tuple(r) for r in store.db.execute(f'SELECT * FROM {table}')] == rows
        assert store.get('cookie') == 'UID=123; CID=keep'
        assert store.get('console_config')['emby_strm_path'] == '/media'
        assert store.get('upgrade_policy')['mode'] == 'upgrade'
        assert store.authenticate('admin', 'test-password-1234')
        assert strm.read_text() == 'http://example/play/p?token=preserved'
    store.close()


def test_cleanup_rolls_back_tables_and_jobs_on_error(tmp_path):
    store = Store(tmp_path, 'admin', 'test-password-1234')
    seed(store)
    store.db.execute("CREATE TRIGGER prevent_cleanup BEFORE DELETE ON settings BEGIN SELECT RAISE(ABORT,'test failure'); END")
    store.db.commit()
    with pytest.raises(sqlite3.IntegrityError):
        store.remove_retired_modules()
    assert store.db.execute('SELECT count(*) FROM organize_jobs').fetchone()[0] == 25
    for table in TABLES:
        assert store.db.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 1
    store.close()


def test_pending_module_output_repairs_stop_but_keep_finished_evidence(tmp_path):
    store = Store(tmp_path, 'admin', 'test-password-1234')
    with store.db:
        for origin in ('mapping-repair:1', 'governance-restore:1'):
            for state in ('running', 'completed'):
                store.db.execute('INSERT INTO organize_jobs(state,config,created,updated) VALUES(?,?,1,1)',
                                 (state, json.dumps({'job_kind': 'strm_sync', 'origin': origin})))
    store.remove_retired_modules()
    rows = store.db.execute('SELECT state,config FROM organize_jobs ORDER BY id').fetchall()
    assert [r['state'] for r in rows] == ['cancelled', 'completed', 'cancelled', 'completed']
    assert all(json.loads(r['config'])['origin'] == 'retired_module' for r in rows)
    store.close()


@pytest.mark.parametrize('kind', ['mapping', 'mapping_rebuild', 'governance', 'governance_execute', 'future_unknown'])
def test_retired_or_unknown_jobs_never_call_cloud(tmp_path, kind):
    async def run():
        store = Store(tmp_path, 'admin', 'test-password-1234')
        with store.db:
            jid = store.db.execute("INSERT INTO organize_jobs(state,config,created,updated) VALUES('queued',?,1,1)",
                                   (json.dumps({'job_kind': kind}),)).lastrowid
        async with httpx.AsyncClient() as http:
            # No cloud methods exist: any accidental cloud access fails this test.
            engine = Organizer(store, object(), http, tmp_path / 'strm')
            await engine.run_job(jid)
            assert engine.job(jid)['state'] == 'cancelled'
            assert engine.job(jid)['notify_state'] == 'not_needed'
        store.close()
    asyncio.run(run())


def test_app_upgrade_removes_routes_before_workers_start(tmp_path, monkeypatch):
    monkeypatch.setenv('ADMIN_USERNAME', 'admin')
    monkeypatch.setenv('ADMIN_PASSWORD', 'test-password-1234')
    store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
    seed(store)
    store.close()
    def check_start(self):
        assert not self.store.db.execute("SELECT 1 FROM sqlite_master WHERE name='governance_plans'").fetchone()
        assert self.store.db.execute('SELECT count(*) FROM organize_jobs').fetchone()[0] == 5
    monkeypatch.setattr(Organizer, 'start', check_start)
    app = create_app(tmp_path / 'data', tmp_path / 'strm', FakeCloud)
    assert not any(r.path.startswith(('/api/v1/mapping', '/api/v1/governance')) for r in app.routes)
    with TestClient(app) as client:
        login = client.post('/api/v1/auth/login', json={'username': 'admin', 'password': 'test-password-1234'})
        assert login.status_code == 200
        for path in ('mapping/summary', 'mapping/paths', 'governance/policy', 'governance/plans'):
            assert client.get('/api/v1/' + path).status_code == 404
        assert client.get('/health/ready').status_code == 200
        assert client.get('/api/v1/settings').status_code == 200
