"""影视探索「分享入库」：解锁后的分享直接进分享库生成 STRM，不转存到自己网盘。"""
from fastapi.testclient import TestClient

from strimkeep.main import create_app


class IngestCloud:
    """只需要能列分享内容：入口本身不做转存，也不碰自己网盘的目录。"""

    def __init__(self, store):
        self.store = store
        self.tree = {'0': [{'id': 'd1', 'name': 'Season 1', 'is_dir': True},
                           {'id': 'f1', 'name': '第1集.mkv', 'is_dir': False, 'size': 1024},
                           {'id': 'n1', 'name': '说明.txt', 'is_dir': False, 'size': 1}],
                     'd1': [{'id': 'f2', 'name': '第2集.mkv', 'is_dir': False, 'size': 2048}]}

    async def close(self):
        pass

    async def status(self):
        return {'connected': True}

    async def list_files(self, cid, offset, limit):
        return {'items': [], 'total': 0, 'offset': offset, 'limit': limit, 'has_more': False}

    async def share_items(self, secret, cid=0):
        return self.tree.get(str(cid), [])


def test_ingest_share_builds_strms_without_transfer(tmp_path, monkeypatch):
    monkeypatch.setenv('ADMIN_USERNAME', 'admin')
    monkeypatch.setenv('ADMIN_PASSWORD', 'test-password-1234')
    app = create_app(tmp_path / 'data', tmp_path / 'strm', IngestCloud)
    with TestClient(app) as client:
        login = client.post('/api/v1/auth/login', json={'username': 'admin', 'password': 'test-password-1234'})
        client.headers['X-CSRF-Token'] = login.json()['csrf']
        app.state.store.set_many({'share_settings': {'auto_organize': False}})
        text = 'https://115.com/s/swabc?password=1234'
        result = client.post('/api/v1/share/ingest', json={'text': text})
        assert result.status_code == 200, result.text
        body = result.json()
        # 只收视频：说明.txt 被跳过；根目录平铺、分享子目录保留布局
        assert body['video_count'] == 2 and body['count'] == 2 and body['skip_count'] == 0
        pending = tmp_path / 'strm' / '分享STRM待整理'
        assert (pending / '第1集.mkv.strm').is_file()
        assert (pending / 'Season 1' / '第2集.mkv.strm').is_file()
        assert 'play115/share/' in (pending / '第1集.mkv.strm').read_text()
        assert body['job_id'] is None
        # 重复入库：已存在的跳过，不会重复写文件
        again = client.post('/api/v1/share/ingest', json={'text': text}).json()
        assert again['count'] == 0 and again['skip_count'] == 2
        # 一个链接一次：多链接或无链接都是 422
        assert client.post('/api/v1/share/ingest', json={
            'text': 'https://115.com/s/swone https://115.com/s/swtwo'}).status_code == 422
        assert client.post('/api/v1/share/ingest', json={'text': '这不是分享链接'}).status_code == 422
