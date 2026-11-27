"""影视探索搜索：搜索词里的年份要拆出来，带年份搜不到时自动放宽一次。

现场问题：输入「剧名 年份」点搜索，TMDB 收到整串 query 直接 0 结果；
再点一次搜索还是同样的串，页面就永远「搜不到」。
"""
import httpx
import pytest
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


def test_expired_browse_cursor_restarts_instead_of_repeating_a_deep_page(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    raw = [1] * 7 + list(range(2, 801))

    def handler(request):
        page = int(request.url.params['page'])
        items = [{'id': value, 'title': str(value)} for value in raw[(page - 1) * 20:page * 20]]
        return httpx.Response(200, json={'results': items, 'total_results': len(raw),
                                         'total_pages': (len(raw) + 19) // 20})

    with TestClient(app) as client:
        client.post('/api/v1/auth/login', json={'username': 'admin', 'password': 'test-password-1234'})
        app.state.store.set_many({'tmdb_key': 'k'})
        app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        first = client.get('/api/v1/explore/list', params={'per_page': 45}).json()
        cursor = first['cursor']
        for number in range(2, 7):
            page = client.get('/api/v1/explore/list',
                              params={'per_page': 45, 'page': number, 'cursor': cursor}).json()
            assert page['page'] == number and not page['restarted']
        for genre in range(1, 9):
            client.get('/api/v1/explore/list', params={'genre': genre})
        fresh = client.get('/api/v1/explore/list', params={'per_page': 45, 'page': 7, 'cursor': cursor}).json()
        assert fresh['restarted'] and fresh['page'] == 1 and fresh['cursor'] != cursor
        assert fresh['items'] == first['items']


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


@pytest.mark.parametrize('per_page', [30, 45])
def test_deduplication_fills_display_pages_without_repeating_works(tmp_path, monkeypatch, per_page):
    app = make_app(tmp_path, monkeypatch)
    raw = list(range(1, 96))
    raw[20:20] = [19, 20, None]
    raw[44:44] = [40, None, 38]
    raw[61:61] = [53, 54]
    requested = []

    def handler(request):
        index = int(request.url.params['page'])
        requested.append(index)
        start = (index - 1) * 20
        return httpx.Response(200, json={
            'results': [{'id': value, 'title': f'作品 {value}'} if value is not None else {}
                        for value in raw[start:start + 20]],
            'total_pages': 6, 'total_results': len(raw),
        })

    with TestClient(app) as client:
        client.post('/api/v1/auth/login', json={'username': 'admin', 'password': 'test-password-1234'})
        app.state.store.set_many({'tmdb_key': 'k'})
        http = app.state.http
        fake = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        app.state.http = fake
        try:
            expected = list(range(1, 96))
            for index, offset in enumerate(range(0, len(expected), per_page), 1):
                result = client.get('/api/v1/explore/list', params={'per_page': per_page, 'page': index}).json()
                assert result['page'] == index
                assert [item['id'] for item in result['items']] == expected[offset:offset + per_page]
                assert result['has_more'] is (offset + per_page < len(expected))
                assert all(item['poster'] == '' for item in result['items'])
            again = client.get('/api/v1/explore/list', params={'per_page': per_page, 'page': 1}).json()
            assert [item['id'] for item in again['items']] == expected[:per_page]
            beyond = client.get('/api/v1/explore/list', params={'per_page': per_page, 'page': index + 1}).json()
            assert beyond['page'] == index and not beyond['has_more']
            assert [item['id'] for item in beyond['items']] == expected[(index - 1) * per_page:]
            assert sorted(requested) == list(range(1, 7))
        finally:
            app.state.http = http
            client.portal.call(fake.aclose)


def test_multiple_genres_and_regions_use_or_inside_each_group(tmp_path, monkeypatch):
    settings = []

    def handler(request):
        settings.append((request.url.path, request.url.params))
        return httpx.Response(200, json={'results': [MOVIE], 'total_pages': 1, 'total_results': 1})

    app = make_app(tmp_path, monkeypatch)
    response = call(app, handler, [
        ('genre', 28), ('genre', 18), ('genre', 28),
        ('region', 'KR'), ('region', 'JP'), ('region', 'KR'), ('year', 2021),
    ])
    assert response.status_code == 200, response.text
    path, params = settings[0]
    assert path.endswith('/discover/movie')
    assert params['with_genres'] == '18|28'
    assert params['with_origin_country'] == 'JP|KR'
    assert params['primary_release_year'] == '2021'
