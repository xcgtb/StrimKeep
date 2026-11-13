"""STRM 同步的全量 / 增量接口：水位推进与「未变化就跳过」的判定。"""
import json
import time

import httpx
from fastapi.testclient import TestClient

from strimkeep.main import create_app
from strimkeep.sync import Sync
from test_app import FakeCloud


class FakeOutputs:
    def __init__(self, root, rows):
        self.root = root
        self.rows = rows

    def get(self, account, fid):
        return self.rows.get(fid)

    def path(self, relative):
        return self.root / relative


class FakeIndex:
    def account(self, config):
        return '1'


class FakeEngine:
    def __init__(self, store, root, rows):
        self.store = store
        self.outputs = FakeOutputs(root, rows)
        self.index = FakeIndex()


def registry_row(updated, name='剧名.S01E01.mkv', size=100, sha1='ABC', files=None, active=1):
    return {'active': active, 'updated': updated,
            'source': json.dumps({'name': name, 'size': size, 'sha1': sha1}),
            'files': json.dumps(files if files is not None else {'剧集/剧名/剧名.S01E01.strm': 'digest'})}


def make_sync(tmp_path, rows, existing=('剧集/剧名/剧名.S01E01.strm',)):
    from strimkeep.store import Store
    store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
    root = tmp_path / 'strm'
    for relative in existing:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('http://play/x', encoding='utf-8')
    engine = FakeEngine(store, root, rows)
    return store, Sync(engine)


SOURCE = {'id': 'f1', 'name': '剧名.S01E01.mkv', 'size': 100, 'sha1': 'ABC',
          'parent_id': '9', 'pickcode': 'pc'}


def test_unchanged_when_registered_after_watermark_and_outputs_exist(tmp_path):
    store, sync = make_sync(tmp_path, {'f1': registry_row(updated=1000)})
    assert sync.unchanged_since('1', SOURCE, 900) is True
    # 水位之后又更新过 → 需要重新核对
    assert sync.unchanged_since('1', SOURCE, 1100) is False
    # 没有水位 / 没登记 / 已停用 → 都不跳过
    assert sync.unchanged_since('1', SOURCE, 0) is False
    assert sync.unchanged_since('1', {**SOURCE, 'id': 'unknown'}, 900) is False
    store.close()


def test_unchanged_requires_same_identity(tmp_path):
    store, sync = make_sync(tmp_path, {'f1': registry_row(updated=1000)})
    for field, value in (('name', '改名.mkv'), ('size', 101), ('sha1', 'DEF')):
        assert sync.unchanged_since('1', {**SOURCE, field: value}, 900) is False, field
    store.close()


def test_missing_local_output_is_never_skipped(tmp_path):
    """本地 STRM 被删/被移走时必须走完整核对，不能因为水位而漏修。"""
    store, sync = make_sync(tmp_path, {'f1': registry_row(updated=1000)}, existing=())
    assert sync.unchanged_since('1', SOURCE, 900) is False
    store.close()


def test_sync_full_and_incremental_endpoints_snapshot_their_mode(tmp_path, monkeypatch):
    monkeypatch.setenv('ADMIN_USERNAME', 'admin')
    monkeypatch.setenv('ADMIN_PASSWORD', 'test-password-1234')
    app = create_app(tmp_path / 'data', tmp_path / 'strm', FakeCloud)
    with TestClient(app) as client:
        login = client.post('/api/v1/auth/login', json={'username': 'admin', 'password': 'test-password-1234'})
        client.headers['X-CSRF-Token'] = login.json()['csrf']
        app.state.store.set_many({'sync_config': {'source_cids': ['100']},
                                  'playback_base': 'http://nas:9000',
                                  'cookie': 'UID=1; CID=t; SEID=t'})

        full = client.post('/api/v1/sync/full', json={})
        assert full.status_code == 200, full.text
        config = json.loads(app.state.store.db.execute(
            'SELECT config FROM organize_jobs WHERE id=?', (full.json()['id'],)).fetchone()['config'])
        assert config['mode'] == 'full' and config['full_scan'] is True
        assert config['_force_full'] is True and config['trust_scan'] is False

        app.state.store.set_meta('sync_watermark', {'at': 1234.5, 'job': 7, 'mode': 'full'})
        incremental = client.post('/api/v1/sync/incremental', json={})
        assert incremental.status_code == 200, incremental.text
        assert incremental.json()['since'] == 1234.5
        config = json.loads(app.state.store.db.execute(
            'SELECT config FROM organize_jobs WHERE id=?', (incremental.json()['id'],)).fetchone()['config'])
        assert config['mode'] == 'incremental' and config['incremental_since'] == 1234.5

        # 显式指定起点覆盖水位
        explicit = client.post('/api/v1/sync/incremental', json={'since': 42})
        assert explicit.json()['since'] == 42

        state = client.get('/api/v1/sync/state')
        assert state.status_code == 200
        body = state.json()
        assert body['watermark']['at'] == 1234.5
        assert body['last_job']['id'] == explicit.json()['id']
        assert body['running'] >= 1 and isinstance(body['stale_count'], int)

def _client(tmp_path, monkeypatch, watermark=None):
    monkeypatch.setenv('ADMIN_USERNAME', 'admin')
    monkeypatch.setenv('ADMIN_PASSWORD', 'test-password-1234')
    app = create_app(tmp_path / 'data', tmp_path / 'strm', FakeCloud)
    client = TestClient(app)
    client.__enter__()
    login = client.post('/api/v1/auth/login', json={'username': 'admin', 'password': 'test-password-1234'})
    client.headers['X-CSRF-Token'] = login.json()['csrf']
    app.state.store.set_many({'sync_config': {'source_cids': ['100']},
                              'playback_base': 'http://nas:9000',
                              'cookie': 'UID=1; CID=t; SEID=t'})
    if watermark is not None:
        app.state.store.set_meta('sync_watermark', watermark)

    async def fake_directory(cid):
        return [{'id': str(cid), 'name': '目录%s' % cid}]

    app.state.cloud.directory = fake_directory
    return app, client


def test_incremental_prefers_the_directory_watermark(tmp_path, monkeypatch):
    app, client = _client(tmp_path, monkeypatch,
                          watermark={'at': 100.0, 'job': 1, 'roots': {'100': 400.0}})
    body = client.post('/api/v1/sync/incremental', json={'source_cids': ['100']}).json()
    assert body['since'] == 400.0, '指定目录时应使用该目录自己的水位'
    body = client.post('/api/v1/sync/incremental', json={}).json()
    assert body['since'] == 100.0, '不指定目录时用全局水位'
    client.__exit__(None, None, None)


def test_dry_run_estimates_without_queueing(tmp_path, monkeypatch):
    app, client = _client(tmp_path, monkeypatch, watermark={'at': 100.0, 'job': 1})
    with app.state.store.db:
        for index, updated in enumerate((90.0, 150.0, 160.0)):
            app.state.store.db.execute(
                'INSERT OR REPLACE INTO output_registry VALUES(?,?,?,?,?,?,?,?,1,?)',
                ('1', 'f%d' % index, '{}', '["100"]', 'p%d' % index, 'c',
                 '剧集/剧名/第%d集.strm' % index, '{}', updated))
    before = app.state.store.db.execute('SELECT count(*) FROM organize_jobs').fetchone()[0]
    body = client.post('/api/v1/sync/incremental', json={'dry_run': True}).json()
    after = app.state.store.db.execute('SELECT count(*) FROM organize_jobs').fetchone()[0]
    assert body['dry_run'] is True and body['changed'] == 2, body
    assert after == before, '预演不该入队'
    client.__exit__(None, None, None)


def test_changes_lists_outputs_registered_after_the_watermark(tmp_path, monkeypatch):
    app, client = _client(tmp_path, monkeypatch)
    with app.state.store.db:
        app.state.store.db.execute(
            'INSERT OR REPLACE INTO output_registry VALUES(?,?,?,?,?,?,?,?,1,?)',
            ('1', 'old', '{}', '["100"]', 'p-old', 'c', '剧集/旧.strm', '{}', 50.0))
        app.state.store.db.execute(
            'INSERT OR REPLACE INTO output_registry VALUES(?,?,?,?,?,?,?,?,1,?)',
            ('1', 'new', '{}', '["100"]', 'p-new', 'c', '剧集/新.strm', '{}', 500.0))
    body = client.get('/api/v1/sync/changes?since=100').json()
    assert [item['file_id'] for item in body['items']] == ['new']
    assert body['count'] == 1
    client.__exit__(None, None, None)
