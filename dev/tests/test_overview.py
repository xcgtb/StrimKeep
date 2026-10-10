import copy
import threading
import time
from types import SimpleNamespace

import pytest
from dev.tests.test_library_fact_counts import cached, series, write_cache
from app import engine, overview, morning, stats, state_store, scheduler


@pytest.fixture
def isolated(cached, monkeypatch, tmp_path):
    monkeypatch.setattr(engine, '_STRM_COUNT_CACHE_FILE', tmp_path / 'counts.json')
    monkeypatch.setattr(engine, 'L_ROOT', tmp_path / 'local')
    monkeypatch.setattr(engine, 'S_ROOT', tmp_path / 'share')
    engine.L_ROOT.mkdir(); engine.S_ROOT.mkdir()
    monkeypatch.setattr(engine, 'EMBY_KEY', '')
    monkeypatch.setattr(engine, '_strm_count_cache', {'ts': 0, 'local': 0, 'share': 0})
    monkeypatch.setattr(engine, '_lib_stats_cache', {'ts': 0, 'data': None})
    monkeypatch.setattr(overview, '_memory', {'path': None, 'stamp': None, 'data': None, 'health_stamp': None})
    monkeypatch.setattr(overview, '_job', {'running': False, 'id': 0, 'started_at': 0,
                        'finished_at': 0, 'phase': '', 'error': '', 'warnings': [], 'forced': False})
    monkeypatch.setattr(overview.config, 'get_strategy', lambda: {'decision': 'quality_first'})


def payload(ts=None):
    return {'schema': 1, 'ts': time.time() if ts is None else ts, 'emby_host': engine.EMBY_HOST,
            'dashboard': {'localCount': '18', 'shareCount': '20', 'libraryHealth': None},
            'library_stats': {'status': 'success', 'rows': [], 'local_total': 18, 'share_total': 20}}


def save(data=None):
    state_store.save(overview._path(), data or payload())


def no_threads(monkeypatch):
    calls = []
    class Thread:
        def __init__(self, **kw): calls.append(kw)
        def start(self): pass
        def join(self, *args): pass
        def is_alive(self): return False
    monkeypatch.setattr(overview.threading, 'Thread', Thread)
    return calls


def test_repeat_reads_and_restart_use_persistent_snapshot_without_scanning(isolated, monkeypatch):
    save(); calls = no_threads(monkeypatch)
    for _ in range(3):
        assert overview.get_overview()['dashboard']['localCount'] == '18'
    overview._memory.update(path=None, stamp=None, data=None)
    assert overview.get_overview()['library_stats']['share_total'] == 20
    assert calls == []


def test_expiry_is_cache_first_and_singleflight_including_manual_clicks(isolated, monkeypatch):
    save(payload(ts=100)); calls = no_threads(monkeypatch)
    response = overview.get_overview()
    assert response['dashboard']['localCount'] == '18' and response['refresh']['running']
    job_id = overview.request_refresh(force=True)['id']
    assert overview.request_refresh(force=True)['id'] == job_id
    assert len(calls) == 1


def test_manual_reset_really_removes_only_derived_sqlite_caches(isolated, monkeypatch):
    save(); write_cache([series()]); state_store.save(engine.MANUAL_DONE_FILE, {'s1': {}})
    derived = [engine.STATE_DIR / 'library_stats_cache.json', engine._STRM_COUNT_CACHE_FILE,
               engine._EMBY_OVERVIEW_CACHE_FILE]
    for path in derived: state_store.save(path, {'ts': 1})
    calls = no_threads(monkeypatch)
    result = overview.request_refresh(force=True)
    assert result['running'] and len(calls) == 1
    assert state_store.read(overview._path(), None) is None
    assert all(state_store.read(path, None) is None for path in derived)
    assert state_store.read(engine.EMBY_LIB_CACHE_FILE)['series'][0]['have_eps'] == 13
    assert state_store.read(engine.MANUAL_DONE_FILE) == {'s1': {}}
    assert overview.get_overview()['dashboard']['localCount'] == '18'


def test_failure_after_manual_clear_preserves_visible_fallback_and_retry(isolated, monkeypatch):
    save(); no_threads(monkeypatch); overview.request_refresh(force=True)
    monkeypatch.setattr(engine, '_recompute_all_stats', lambda: (_ for _ in ()).throw(OSError('offline')))
    overview._refresh_worker(True)
    r = overview.get_overview()
    assert r['dashboard']['localCount'] == '18'
    assert r['refresh']['error'] and not r['refresh']['running']
    assert state_store.read(overview._path(), None) is None
    assert overview.request_refresh(force=True)['id'] == 2


def test_completed_refresh_persists_accurate_counts_and_zero(isolated, monkeypatch):
    (engine.L_ROOT / 'a.strm').write_text('https://example.invalid/a')
    def collect(lib):
        return dict(payload(), library_stats=lib,
                    dashboard={'localCount': str(lib['local_total']), 'shareCount': str(lib['share_total'])})
    monkeypatch.setattr(overview, '_collect', collect)
    overview._refresh_worker(False)
    r = overview.get_overview()
    assert r['dashboard'] == {'localCount': '1', 'shareCount': '0'}
    assert state_store.read(overview._path())['library_stats']['local_total'] == 1
    assert not r['refresh']['running']


def test_stats_survive_restart_and_expire_in_background(isolated, monkeypatch):
    (engine.L_ROOT / 'one.strm').write_text('url')
    first = stats._recompute_all_stats()
    engine._lib_stats_cache.update(ts=0, data=None)
    monkeypatch.setattr(stats, '_recompute_all_stats', lambda: (_ for _ in ()).throw(AssertionError('scan forbidden')))
    assert stats.action_library_stats(SimpleNamespace()) == first
    state_store.save(engine.STATE_DIR / 'library_stats_cache.json', dict(first, ts=100))
    engine._lib_stats_cache.update(ts=0, data=None)
    calls = no_threads(monkeypatch)
    assert stats.action_library_stats(SimpleNamespace())['local_total'] == 1
    assert len(calls) == 1


def test_shared_facts_and_manual_completion_update_cached_summary_immediately(isolated, monkeypatch):
    save(); write_cache([series(13)], facts_ts=time.time())
    monkeypatch.setattr(morning, 'read_manual_done', lambda: state_store.read(engine.MANUAL_DONE_FILE, {}))
    r = overview.get_overview()['dashboard']['libraryHealth']
    assert r['episodes'] == 13 and r['stats']['missing'] == 1
    write_cache([series(0)], facts_ts=time.time())
    assert overview.get_overview()['dashboard']['libraryHealth']['episodes'] == 0
    state_store.save(engine.MANUAL_DONE_FILE, {'s1': {}})
    assert overview.get_overview()['dashboard']['libraryHealth']['stats']['aligned'] == 1


def test_fact_refresh_updates_counts_without_claiming_new_tmdb_comparison(isolated, monkeypatch):
    old = series(13)
    old['seasons'] = [{'season': 1, 'episodes': 13}]
    old['tmdb_info'].update(tmdb_status='Ended', seasons=[{'season': 1, 'tmdb': 20}], declared_total=24)
    write_cache([old], ts=100)
    fresh = series(20); fresh.pop('tmdb_info'); fresh['seasons'] = [{'season': 1, 'episodes': 20}]
    monkeypatch.setattr(engine, '_build_emby_library_overview', lambda: {'series': [fresh], 'movies': []})
    overview._refresh_facts()
    cache = state_store.read(engine.EMBY_LIB_CACHE_FILE)
    assert cache['ts'] == 100 and cache['facts_ts'] > 100
    assert cache['series'][0]['tmdb_info']['match_status'] == 'aligned'
    assert cache['series'][0]['tmdb_info']['declared_total'] == 24
    assert engine.unified_health()['episodes'] == 20


def test_changed_identity_does_not_inherit_other_titles_comparison(isolated):
    old = series(13); fresh = series(20); fresh.pop('tmdb_info'); fresh['name'] = '另一个节目'
    result = overview._carry_metadata({'series': [fresh], 'movies': []}, {'series': [old], 'ts': 100})
    assert not result['series'][0].get('tmdb_info') and result['ts'] == 100


def test_recommendations_are_cached_trending_with_type_and_library_status(isolated, monkeypatch):
    calls = []
    def page(path, params, media, retry):
        calls.append((path, media, retry))
        return {'results': [{'id': 5, 'name': '示例剧集', 'first_air_date': '2026-10-10',
                             'vote_average': 8.2, 'poster_path': '/abc.jpg'}]}, {'page_ts': 100}
    monkeypatch.setattr(overview.tmdb, '_explore_page', page)
    monkeypatch.setattr(overview.tmdb, '_explore_library_status', lambda rows: {'cards': [{'in_emby': True, 'library_status': 'available'}]})
    r = overview.recommendations('tv')
    assert r['cards'][0]['title'] == '示例剧集' and r['cards'][0]['in_emby']
    assert r['cards'][0]['poster'] == '/api/tmdb/poster/abc.jpg'
    assert calls == [('/trending/tv/week', 'tv', False)]
    with pytest.raises(ValueError): overview.recommendations('bad')


def test_recommendation_pending_and_error_do_not_become_successful_empty_lists(isolated, monkeypatch):
    monkeypatch.setattr(overview.tmdb, '_explore_page', lambda *a, **kw: (None, {'status': 'error', 'message': '未配置 TMDB_KEY'}))
    assert overview.recommendations()['status'] == 'error'
    monkeypatch.setattr(overview.tmdb, '_explore_page', lambda *a, **kw: (None, {'status': 'pending'}))
    assert overview.recommendations()['status'] == 'pending'


def test_new_routes_require_auth_and_return_lightweight_snapshot(isolated, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.routers.deps import auth
    save(); no_threads(monkeypatch)
    with TestClient(app) as client:
        assert client.get('/api/overview').status_code == 401
        assert client.post('/api/overview/refresh').status_code == 401
        assert client.get('/api/overview/recommendations').status_code == 401
        app.dependency_overrides[auth] = lambda: True
        try:
            r = client.get('/api/overview').json()
            assert r['status'] == 'success' and 'series' not in r
            assert client.post('/api/overview/refresh').json()['refresh']['running']
            assert client.post('/api/cache/refresh').json()['refresh']['id'] == 1
            assert client.get('/api/overview/recommendations?media=invalid').status_code == 400
        finally: app.dependency_overrides.pop(auth, None)
