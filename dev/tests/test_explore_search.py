"""Search must not wait on browsing or bulk-cache I/O; failures remain retryable."""
import threading
import time
import urllib.error

import pytest

from app import engine, tmdb


@pytest.fixture
def search_state(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, 'STATE_DIR', tmp_path)
    monkeypatch.setattr(tmdb, '_explore_page_state', None)
    monkeypatch.setattr(tmdb, '_explore_slots', threading.BoundedSemaphore(2))
    monkeypatch.setattr(tmdb, '_explore_background_slots', threading.BoundedSemaphore(1))
    return tmdb._explore_state()


def finish(state):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        with tmdb._explore_page_lock:
            if not state['jobs']:
                return
        time.sleep(.005)
    raise AssertionError('fixture worker did not finish')


def test_search_uses_only_small_page_cache_and_scoped_network_budget(search_state, monkeypatch):
    seen = []

    class Client:
        key = 'fake'; calls = 1

        def __init__(self, cache=None):
            assert cache == {}, 'search must not load full TMDB cache'

        def get(self, path, **params):
            window = tmdb._explore_request_window.get()
            seen.append((path, params, dict(window)))
            return {'results': [{'id': 100, 'name': '测试剧'}], 'total_pages': 1}

        def save(self):
            raise AssertionError('search must not rewrite full TMDB cache')

    monkeypatch.setattr(engine, 'Tmdb', Client)
    _, meta = tmdb._explore_page('/search/tv', {'query': '测试剧', 'page': 1}, 'tv')
    assert meta['status'] == 'pending'
    finish(search_state)
    data, _ = tmdb._explore_page('/search/tv', {'query': '测试剧', 'page': 1}, 'tv')
    assert data['results'] == [{'id': 100, 'name': '测试剧'}]
    assert len(seen) == 1 and seen[0][2]['timeout'] == 4
    assert seen[0][2]['attempts'] == 2
    assert tmdb._explore_request_window.get() is None


def test_slow_browsing_and_prefetch_leave_a_slot_for_search(search_state, monkeypatch):
    entered, release, searched = threading.Event(), threading.Event(), threading.Event()
    calls = []

    class Client:
        key = 'fake'

        def __init__(self, cache=None): pass

        def get(self, path, **params):
            calls.append((path, params['page']))
            if path == '/discover/movie':
                entered.set()
                assert release.wait(3)
            else:
                searched.set()
            return {'results': [], 'total_pages': 2}

    monkeypatch.setattr(engine, 'Tmdb', Client)
    try:
        tmdb._explore_page('/discover/movie', {'page': 1}, 'movie')
        assert entered.wait(1)
        _, meta = tmdb._explore_page('/discover/movie', {'page': 2}, 'movie')
        assert meta['status'] == 'pending' and meta['refreshing'] is False
        tmdb._explore_page('/search/tv', {'query': '新剧', 'page': 1}, 'tv')
        assert searched.wait(1), 'search was blocked by discovery/prefetch'
        assert ('/discover/movie', 2) not in calls
    finally:
        release.set()
        finish(search_state)
    data, _ = tmdb._explore_page('/search/tv', {'query': '新剧', 'page': 1}, 'tv')
    assert data['results'] == []


def test_manual_retry_skips_failed_query_cooldown_without_poll_retry_storm(search_state, monkeypatch):
    calls = []

    class Client:
        key = 'fake'

        def __init__(self, cache=None): pass

        def get(self, path, **params):
            calls.append(1)
            if len(calls) == 1:
                raise TimeoutError('url with secret-api-key')
            return {'results': [{'id': 100}], 'total_pages': 1}

    monkeypatch.setattr(engine, 'Tmdb', Client)
    args = ('/search/tv', {'query': '测试剧', 'page': 1}, 'tv')
    tmdb._explore_page(*args)
    finish(search_state)
    for _ in range(5):
        data, meta = tmdb._explore_page(*args)
        assert data is None and meta['error_code'] == 'timeout'
        assert 'secret-api-key' not in meta['message']
    assert len(calls) == 1
    tmdb._explore_page(*args, retry=True)
    finish(search_state)
    data, _ = tmdb._explore_page(*args)
    assert data['results'] == [{'id': 100}] and len(calls) == 2


@pytest.mark.parametrize('error,code', [
    (urllib.error.HTTPError('secret-url', 401, 'bad key', {}, None), 'auth'),
    (urllib.error.HTTPError('secret-url', 403, 'denied', {}, None), 'auth'),
    (urllib.error.HTTPError('secret-url', 429, 'limited', {}, None), 'rate_limit'),
    (urllib.error.HTTPError('secret-url', 503, 'offline', {}, None), 'http'),
    (urllib.error.URLError(TimeoutError('secret-url')), 'timeout'),
    (urllib.error.URLError('secret-url'), 'network'),
    (ValueError('secret-url'), 'response'),
])
def test_failures_have_safe_actionable_reasons(error, code):
    wrapped = tmdb.TmdbError('secret-url')
    wrapped.__cause__ = error
    actual, message = tmdb._explore_failure(wrapped)
    assert actual == code and 'secret' not in message


def test_real_tmdb_auth_failure_keeps_its_http_reason_without_exposing_key(search_state, monkeypatch):
    monkeypatch.setattr(engine, 'RUNTIME_CFG', dict(engine.RUNTIME_CFG, tmdb_key='secret-api-key'))

    def denied(*args, **kwargs):
        raise urllib.error.HTTPError('secret-url', 401, 'bad key', {}, None)

    monkeypatch.setattr(tmdb.urllib.request, 'urlopen', denied)
    tmdb._explore_page('/search/tv', {'query': '测试剧', 'page': 1}, 'tv')
    finish(search_state)
    data, meta = tmdb._explore_page('/search/tv', {'query': '测试剧', 'page': 1}, 'tv')
    assert data is None and meta['error_code'] == 'auth'
    assert 'secret' not in meta['message'] and '密钥' in meta['message']


def test_http_search_retry_returns_requested_title_after_failure(search_state, monkeypatch):
    monkeypatch.setenv('WEB_PASSWORD', 'isolated-fixture-password')
    from fastapi.testclient import TestClient
    from app.main import app
    from app.routers.deps import auth

    calls = []

    class Client:
        key = 'fake'

        def __init__(self, cache=None): pass

        def get(self, path, **params):
            calls.append((path, params['query']))
            if len(calls) == 1:
                raise TimeoutError('fixture')
            return {'results': [{'id': 100, 'name': params['query']}], 'total_pages': 1}

    monkeypatch.setattr(engine, 'Tmdb', Client)
    monkeypatch.setattr(tmdb, 'emby_library_index', lambda: {})
    monkeypatch.setattr(engine, 'cached_library_view', lambda: None)
    monkeypatch.setitem(app.dependency_overrides, auth, lambda: True)
    client = TestClient(app)  # No lifespan / scheduler / external services.
    params = {'q': '测试剧', 'media': 'tv'}
    assert client.get('/api/explore', params=params).json()['status'] == 'pending'
    finish(search_state)
    assert client.get('/api/explore', params=params).json()['error_code'] == 'timeout'
    client.get('/api/explore', params=dict(params, retry=1))
    finish(search_state)
    result = client.get('/api/explore', params=params).json()
    assert result['status'] == 'success' and result['cards'][0]['title'] == '测试剧'
    assert calls == [('/search/tv', '测试剧')] * 2
