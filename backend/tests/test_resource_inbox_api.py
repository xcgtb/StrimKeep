from fastapi.testclient import TestClient

from strimkeep.main import create_app


class FakeCloud:
    def __init__(self, store):
        self.store = store

    async def close(self):
        pass

    async def status(self):
        return {'connected': False}


def test_webhook_enters_the_inbox(tmp_path, monkeypatch):
    monkeypatch.setenv('ADMIN_USERNAME', 'admin')
    monkeypatch.setenv('ADMIN_PASSWORD', 'test-password-1234')
    app = create_app(tmp_path / 'data', tmp_path / 'strm', FakeCloud)
    with TestClient(app) as client:
        login = client.post('/api/v1/auth/login',
                            json={'username': 'admin', 'password': 'test-password-1234'})
        assert login.status_code == 200
        client.headers['X-CSRF-Token'] = login.json()['csrf']
        token = client.post('/api/v1/resources/webhook-token').json()['token']
        payload = {
            'title': 'Webhook Movie',
            'guid': 'webhook-1',
            'magnetUrl': 'magnet:?xt=urn:btih:ABCDEF123456&dn=Movie',
        }
        response = client.post('/api/v1/resources/webhook/' + token, json=payload)
        assert response.status_code == 200, response.text
        assert response.json()['accepted'] == 1
        inbox = client.get('/api/v1/resources/inbox').json()
        assert inbox['items'][0]['source'] == 'webhook'
        assert inbox['items'][0]['title'] == 'Webhook Movie'
        assert 'magnet:?xt=urn:btih:ABCDEF123456' in inbox['items'][0]['text']
        feed = client.post('/api/v1/resources/feeds', json={
            'name': 'Disabled feed', 'url': 'https://example.com/rss',
            'enabled': False, 'interval': 300})
        assert feed.status_code == 200, feed.text
        feeds = client.get('/api/v1/resources/feeds').json()['items']
        assert feeds[0]['name'] == 'Disabled feed'
        sources = client.get('/api/v1/resources/sources').json()
        assert sources['feeds'][0]['name'] == 'Disabled feed'
        assert sources['webhook_configured'] is True
