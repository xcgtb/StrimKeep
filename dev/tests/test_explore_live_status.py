"""Library-only updates share discovery's facts without calling TMDB."""
import threading
import time

import pytest
from fastapi import HTTPException
from app import engine, tmdb, morning
from app.routers import library
from dev.tests.test_library_fact_counts import cached, series, write_cache


@pytest.fixture
def identity(monkeypatch):
    index = {'movie:100': {'id': 'm1', 'in_local': True},
             'tv:100': {'id': 's1', 'in_share': True}}
    monkeypatch.setattr(tmdb, 'emby_library_index', lambda: index)
    monkeypatch.setattr(engine, '_emby_index_cache', {'data': index, 'ts': time.time()})
    monkeypatch.setattr(engine, '_emby_index_refresh_lock', threading.Lock())
    monkeypatch.setattr(tmdb, '_explore_page', lambda *a, **kw: pytest.fail('no discovery requests'))
    monkeypatch.setattr(engine, 'Tmdb', lambda *a, **kw: pytest.fail('no TMDB client'))
    return index


def test_shared_facts_update_movie_tv_collision_zero_and_manual_done(cached, identity, monkeypatch):
    write_cache([series(0)], facts_ts=100)
    body = {'cards': [{'type': 'movie', 'tmdb_id': 100}, {'type': 'tv', 'tmdb_id': '100'}]}
    first = library.api_explore_library_status(body)['cards']
    assert first[0]['in_local'] and first[0]['eps'] is None
    assert first[1]['eps']['have'] == 0
    write_cache([series(20)], facts_ts=200)
    monkeypatch.setattr(morning, 'read_manual_done', lambda: {'s1': {}})
    result = library.api_explore_library_status(body)
    tv = result['cards'][1]
    assert tv['eps']['have'] == 20 and tv['eps']['manual_done']
    assert tv['facts_version'] == morning.cached_library_view()['facts_version']
    assert tv['facts_ts'] == 200
    assert '_emby_has_image' not in tv


def test_index_finishes_after_long_sync_or_recovers_from_failure(cached, identity, monkeypatch):
    cache = engine._emby_index_cache
    cache.update(data=None, ts=0, error_ts=time.time())
    identity.clear()
    body = {'cards': [{'type': 'movie', 'tmdb_id': '100'}]}
    before = library.api_explore_library_status(body)
    assert before['cards'][0]['library_status'] == 'unavailable' and before['library_error']
    identity['movie:100'] = {'id': 'm1', 'in_share': True}
    cache.update(data=identity, ts=time.time(), error_ts=0)
    after = library.api_explore_library_status(body)
    assert after['cards'][0]['library_status'] == 'available'
    assert after['cards'][0]['in_share'] and not after['library_error']


def test_ambiguous_series_remains_unknown(cached, identity):
    a, b = series(3), series(20)
    b['id'] = 's2'
    write_cache([a, b])
    card = library.api_explore_library_status({'cards': [{'type': 'tv', 'tmdb_id': '100'}]})['cards'][0]
    assert card['eps'] is None and card['eps_source'] == 'ambiguous'


@pytest.mark.parametrize('body', [{}, {'cards': None}, {'cards': {}}, {'cards': [None]},
    {'cards': [{'type': 'person', 'tmdb_id': 1}]}, {'cards': [{'type': 'movie', 'tmdb_id': '../foo'}]},
    {'cards': [{'type': 'movie', 'tmdb_id': '１００'}]},
    {'cards': [{'type': 'movie', 'tmdb_id': '1' * 21}]},
    {'cards': [{'type': 'movie', 'tmdb_id': 1}] * 201}])
def test_invalid_or_unbounded_requests_rejected(body, monkeypatch):
    monkeypatch.setattr(tmdb, '_explore_library_status', lambda *a: pytest.fail('invalid request reads facts'))
    with pytest.raises(HTTPException) as error:
        library.api_explore_library_status(body)
    assert error.value.status_code == 400


def test_empty_and_unauthenticated_requests_do_not_trigger_sync(monkeypatch):
    from app.main import app
    from app.routers.deps import auth
    from fastapi.testclient import TestClient
    monkeypatch.setattr(tmdb, '_explore_library_status', lambda *a: pytest.fail('unexpected library work'))
    assert library.api_explore_library_status({'cards': []})['cards'] == []
    prior = app.dependency_overrides.pop(auth, None)
    try:
        assert TestClient(app).post('/api/explore/library-status', json={'cards': []}).status_code == 401
    finally:
        if prior is not None:
            app.dependency_overrides[auth] = prior
