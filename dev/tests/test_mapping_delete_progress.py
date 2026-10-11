"""Actual delete chain under temporary roots, progress isolation and bounded jobs."""
import threading
import time
import pytest
from fastapi import HTTPException
from app import deletion_progress as jobs, engine, wash, morning
from app.routers import library


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    for attr, name in [('L_ROOT','local'), ('S_ROOT','share'), ('CLOUD_L_ROOT','cloud'),
                       ('DATA_DIR','data'), ('STATE_DIR','data/state')]:
        root = tmp_path / name
        root.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(engine, attr, root)
    monkeypatch.setattr(engine, 'LOCK_FILE', engine.DATA_DIR / 'agent.lock')
    monkeypatch.setattr(engine, 'WASH_RESIDUAL_FILE', engine.STATE_DIR / 'residuals.json')
    monkeypatch.setattr(engine, 'db_doc_set', lambda *a, **k: None)
    monkeypatch.setattr(wash, '_eng', lambda: engine)
    monkeypatch.setattr(library, 'engine', engine)
    monkeypatch.setattr(jobs, '_records', {})
    return tmp_path


def put(root, name):
    path = root / '电影/分类/样本 {tmdb-7}' / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('temporary')
    return path


def wait_done(token):
    end = time.monotonic() + 3
    while time.monotonic() < end:
        rec = jobs.read(token)
        if rec['state'] == 'done':
            return rec
        time.sleep(.005)
    pytest.fail('job did not settle')


def test_live_progress_before_strm_and_after_each_file(isolated, monkeypatch):
    files = [put(engine.L_ROOT, 'a.strm'), put(engine.L_ROOT, 'b.strm')]
    video = put(engine.CLOUD_L_ROOT, 'a.mkv')
    monkeypatch.setattr(engine, 'cloud_videos', lambda f, *a: ([video], 'exact') if f.name == 'a.strm' else ([], 'none'))
    snapshots, cloud_stage = [], []
    def observe(snapshot):
        snapshots.append(dict(snapshot))
        if snapshot['cloud_removed'] == 1 and snapshot['processed'] == 0:
            cloud_stage.append((files[0].exists(), video.exists()))
    result = wash.safe_delete_files(files, engine.L_ROOT, engine.CLOUD_L_ROOT, progress=observe)
    assert result['strm_removed'] == result['cloud_removed'] == 1
    assert cloud_stage and all(state == (True, False) for state in cloud_stage), 'cloud must be deleted before STRM'
    assert not files[0].exists() and files[1].exists()
    assert any(s['processed'] == 1 and s['strm_removed'] == 1 for s in snapshots)
    assert snapshots[-1]['processed'] == 2 and snapshots[-1]['retained'] == 1
    assert snapshots[-1]['error_count'] == 1 and snapshots[-1]['phase'] == 'finalizing'


@pytest.mark.parametrize('confidence', ['none', 'ambiguous', 'unique_fallback'])
def test_protected_files_are_processed_but_not_reported_deleted(isolated, monkeypatch, confidence):
    f = put(engine.L_ROOT, 'a.strm')
    video = put(engine.CLOUD_L_ROOT, 'a.mkv')
    monkeypatch.setattr(engine, 'cloud_videos', lambda *a: ([video], confidence))
    snapshots = []
    result = wash.safe_delete_files([f], engine.L_ROOT, engine.CLOUD_L_ROOT, progress=snapshots.append)
    assert f.exists() and video.exists()
    assert result['strm_removed'] == result['cloud_removed'] == 0
    assert snapshots[-1]['processed'] == snapshots[-1]['retained'] == 1


def test_observer_failure_cannot_interrupt_deletion_and_share_never_matches_cloud(isolated, monkeypatch):
    f = put(engine.S_ROOT, 'a.strm')
    def forbidden(*a):
        pytest.fail('share must not access cloud')
    def broken(*a):
        raise RuntimeError('observer failure')
    monkeypatch.setattr(engine, 'cloud_videos', forbidden)
    result = wash.safe_delete_files([f], engine.S_ROOT, progress=broken)
    assert result['strm_removed'] == 1 and result['cloud_removed'] == 0 and not f.exists()


def test_one_worker_no_queue_idempotent_replay_and_read_copy(isolated):
    started, release = threading.Event(), threading.Event()
    calls = []
    token = 'a' * 32
    def execute(progress):
        calls.append(1)
        progress({'processed':1, 'total':3})
        started.set()
        assert release.wait(3)
        return {'status':'success', 'strm_removed':3}
    try:
        assert jobs.submit(token, ('s','local'), execute)['status'] == 'accepted'
        assert started.wait(2)
        assert jobs.submit(token, ('s','local'), execute)['status'] == 'accepted'
        assert jobs.submit(token, ('s','share'), execute)['status'] == 'error'
        assert jobs.submit('b'*32, ('other','local'), execute)['status'] == 'busy'
        rec = jobs.read(token)
        assert rec['processed'] == 1 and rec['state'] == 'running'
        rec['processed'] = 99
        assert jobs.read(token)['processed'] == 1
    finally:
        release.set()
        result = wait_done(token)
    assert result['result']['strm_removed'] == 3
    assert jobs.submit(token, ('s','local'), execute)['status'] == 'accepted'
    assert calls == [1], 'replayed operation must not delete again'


def test_records_bounded_and_restart_does_not_replay(isolated, monkeypatch):
    for n in range(35):
        token = f'{n:032x}'
        jobs.submit(token, ('movie', str(n)), lambda p: {'status':'success'})
        wait_done(token)
    assert len(jobs._records) <= 32
    assert jobs.submit('invalid', ('m','local'), lambda p: pytest.fail())['status'] == 'error'
    monkeypatch.setattr(jobs, '_records', {})
    with pytest.raises(HTTPException) as exc:
        library.api_delete_progress(token)
    assert exc.value.status_code == 404


def test_async_movie_route_uses_same_safe_chain_progress_and_keeps_partial_cache(isolated, monkeypatch):
    f = put(engine.L_ROOT, 'a.strm')
    monkeypatch.setattr(engine, 'find_movie_strms_by_tmdb', lambda *a: [f])
    monkeypatch.setattr(engine, 'cloud_videos', lambda *a: ([], 'none'))
    monkeypatch.setattr(engine, 'container_to_emby_path', lambda *a: 'x')
    for name in ['invalidate_media_caches', 'notify_emby_deleted', 'write_audit_log']:
        monkeypatch.setattr(engine, name, lambda *a, **k: None)
    monkeypatch.setattr(engine, 'patch_emby_lib_cache_after_movie_delete', lambda *a: pytest.fail('retained files must keep movie cache'))
    token = 'a'*32
    result = library.api_movie_delete({'tmdb_id':'7', 'target':'local', 'dry_run':False,
                                      'async_delete':True, 'operation_id':token})
    assert result['status'] == 'accepted'
    rec = wait_done(token)
    assert rec['processed'] == 1 and rec['retained'] == 1
    assert rec['result']['strm_removed'] == 0 and not rec['result']['movie_target_removed']
    assert f.exists()


def test_authenticated_http_acceptance_and_progress_reads(isolated, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.routers import deps
    started, release = threading.Event(), threading.Event()
    calls = []
    def execute(sid, body, progress=None):
        calls.append(sid)
        progress({'processed':2, 'total':4, 'strm_removed':1, 'cloud_removed':1, 'retained':1})
        started.set()
        assert release.wait(3)
        return {'status':'success', 'count':4, 'strm_removed':3, 'cloud_removed':3}
    monkeypatch.setattr(library, '_series_delete', execute)
    app = FastAPI()
    app.include_router(library.router)
    client = TestClient(app)
    token = 'c'*32
    path = '/api/emby/delete/progress/' + token
    body = {'target':'local', 'dry_run':False, 'async_delete':True, 'operation_id':token}
    credentials = (deps.WEB_USER, deps.WEB_PASSWORD)
    try:
        assert client.post('/api/emby/series/s/delete', json=body).status_code == 401
        assert calls == []
        assert client.post('/api/emby/series/s/delete', json=body, auth=credentials).json()['status'] == 'accepted'
        assert started.wait(2)
        assert client.get(path).status_code == 401
        response = client.get(path, auth=credentials)
        assert response.headers['cache-control'] == 'no-store'
        progress = response.json()
        assert progress['state'] == 'running' and progress['processed'] == 2
        assert progress['strm_removed'] == progress['cloud_removed'] == progress['retained'] == 1
        assert 'identity' not in progress and 'updated' not in progress
        assert client.post('/api/emby/series/s/delete', json=body, auth=credentials).json()['status'] == 'accepted'
        assert calls == ['s']
    finally:
        release.set()
        wait_done(token)
    assert client.get(path, auth=credentials).json()['result']['strm_removed'] == 3


@pytest.mark.parametrize('target', ['local','share'])
def test_two_library_movie_cache_preserves_other_library(isolated, monkeypatch, target):
    data = {'movies':[{'id':'both','tmdb_id':'7','in_local':True,'in_share':True},
                      {'id':'only','tmdb_id':'7', 'in_local':target=='local', 'in_share':target=='share'},
                      {'id':'other','tmdb_id':'8','in_local':True}], 'stats':{}}
    monkeypatch.setattr(morning, '_patch_all_caches', lambda fn: fn(data))
    assert morning.patch_emby_lib_cache_after_movie_delete('7',target) == 1
    assert [x['id'] for x in data['movies']] == ['both','other']
    assert data['movies'][0]['in_'+target] is False
    assert data['movies'][0]['in_'+('share' if target=='local' else 'local')] is True
    assert data['stats']['total_movies'] == 2
