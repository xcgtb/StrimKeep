"""影视探索搜索：搜索词里的年份要拆出来，带年份搜不到时自动放宽一次。

现场问题：输入「剧名 年份」点搜索，TMDB 收到整串 query 直接 0 结果；
再点一次搜索还是同样的串，页面就永远「搜不到」。
"""
import httpx
from fastapi.testclient import TestClient

from strimkeep import explore_api
from strimkeep.main import create_app
from strimkeep.organizer import Organizer
from test_organize import MemoryCloud

MOVIE = {'id': 438631, 'title': '沙丘', 'poster_path': '/d.jpg',
         'vote_average': 8.0, 'release_date': '2021-09-15'}


def make_app(tmp_path, monkeypatch):
    monkeypatch.setenv('ADMIN_USERNAME', 'admin')
    monkeypatch.setenv('ADMIN_PASSWORD', 'test-password-1234')
    monkeypatch.setattr(Organizer, 'start', lambda self: None)
    monkeypatch.setattr(explore_api, '_emby_cache', {'at': 0, 'index': {}})
    return create_app(tmp_path / 'data', tmp_path / 'strm', MemoryCloud)


def call(app, handler, params):
    with TestClient(app) as client:
        login = client.post('/api/v1/auth/login',
                            json={'username': 'admin', 'password': 'test-password-1234'})
        assert login.status_code == 200
        app.state.store.set_many({'tmdb_key': 'k'})
        api = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        app.state.http = api
        return client.get('/api/v1/explore/list', params=params)


def test_search_splits_trailing_year_into_filter(tmp_path, monkeypatch):
    seen = []

    def handler(request):
        if request.url.host != 'api.themoviedb.org':
            return httpx.Response(404, json={})
        seen.append(dict(request.url.params))
        return httpx.Response(200, json={'results': [MOVIE], 'total_pages': 1, 'total_results': 1})

    response = call(make_app(tmp_path, monkeypatch), handler,
                    {'kind': 'movie', 'q': '沙丘 2021'})
    assert response.status_code == 200, response.text
    assert response.json()['items'], response.text
    search = [params for params in seen if params.get('query')]
    assert search, seen
    assert search[0]['query'] == '沙丘'
    assert search[0]['primary_release_year'] == '2021'


def test_year_without_results_is_relaxed_once(tmp_path, monkeypatch):
    seen = []

    def handler(request):
        if request.url.host != 'api.themoviedb.org':
            return httpx.Response(404, json={})
        params = dict(request.url.params)
        seen.append(params)
        if params.get('primary_release_year'):
            return httpx.Response(200, json={'results': [], 'total_pages': 0, 'total_results': 0})
        return httpx.Response(200, json={'results': [MOVIE], 'total_pages': 1, 'total_results': 1})

    response = call(make_app(tmp_path, monkeypatch), handler,
                    {'kind': 'movie', 'q': '沙丘 2019'})
    assert response.status_code == 200, response.text
    assert response.json()['items'], '去掉年份后应该能搜到'
    searches = [params for params in seen if params.get('query')]
    assert searches[0].get('primary_release_year') == '2019'
    assert 'primary_release_year' not in searches[-1], searches


def test_name_search_allows_year_but_rejects_other_filters(tmp_path, monkeypatch):
    def handler(request):
        if request.url.host != 'api.themoviedb.org':
            return httpx.Response(404, json={})
        return httpx.Response(200, json={'results': [MOVIE], 'total_pages': 1, 'total_results': 1})

    app = make_app(tmp_path, monkeypatch)
    ok = call(app, handler, {'kind': 'movie', 'q': '沙丘', 'year': 2021})
    assert ok.status_code == 200, ok.text
    blocked = call(app, handler, {'kind': 'movie', 'q': '沙丘', 'genre': 28})
    assert blocked.status_code == 422
