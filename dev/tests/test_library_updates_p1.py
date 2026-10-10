"""P1 incremental fact patches: new series, union, failure guards and bounded events."""
from types import SimpleNamespace

import pytest
from app import engine, morning, tmdb, library_updates, state_store
from app.routers import library


def ep(n, sid, lib='share'):
    return {'Id': f'{sid}-{n}', 'Path': f'/media/{lib}/show/S01E{n:02d}.strm',
            'ParentIndexNumber': 1, 'IndexNumber': n}


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(engine, 'STATE_DIR', tmp_path)
    for attr, filename in [('EMBY_LIB_CACHE_FILE', 'emby_lib_cache.json'),
                           ('LIBRARY_SNAPSHOT_FILE', 'library_snapshot.json'),
                           ('_EMBY_OVERVIEW_CACHE_FILE', 'emby_overview_cache.json'),
                           ('_EMBY_INDEX_CACHE_FILE', 'emby_index_cache.json')]:
        monkeypatch.setattr(engine, attr, tmp_path / filename)
    monkeypatch.setattr(engine, 'EMBY_PATHS', SimpleNamespace(lib_of=lambda path: (
        'local' if '/local/' in path else 'share' if '/share/' in path else None)))
    monkeypatch.setattr(morning, '_patch_all_caches', lambda fn: None)
    state_store.save(engine.EMBY_LIB_CACHE_FILE, {'ts': 100, 'facts_ts': 100,
        'series': [], 'movies': [], 'stats': {'total_series': 0}})
    return tmp_path


def seed_meta(monkeypatch, sid, name='新剧', tmdb_id='995'):
    requests = []
    def fetch(path, params=None, **kwargs):
        requests.append((path, params))
        assert path == '/Items' and params['Ids'] == sid
        assert params['IncludeItemTypes'] == 'Series' and params['Limit'] == 2
        # /Items/{id} returns 404 on some deployed Emby versions; the list
        # route must return one exact Series instead.
        return {'Items': [{'Type': 'Series', 'Id': sid, 'Name': name,
                'Path': '/media/share/show', 'ProductionYear': 2026,
                'ProviderIds': {'Tmdb': tmdb_id}, 'ImageTags': {'Primary': 'x'},
                'Genres': []}], 'TotalRecordCount': 1}
    monkeypatch.setattr(engine, 'emby_request', fetch)
    return requests


def test_new_series_is_added_pending_and_published(isolated, monkeypatch):
    seed_meta(monkeypatch, 'sid1')
    monkeypatch.setattr(morning, '_live_series_episodes', lambda sid: [ep(1, sid), ep(5, sid)])
    result = engine.refresh_mapping_cache_after_ingest({'ok': True,
        'episodes_raw': [{'series_id': 'sid1'}]})
    assert result['status'] == 'updated' and result['created'] == 1
    saved = state_store.read(engine.EMBY_LIB_CACHE_FILE)
    assert len(saved['series']) == 1
    item = saved['series'][0]
    assert item['have_eps'] == 2 and item['share_eps'] == 2 and item['local_eps'] == 0
    assert item['in_share'] is True and item['in_local'] is False
    assert item['tmdb_info']['match_status'] == 'pending'
    assert not item['complete'] and item['seasons'][0]['missing'] == [2, 3, 4]
    assert saved['ts'] == 100  # no forged full TMDB scan
    change = library_updates.changes(0)
    assert change['full'] is False and change['ids'] == ['sid1']
    response = library.api_library_changes(since=0)
    assert response['series'][0]['id'] == 'sid1' and response['full'] is False


def test_new_series_union_merges_same_verified_tmdb_but_not_other(isolated, monkeypatch):
    base = {'id': 'local1', 'series_ids': ['local1'], 'name': '新剧', 'year': 2026,
            'tmdb_id': '995', 'tmdb_info': {'match_status': 'pending'},
            'have_eps': 2, 'local_eps': 2, 'share_eps': 0, 'seasons': []}
    state_store.save(engine.EMBY_LIB_CACHE_FILE, {'ts': 100, 'series': [base], 'movies': []})
    seed_meta(monkeypatch, 'share1')
    monkeypatch.setattr(morning, '_live_series_episodes', lambda sid: (
        [ep(1, sid, 'local'), ep(2, sid, 'local')] if sid == 'local1' else
        [ep(2, sid), ep(3, sid)]))
    result = engine.refresh_mapping_cache_after_ingest({'ok': True,
        'episodes_raw': [{'series_id': 'share1'}]})
    assert result['updated'] == 1 and result['created'] == 0
    data = state_store.read(engine.EMBY_LIB_CACHE_FILE)['series']
    assert len(data) == 1
    row = data[0]
    assert set(row['series_ids']) == {'local1', 'share1'}
    assert row['have_eps'] == 3 and row['local_eps'] == 2 and row['share_eps'] == 2
    assert row['in_share'] is True and row['in_local'] is True
    assert '/media/share/show' in row['paths']



@pytest.mark.parametrize('reply', [
    {'Items': [{'Type': 'Series', 'Id': 'wrong', 'Name': '新剧', 'Path': '/media/share/show'}], 'TotalRecordCount': 1},
    {'Items': [{'Type': 'Movie', 'Id': 'sid1', 'Name': '新剧', 'Path': '/media/share/show'}], 'TotalRecordCount': 1},
    {'Items': [{'Type': 'Series', 'Id': 'sid1', 'Name': '新剧', 'Path': '/media/share/show'}], 'TotalRecordCount': 4295},
    {'Items': [], 'TotalRecordCount': 0},
    {'Items': [{'Type': 'Series', 'Id': 'sid1', 'Name': '新剧', 'Path': '/media/share/show'},
               {'Type': 'Series', 'Id': 'other', 'Name': '错剧', 'Path': '/media/share/show'}], 'TotalRecordCount': 2},
    {'Items': None, 'TotalRecordCount': 1},
])
def test_new_series_bad_ids_results_never_create_or_publish(isolated, monkeypatch, reply):
    monkeypatch.setattr(engine, 'emby_request', lambda path, params=None, **kwargs: reply)
    monkeypatch.setattr(morning, '_live_series_episodes', lambda sid: [ep(1, sid)])
    before = state_store.read(engine.EMBY_LIB_CACHE_FILE)
    result = engine.refresh_mapping_cache_after_ingest({'ok': True,
        'episodes_raw': [{'series_id': 'sid1'}]})
    assert result['created'] == 0 and result['updated'] == 0
    assert state_store.read(engine.EMBY_LIB_CACHE_FILE) == before
    assert library_updates.changes(0)['version'] == 0


def test_new_series_emulator_404_details_but_ids_query_succeeds(isolated, monkeypatch):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import json
    import threading
    from urllib.parse import urlsplit, parse_qs
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urlsplit(self.path)
            if url.path != '/Items':
                self.send_error(404)
                return
            params = parse_qs(url.query)
            if params.get('Ids') != ['sid1']:
                self.send_error(400)
                return
            blob = json.dumps({'Items': [{'Id': 'sid1', 'Type': 'Series',
                'Name': '新剧', 'Path': '/media/share/show',
                'ProviderIds': {'Tmdb': '995'}, 'ProductionYear': 2026}],
                'TotalRecordCount': 1}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(blob)))
            self.end_headers()
            self.wfile.write(blob)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        monkeypatch.setattr(engine, 'EMBY_HOST', 'http://127.0.0.1:' + str(server.server_port))
        monkeypatch.setattr(engine, 'EMBY_KEY', 'fake-test-token')
        # Keep the real urllib-backed Emby request rather than a mocked one.
        monkeypatch.setattr(morning, '_live_series_episodes', lambda sid: [ep(5, sid)])
        result = engine.refresh_mapping_cache_after_ingest({'ok': True,
            'episodes_raw': [{'series_id': 'sid1'}]})
        assert result['created'] == 1
        assert state_store.read(engine.EMBY_LIB_CACHE_FILE)['series'][0]['have_eps'] == 1
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)

def test_single_series_failure_never_replaces_good_cache(isolated, monkeypatch):
    original = {'id': 'sid1', 'series_ids': ['sid1'], 'name': '安全剧', 'year': 2026,
                'have_eps': 12, 'tmdb_info': {'match_status': 'missing', 'diff': -10}}
    state_store.save(engine.EMBY_LIB_CACHE_FILE, {'ts': 100, 'series': [original], 'movies': []})
    monkeypatch.setattr(morning, '_live_series_episodes', lambda sid: (_ for _ in ()).throw(TimeoutError('offline')))
    result = engine.refresh_mapping_cache_after_ingest({'ok': True,
        'episodes_raw': [{'series_id': 'sid1'}]})
    assert result['updated'] == 0
    assert state_store.read(engine.EMBY_LIB_CACHE_FILE)['series'][0] == original
    assert library_updates.changes(0)['version'] == 0


def test_truncated_episode_page_is_not_mistaken_for_empty(monkeypatch):
    monkeypatch.setattr(engine, 'emby_request', lambda path, params, **kw:
                        {'Items': [ep(1, 's')], 'TotalRecordCount': 6001})
    with pytest.raises(RuntimeError, match='分页上限'):
        morning._live_series_episodes('s')


def test_revision_journal_is_bounded_and_falls_back_to_full(isolated):
    for i in range(70):
        library_updates.publish([f'sid{i}'])
    assert library_updates.changes(70)['ids'] == []
    assert library_updates.changes(69)['ids'] == ['sid69']
    assert library_updates.changes(0)['full'] is True
    assert library_updates.changes(999)['full'] is True
    library_updates.publish(full=True)
    assert library_updates.changes(70)['full'] is True


def test_explore_index_patch_keeps_full_scan_age(isolated, monkeypatch):
    monkeypatch.setattr(engine, '_emby_index_cache', {'ts': 50, 'data': {}, 'error_ts': 0})
    assert tmdb.patch_explore_index_from_mapping([{'id':'s1','series_ids':['s1'],
            'tmdb_id':'995','name':'新剧','year':2026,'path':'/media/share/show',
            'paths':['/media/share/show'],'in_share':True,'in_local':False,
            'has_image':True}])
    row = engine._emby_index_cache['data']['tv:995']
    assert row['id'] == 's1' and row['in_share'] and row['has_image']
    assert engine._emby_index_cache['ts'] == 50


def test_changes_endpoint_auth_and_network_isolation(isolated, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.routers.deps import auth
    client = TestClient(app)
    assert client.get('/api/library/changes?since=0').status_code == 401
    assert client.get('/api/library/events').status_code == 401
    app.dependency_overrides[auth] = lambda: True
    try:
        monkeypatch.setattr(engine, 'emby_request', lambda *a, **kw:
                            (_ for _ in ()).throw(AssertionError('network request')))
        assert client.get('/api/library/changes?since=-1').status_code == 400
        assert client.get('/api/library/changes?since=0').json()['version'] == 0
        library_updates.publish(['sid1'])
        # Unknown changed IDs must request a full reconcile; do not fabricate rows.
        answer = client.get('/api/library/changes?since=0').json()
        assert answer['full'] and answer['series'] == []
    finally:
        app.dependency_overrides.pop(auth, None)
