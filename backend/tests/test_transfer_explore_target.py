"""影视探索的转存目录：可以单独设，留空跟随 Web 转存目录。

探索页是独立入口（和 Web / Telegram 一样有自己的落盘位置），所以目标目录单独存一份；
不设时沿用 Web 的目录，老行为不变。
"""
from fastapi.testclient import TestClient
from test_directory_readback import api_setup


def login(client):
    data = client.post('/api/v1/auth/login',
                       json={'username': 'admin', 'password': 'test-password-1234'}).json()
    client.headers['X-CSRF-Token'] = data['csrf']


def test_explore_target_defaults_to_web_and_can_be_set_separately(tmp_path, monkeypatch):
    app, _ = api_setup(tmp_path, monkeypatch)
    with TestClient(app) as client:
        login(client)
        assert client.put('/api/v1/transfer/config', json={'target_cid': '100'}).status_code == 200
        config = client.get('/api/v1/transfer/config').json()['config']
        assert config['target_cid'] == '100'
        assert config['explore_cid'] == '', '没设过就留空，页面按「跟随 Web 目录」显示'

        saved = client.put('/api/v1/transfer/config',
                           json={'target_cid': '100', 'explore_cid': '200', 'explore_label': '影视探索'})
        assert saved.status_code == 200, saved.text
        config = client.get('/api/v1/transfer/config').json()['config']
        assert (config['explore_cid'], config['explore_label']) == ('200', '影视探索')

        # 清空 = 回到跟随
        client.put('/api/v1/transfer/config', json={'target_cid': '100', 'explore_cid': ''})
        assert client.get('/api/v1/transfer/config').json()['config']['explore_cid'] == ''


def test_explore_target_rejects_root_and_missing_directory(tmp_path, monkeypatch):
    app, _ = api_setup(tmp_path, monkeypatch)
    with TestClient(app) as client:
        login(client)
        assert client.put('/api/v1/transfer/config',
                          json={'target_cid': '100', 'explore_cid': '0'}).status_code == 422
        assert client.put('/api/v1/transfer/config',
                          json={'target_cid': '100', 'explore_cid': '999'}).status_code >= 400
