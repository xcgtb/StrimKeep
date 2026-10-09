"""Exploration and mapping consume the same cached facts, including valid zero."""
from app import state_store as _state
import copy
import time
from types import SimpleNamespace
import pytest
from dev.tests.test_library_fact_counts import cached, series, write_cache
from app import engine, morning, tmdb


def explore(monkeypatch, ids=(100,)):
    class FakeTmdb:
        key = 'fake'; calls = 0; hits = 0
        def __init__(self, cache=None): pass
        def get(self, path, **kw):
            assert path == '/discover/tv', 'no per-card TMDB requests'
            return {'results': [{'id': tid, 'name': '临时剧'} for tid in ids] if kw['page'] == 1 else []}
        def save(self): pass
    monkeypatch.setattr(engine, 'Tmdb', FakeTmdb)
    monkeypatch.setattr(tmdb, 'emby_library_index', lambda: {'tv:'+str(tid): {'id': 's1', 'in_local': True} for tid in ids})
    monkeypatch.setattr(tmdb, '_emby_series_live_eps', lambda *a: (_ for _ in ()).throw(AssertionError('no per-card Emby calls')))
    # New async page cache is isolated per helper invocation; wait only on fake APIs.
    monkeypatch.setattr(tmdb, '_explore_page_state', None)
    _state.remove(engine.STATE_DIR / 'explore_pages.json')
    for _ in range(200):
        result = engine.action_explore(SimpleNamespace(media='tv'))
        if result['status'] != 'pending':
            return result
        time.sleep(.005)
    raise AssertionError('fake exploration did not complete')


def test_explore_mapping_and_health_agree_on_newest_facts(cached, monkeypatch):
    write_cache([series(12)], facts_ts=100)
    morning.save_library_snapshot(morning.build_library_health_snapshot())
    write_cache([series(13)], facts_ts=200)
    result = explore(monkeypatch)
    card = result['cards'][0]
    view = morning.cached_library_view()
    assert card['eps']['have'] == view['series'][0]['have_eps'] == engine.unified_health()['episodes'] == 13
    assert card['facts_version'] == result['facts_version'] == view['facts_version'] == engine.unified_health()['facts_version']
    assert card['facts_ts'] == 200 and card['facts_stale'] is True


def test_explicit_zero_and_unknown_total_are_retained(cached, monkeypatch):
    row = series(0); row['total_episodes'] = 20
    row['tmdb_info'].update(declared_total=0, tmdb_total=20)
    write_cache([row])
    card = explore(monkeypatch)['cards'][0]
    assert card['eps']['have'] == card['eps']['total'] == 0


def test_no_cache_or_missing_row_does_not_invent_zero(cached, monkeypatch):
    card = explore(monkeypatch)['cards'][0]
    assert card['eps'] is None and card['eps_source'] == 'unavailable'
    write_cache([series()])
    card = explore(monkeypatch, ids=(101,))['cards'][0]
    assert card['eps'] is None and card['eps_source'] == 'unavailable'


def test_ambiguous_identity_does_not_pick_an_arbitrary_row(cached, monkeypatch):
    a = series(13); b = series(20); b['id'] = 'different'
    write_cache([a, b])
    card = explore(monkeypatch)['cards'][0]
    assert card['eps'] is None and card['eps_source'] == 'ambiguous'


def test_mapping_cache_api_is_read_only_and_versions_match(cached):
    from app.routers import library
    original = write_cache([series(13)], facts_ts=200)
    response = library.api_emby_library(cache_only=1)
    assert response['status'] == 'success' and response['series'][0]['have_eps'] == 13
    assert response['facts_version'] == engine.unified_health()['facts_version']
    assert response['series'][0]['facts_version'] == response['facts_version']
    import json
    assert _state.read(engine.EMBY_LIB_CACHE_FILE) == original


def test_season_breakdown_preserves_union_and_library_counts(cached, monkeypatch):
    monkeypatch.setattr(engine, 'EMBY_PATHS', SimpleNamespace(lib_of=lambda p: 'share' if p.startswith('/share') else 'local'))
    row = series(); row.pop('tmdb_info')
    episodes = [dict(ParentIndexNumber=1, IndexNumber=1, Path='/local/a'),
                dict(ParentIndexNumber=1, IndexNumber=1, Path='/share/a'),
                dict(ParentIndexNumber=1, IndexNumber=2, Path='/share/b'),
                dict(ParentIndexNumber=2, IndexNumber=1, Path='/local/c')]
    morning._resync_series_entry(row, episodes)
    assert row['have_eps'] == 3 and row['local_eps'] == row['share_eps'] == 2
    assert [(s['season'], s['episodes'], s['local_eps'], s['share_eps']) for s in row['seasons']] == [(1, 2, 1, 2), (2, 1, 1, 0)]


def test_manual_done_is_shared_without_mutating_source(cached, monkeypatch):
    original = write_cache([series(13)])
    monkeypatch.setattr(morning, 'read_manual_done', lambda: {'s1': {}})
    card = explore(monkeypatch)['cards'][0]
    assert card['eps']['match_status'] == 'aligned' and card['eps']['manual_done'] is True
    assert morning.cached_library_view()['series'][0]['_md'] is True
    assert original['series'][0]['tmdb_info']['match_status'] == 'missing'


def test_ingest_resync_keeps_declared_total_and_poster(cached, monkeypatch):
    monkeypatch.setattr(engine, 'EMBY_PATHS', SimpleNamespace(lib_of=lambda p: 'local'))
    row = series(1)
    row['tmdb_info'].update(declared_total=24, poster='/poster.jpg', tmdb_total=12,
                            seasons=[{'season': 1, 'tmdb': 12}], tmdb_status='Returning Series')
    morning._resync_series_entry(row, [dict(ParentIndexNumber=1, IndexNumber=2, Path='/local/a')])
    assert row['tmdb_info']['declared_total'] == 24 and row['tmdb_info']['tmdb_total'] == 12
    assert row['tmdb_info']['poster'] == '/poster.jpg'


def test_full_overview_also_saves_season_library_breakdown(cached, monkeypatch):
    monkeypatch.setattr(engine, 'EMBY_PATHS', SimpleNamespace(lib_of=lambda p: 'share' if p.startswith('/share') else 'local'))
    monkeypatch.setattr(engine, '_disk_tmdb_lookup', lambda: {})
    monkeypatch.setattr(engine, '_disk_eps_by_tmdb', lambda: {})
    monkeypatch.setattr(engine, '_alive_dir_map', lambda paths: {})
    monkeypatch.setattr(engine, '_fetch_all_episodes', lambda: [
        dict(SeriesId='s1', ParentIndexNumber=1, IndexNumber=1, Path='/local/a'),
        dict(SeriesId='s2', ParentIndexNumber=1, IndexNumber=1, Path='/share/a'),
        dict(SeriesId='s2', ParentIndexNumber=1, IndexNumber=2, Path='/share/b')])
    monkeypatch.setattr(engine, 'emby_request', lambda path, params: {'Items': [
        dict(Id='s1', Name='临时剧', Path='/local/title', ProviderIds={'Tmdb': '100'}),
        dict(Id='s2', Name='临时剧', Path='/share/title', ProviderIds={'Tmdb': '100'})]
        if params['IncludeItemTypes'] == 'Series' else []})
    row = morning._build_emby_library_overview()['series'][0]
    assert row['have_eps'] == 2
    assert row['seasons'][0]['local_eps'] == 1 and row['seasons'][0]['share_eps'] == 2
