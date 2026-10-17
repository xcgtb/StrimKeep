"""P5 integration invariants (run from StrimKeep repository root)."""
import time
import pytest
from fastapi import HTTPException
from app import engine, morning, library_pagination, state_store
from app.routers import library
from dev.tests.test_library_fact_counts import cached, series, write_cache


@pytest.fixture(autouse=True)
def clear_paged_cache():
    library_pagination._cache['key'] = None
    library_pagination._cache['until'] = 0
    yield
    library_pagination._cache['key'] = None


def test_page_never_leaks_paths_and_retains_zero(cached):
    row = series(0)
    row.update(path='/local/秘密路径', paths=['/local/秘密路径/分集.strm'])
    write_cache([row])
    out = library_pagination.page(media='series', limit=2)
    assert out['status'] == 'success' and out['total'] == 1
    assert out['rows'][0]['have_eps'] == 0
    assert 'path' not in out['rows'][0] and 'paths' not in out['rows'][0]
    assert out['has_more'] is False


def test_movie_detail_reports_per_library_versions(cached, monkeypatch):
    movie = {'id': 'm', 'name': '电影', 'ids': ['m', 'm2'], 'in_local': True, 'in_share': True,
             'paths': ['/local/a.strm', '/share/a.strm', '/share/a.strm']}
    monkeypatch.setattr(library_pagination, '_read', lambda: True)
    monkeypatch.setitem(library_pagination._cache, 'movies_by_id', {'m': movie})
    monkeypatch.setitem(library_pagination._cache, 'meta', {})
    monkeypatch.setattr(engine.EMBY_PATHS, 'lib_of', lambda path, fallback=False: 'local' if path.startswith('/local/') else 'share')
    item = library_pagination.detail('movies', 'm')['item']
    assert item['local_versions'] == item['share_versions'] == 1


def test_global_status_sorting_and_paging_no_duplication(cached):
    a = series(3); a['id'] = 'a'; a['name'] = 'A'
    b = series(8); b['id'] = 'b'; b['name'] = 'B'; b['tmdb_info']['match_status'] = 'aligned'
    c = series(0); c['id'] = 'c'; c['name'] = 'C'; c['tmdb_info']['diff'] = -99
    write_cache([b, a, c])
    first = library_pagination.page(media='series', limit=1)
    second = library_pagination.page(media='series', offset=1, limit=1)
    third = library_pagination.page(media='series', offset=2, limit=1)
    assert first['total'] == 3
    assert [x['rows'][0]['id'] for x in (first, second, third)] == ['c','a','b']
    assert first['has_more'] and not third['has_more']
    assert library_pagination.page(media='series', status='aligned')['total'] == 1


def test_manual_done_and_count_revision_invalidate_pages(cached, monkeypatch):
    monkeypatch.setattr(morning, 'read_manual_done', lambda: state_store.read(engine.MANUAL_DONE_FILE, {}))
    write_cache([series(3)], facts_ts=100)
    first = library_pagination.page()
    write_cache([series(20)], facts_ts=200)
    second = library_pagination.page()
    assert first['rows'][0]['have_eps'] == 3 and second['rows'][0]['have_eps'] == 20
    assert first['facts_version'] != second['facts_version']
    state_store.save(engine.MANUAL_DONE_FILE, {'s1': {}})
    marked = library_pagination.page()
    assert marked['rows'][0]['_md'] is True
    assert marked['rows'][0]['tmdb_info']['match_status'] == 'aligned'


def test_member_scope_and_category_use_global_data(cached):
    one = series(); one['id'] = 'a'; one.update(in_local=True,in_share=False,path='/media/国产剧/片名')
    two = series(); two['id'] = 'b'; two.update(in_local=False,in_share=True,path='/media/欧美剧/片名')
    write_cache([one,two])
    local = library_pagination.page(scope='local')
    share = library_pagination.page(scope='share')
    cn = library_pagination.page(category='国产剧', category_mode='seg')
    assert [c['id'] for c in local['rows']] == ['a']
    assert [c['id'] for c in share['rows']] == ['b']
    assert [c['id'] for c in cn['rows']] == ['a']


def test_detail_is_authoritative_and_page_is_only_projection(cached):
    row = series(4)
    row.update(path='/media/local/title', seasons=[{'season':1,'episodes':4}])
    write_cache([row])
    listing = library_pagination.page()
    detail = library_pagination.detail('series', row['id'])
    assert 'seasons' not in listing['rows'][0]
    assert detail['item']['path'] == '/media/local/title'
    assert detail['item']['seasons'][0]['episodes'] == 4
    assert library_pagination.detail('series', 'unknown')['status'] == 'notfound'


@pytest.mark.parametrize('kwargs', [dict(media='unknown'),dict(limit=0),dict(limit=1000),
    dict(offset=-1),dict(category='x'*81),dict(keyword='a'*121),dict(scope='secret')])
def test_pagination_rejects_unbounded_and_invalid_queries(kwargs):
    with pytest.raises(ValueError):
        library_pagination.page(**kwargs)


def test_router_requires_authentication(cached):
    from app.main import app
    from app.routers.deps import auth
    from fastapi.testclient import TestClient
    write_cache([series()])
    old = app.dependency_overrides.pop(auth,None)
    try:
        client = TestClient(app)
        assert client.get('/api/emby/library/page').status_code == 401
        assert client.get('/api/emby/library/item?media=series&id=s1').status_code == 401
        app.dependency_overrides[auth] = lambda: None
        assert client.get('/api/emby/library/page?limit=1').status_code == 200
        assert client.get('/api/emby/library/item?media=series&id=missing').status_code == 404
        assert client.get('/api/emby/library/page?limit=1000').status_code == 400
    finally:
        app.dependency_overrides.pop(auth,None)
        if old is not None:
            app.dependency_overrides[auth] = old
