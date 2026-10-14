"""Fault injection for durable cleanup, statistics and startup readiness.

Temporary SQLite/media only; remote APIs and background services are replaced.
"""
import errno
import json
import subprocess
import sys
import os
from pathlib import Path
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app import engine, governance, state_store, storage, stats, readiness, main, overview
from dev.tests.test_plan_file_guard import setup, duplicate, execute


@pytest.fixture
def media(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch)
    monkeypatch.setattr(engine, '_STRM_COUNT_CACHE_FILE', engine.STATE_DIR / 'strm_count_cache.json')
    monkeypatch.setattr(engine, '_strm_count_cache', {'ts': 0, 'local': 0, 'share': 0})
    monkeypatch.setattr(engine, '_lib_stats_cache', {'ts': 0, 'data': None})
    monkeypatch.setattr(stats, '_STATS_STATUS', {'running': False, 'error': '', 'checked_at': 0, 'success_at': 0})
    return tmp_path


def fail_mutation(*args, **kwargs):
    pytest.fail('must not enter media deletion')


def test_failed_executing_save_preserves_plan_and_both_files(media, monkeypatch):
    low, high = duplicate()
    pid = engine.save_plan(engine.build_plan())
    before = engine.load_plan(pid)
    monkeypatch.setattr(state_store, 'compare_and_save', lambda *a: (_ for _ in ()).throw(OSError(errno.ENOSPC, 'fixture')))
    monkeypatch.setattr(engine, 'safe_delete_files', fail_mutation)
    result = execute(pid)
    assert result['code'] == 'plan_state_unsaved'
    assert engine.load_plan(pid) == before and low.is_file() and high.is_file()


def test_false_executing_save_also_stops_before_media(media, monkeypatch):
    low, high = duplicate(); pid = engine.save_plan(engine.build_plan())
    monkeypatch.setattr(governance, 'save_plan_state', lambda *a, **k: False)
    monkeypatch.setattr(engine, 'safe_delete_files', fail_mutation)
    assert execute(pid)['code'] == 'plan_state_unsaved'
    assert low.exists() and high.exists()


def test_real_sqlite_claim_failure_rolls_back_before_delete(media, monkeypatch):
    low, high = duplicate(); pid = engine.save_plan(engine.build_plan()); before = engine.load_plan(pid)
    con = storage._conn()
    con.execute("CREATE TRIGGER claim_failure BEFORE INSERT ON plans WHEN json_extract(NEW.payload, '$.state')='executing' BEGIN SELECT RAISE(ABORT,'fixture full'); END")
    con.commit()
    monkeypatch.setattr(engine, 'safe_delete_files', fail_mutation)
    assert execute(pid)['code'] == 'plan_state_unsaved'
    assert engine.load_plan(pid) == before and low.exists() and high.exists() and not con.in_transaction


@pytest.mark.parametrize('state', ['executing', 'interrupted'])
@pytest.mark.parametrize('dry', [False, True])
def test_interrupted_plan_cannot_be_replayed_or_dry_run(media, monkeypatch, state, dry):
    low, high = duplicate(); pid = engine.save_plan(engine.build_plan())
    engine.save_plan_state(pid, state)
    before = engine.load_plan(pid)
    monkeypatch.setattr(engine, 'safe_delete_files', fail_mutation)
    assert execute(pid, dry)['code'] == 'plan_interrupted'
    assert engine.load_plan(pid) == before and low.exists() and high.exists()


def test_claim_compares_whole_confirmed_snapshot(media, monkeypatch):
    duplicate(); pid = engine.save_plan(engine.build_plan()); expected = engine.load_plan(pid)
    changed = dict(expected, rule_sig='different-rule')
    state_store.save(engine.STATE_DIR / ('plan_' + pid + '.json'), changed)
    assert not governance.save_plan_state(pid, 'executing', expected=expected)
    assert engine.load_plan(pid) == changed


def test_two_threads_cannot_claim_one_plan(media):
    duplicate(); pid = engine.save_plan(engine.build_plan()); expected = engine.load_plan(pid)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: governance.save_plan_state(pid, 'executing', expected=expected), range(2)))
    assert sorted(outcomes) == [False, True]
    assert engine.load_plan(pid)['state'] == 'executing'


def test_two_cli_processes_cannot_claim_one_plan(media):
    duplicate(); pid = engine.save_plan(engine.build_plan()); expected = engine.load_plan(pid)
    path = engine.STATE_DIR / ('plan_' + pid + '.json')
    code = "from app import state_store; import sys,json; old=json.loads(sys.argv[2]); print(state_store.compare_and_save(sys.argv[1],old,dict(old,state='executing')))"
    env = dict(os.environ, AGENT_DATA=str(engine.DATA_DIR))
    workers = [subprocess.Popen([sys.executable, '-c', code, str(path), json.dumps(expected)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env) for _ in range(2)]
    answers = []
    for worker in workers:
        out, err = worker.communicate(timeout=20)
        assert worker.returncode == 0, err
        answers.append(out.strip())
    assert sorted(answers) == ['False', 'True']
    assert engine.load_plan(pid)['state'] == 'executing'


def test_legacy_cli_cleanup_creates_durable_plan_before_deleting(media, monkeypatch):
    low, high = duplicate(); original = engine.safe_delete_files; claims = []
    def checked(*args, **kwargs):
        rows = storage._execute('SELECT state FROM plans').fetchall()
        assert rows and any(row[0] == 'executing' for row in rows)
        claims.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(engine, 'safe_delete_files', checked)
    result = engine.action_inter_clean(SimpleNamespace(plan='', dry_run=False, notify=False))
    assert result['status'] == 'success' and claims and not low.exists() and high.exists()
    assert storage._execute('SELECT state FROM plans').fetchall() == [('done',)]


def test_legacy_cli_plan_storage_failure_never_mutates(media, monkeypatch):
    low, high = duplicate()
    monkeypatch.setattr(governance, 'save_plan', lambda *a: (_ for _ in ()).throw(OSError('fixture write failure')))
    monkeypatch.setattr(engine, 'safe_delete_files', fail_mutation)
    with pytest.raises(OSError):
        engine.action_inter_clean(SimpleNamespace(plan='', dry_run=False, notify=False))
    assert low.exists() and high.exists()


def saved_totals():
    return {name: state_store.read(engine.STATE_DIR / name) for name in (
        'library_stats_cache.json', 'strm_directory_counts.json', 'strm_count_cache.json')}


@pytest.mark.parametrize('problem', ['missing', 'file', 'replaced', 'file_symlink', 'directory_symlink', 'permission'])
def test_bad_stat_scan_preserves_all_previous_documents(media, monkeypatch, problem):
    video = engine.L_ROOT / 'sample.strm'; video.write_text('fixture')
    before = stats._recompute_all_stats(); documents = saved_totals()
    if problem in ('missing', 'file', 'replaced'):
        engine.L_ROOT.rename(media / 'offline-root')
        if problem == 'file': engine.L_ROOT.write_text('not a directory')
        elif problem == 'replaced': engine.L_ROOT.mkdir()
    elif problem == 'file_symlink':
        (engine.L_ROOT / 'link.strm').symlink_to(video)
    elif problem == 'directory_symlink':
        (engine.L_ROOT / 'linked').symlink_to(engine.S_ROOT, target_is_directory=True)
    else:
        actual = stats.os.walk
        def denied(root, onerror):
            if root == engine.L_ROOT:
                yield str(root), [], ['sample.strm']
                onerror(PermissionError(errno.EACCES, 'fixture denied', str(root / 'unreadable')))
            else:
                yield from actual(root, onerror=onerror)
        monkeypatch.setattr(stats.os, 'walk', denied)
    with pytest.raises(stats.LibraryScanError): stats._recompute_all_stats()
    assert saved_totals() == documents
    assert engine._strm_count_cache['local'] == before['local_total'] == 1
    assert stats.scan_status()['error'] and not stats.scan_status()['running']


def test_root_swapped_during_walk_never_publishes_partial_result(media, monkeypatch):
    (engine.L_ROOT / 'sample.strm').write_text('fixture'); stats._recompute_all_stats()
    documents = saved_totals(); actual = stats._statistics_files
    def swap(root):
        yield from actual(root)
        if root == engine.S_ROOT:
            root.rename(media / 'old-share'); root.mkdir()
    monkeypatch.setattr(stats, '_statistics_files', swap)
    with pytest.raises(stats.LibraryScanError): stats._recompute_all_stats(allow_root_change=True)
    assert saved_totals() == documents


def test_valid_empty_share_and_intentional_zero_still_work(media):
    video = engine.L_ROOT / 'sample.strm'; video.write_text('fixture')
    assert stats._recompute_all_stats()['share_total'] == 0
    video.unlink()
    data = stats._recompute_all_stats()
    assert data['local_total'] == data['share_total'] == 0
    assert not stats.scan_status()['error']


def test_manual_complete_scan_can_verify_replacement_mount(media):
    (engine.L_ROOT / 'sample.strm').write_text('fixture'); stats._recompute_all_stats()
    engine.L_ROOT.rename(media / 'old-root'); engine.L_ROOT.mkdir()
    (engine.L_ROOT / 'new.strm').write_text('new fixture')
    with pytest.raises(stats.LibraryScanError): stats._recompute_all_stats()
    assert stats._recompute_all_stats(allow_root_change=True)['local_total'] == 1
    assert stats._recompute_all_stats()['local_total'] == 1 and not stats.scan_status()['error']


def test_first_count_does_not_persist_missing_root_as_zero(media):
    engine.S_ROOT.rmdir()
    with pytest.raises(stats.LibraryScanError): stats._get_strm_counts()
    assert state_store.read(engine._STRM_COUNT_CACHE_FILE, None) is None


def test_manual_refresh_failure_keeps_counts_even_after_memory_restart(media, monkeypatch):
    (engine.L_ROOT / 'sample.strm').write_text('fixture'); stats._recompute_all_stats()
    documents = saved_totals()
    monkeypatch.setattr(overview, '_memory', {'path':None, 'stamp':None, 'data':None, 'health_stamp':None})
    monkeypatch.setattr(overview, '_job', {'running':False,'id':0,'started_at':0,'finished_at':0,
                                        'phase':'','error':'','warnings':[],'forced':False})
    class Thread:
        def __init__(self, **kw): pass
        def start(self): pass
    monkeypatch.setattr(overview.threading, 'Thread', Thread)
    overview.request_refresh(force=True)
    engine.L_ROOT.rename(media / 'offline-root')
    overview._refresh_worker(True)
    assert saved_totals() == documents and overview._job['error']
    engine._strm_count_cache.update(ts=0, local=0, share=0)
    engine._lib_stats_cache.update(ts=0, data=None)
    assert stats._get_strm_counts() == (1, 0)


@pytest.fixture
def startup_state(monkeypatch):
    monkeypatch.setattr(readiness, '_STATE', {'phase': 'not_started', 'ready': False, 'message': '', 'ts': 0})
    monkeypatch.setattr(main.engine, 'db_migrate', lambda: {'fixture': True})
    monkeypatch.setattr(main.engine, 'reload_config', lambda: {})
    monkeypatch.setattr(main.bot, 'start', lambda: None)
    monkeypatch.setattr(main.bot, 'stop', lambda: None)
    monkeypatch.setattr(main.scheduler, 'start', lambda: None)
    monkeypatch.setattr(main.scheduler, 'stop', lambda: None)
    monkeypatch.setenv('ENABLE_CD2_WATCHDOG', '0')


def test_failed_startup_keeps_diagnostics_available_and_blocks_business(startup_state, monkeypatch):
    def fail(): raise PermissionError(errno.EACCES, 'secret private fixture')
    monkeypatch.setattr(main.engine, 'db_migrate', fail)
    monkeypatch.setattr(main.scheduler, 'start', fail_mutation)
    monkeypatch.setattr(main.bot, 'start', fail_mutation)
    with TestClient(main.app) as client:
        assert client.get('/api/health').status_code == 200
        probe = client.get('/api/ready')
        assert probe.status_code == 503 and probe.json()['phase'] == 'failed'
        assert 'secret' not in json.dumps(probe.json())
        blocked = client.post('/api/governance/clean', json={})
        assert blocked.status_code == 503
        assert client.get('/api/ingest?force=1').status_code == 503
        login = client.post('/api/login', json={'username':'admin', 'password':'test-pass-123'})
        assert login.status_code == 200
        runtime = client.get('/api/runtime/status').json()
        assert runtime['readiness']['ready'] is False
        assert 'secret private fixture' not in json.dumps(runtime)


def test_successful_restart_and_remote_offline_do_not_fail_readiness(startup_state, monkeypatch):
    readiness.set_state('failed', 'old fixture failure')
    monkeypatch.setattr(main.engine, 'emby_request', lambda *a, **k: (_ for _ in ()).throw(OSError('offline')))
    with TestClient(main.app) as client:
        assert client.get('/api/ready').status_code == 200
        assert readiness.snapshot()['phase'] == 'ready'
    assert readiness.snapshot()['phase'] == 'stopped'


def test_scheduler_start_failure_is_not_ready(startup_state, monkeypatch):
    monkeypatch.setattr(main.scheduler, 'start', lambda: (_ for _ in ()).throw(RuntimeError('fixture')))
    with TestClient(main.app) as client:
        assert client.get('/api/ready').status_code == 503
        assert readiness.snapshot()['ready'] is False
