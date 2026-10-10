import gzip
import json
import time

import pytest
from dev.tests.test_library_fact_counts import cached, series, write_cache
from app import engine, morning, state_store, library_response
from app.routers import library


@pytest.fixture(autouse=True)
def reset_response_cache(monkeypatch):
    monkeypatch.setattr(library_response, '_cache', {'key': None, 'expires': 0, 'plain': None, 'gzip': None})


def decoded(response):
    body = response.body
    if response.headers.get('content-encoding') == 'gzip':
        body = gzip.decompress(body)
    return json.loads(body)


def test_response_retains_all_counts_details_and_category_paths(cached):
    row = series(13)
    row['paths'] = ['/local/国产剧/示例', '/share/国产剧/示例']
    row['seasons'] = [{'season': 1, 'local_eps': 13, 'share_eps': 3, 'missing': list(range(14,100))}]
    write_cache([row] * 5)
    expected = engine.cached_library_view()
    response = library_response.cached_mapping_response('gzip, deflate')
    actual = decoded(response)
    assert actual['series'] == expected['series']
    assert actual['stats'] == expected['stats'] and actual['facts_version'] == expected['facts_version']
    assert response.headers['content-encoding'] == 'gzip'
    assert 'private' in response.headers['cache-control']
    assert 'Accept-Encoding' in response.headers['vary']


def test_cached_response_skips_full_view_conversion_and_returns_safe_bytes(cached, monkeypatch):
    write_cache([series(13)])
    original = engine.cached_library_view
    calls = []
    def view():
        calls.append(1)
        return original()
    monkeypatch.setattr(engine, 'cached_library_view', view)
    first = library_response.cached_mapping_response()
    assert decoded(first)['series'][0]['have_eps'] == 13
    first.headers['Content-Encoding'] = 'modified-client-header'
    second = library_response.cached_mapping_response()
    assert calls == [1] and first.body is second.body
    assert 'content-encoding' not in second.headers


def test_ingest_delete_manual_done_and_host_updates_invalidate_immediately(cached, monkeypatch):
    monkeypatch.setattr(morning, 'read_manual_done', lambda: state_store.read(engine.MANUAL_DONE_FILE, {}))
    write_cache([series(13)], facts_ts=100)
    assert decoded(library_response.cached_mapping_response())['series'][0]['have_eps'] == 13
    write_cache([series(20)], facts_ts=200)
    assert decoded(library_response.cached_mapping_response())['series'][0]['have_eps'] == 20
    state_store.save(engine.MANUAL_DONE_FILE, {'s1': {}})
    done = decoded(library_response.cached_mapping_response())
    assert done['series'][0]['_md'] and done['stats']['manual_done'] == 1
    state_store.save(engine.MANUAL_DONE_FILE, {})
    assert not decoded(library_response.cached_mapping_response())['series'][0]['_md']
    monkeypatch.setattr(engine, 'EMBY_HOST', 'http://new-host:8096')
    assert decoded(library_response.cached_mapping_response())['emby_host'] == 'http://new-host:8096'
    write_cache([], facts_ts=300)
    assert decoded(library_response.cached_mapping_response())['series'] == []


def test_missing_cache_is_not_an_empty_success(cached):
    assert library_response.cached_mapping_response() is None
    assert library.api_emby_library(web=1, cache_only=1) == {'status': 'nocache'}


@pytest.mark.parametrize('encoding,wants_gzip', [('gzip',True), ('br, gzip;q=0.5',True),
    ('gzip;q=0',False), ('identity',False), ('',False), ('gzip;q=bad',False)])
def test_negotiates_gzip(encoding, wants_gzip):
    assert library_response._gzip_accepted(encoding) == wants_gzip


def test_expiry_and_memory_limit_do_not_keep_old_or_large_responses(cached, monkeypatch):
    write_cache([series(13)])
    library_response.cached_mapping_response()
    library_response._cache['expires'] = 0
    real = engine.cached_library_view
    calls = []
    monkeypatch.setattr(engine, 'cached_library_view', lambda: calls.append(1) or real())
    library_response.cached_mapping_response()
    assert calls == [1]
    monkeypatch.setattr(library_response, '_MAX_BYTES', 1)
    library_response._cache['expires'] = 0
    assert decoded(library_response.cached_mapping_response())['status'] == 'success'
    assert library_response._cache['plain'] is None


def test_authenticated_http_negotiation_matches_legacy_endpoint(cached, monkeypatch):
    from app.main import app
    from app.routers.deps import auth
    from fastapi.testclient import TestClient
    write_cache([series(13)] * 5)
    prior = app.dependency_overrides.pop(auth, None)
    client = TestClient(app)
    assert client.get('/api/emby/library?cache_only=1&web=1').status_code == 401
    app.dependency_overrides[auth] = lambda: None
    try:
        fast = client.get('/api/emby/library?cache_only=1&web=1', headers={'Accept-Encoding':'gzip'})
        old = client.get('/api/emby/library?cache_only=1')
        assert fast.status_code == 200 and fast.headers['content-encoding'] == 'gzip'
        assert fast.json()['series'] == old.json()['series']
        assert fast.json()['stats'] == old.json()['stats']
    finally:
        app.dependency_overrides.pop(auth, None)
        if prior is not None:
            app.dependency_overrides[auth] = prior
