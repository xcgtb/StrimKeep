import json

import httpx
import pytest
from fastapi.testclient import TestClient

from strimkeep.main import create_app
from strimkeep.resource_discovery import merge_results, resource_items, validate_base
from strimkeep.store import Store
from test_app import FakeCloud


def test_search_normalizes_links_and_keeps_their_origins():
    assert validate_base('http://192.168.1.10:8888/') == 'http://192.168.1.10:8888'
    for value in ('', 'http://example.org', 'http://u:p@192.168.1.10', 'https://example.org/path',
                  'https://example.org\\@192.168.1.10', 'http://169.254.169.254', 'file:///tmp/x'):
        with pytest.raises(ValueError):
            validate_base(value)
    tg = resource_items(
        'https://115cdn.com/s/abc123 提取码：abcd\nhttps://example.org/details\nmagnet:?dn=movie',
        '示例片名', 'TG')
    site = resource_items('https://115.com/s/abc123', '示例片名', '盘搜')
    magnet = resource_items('magnet:?xt=urn:btih:' + 'A' * 32, '示例', '盘搜')
    same_magnet = resource_items('magnet:?xt=urn:btih:' + '0' * 40, '示例', '另一个站')
    items, truncated = merge_results([site + magnet, tg + same_magnet])
    assert not truncated
    assert [(item['key'], item['source_names']) for item in items] == [
        ('share:abc123', ['盘搜', 'TG']), ('magnet:' + '0' * 40, ['盘搜', '另一个站'])]
    assert items[0]['link'] == 'https://115.com/s/abc123?password=abcd'


def test_aggregation_and_legacy_token_removal(tmp_path, monkeypatch):
    monkeypatch.setenv('ADMIN_USERNAME', 'admin')
    monkeypatch.setenv('ADMIN_PASSWORD', 'test-password-1234')
    data = tmp_path / 'data'
    previous = Store(data, 'admin', 'test-password-1234')
    previous.set_many({'resource_discovery_base': 'https://legacy.example',
                       'resource_discovery_token': 'legacy-token'})
    previous.close()
    app = create_app(data, tmp_path / 'strm', FakeCloud)
    seen = []

    def handler(request):
        seen.append(request.url.host)
        assert request.url.path == '/api/search'
        assert 'authorization' not in request.headers
        assert json.loads(request.content) == {
            'kw': '示例片名', 'res': 'merge', 'cloud_types': ['115', 'magnet', 'ed2k']}
        if request.url.host == 'broken.example':
            return httpx.Response(302, headers={'Location': 'https://elsewhere.example'})
        return httpx.Response(200, json={'code': 0, 'data': {'total': 2, 'merged_by_type': {
            '115': [{'url': 'https://115.com/s/example', 'password': 'abcd', 'note': '4K 版本'}],
            'magnet': [{'url': 'magnet:?xt=urn:btih:' + 'a' * 40}],
        }}})

    async def tg_search(name, title):
        assert (name, title) == ('movies_example', '示例片名')
        return [{'text': '示例片名\n1080p 版本', 'links': ['https://115.com/s/example'],
                 'published_at': '2026-11-26T10:00:00+00:00'}]

    settings = '/api/v1/explore/resource-config'
    search = '/api/v1/explore/resources'
    body = {'title': '示例片名', 'tmdb_id': 42, 'media_type': 'movie', 'year': '2026'}
    with TestClient(app) as client:
        assert client.get(settings).status_code == 401
        login = client.post('/api/v1/auth/login', json={'username': 'admin', 'password': 'test-password-1234'})
        client.headers['X-CSRF-Token'] = login.json()['csrf']
        assert client.post(search, json=body).status_code == 409
        assert app.state.store.get('resource_discovery_token', None) is None
        assert app.state.store.get('resource_discovery_base', None) is None
        app.state.store.set_many({'channel_monitor': {'channels': [{'name': 'movies_example'}]}})
        monkeypatch.setattr(app.state.organizer.telegram, 'search_channel', tg_search)
        assert client.put(settings, json={'telegram_channels': ['unknown_channel']}).status_code == 422
        sites = [
            {'name': '盘搜', 'base_url': 'https://search.example'},
            {'name': '不可用', 'base_url': 'https://broken.example'},
            {'name': '已停用', 'base_url': 'https://disabled.example', 'enabled': False},
        ]
        config = client.put(settings, json={'sites': sites, 'telegram_channels': ['movies_example']})
        assert config.status_code == 200
        assert 'legacy' not in config.text
        assert config.json()['sites'][2]['enabled'] is False
        assert client.put(settings, json={'sites': [sites[0], {**sites[0], 'name': '不同名称'}]}).status_code == 422
        original_http = app.state.http
        mock_http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        app.state.http = mock_http
        try:
            response = client.post(search, json=body)
            assert response.status_code == 200
            result = response.json()
            assert result['items'][0]['source_names'] == ['盘搜', 'movies_example']
            assert result['items'][0]['link'] == 'https://115.com/s/example?password=abcd'
            assert result['items'][1]['link_type'] == 'magnet'
            assert 'HTTP 302' in result['sources'][1]['error']
            assert set(seen) == {'search.example', 'broken.example'}
        finally:
            app.state.http = original_http
            client.portal.call(mock_http.aclose)
        assert client.post(search + '/resolve', json={}).status_code in (404, 405)



def test_removed_site_credentials_are_cleaned_without_losing_torznab(tmp_path, monkeypatch):
    monkeypatch.setenv('ADMIN_USERNAME', 'admin')
    monkeypatch.setenv('ADMIN_PASSWORD', 'test-password-1234')
    data = tmp_path / 'data'
    site = {'kind': 'torznab', 'name': 'Tracker', 'base_url': 'https://tracker.example/1/api'}
    previous = Store(data, 'admin', 'test-password-1234')
    previous.set_many({'resource_search_sources': {
        'sites': [{**site, 'api_key': 'tracker-key'}],
        're0': {'enabled': True, 'client_id': 'retired-app', 'app_secret': 'retired-secret',
                'access_token': 'retired-token', 'refresh_token': 'retired-refresh'},
    }})
    previous.close()
    app = create_app(data, tmp_path / 'strm', FakeCloud)
    seen = []
    magnet = 'magnet:?xt=urn:btih:' + 'a' * 40

    def handler(request):
        seen.append((request.method, request.url.host, request.url.path))
        assert request.url.params['apikey'] == 'tracker-key'
        assert request.url.params['q'] == 'Example'
        assert request.url.params['t'] == 'search'
        assert 'authorization' not in request.headers
        xml = f"""<rss xmlns:torznab="http://torznab.com/schemas/2015/feed">
          <channel>
            <item><title>Torrent with magnet</title>
              <torznab:attr name="magneturl" value="{magnet}"/>
            </item>
            <item><title>Torrent without magnet</title>
              <enclosure url="https://tracker.example/dl?secret=private" type="application/x-bittorrent"/>
            </item>
          </channel>
        </rss>"""
        return httpx.Response(200, text=xml)

    with TestClient(app) as client:
        login = client.post('/api/v1/auth/login', json={'username': 'admin', 'password': 'test-password-1234'})
        client.headers['X-CSRF-Token'] = login.json()['csrf']
        settings = '/api/v1/explore/resource-config'
        assert 're0' not in app.state.store.get('resource_search_sources')
        config = client.get(settings)
        assert config.json()['sites'][0]['api_key_configured'] is True
        assert 're0' not in config.json() and 'tracker-key' not in config.text
        assert client.put(settings, json={'sites': [site]}).status_code == 200
        assert client.post('/api/v1/explore/re0/authorize', json={}).status_code in (404, 405)
        original_http = app.state.http
        mock_http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        app.state.http = mock_http
        try:
            response = client.post('/api/v1/explore/resources',
                                   json={'title': 'Example', 'tmdb_id': 42, 'media_type': 'tv'})
        finally:
            app.state.http = original_http
            client.portal.call(mock_http.aclose)
        assert response.status_code == 200
        [torrent] = response.json()['items']
        assert torrent['link'] == magnet
        assert 'private' not in response.text
        assert seen == [('GET', 'tracker.example', '/1/api')]
