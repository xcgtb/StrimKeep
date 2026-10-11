"""P4 first-screen behavior: bounded read-only preview of authoritative facts."""
import time
from app import engine, library_first_screen as quick, library_response
from dev.tests.test_library_fact_counts import cached, series, write_cache


def setup_function():
    quick._cache.update(key=None, expires=0, data=None)


def test_preview_is_bounded_and_omits_private_paths(cached):
    rows = []
    for i in range(150):
        row = series(i + 1)
        row.update(id=f's{i}', tmdb_id=str(1000+i), path='/media/private/some-title',
                   paths=['/media/private/a'], name=f'影视{i:03}')
        rows.append(row)
    write_cache(rows, facts_ts=time.time())
    res = quick.preview()
    assert res['status'] == 'success' and res['preview'] is True
    assert len(res['series']) == 48
    assert len(res['movies']) <= 48
    assert all('path' not in row and 'paths' not in row and 'seasons' not in row
               for row in res['series'])
    assert res['facts_version'] == engine.cached_library_view()['facts_version']


def test_preview_invalidation_reuses_unchanged_and_updates_counts(cached, monkeypatch):
    write_cache([series(3)], facts_ts=100)
    orig = engine.cached_library_view
    calls = []
    monkeypatch.setattr(engine, 'cached_library_view', lambda: calls.append(1) or orig())
    first = quick.preview()
    second = quick.preview()
    assert second is first and calls == [1]
    write_cache([series(19)], facts_ts=200)
    current = quick.preview()
    assert current['series'][0]['have_eps'] == 19
    assert current['facts_version'] != first['facts_version']
    assert calls == [1, 1]


def test_manual_done_and_unknown_zero_remain_authoritative(cached, monkeypatch):
    row = series(0)
    row['total_episodes'] = 15
    row['tmdb_info']['declared_total'] = 0
    write_cache([row])
    first = quick.preview()['series'][0]
    assert first['have_eps'] == 0 and first['tmdb_info']['declared_total'] == 0
    from app import morning, state_store
    monkeypatch.setattr(morning, 'read_manual_done', lambda: state_store.read(engine.MANUAL_DONE_FILE, {}))
    state_store.save(engine.MANUAL_DONE_FILE, {'s1': {'name': 'manual'}})
    updated = quick.preview()['series'][0]
    assert updated['_md'] is True
    assert updated['tmdb_info']['match_status'] == 'aligned'


def test_no_cache_is_never_empty_success(cached):
    assert quick.preview() == {'status': 'nocache'}


def test_preview_only_retains_required_identity_fields(cached):
    row = series(1)
    row['tmdb_info']['poster'] = '/foo.jpg'
    row['in_local'] = True
    row['in_share'] = True
    write_cache([row])
    got = quick.preview()['series'][0]
    assert got['in_local'] and got['in_share']
    assert got['tmdb_info']['poster'] == '/foo.jpg'
    assert 'series_ids' not in got


def test_preview_http_requires_auth(cached):
    from app.main import app
    from app.routers.deps import auth
    from fastapi.testclient import TestClient
    prior = app.dependency_overrides.pop(auth, None)
    try:
        assert TestClient(app).get('/api/emby/library/preview').status_code == 401
    finally:
        if prior is not None:
            app.dependency_overrides[auth] = prior
