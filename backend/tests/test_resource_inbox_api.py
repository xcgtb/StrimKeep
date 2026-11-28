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
        async def fake_directory(cid):
            return {}
        app.state.cloud.directory = fake_directory
        imported = client.post('/api/v1/resources/feeds/import', json={
            'xml': '<rss version="2.0"><channel><item><title>Imported</title>'
                   '<guid>import-1</guid><description>https://115.com/s/abcd1234?password=efgh'
                   '</description></item></channel></rss>',
            'target_cid': '123'})
        assert imported.status_code == 200, imported.text
        assert imported.json()['accepted'] == 1

        row_id = inbox['items'][0]['id']
        with app.state.store.db:
            app.state.store.db.execute("UPDATE resource_inbox SET status='queued' WHERE id=?", (row_id,))
        assert client.post(f'/api/v1/resources/inbox/{row_id}/skip').status_code == 409
        assert client.delete(f'/api/v1/resources/inbox/{row_id}').status_code == 409
        with app.state.store.db:
            app.state.store.db.execute("UPDATE resource_inbox SET status='processing' WHERE id=?", (row_id,))
        assert client.post(f'/api/v1/resources/inbox/{row_id}/retry').status_code == 409
        with app.state.store.db:
            app.state.store.db.execute("UPDATE resource_inbox SET status='pending' WHERE id=?", (row_id,))

        old_token = token
        token = client.post('/api/v1/resources/webhook-token').json()['token']
        assert token != old_token
        assert client.post('/api/v1/resources/webhook/' + old_token, json=payload).status_code == 200
        app.state.store.set_meta('resource_webhook_previous_until', 0)
        assert client.post('/api/v1/resources/webhook/' + old_token, json=payload).status_code == 404
        assert client.post('/api/v1/resources/webhook/' + token, json=payload).status_code == 200
