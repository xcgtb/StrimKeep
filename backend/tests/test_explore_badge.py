"""探索页角标：电影与剧集 TMDB ID 撞号时，剧集不能被电影覆盖成「在库」。"""
import httpx
from fastapi.testclient import TestClient

from strimkeep import explore_api
from strimkeep.main import create_app
from strimkeep.organizer import Organizer
from test_organize import MemoryCloud


def test_tv_badge_keeps_series_when_movie_shares_tmdb_id(tmp_path, monkeypatch):
    monkeypatch.setenv('ADMIN_USERNAME', 'admin')
    monkeypatch.setenv('ADMIN_PASSWORD', 'test-password-1234')
    monkeypatch.setattr(Organizer, 'start', lambda self: None)
    monkeypatch.setattr(explore_api, '_emby_cache', {'at': 0, 'index': {}})
    app = create_app(tmp_path / 'data', tmp_path / 'strm', MemoryCloud)

    def handler(request):
        host, path = request.url.host, request.url.path
        if host == 'api.themoviedb.org':
            if path.endswith('/discover/tv'):
                return httpx.Response(200, json={'results': [{
                    'id': 1480, 'name': '甜心俏佳人', 'poster_path': '/p.jpg',
                    'vote_average': 7.0, 'first_air_date': '1997-09-08'}], 'total_pages': 1})
            if path.endswith('/tv/1480'):
                return httpx.Response(200, json={'number_of_episodes': 10})
            return httpx.Response(404, json={})
        if path.endswith('/Items'):
            kind = request.url.params.get('IncludeItemTypes')
            items = {'Series': [{'Id': 'S1', 'ProviderIds': {'Tmdb': '1480'}}],
                     'Movie': [{'Id': 'M1', 'ProviderIds': {'Tmdb': '1480'}}]}.get(kind, [])
            return httpx.Response(200, json={'Items': items, 'TotalRecordCount': len(items)})
        if path.endswith('/Shows/S1/Episodes'):
            return httpx.Response(200, json={'TotalRecordCount': 10, 'Items': []})
        return httpx.Response(404, json={})

    with TestClient(app) as client:
        login = client.post('/api/v1/auth/login',
                            json={'username': 'admin', 'password': 'test-password-1234'})
        assert login.status_code == 200
        store = app.state.organizer.store
        store.set_many({'tmdb_key': 'k', 'emby_url': 'http://emby.local', 'emby_key': 'e'})
        api = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        app.state.http = api
        app.state.organizer.http = api
        data = client.get('/api/v1/explore/list', params={'kind': 'tv', 'sort': 'popular'}).json()

    item = data['items'][0]
    assert item['badge'] == '已完整' and item['ratio'] == '10/10', item


def test_duplicate_copies_do_not_make_ratio_exceed_total(tmp_path, monkeypatch):
    """库里同一部剧有两套副本时，Emby 会报 16 集；角标必须按去重后的 8/8 显示。"""
    monkeypatch.setenv('ADMIN_USERNAME', 'admin')
    monkeypatch.setenv('ADMIN_PASSWORD', 'test-password-1234')
    monkeypatch.setattr(Organizer, 'start', lambda self: None)
    monkeypatch.setattr(explore_api, '_emby_cache', {'at': 0, 'index': {}})
    app = create_app(tmp_path / 'data', tmp_path / 'strm', MemoryCloud)

    def handler(request):
        host, path = request.url.host, request.url.path
        if host == 'api.themoviedb.org':
            if path.endswith('/discover/tv'):
                return httpx.Response(200, json={'results': [{
                    'id': 1481, 'name': '重复副本剧', 'poster_path': '/p.jpg',
                    'vote_average': 8.0, 'first_air_date': '2020-01-01'}], 'total_pages': 1})
            if path.endswith('/tv/1481'):
                return httpx.Response(200, json={'number_of_episodes': 8})
            return httpx.Response(404, json={})
        if path.endswith('/Shows/S2/Episodes'):
            if request.url.params.get('Fields'):
                items = [{'ParentIndexNumber': 1, 'IndexNumber': i} for i in range(1, 9)] * 2
                return httpx.Response(200, json={'Items': items, 'TotalRecordCount': len(items)})
            return httpx.Response(200, json={'Items': [], 'TotalRecordCount': 16})
        if path.endswith('/Items'):
            kind = request.url.params.get('IncludeItemTypes')
            items = {'Series': [{'Id': 'S2', 'ProviderIds': {'Tmdb': '1481'}}]}.get(kind, [])
            return httpx.Response(200, json={'Items': items, 'TotalRecordCount': len(items)})
        return httpx.Response(404, json={})

    with TestClient(app) as client:
        assert client.post('/api/v1/auth/login',
                           json={'username': 'admin', 'password': 'test-password-1234'}).status_code == 200
        app.state.organizer.store.set_many(
            {'tmdb_key': 'k', 'emby_url': 'http://emby.local', 'emby_key': 'e'})
        api = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        app.state.http = api
        app.state.organizer.http = api
        data = client.get('/api/v1/explore/list', params={'kind': 'tv', 'sort': 'popular'}).json()

    item = data['items'][0]
    assert item['badge'] == '已完整' and item['ratio'] == '8/8', item


def test_explore_pagination_keeps_every_item_and_unknown_emby(tmp_path, monkeypatch):
    monkeypatch.setenv('ADMIN_USERNAME','admin'); monkeypatch.setenv('ADMIN_PASSWORD','test-password-1234')
    monkeypatch.setattr(Organizer,'start',lambda self: None)
    monkeypatch.setattr(explore_api, '_emby_cache', {'at':0,'index':{}})
    app=create_app(tmp_path/'data',tmp_path/'strm',MemoryCloud)
    requests=[]
    def handler(request):
        page=int(request.url.params.get('page','1')); requests.append(page)
        start=(page-1)*20
        return httpx.Response(200,json={'results':[{'id':i,'title':str(i)} for i in range(start,start+20)],'total_results':95,'total_pages':5})
    with TestClient(app) as client:
        login=client.post('/api/v1/auth/login',json={'username':'admin','password':'test-password-1234'})
        assert login.status_code==200
        app.state.organizer.store.set_many({'tmdb_key':'k'})
        original=app.state.http; app.state.http=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        one=client.get('/api/v1/explore/list',params={'kind':'movie','per_page':30,'page':1}).json()
        two=client.get('/api/v1/explore/list',params={'kind':'movie','per_page':30,'page':2}).json()
        assert [x['id'] for x in one['items']]==list(range(30))
        assert [x['id'] for x in two['items']]==list(range(30,60))
        assert one['total_pages']==4 and set(requests)=={1,2,3}
        assert all(x['badge']=='状态未知' for x in one['items'])
        import asyncio
        asyncio.run(app.state.http.aclose());app.state.http=original


def test_truncated_emby_index_never_claims_absent(tmp_path, monkeypatch):
    monkeypatch.setenv('ADMIN_USERNAME','admin'); monkeypatch.setenv('ADMIN_PASSWORD','test-password-1234')
    monkeypatch.setattr(Organizer,'start',lambda self: None)
    monkeypatch.setattr(explore_api, '_emby_cache', {'at':0,'index':{}})
    app=create_app(tmp_path/'data',tmp_path/'strm',MemoryCloud)
    def handler(request):
        if request.url.host=='api.themoviedb.org':
            return httpx.Response(200,json={'results':[{'id':10,'title':'A'},{'id':11,'title':'B'}], 'total_results':2,'total_pages':1})
        if request.url.params.get('IncludeItemTypes')=='Movie':
            return httpx.Response(200,json={'Items':[{'ProviderIds':{'Tmdb':'10'}}], 'TotalRecordCount':2})
        return httpx.Response(503)
    with TestClient(app) as client:
        client.post('/api/v1/auth/login',json={'username':'admin','password':'test-password-1234'})
        app.state.organizer.store.set_many({'tmdb_key':'k','emby_url':'http://emby.local','emby_key':'secret'})
        original=app.state.http;app.state.http=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        result=client.get('/api/v1/explore/list',params={'kind':'movie','per_page':20}).json()['items']
        assert {x['id']:x['badge'] for x in result}=={10:'已入库',11:'状态未知'}
        import asyncio
        asyncio.run(app.state.http.aclose());app.state.http=original


def test_explore_combines_sort_and_filters(tmp_path, monkeypatch):
    monkeypatch.setenv('ADMIN_USERNAME', 'admin')
    monkeypatch.setenv('ADMIN_PASSWORD', 'test-password-1234')
    monkeypatch.setattr(Organizer, 'start', lambda self: None)
    app = create_app(tmp_path / 'data', tmp_path / 'strm', MemoryCloud)
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={'results': [], 'total_results': 0})
    with TestClient(app) as client:
        client.post('/api/v1/auth/login', json={'username': 'admin', 'password': 'test-password-1234'})
        app.state.organizer.store.set_many({'tmdb_key': 'k'})
        original = app.state.http
        app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        result = client.get('/api/v1/explore/list', params={
            'kind': 'movie', 'sort': 'now_playing',
            'region': 'CN', 'genre': 18, 'year': 2026, 'per_page': 20})
        assert result.status_code == 200
        request = calls[0]
        assert request.url.path.endswith('/discover/movie')
        assert request.url.params['with_origin_country'] == 'CN'
        assert request.url.params['with_genres'] == '18'
        assert request.url.params['primary_release_year'] == '2026'
        assert request.url.params['sort_by'] == 'popularity.desc'
        assert 'primary_release_date.gte' in request.url.params
        assert 'primary_release_date.lte' in request.url.params
        assert client.get('/api/v1/explore/list', params={'sort':'trending','region':'CN'}).status_code == 422
        assert client.get('/api/v1/explore/list', params={'q':'abc','region':'CN'}).status_code == 422
        upcoming = client.get('/api/v1/explore/list', params={'kind':'movie','sort':'upcoming','region':'CN','per_page':20})
        assert upcoming.status_code == 200
        assert calls[-1].url.params['with_origin_country'] == 'CN'
        assert 'primary_release_date.lte' not in calls[-1].url.params
        assert client.get('/api/v1/explore/list', params={'kind':'tv','sort':'upcoming'}).status_code == 422
        import asyncio
        asyncio.run(app.state.http.aclose()); app.state.http = original


def test_series_cache_expires_and_bounds_entries():
    cache = explore_api._SeriesCountCache(limit=2, ttl=10)
    cache.put(('a', 'one'), 0, 1)
    cache.put(('a', 'two'), 2, 2)
    assert cache.get(('a', 'one'), 3) == 0
    cache.put(('b', 'three'), 3, 3)
    assert cache.get(('a', 'two'), 3) is None
    assert cache.get(('a', 'one'), 3) == 0
    assert len(cache.entries) == 2
    assert cache.get(('a', 'one'), 11) is None
    assert len(cache.entries) == 1
    cache.put(('c', 'four'), 4, 14)
    assert list(cache.entries) == [('c', 'four')]


def test_series_counts_follow_emby_connection_change(tmp_path, monkeypatch):
    monkeypatch.setenv('ADMIN_USERNAME', 'admin')
    monkeypatch.setenv('ADMIN_PASSWORD', 'test-password-1234')
    monkeypatch.setattr(Organizer, 'start', lambda self: None)
    monkeypatch.setattr(explore_api, '_emby_cache', {'at': 0, 'index': {}})
    app = create_app(tmp_path / 'data', tmp_path / 'strm', MemoryCloud)
    calls = []
    def handler(request):
        host, path = request.url.host, request.url.path
        if host == 'api.themoviedb.org':
            if path.endswith('/discover/tv'):
                return httpx.Response(200, json={'results': [{'id': 1480, 'name': '剧'}], 'total_pages': 1})
            return httpx.Response(200, json={'number_of_episodes': 8})
        if path.endswith('/Items'):
            items = [{'Id': 'S1', 'ProviderIds': {'Tmdb': '1480'}}] if request.url.params.get('IncludeItemTypes') == 'Series' else []
            return httpx.Response(200, json={'Items': items, 'TotalRecordCount': len(items)})
        if path.endswith('/Shows/S1/Episodes'):
            count = 8 if host == 'emby1.local' else 6
            calls.append((host, bool(request.url.params.get('Fields'))))
            if request.url.params.get('Fields'):
                items = [{'ParentIndexNumber': 1, 'IndexNumber': i} for i in range(1, count + 1)] * 2
                return httpx.Response(200, json={'Items': items, 'TotalRecordCount': len(items)})
            return httpx.Response(200, json={'Items': [], 'TotalRecordCount': count * 2})
        return httpx.Response(404)
    with TestClient(app) as client:
        assert client.post('/api/v1/auth/login', json={'username': 'admin', 'password': 'test-password-1234'}).status_code == 200
        store = app.state.organizer.store
        store.set_many({'tmdb_key': 'k', 'emby_url': 'http://emby1.local', 'emby_key': 'e'})
        api = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        app.state.http = api
        app.state.organizer.http = api
        first = client.get('/api/v1/explore/list', params={'kind': 'tv'}).json()['items'][0]
        again = client.get('/api/v1/explore/list', params={'kind': 'tv'}).json()['items'][0]
        store.set_many({'emby_url': 'http://emby2.local'})
        second = client.get('/api/v1/explore/list', params={'kind': 'tv'}).json()['items'][0]
        assert (first['ratio'], again['ratio'], second['ratio']) == ('8/8', '8/8', '6/8')
        assert calls == [('emby1.local', False), ('emby1.local', True), ('emby2.local', False), ('emby2.local', True)]
