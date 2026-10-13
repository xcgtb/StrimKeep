"""Read-only Mapping HTTP responses are never cacheable, including nocache."""
from fastapi.testclient import TestClient
from app.main import app
from app.routers.deps import auth
from app import library_pagination


def test_page_nocache_is_no_store(monkeypatch):
    monkeypatch.setattr(library_pagination, 'page', lambda **kwargs: {'status': 'nocache'})
    previous = app.dependency_overrides.get(auth)
    app.dependency_overrides[auth] = lambda: None
    try:
        client = TestClient(app)
        r = client.get('/api/emby/library/page?media=series&limit=1')
        assert r.status_code == 200
        assert r.json()['status'] == 'nocache'
        assert 'no-store' in r.headers.get('cache-control', '')
        assert 'Authorization' in r.headers.get('vary', '')
    finally:
        if previous is None:
            app.dependency_overrides.pop(auth, None)
        else:
            app.dependency_overrides[auth] = previous


def test_one_revision_race_is_retried(monkeypatch):
    from app import library_response
    rev = {'n':0}
    def key():
        rev['n'] += 1
        return 1 if rev['n'] <= 2 else 2
    monkeypatch.setattr(library_response, '_key', key)
    monkeypatch.setattr(library_pagination.engine, 'cached_library_view',
                        lambda: {'series': [], 'movies': [], 'stats': {}, 'facts_version':'v2'})
    monkeypatch.setattr(library_pagination.morning, 'read_manual_done', lambda: {})
    library_pagination._cache.update(key=None, until=0)
    assert library_pagination.page()['status'] == 'success'
    assert rev['n'] >= 4
