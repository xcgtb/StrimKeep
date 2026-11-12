"""频道改名：公开转私密后用户名失效，换成数字 ID 时落点和游标都要保留。

现场：频道原本公开（有用户名），改成私密后用户名被释放，`get_entity('旧用户名')`
直接失败，只能换成数字 ID。界面原来没有改名入口，只能移除重加 —— 落点、正则、
分流规则全丢，游标也回到 0（重扫当前页）。
"""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from strimkeep.main import create_app


class FakeCloud:
    def __init__(self, store):
        self.store = store

    async def close(self):
        pass

    async def status(self):
        return {'connected': False}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('ADMIN_USERNAME', 'admin')
    monkeypatch.setenv('ADMIN_PASSWORD', 'test-password-1234')
    app = create_app(tmp_path / 'data', tmp_path / 'strm', FakeCloud)
    with TestClient(app) as c:
        response = c.post('/api/v1/auth/login',
                          json={'username': 'admin', 'password': 'test-password-1234'})
        assert response.status_code == 200
        c.headers['X-CSRF-Token'] = response.json()['csrf']
        yield c


def save(client, channels):
    response = client.put('/api/v1/channels', json={
        'enabled': True, 'notify': True, 'schedule_mode': 'interval', 'interval': 300,
        'channels': channels})
    assert response.status_code == 200, response.text
    return response.json()


def test_rename_keeps_rules_and_moves_the_cursor(client, tmp_path):
    save(client, [{'name': 'ltxjasna', 'enabled': True, 'source': 'preview',
                   'share_mode': 'strm', 'include': '4K', 'share_cid': '123',
                   'share_label': '电影'}])
    db = sqlite3.connect(tmp_path / 'data' / 'data' / 'state.db')
    with db:
        db.execute("UPDATE channel_state SET last_message_id=12345, title='Jasna LTX 频道' "
                   "WHERE name='ltxjasna'")

    response = client.post('/api/v1/channels/rename',
                           json={'old': 'ltxjasna', 'new': '-1004419698318'})
    assert response.status_code == 200, response.text

    channels = response.json()['config']['channels']
    assert [c['name'] for c in channels] == ['-1004419698318']
    assert channels[0]['share_cid'] == '123' and channels[0]['include'] == '4K'
    # 数字 ID 只能用会话读，改名时一并改过来，否则下次保存会被校验拦下
    assert channels[0]['source'] == 'session'
    # 游标和标题一起搬过去，改名后不重扫旧帖；旧名字的行不留残留
    assert db.execute("SELECT name,last_message_id,title FROM channel_state").fetchall() == [
        ('-1004419698318', 12345, 'Jasna LTX 频道')]


def test_rename_refuses_a_name_already_in_the_list(client):
    save(client, [{'name': 'ltxjasna'}, {'name': '-1004419698318', 'source': 'session'}])
    response = client.post('/api/v1/channels/rename',
                           json={'old': 'ltxjasna', 'new': '-1004419698318'})
    assert response.status_code == 409


def test_rename_unknown_channel_is_rejected(client):
    save(client, [{'name': 'ltxjasna'}])
    response = client.post('/api/v1/channels/rename',
                           json={'old': 'nosuchchan', 'new': '-1004419698318'})
    assert response.status_code == 404


def test_rename_to_an_invalid_name_is_rejected(client):
    save(client, [{'name': 'ltxjasna'}])
    response = client.post('/api/v1/channels/rename',
                           json={'old': 'ltxjasna', 'new': 'https://t.me/+invitehash'})
    assert response.status_code == 422
