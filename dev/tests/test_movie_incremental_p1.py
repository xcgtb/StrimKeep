"""P1 movie-only incremental synchronization, conservative identity and UI journal."""
from types import SimpleNamespace

import pytest
from app import engine, morning, tmdb, library_updates, state_store
from app.routers import library


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, 'STATE_DIR', tmp_path)
    for attr, name in [('EMBY_LIB_CACHE_FILE', 'emby_lib_cache.json'),
                       ('LIBRARY_SNAPSHOT_FILE', 'library_snapshot.json'),
                       ('_EMBY_OVERVIEW_CACHE_FILE', 'emby_overview_cache.json'),
                       ('_EMBY_INDEX_CACHE_FILE', 'emby_index_cache.json')]:
        monkeypatch.setattr(engine, attr, tmp_path / name)
    monkeypatch.setattr(engine, 'EMBY_PATHS', SimpleNamespace(lib_of=lambda path:
        'local' if '/local/' in path else 'share' if '/share/' in path else None))
    monkeypatch.setattr(morning, '_patch_all_caches', lambda fn: None)
    monkeypatch.setattr(engine, '_emby_index_cache', {'ts': 50, 'data': {}, 'error_ts': 0})
    state_store.save(engine.EMBY_LIB_CACHE_FILE, {'ts': 100, 'series': [], 'movies': [],
                                                    'stats': {'total_movies': 0}})


def meta(mid='m1', path='/media/share/movie.strm', tmdb_id='321', name='电影甲', year=2026):
    return {'Id': mid, 'Type': 'Movie', 'Name': name, 'Path': path,
            'ProductionYear': year, 'ProviderIds': {'Tmdb': tmdb_id},
            'Genres': ['剧情'], 'ImageTags': {'Primary': 'abc'}}


def fetch_meta(monkeypatch, items):
    def fetch(path, params=None, **kw):
        assert path == '/Items' and params['IncludeItemTypes'] == 'Movie'
        assert params['Limit'] == 2 and params['StartIndex'] == 0
        row = items.get(params['Ids'])
        if isinstance(row, Exception):
            raise row
        return {'Items': [row] if row else [], 'TotalRecordCount': 1 if row else 0}
    monkeypatch.setattr(engine, 'emby_request', fetch)


def stored():
    return state_store.read(engine.EMBY_LIB_CACHE_FILE)


def test_movie_only_new_identity_updates_map_explore_and_version(env, monkeypatch):
    fetch_meta(monkeypatch, {'m1': meta()})
    result = engine.refresh_mapping_cache_after_ingest({'ok': True, 'movies_raw': [{'id': 'm1'}]})
    assert result['created'] == 1 and result['movies_updated'] == 1
    data = stored()
    assert data['ts'] == 100 and data['stats']['total_movies'] == 1
    row = data['movies'][0]
    assert row['tmdb_id'] == '321' and row['in_share'] and not row['in_local']
    assert row['ids'] == ['m1']
    assert engine._emby_index_cache['data']['movie:321']['in_share']
    assert engine._emby_index_cache['ts'] == 50
    changes = library_updates.changes(0)
    assert changes['movie_ids'] == ['m1'] and changes['ids'] == [] and not changes['full']
    answer = library.api_library_changes(since=0)
    assert not answer['full'] and answer['movies'][0]['id'] == 'm1'


def test_movie_cross_library_merge_strict_id_title_year(env, monkeypatch):
    original = {'id': 'm0', 'ids': ['m0'], 'name': '电影甲', 'year': 2026,
                'tmdb_id': '321', 'in_local': True, 'in_share': False,
                'path': '/media/local/movie.strm', 'paths': ['/media/local/movie.strm'],
                'poster_tmdb': '/the-poster.jpg'}
    state_store.save(engine.EMBY_LIB_CACHE_FILE, {'ts': 100, 'series': [], 'movies': [original]})
    fetch_meta(monkeypatch, {'m0': meta('m0', '/media/local/movie.strm'), 'm1': meta()})
    result = engine.refresh_mapping_cache_after_ingest({'ok': True, 'movies_raw': [{'id': 'm1'}]})
    assert result['created'] == 0 and result['movies_updated'] == 1
    row = stored()['movies'][0]
    assert set(row['ids']) == {'m0', 'm1'}
    assert row['in_local'] and row['in_share'] and row['poster_tmdb'] == '/the-poster.jpg'
    assert len(stored()['movies']) == 1
    assert set(library_updates.changes(0)['movie_ids']) == {'m0', 'm1'}
    # Repeated unchanged ingest does not publish or alter facts.
    before = library_updates.changes(0)['version']
    assert engine.refresh_mapping_cache_after_ingest({'ok': True, 'movies_raw': [{'id': 'm1'}]})['movies_updated'] == 0
    assert library_updates.changes(0)['version'] == before


@pytest.mark.parametrize('reply', [
    {'Items': [meta(mid='wrong')], 'TotalRecordCount': 1},
    {'Items': [dict(meta(), Type='Series')], 'TotalRecordCount': 1},
    {'Items': [meta()], 'TotalRecordCount': 5405},
    {'Items': [meta(), meta('other')], 'TotalRecordCount': 2},
    {'Items': [], 'TotalRecordCount': 0},
    {'Items': None, 'TotalRecordCount': 1},
    {'Items': [meta(path='/other/movie.strm')], 'TotalRecordCount': 1},
])
def test_movie_bad_metadata_preserves_old_snapshot(env, monkeypatch, reply):
    monkeypatch.setattr(engine, 'emby_request', lambda *a, **k: reply)
    before = stored()
    result = engine.refresh_mapping_cache_after_ingest({'ok': True, 'movies_raw': [{'id': 'm1'}]})
    assert result['movies_updated'] == 0 and stored() == before
    assert library_updates.changes(0)['version'] == 0


def test_movie_same_name_different_tmdb_does_not_merge(env, monkeypatch):
    base = {'id': 'm0', 'ids': ['m0'], 'name': '电影甲', 'year': 2026, 'tmdb_id': '999'}
    state_store.save(engine.EMBY_LIB_CACHE_FILE, {'ts': 100, 'movies': [base], 'series': []})
    fetch_meta(monkeypatch, {'m1': meta()})
    result = engine.refresh_mapping_cache_after_ingest({'ok': True, 'movies_raw': [{'id': 'm1'}]})
    assert result['created'] == 1 and len(stored()['movies']) == 2


def test_movie_cross_library_timeout_preserves_original(env, monkeypatch):
    base = {'id': 'm0', 'ids': ['m0'], 'name': '电影甲', 'year': 2026, 'tmdb_id': '321'}
    state_store.save(engine.EMBY_LIB_CACHE_FILE, {'ts': 100, 'movies': [base], 'series': []})
    fetch_meta(monkeypatch, {'m0': TimeoutError('Emby offline'), 'm1': meta()})
    original = stored()
    result = engine.refresh_mapping_cache_after_ingest({'ok': True, 'movies_raw': [{'id': 'm1'}]})
    assert result['updated'] == 0 and stored() == original


def test_movie_update_requires_successful_ingest(env, monkeypatch):
    fetch_meta(monkeypatch, {'m1': meta()})
    assert engine.refresh_mapping_cache_after_ingest({'ok': False, 'movies_raw': [{'id': 'm1'}]})['updated'] == 0
    assert stored()['movies'] == []
