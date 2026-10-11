"""Real cache/HTTP paths with disposable storage and bounded simulated slow I/O."""
import io
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from app import posters, poster_store, config
from app.routers import library


def test_signed_in_web_cookie_still_loads_both_poster_sources(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app import engine
    from app.routers import deps
    monkeypatch.setattr(posters, 'get_emby_poster', lambda *a, **kw: (b'poster', 'image/jpeg'))
    monkeypatch.setattr(posters, 'get_tmdb_poster', lambda *a, **kw: (b'poster', 'image/jpeg'))
    with TestClient(app) as client:
        monkeypatch.setattr(engine, 'EMBY_KEY', 'dummy-poster-key')
        monkeypatch.setattr(engine, 'EMBY_HOST', 'http://emby.invalid')
        response = client.post('/api/login', json={'username':deps.WEB_USER, 'password':deps.WEB_PASSWORD})
        assert response.status_code == 200
        for path in ['/api/emby/poster/123?size=w342', '/api/tmdb/poster/safe.jpg?size=w342']:
            response = client.get(path)
            assert response.status_code == 200 and response.content == b'poster'
            assert response.headers['cache-control'].startswith('private,')
        monkeypatch.setattr(posters, 'get_emby_poster', lambda *a, **kw: (_ for _ in ()).throw(posters.PosterBusyError('busy')))
        response = client.get('/api/emby/poster/123')
        assert response.status_code == 503 and response.headers['retry-after'] == '1'


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'DATA_DIR', tmp_path)
    with posters._lock:
        posters._cache.clear()
        posters._inflight.clear()
    yield
    with posters._lock:
        posters._cache.clear()
        posters._inflight.clear()


def image(data=b'\xff\xd8\xfffixture', content_type='image/jpeg'):
    response = io.BytesIO(data)
    response.headers = {'Content-Type': content_type}
    return response


def test_tmdb_size_separation_and_restart_disk_hit(monkeypatch):
    calls = []
    monkeypatch.setattr(posters.network, 'open_external', lambda url, **kw: (calls.append(url) or image(url.encode())))
    small = posters.get_tmdb_poster('sample.jpg', 'w342')
    large = posters.get_tmdb_poster('sample.jpg')
    assert small != large
    with posters._lock:
        posters._cache.clear()
    monkeypatch.setattr(posters.network, 'open_external', lambda *a, **kw: pytest.fail('disk hit went online'))
    assert posters.get_tmdb_poster('sample.jpg', 'w342') == small
    assert posters.get_tmdb_poster('sample.jpg') == large
    assert len(calls) == 2


def test_emby_thumbnail_cached_scoped_and_never_external_proxy(monkeypatch):
    calls = []
    monkeypatch.setattr(posters.network, 'open_external', lambda *a, **kw: pytest.fail('local Emby used external proxy'))
    def outbound(request, timeout):
        calls.append((request.full_url, request.get_header('X-emby-token')))
        return image(str(len(calls)).encode())
    monkeypatch.setattr(posters.urllib.request, 'urlopen', outbound)
    args = ('123', 'http://emby.test', 'token-a', 'w342')
    first = posters.get_emby_poster(*args)
    assert posters.get_emby_poster(*args) == first
    with posters._lock:
        posters._cache.clear()
    assert posters.get_emby_poster(*args) == first
    assert 'MaxWidth=342&Quality=90' in calls[0][0]
    assert calls[0][1] == 'token-a'
    assert posters.get_emby_poster('123', 'http://other.test', 'token-a', 'w342') != first
    assert posters.get_emby_poster('123', 'http://emby.test', 'token-b', 'w342') != first
    assert len(calls) == 3
    for path in config.DATA_DIR.rglob('*'):
        if path.is_file():
            assert b'token-a' not in path.read_bytes()


def test_six_upstream_limit_does_not_queue_unbounded_callers(monkeypatch):
    poster_store.write('tmdb:w500:already-cached.jpg', b'cached', 'image/jpeg')
    entered = threading.Barrier(7)
    release = threading.Event()
    def outbound(*a, **kw):
        entered.wait(timeout=5)
        assert release.wait(5)
        return image()
    monkeypatch.setattr(posters.network, 'open_external', outbound)
    with ThreadPoolExecutor(max_workers=6) as pool:
        jobs = [pool.submit(posters.get_tmdb_poster, f'slow{i}.jpg') for i in range(6)]
        entered.wait(timeout=5)
        try:
            start = time.monotonic()
            with pytest.raises(posters.PosterBusyError):
                posters.get_tmdb_poster('extra.jpg')
            assert time.monotonic() - start < .5
            assert len(posters._inflight) == 6
            assert posters.get_tmdb_poster('already-cached.jpg') == (b'cached', 'image/jpeg')
        finally:
            release.set()
        assert all(job.result(timeout=5) == (b'\xff\xd8\xfffixture', 'image/jpeg') for job in jobs)
    assert not posters._inflight


def test_store_size_and_count_bounds_do_not_touch_other_data(monkeypatch):
    monkeypatch.setattr(poster_store, 'MAX_BYTES', 300)
    monkeypatch.setattr(poster_store, 'MAX_ITEMS', 2)
    directory = config.DATA_DIR / 'poster-cache-v1'
    directory.mkdir()
    private = directory / 'keep.txt'
    private.write_text('leave alone')
    for n in range(12):
        poster_store.write(str(n), bytes([n]) * 90, 'image/jpeg')
    files = list(directory.glob('*.img'))
    assert len(files) <= 2 and sum(path.stat().st_size for path in files) <= 300
    assert poster_store.read('11', 86400) == (bytes([11]) * 90, 'image/jpeg')
    assert private.read_text() == 'leave alone'
    assert not list(directory.glob('*.tmp'))


def test_expired_corrupt_and_partial_disk_cache_refetch(monkeypatch):
    calls = []
    monkeypatch.setattr(posters.network, 'open_external', lambda *a, **kw: (calls.append(1) or image()))
    posters.get_tmdb_poster('stale.jpg')
    with posters._lock:
        posters._cache.clear()
    path = poster_store._path('tmdb:w500:stale.jpg')
    past = time.time() - 90000
    os.utime(path, (past, past))
    posters.get_tmdb_poster('stale.jpg')
    with posters._lock:
        posters._cache.clear()
    path.write_bytes(b'{"v":1,"type":"image/jpeg","size":5}\nX')
    posters.get_tmdb_poster('stale.jpg')
    assert len(calls) == 3


def test_unwritable_disk_is_best_effort(monkeypatch):
    monkeypatch.setattr(posters.network, 'open_external', lambda *a, **kw: image())
    monkeypatch.setattr(poster_store.os, 'replace', lambda *a: (_ for _ in ()).throw(OSError('read only')))
    assert posters.get_tmdb_poster('readonly.jpg')[0] == b'\xff\xd8\xfffixture'
    assert not list(config.DATA_DIR.rglob('*.tmp'))
    assert posters.get_tmdb_poster('readonly.jpg')[0] == b'\xff\xd8\xfffixture'


@pytest.mark.parametrize('data,content_type', [(b'', 'image/jpeg'), (b'html', 'text/html'), (b'x' * (2 * 1024 * 1024 + 1), 'image/jpeg')])
def test_invalid_image_is_not_saved_and_retry_remains_available(monkeypatch, data, content_type):
    monkeypatch.setattr(posters.network, 'open_external', lambda *a, **kw: image(data, content_type))
    with pytest.raises(ValueError):
        posters.get_tmdb_poster('bad.jpg')
    assert not list(config.DATA_DIR.rglob('*.img'))
    assert not posters._inflight
    monkeypatch.setattr(posters.network, 'open_external', lambda *a, **kw: image())
    assert posters.get_tmdb_poster('bad.jpg')[0] == b'\xff\xd8\xfffixture'


def test_invalid_size_cannot_change_upstream_or_use_arbitrary_urls(monkeypatch):
    monkeypatch.setattr(posters.network, 'open_external', lambda *a, **kw: pytest.fail('invalid size accessed network'))
    for size in ['original', 'w99999', 'https://evil.test', '../w342']:
        with pytest.raises(ValueError):
            posters.get_tmdb_poster('safe.jpg', size)


def test_routes_auth_busy_response_and_size_parameter(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.routers.deps import auth
    with TestClient(app) as client:
        assert client.get('/api/emby/poster/123').status_code == 401
        assert client.get('/api/tmdb/poster/safe.jpg').status_code == 401
        app.dependency_overrides[auth] = lambda: None
        try:
            seen = []
            monkeypatch.setattr(posters, 'get_tmdb_poster', lambda filename, size: (seen.append(size) or (b'\xff\xd8\xfffixture', 'image/jpeg')))
            assert client.get('/api/tmdb/poster/safe.jpg?size=w342').status_code == 200
            assert seen == ['w342']
            monkeypatch.setattr(posters, 'get_tmdb_poster', lambda *a: (_ for _ in ()).throw(posters.PosterBusyError('busy')))
            result = client.get('/api/tmdb/poster/safe.jpg')
            assert result.status_code == 503 and result.headers['retry-after'] == '1'
        finally:
            app.dependency_overrides.pop(auth, None)
