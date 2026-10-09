"""Isolated storage failures, recovery and transaction boundaries; no media/API calls."""
import errno
import json
import os
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
os.environ.setdefault('WEB_PASSWORD', 'test-pass-123')
# Keep even module-import defaults in temporary state when this suite runs alone.
_TMP = Path(tempfile.mkdtemp(prefix='strimkeep-storage-status-tests-'))
for _key, _leaf in [('AGENT_DATA', 'data'), ('L_ROOT', 'local'), ('S_ROOT', 'share'), ('CLOUD_L_ROOT', 'cloud')]:
    os.environ[_key] = str(_TMP / _leaf)
os.environ['TG_BOT_TOKEN'] = ''
os.environ['TMDB_KEY'] = ''
from app import engine, storage, storage_status, subscribe, governance, logger, wash, state_store


@pytest.fixture
def state(tmp_path, monkeypatch):
    directory = tmp_path / 'state'
    directory.mkdir()
    monkeypatch.setattr(engine, 'STATE_DIR', directory)
    monkeypatch.setattr(engine, 'SUB_STATE_FILE', directory / 'subscriptions_state.json')
    monkeypatch.setattr(engine, 'GOV_LATEST_FILE', directory / 'gov_latest.json')
    monkeypatch.setattr(storage, '_state_dir', lambda: directory)
    monkeypatch.setattr(logger, 'RECORDS_DIR', tmp_path / 'records')
    monkeypatch.setattr(logger, 'RECORDS_FILE', tmp_path / 'records/audit.jsonl')
    yield directory
    con = storage._DBS.pop(str(directory / storage.DB_NAME), None)
    if con is not None:
        con.close()
    with storage_status._LOCK:
        storage_status._STATES.pop(str(directory), None)


def test_failed_mirror_is_visible_redacted_and_same_operation_recovers(state, caplog):
    private = 'secret_token=very-private-secret /private/movie-name'
    with patch.object(storage, '_execute', side_effect=OSError(errno.ENOSPC, private)):
        assert storage.db_doc_set('secret document', {'secret': private}) is False
    health = storage.storage_health()
    assert health['status'] == 'degraded'
    assert health['issues'][0]['code'] == 'full'
    assert private not in json.dumps(health) and private not in caplog.text
    assert storage.db_kv_set('healthy', 'ok')
    assert storage.storage_health()['status'] == 'degraded'
    assert storage.db_doc_set('normal', {})
    assert storage.storage_health()['status'] == 'no_observed_error'


def test_invalid_or_empty_write_does_not_claim_recovery(state):
    with patch.object(storage, '_conn', side_effect=PermissionError('private detail')):
        assert not storage.db_media_index_upsert('root', [('x', 0, 0, {})])
    assert storage.db_media_index_upsert('root', [])
    assert storage.storage_health()['status'] == 'degraded'


def test_failed_subscription_transaction_preserves_old_state_on_later_commit(state):
    assert storage.db_save_sub_state('s', {'old': {'count': 1}})
    con = storage._conn()
    con.execute("CREATE TRIGGER fail_insert BEFORE INSERT ON sub_state WHEN NEW.sid='fail' BEGIN SELECT RAISE(ABORT,'fixture'); END")
    con.commit()
    assert not storage.db_save_sub_state('s', {'first': {'count': 2}, 'fail': {'count': 3}})
    assert not con.in_transaction
    assert storage.db_kv_set('after-failure', 'commits')
    assert storage.db_load_sub_state('s') == {'old': {'count': 1}}
    con.execute('DROP TRIGGER fail_insert')
    con.commit()
    assert storage.db_save_sub_state('s', {'new': {'count': 4}})
    assert storage.db_load_sub_state('s') == {'new': {'count': 4}}


def test_failed_index_batch_is_atomic(state):
    assert storage.db_media_index_upsert('root', [('old', 1, 1, {})])
    con = storage._conn()
    con.execute("CREATE TRIGGER fail_index BEFORE INSERT ON media_index WHEN NEW.path='fail' BEGIN SELECT RAISE(ABORT,'fixture'); END")
    con.commit()
    assert not storage.db_media_index_upsert('root', [('first', 2, 2, {}), ('fail', 3, 3, {})])
    assert storage.db_kv_set('later', 'value')
    assert set(storage.db_media_index_load('root')) == {'old'}


def test_permission_read_is_not_a_subscription_reset(state):
    subscribe._save_sub_state({'a': {'notified_episodes': ['S01E01']}})
    with patch.object(storage, '_conn', side_effect=PermissionError(errno.EACCES, 'private filename')):
        with pytest.raises(RuntimeError, match='原状态保留'):
            subscribe._load_sub_state()
    assert storage.db_load_sub_state(engine.SUB_STATE_FILE.name)['a']['notified_episodes'] == ['S01E01']
    assert any(x['operation'] == 'state_read:subscriptions_state' for x in storage.storage_health()['issues'])
    assert subscribe._load_sub_state()['a']
    assert not any(x['operation'] == 'state_read:subscriptions_state' for x in storage.storage_health()['issues'])


def test_subscription_missing_and_corrupt_keep_distinct_semantics(state):
    subscribe._save_sub_state({'a': {'notified_episodes': ['S01E01']}})
    engine.SUB_STATE_FILE.write_text('{broken')
    assert subscribe._load_sub_state()['a']
    assert engine.SUB_STATE_FILE.read_text() == '{broken'
    engine.SUB_STATE_FILE.unlink()
    assert subscribe._load_sub_state()['a']
    state_store.remove(engine.SUB_STATE_FILE)
    assert subscribe._load_sub_state() == {}


def test_corrupt_subscription_without_fallback_stops_instead_of_empty_success(state):
    engine.SUB_STATE_FILE.write_text('{broken')
    with pytest.raises(RuntimeError, match='原状态保留'):
        subscribe._load_sub_state()
    assert storage.storage_health()['status'] == 'degraded'


def test_atomic_file_write_failure_preserves_original_and_no_temp_or_backup(state):
    path = state / 'ordinary.json'
    state_store.save(path, {'original': True})
    con = storage._conn()
    con.execute("CREATE TRIGGER fail_doc BEFORE INSERT ON docs WHEN NEW.name='ordinary' BEGIN SELECT RAISE(ABORT,'fixture'); END")
    con.commit()
    with pytest.raises(OSError):
        storage_status.write_json(path, {'next': True}, 'state_write', state)
    assert state_store.read(path) == {'original': True}
    assert not list(state.glob('*.json')) and not list(state.glob('*.tmp'))
    con.execute('DROP TRIGGER fail_doc'); con.commit()
    state_store.save(path, {'next': True})
    assert state_store.read(path) == {'next': True}


def test_audit_export_failure_still_writes_database(state):
    with patch.object(Path, 'open', side_effect=AssertionError('no JSONL exports')):
        assert logger.write('目录清理', '记录保存', ['line']) is True
    assert storage.db_recent_audit(1)[0]['title'] == '记录保存'
    assert not logger.RECORDS_FILE.exists()


def test_snapshot_has_no_database_or_filesystem_probe(state):
    with patch.object(storage, '_conn', side_effect=AssertionError('no db probe')), \
         patch.object(Path, 'stat', side_effect=AssertionError('no filesystem probe')):
        assert storage.storage_health()['scope'] == 'current_process'


def test_dedup_expiration_applies_even_before_physical_purge(state):
    storage._execute('INSERT INTO dedup VALUES (?,?)', ('old', 1))
    assert not storage.db_dedup_seen('old')
    assert storage.db_dedup_add('new')
    assert storage.db_dedup_seen('new')
    assert storage._execute('SELECT 1 FROM dedup WHERE k=?', ('old',)).fetchone() is None


def test_plan_retention_uses_same_creation_time_in_both_stores(state):
    import time
    old = {'schema_version': 2, 'id': 'abcd', 'ts': time.time()-9*86400, 'state': 'done'}
    pending = {'schema_version': 2, 'id': 'bbcc', 'ts': time.time()-9*86400, 'state': 'pending'}
    for data in (old, pending):
        state_store.save(state / ('plan_'+data['id']+'.json'), data)
    wash.purge_old()
    assert storage.db_load_plan('abcd') and storage.db_load_plan('bbcc') is None
    assert not list(state.glob('plan_*.json'))


def test_failed_retention_preserves_unreadable_pair_but_removes_other_expired_pairs(state):
    import time
    for pid in ('abcd', 'bbcc'):
        state_store.save(state/('plan_'+pid+'.json'), {'schema_version':2,'id':pid,'ts':time.time()-9*86400,'state':'pending'})
    con = storage._conn()
    con.execute("CREATE TRIGGER fail_purge BEFORE DELETE ON plans WHEN OLD.id='bbcc' BEGIN SELECT RAISE(ABORT,'fixture'); END")
    con.commit()
    wash.purge_old()
    assert storage.db_load_plan('abcd') and storage.db_load_plan('bbcc')
    assert any(x['operation']=='db_purge_plans' for x in storage.storage_health()['issues'])
    con.execute('DROP TRIGGER fail_purge'); con.commit()
    wash.purge_old()
    assert storage.db_load_plan('abcd') is None and storage.db_load_plan('bbcc') is None


def test_retention_skips_malformed_files_and_symlinks(state):
    malformed = state/'plan_abcd.json'; malformed.write_text('{broken')
    outside = state.parent/'outside.json'; outside.write_text('{"private":true}')
    (state/'plan_bbcc.json').symlink_to(outside)
    wash.purge_old()
    assert malformed.read_text() == '{broken' and outside.read_text() == '{"private":true}'
    assert (state/'plan_bbcc.json').is_symlink()


def test_log_coalescing_and_issue_budget(state, caplog, monkeypatch):
    monkeypatch.setattr(storage_status.time, 'time', lambda: 10000)
    for i in range(50):
        storage_status.record_failure(state, 'same', OSError(errno.ENOSPC, 'private'))
    assert caplog.text.count('存储异常 [same]') == 1
    assert storage_status.snapshot(state)['issues'][0]['count'] == 50
    for i in range(storage_status.MAX_ISSUES + 10):
        storage_status.record_failure(state, 'op'+str(i), ValueError('private'))
    assert len(storage_status.snapshot(state)['issues']) == storage_status.MAX_ISSUES
    assert '_logged_at' not in json.dumps(storage_status.snapshot(state))


def test_status_endpoint_requires_auth_and_contains_only_classified_data(state):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.routers.deps import auth
    client = TestClient(app)
    assert client.get('/api/storage/status').status_code == 401
    app.dependency_overrides[auth] = lambda: True
    try:
        with patch.object(storage, '_execute', side_effect=sqlite3.OperationalError('sensitive SQL')):
            assert not storage.db_doc_set('secret', {})
        response = client.get('/api/storage/status')
        assert response.status_code == 200
        assert response.json()['storage']['status'] == 'degraded'
        assert 'sensitive SQL' not in response.text
    finally:
        app.dependency_overrides.pop(auth, None)
