"""Cancellation exits both scan workers and preserves the last published result."""
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import pytest

os.environ.setdefault('WEB_PASSWORD', 'test-pass-123')
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app import engine, governance, lib, storage, tasks


@pytest.fixture
def media(tmp_path, monkeypatch):
    for key, sub in [('L_ROOT', 'local'), ('S_ROOT', 'share'), ('CLOUD_L_ROOT', 'cloud'),
                     ('DATA_DIR', 'data'), ('STATE_DIR', 'data/state')]:
        path = tmp_path / sub; path.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(engine, key, path)
    monkeypatch.setattr(engine, 'GOV_LATEST_FILE', engine.STATE_DIR / 'gov_latest.json')
    monkeypatch.setattr(engine, '_strm_count_cache', {})
    monkeypatch.setenv('INGEST_QUIET_MINUTES', '0')
    monkeypatch.setattr(engine, '_strategy', lambda: {
        'decision': 'quality_first', 'match_strategy': 'title_year',
        'multi_season_protect': 'compare', 'tie_keep_local': False,
        'exempt_keywords': [], 'special_action': 'compare'})
    for name in ('notify_telegram', '_save_strm_count_disk'):
        monkeypatch.setattr(engine, name, lambda *a, **kw: None)
    monkeypatch.setattr(governance, 'write_audit_log', lambda *a, **kw: None)
    for root in (engine.L_ROOT, engine.S_ROOT):
        root.joinpath('样本.720p.strm').write_text('http://test.invalid/fake')
    engine.S_ROOT.joinpath('样本.1080p.strm').write_text('http://test.invalid/fake')
    engine._invalidate_lib_cache()
    yield
    engine._invalidate_lib_cache()


def check():
    return engine.action_inter_check(SimpleNamespace(silent=True))


def published():
    from app import state_store as state
    import json
    files = {p['id']: engine.load_plan(p['id']) for p in engine.db_list_plans(100)}
    files['latest'] = state.read(engine.GOV_LATEST_FILE)
    files['scan_status'] = state.read(engine.STATE_DIR / 'gov_scan_status.json')
    return files, dict(engine._strm_count_cache)


def index():
    return storage._execute('SELECT * FROM media_index ORDER BY root,path').fetchall()


def test_mid_walk_stops_both_workers_without_partial_index_or_publication(media):
    assert check()['plan_id']
    before, rows = published(), index()
    for root in (engine.L_ROOT, engine.S_ROOT): root.joinpath('新增.2160p.strm').write_text('fake')
    reached = threading.Barrier(3); release = threading.Event(); exited = []
    def walk(root, onerror=None):
        try:
            yield str(root), [], sorted(p.name for p in Path(root).glob('*.strm'))
            reached.wait(5); release.wait(5)
            yield str(root), [], []
        finally:
            exited.append(str(root))
    manager = tasks.TaskManager()
    with patch.object(lib.os, 'walk', walk):
        task = manager.spawn('inter_check', check, cancellable=True)
        try:
            reached.wait(5)
            assert manager.cancel(task.id)[1]
            assert manager.cancel(task.id)[1]  # repeated requests are harmless
            assert task.status == 'running' and manager.running() is task
            with pytest.raises(tasks.TaskBusy): manager.spawn('inter_check', check)
        finally:
            release.set()
        assert task.done.wait(5)
    assert task.status == 'cancelled' and task.error is None
    assert set(exited) == {str(engine.L_ROOT), str(engine.S_ROOT)}
    assert published() == before and index() == rows
    assert lib._lib_building == {} and lib._lib_cache == {}
    assert manager.running() is None
    retry = manager.spawn('inter_check', check, cancellable=True)
    assert retry.done.wait(5) and retry.status == 'success'


def test_cancel_during_plan_fingerprints_publishes_no_new_plan(media):
    check(); before = published()
    entered, release = threading.Event(), threading.Event()
    real = governance._plan_guard.fingerprint
    def fingerprint(*args):
        entered.set(); release.wait(5)
        return real(*args)
    manager = tasks.TaskManager()
    with patch.object(governance._plan_guard, 'fingerprint', fingerprint):
        task = manager.spawn('inter_check', check, cancellable=True)
        try:
            assert entered.wait(5)
            manager.cancel(task.id)
        finally: release.set()
        assert task.done.wait(5)
    assert task.status == 'cancelled' and published() == before


def test_single_flight_waiter_can_cancel_without_stopping_other_owner(media):
    event = threading.Event(); entered = threading.Event(); key = str(engine.L_ROOT)
    with lib._lib_building_lock: lib._lib_building[key] = event
    manager = tasks.TaskManager()
    def waiting():
        entered.set(); return engine._get_lib(engine.L_ROOT)
    try:
        task = manager.spawn('inter_check', waiting, cancellable=True)
        assert entered.wait(5); manager.cancel(task.id)
        assert task.done.wait(2) and task.status == 'cancelled'
        assert lib._lib_building[key] is event
    finally:
        with lib._lib_building_lock: lib._lib_building.pop(key, None)
        event.set()


def test_commit_wins_race_and_finished_scan_is_not_reported_cancelled():
    manager = tasks.TaskManager(); ready, release = threading.Event(), threading.Event()
    def job():
        tasks.begin_publication(); ready.set(); release.wait(5)
        return {'status': 'success'}
    task = manager.spawn('inter_check', job, cancellable=True)
    try:
        assert ready.wait(5)
        with pytest.raises(ValueError, match='保存结果'): manager.cancel(task.id)
    finally: release.set()
    assert task.done.wait(5) and task.status == 'success'
    assert manager.cancel(task.id) == (task, False)


def test_cancel_wins_race_before_commit():
    manager = tasks.TaskManager(); ready, release = threading.Event(), threading.Event(); writes = []
    def job():
        ready.set(); release.wait(5); tasks.begin_publication(); writes.append('published')
    task = manager.spawn('inter_check', job, cancellable=True)
    try:
        assert ready.wait(5); manager.cancel(task.id)
    finally: release.set()
    assert task.done.wait(5) and task.status == 'cancelled' and not writes


def test_cancel_api_is_authenticated_and_never_cancels_cleanup(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.routers import system, governance as route
    manager = tasks.TaskManager(); monkeypatch.setattr(tasks, 'manager', manager)
    app = FastAPI(); app.include_router(system.router); app.include_router(route.router)
    client = TestClient(app)
    assert client.post('/api/task/anything/cancel').status_code == 401
    app.dependency_overrides[system.auth] = lambda: True
    assert client.post('/api/task/anything/cancel').status_code == 404
    release = threading.Event()
    task = manager.spawn('inter_clean', lambda: release.wait(5), cancellable=True)
    try:
        assert client.post(f'/api/task/{task.id}/cancel').status_code == 409
        assert not task.cancel_event.is_set()
    finally: release.set(); assert task.done.wait(5)
    entered = threading.Event()
    def job(args):
        entered.set(); tasks.current_task().cancel_event.wait(5); tasks.checkpoint()
    monkeypatch.setitem(engine.ACTIONS, 'inter_check', job)
    result = client.post('/api/check').json(); assert entered.wait(5)
    tid = result['task_id']; task = manager.get(tid)
    assert task.cancellable
    assert client.post(f'/api/task/{tid}/cancel').json()['cancel_requested']
    assert task.done.wait(5)
    assert client.get(f'/api/task/{tid}').json()['status'] == 'cancelled'


def test_cancelled_context_does_not_leak_into_next_thread_job():
    task = tasks.Task('inter_check', cancellable=True); task.cancel_event.set()
    with pytest.raises(tasks.TaskCancelled): tasks.run_in_task(task, lambda: None)
    assert tasks.current_task() is None
    tasks.checkpoint()
