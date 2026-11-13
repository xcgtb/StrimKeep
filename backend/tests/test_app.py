import asyncio
import json
import sqlite3
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from strimkeep.cloud115 import Cloud115
from strimkeep.main import create_app
from strimkeep.probe import summarize
from strimkeep.store import Store


class FakeCloud:
    def __init__(self, store):
        self.store = store
        self.calls = []

    async def close(self):
        pass

    async def status(self):
        return {'connected': True}

    async def list_files(self, cid, offset, limit):
        return {'items': [], 'total': 0, 'offset': offset, 'limit': limit, 'has_more': False}

    async def file(self, file_id):
        return {'id': file_id, 'name': '测试视频.mkv', 'pickcode': 'testpickcode', 'size': 123}

    async def url(self, pickcode, ua):
        self.calls.append((pickcode, ua))
        return 'https://cdn.example/video.mkv?temporary=secret', {'User-Agent': ua}


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv('ADMIN_USERNAME', 'admin')
    monkeypatch.setenv('ADMIN_PASSWORD', 'test-password-1234')
    return create_app(tmp_path / 'data', tmp_path / 'strm', FakeCloud)


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        response = c.post('/api/v1/auth/login', json={'username': 'admin', 'password': 'test-password-1234'})
        assert response.status_code == 200
        c.headers['X-CSRF-Token'] = response.json()['csrf']
        yield c


def sample(client):
    assert client.put('/api/v1/settings', json={'playback_base': 'https://media.example:9443'}).status_code == 200
    response = client.post('/api/v1/playbacks', json={'file_id': '123'})
    assert response.status_code == 200, response.text
    return response.json()


def test_auth_csrf_origin_and_no_secret_echo(app, client):
    other = TestClient(app)
    assert other.get('/api/v1/settings').status_code == 401
    assert client.put('/api/v1/settings', json={}, headers={'X-CSRF-Token': ''}).status_code == 403
    assert client.put('/api/v1/settings', json={}, headers={'Origin': 'https://attacker.example'}).status_code == 403
    bad = client.post('/api/v1/auth/login', json={'username': '', 'password': 'never-echo-this-secret'})
    assert bad.status_code == 422 and 'never-echo' not in bad.text
    client.post('/api/v1/auth/logout')
    assert client.get('/api/v1/status').status_code == 401


def test_credentials_encrypted_and_first_admin_survives_restart(tmp_path):
    root = tmp_path / 'data'
    store = Store(root, 'admin', 'original-password-123')
    store.set_many({'cookie': 'UID=abc; CID=def; SEID=very-sensitive-cookie'})
    store.close()
    assert b'very-sensitive-cookie' not in (root / 'data/state.db').read_bytes()
    again = Store(root, 'different', 'different-password-123')
    assert again.authenticate('admin', 'original-password-123')
    assert not again.authenticate('different', 'different-password-123')
    assert 'very-sensitive' in again.get('cookie')
    again.close()
    (root / 'config/master.key').unlink()
    with pytest.raises(RuntimeError, match='密钥缺失'):
        Store(root, 'admin', 'original-password-123')


def test_session_is_30_days_and_survives_store_restart(tmp_path, monkeypatch):
    from strimkeep import store as store_module
    now = 2000000000.0
    monkeypatch.setattr(store_module.time, 'time', lambda: now)
    root = tmp_path / 'data'
    store = Store(root, 'admin', 'test-password-1234')
    token, _ = store.new_session()
    assert store.session(token)['expires'] == now + 30 * 24 * 60 * 60
    store.close()
    store = Store(root, 'admin', 'test-password-1234')
    assert store.session(token) is not None
    monkeypatch.setattr(store_module.time, 'time', lambda: now + 30 * 24 * 60 * 60 + 1)
    assert store.session(token) is None
    store.close()


def test_login_cookie_has_30_day_lifetime(app):
    with TestClient(app) as client:
        response=client.post('/api/v1/auth/login', json={'username':'admin','password':'test-password-1234'})
        assert 'Max-Age=2592000' in response.headers['set-cookie']


def test_config_never_returns_secret_values(client):
    values = {'cookie': 'UID=abc; CID=def; SEID=secret-cookie', 'tmdb_key': 'secret-tmdb', 'emby_key': 'secret-emby'}
    response = client.put('/api/v1/settings', json=values)
    assert response.status_code == 200
    for value in values.values():
        assert value not in response.text
    assert response.json()['cookie_configured'] is True
    assert client.put('/api/v1/settings', json={'playback_base': 'file:///etc/passwd'}).status_code == 422


def test_health_independent_of_external_accounts(client):
    assert client.get('/health/ready').json()['status'] == 'ready'
    assert client.get('/api/v1/status').json()['capabilities']['upgrade'] is True


def test_strm_get_head_ua_and_token_lifecycle(client, app):
    result = sample(client)
    parts = urlsplit(result['url'])
    route = parts.path + '?' + parts.query
    assert client.get(route, follow_redirects=False, headers={'User-Agent': 'Infuse/Test'}).status_code == 302
    assert app.state.cloud.calls[-1] == ('testpickcode', 'Infuse/Test')
    head = client.head(route, follow_redirects=False, headers={'User-Agent': 'Senplay/Test'})
    assert head.status_code == 302 and not head.content
    assert head.headers['location'].startswith('https://cdn.example/')
    assert not parts.query
    assert client.get(parts.path + '?token=invalid', follow_redirects=False).status_code == 302
    download = client.get(f'/api/v1/playbacks/{result["id"]}/strm')
    assert download.text.strip() == result['url']
    assert 'no-store' in head.headers['cache-control']
    client.post(f'/api/v1/playbacks/{result["id"]}/revoke')
    assert client.get(route).status_code == 403
    regenerated = client.post(f'/api/v1/playbacks/{result["id"]}/regenerate').json()['url']
    assert regenerated == result['url']
    assert client.get(route, follow_redirects=False).status_code == 302
    p = urlsplit(regenerated)
    assert client.get(p.path + '?' + p.query, follow_redirects=False).status_code == 302


def test_write_error_rolls_back_sample_db(client, app, monkeypatch):
    client.put('/api/v1/settings', json={'playback_base': 'https://media.example'})
    import strimkeep.main as main
    def failed(*args):
        raise OSError('simulated disk error')
    monkeypatch.setattr(main.os, 'replace', failed)
    # Exceptions propagate in TestClient by design; the transaction must still roll back.
    with pytest.raises(OSError):
        client.post('/api/v1/playbacks', json={'file_id': '123'})
    assert app.state.store.db.execute('SELECT count(*) FROM playbacks').fetchone()[0] == 0


def test_tmdb_and_emby_contract(client, app):
    client.put('/api/v1/settings', json={'tmdb_key': 'key', 'emby_url': 'http://emby:8096', 'emby_key': 'emby-key'})
    requests = []
    def handler(request):
        requests.append(request)
        if request.url.host == 'api.themoviedb.org':
            assert request.url.params['api_key'] == 'key'
            return httpx.Response(200, json={'results': [{'id': 11, 'title': '样例', 'release_date': '2001-01-01'}]})
        assert request.headers['X-Emby-Token'] == 'emby-key'
        return httpx.Response(200, json={'ServerName': 'test', 'Version': '1'})
    app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)
    result = client.get('/api/v1/tmdb/search?q=test&media_type=movie')
    assert result.json()['items'][0]['id'] == 11
    assert client.post('/api/v1/connections/emby/test').json()['server'] == 'test'
    assert client.post('/api/v1/emby/scan').json()['accepted'] is True
    assert len([r for r in requests if r.url.path.endswith('/Library/Refresh')]) == 1


def test_emby_failure_not_retried(client, app):
    client.put('/api/v1/settings', json={'emby_url': 'http://emby:8096', 'emby_key': 'key'})
    calls = []
    def fail(request):
        calls.append(request)
        return httpx.Response(503)
    app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(fail), trust_env=False)
    assert client.post('/api/v1/emby/scan').status_code == 502
    assert len(calls) == 1


def test_probe_unknown_dynamic_range_not_sdr():
    data = {'streams': [{'codec_type': 'video', 'width': 1920, 'height': 1080,
                         'codec_name': 'hevc', 'pix_fmt': 'yuv420p10le', 'avg_frame_rate': '24000/1001'}]}
    result = summarize(data)
    assert result['dynamic_range'] == '未知'
    assert result['bit_depth'] == 10 and result['fps'] == 23.976


def test_real_sdk_read_and_qr_contract(tmp_path):
    async def scenario():
        store = Store(tmp_path, 'admin', 'original-password-123')
        store.set_many({'cookie': 'UID=1_A1; CID=fake; SEID=fake'})
        cloud = Cloud115(store)
        await cloud.http.aclose()
        requests = []
        def handler(req):
            requests.append(req)
            path = req.url.path
            if path.endswith('/token/'):
                value = {'state': True, 'data': {'uid': 'qr-user', 'time': 1, 'sign': 'sign', 'qrcode': 'https://115.com/scan/test'}}
            elif path == '/get/status/':
                value = {'state': True, 'data': {'status': 2}}
            elif path.endswith('/login/qrcode/'):
                value = {'state': True, 'data': {'cookie': {'UID': '2_A1', 'CID': 'new', 'SEID': 'new'}}}
            elif path == '/files/file':
                value = {'state': True, 'data': [{'file_id': '123', 'file_category': '1', 'file_name': 'video.mkv', 'pick_code': 'pick', 'file_size': '42'}]}
            elif path == '/files/supervision':
                value = {'state': True, 'data': {'file_id': '123', 'file_name': 'video.mkv',
                    'pick_code': 'pick', 'file_size': '42', 'parent_id': '0', 'file_sha1': 'A' * 40}}
            elif path == '/files':
                value = {'state': True, 'count': 2, 'data': [
                    {'cid': '10', 'pid': '0', 'n': 'folder'},
                    {'fid': '123', 'cid': '0', 'n': 'video.mkv', 's': '42', 'pc': 'pick'}]}
            else:
                value = {'state': True}
            return httpx.Response(200, json=value)
        cloud.http = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)
        listing = await cloud.list_files(0, 0, 100)
        assert listing['items'][0]['is_dir'] is True
        assert listing['items'][1]['pickcode'] == 'pick'
        actual = await cloud.file('123')
        assert actual['name'] == 'video.mkv' and actual['parent_id'] == '0'
        qr = await cloud.qr_start('android')
        assert qr['image'].startswith('data:image/png;base64,')
        assert (await cloud.qr_poll(qr['id']))['status'] == 'connected'
        assert '2_A1' in store.get('cookie')
        assert all(r.method == 'GET' or r.url.path.endswith('/login/qrcode/') or
                   (r.method == 'POST' and r.url.path == '/files/supervision') for r in requests)
        await cloud.close(); store.close()
    asyncio.run(scenario())


def test_share_playback_without_token_and_legacy_url_keeps_ua_and_revoke(client, app):
    sid='a'*32
    async def share_url(file_id, code, password, ua):
        app.state.cloud.calls.append((file_id,code,password,ua))
        return 'https://cdn.example/shared.mkv', {}
    app.state.cloud.share_url_download=share_url
    # 这条记录模拟「老版本写下的分享 STRM」：文件必须在盘上。否则盘面核对
    # （output_reconcile，GRACE 内不动；created=1 早就过了宽限期）会把它标成撤销，
    # 用例就随机拿到 403——这是用例自己的夹具不自洽，不是接口的问题。
    (app.state.strms / 'x.strm').write_text(
        'http://nas:9000/play115/share/' + sid + '/x.mkv', encoding='utf-8')
    with app.state.store.db:
        app.state.store.db.execute("INSERT INTO share_strms(id,share_code,receive_code,file_id,pickcode,name,strm_path,token_hash,revoked,created) VALUES(?,?,?,?,?,?,?,?,0,?)", (sid,'abc','1234','999','','x.mkv','x.strm','legacy-hash',1))
    for suffix in ('','?token=old-token','/视频.S01E01.mkv'):
        response=client.get('/play115/share/'+sid+suffix,headers={'User-Agent':'Infuse/Test'},follow_redirects=False)
        assert response.status_code==302
        assert app.state.cloud.calls[-1]==('999','abc','1234','Infuse/Test')
    with app.state.store.db:
        app.state.store.db.execute('UPDATE share_strms SET revoked=1 WHERE id=?',(sid,))
    assert client.get('/play115/share/'+sid,follow_redirects=False).status_code==403


def test_share_playback_records_metrics_and_throttles_per_entry(client, app):
    """0.9.58：分享取链也要有指标和入口限流。

    以前分享库播放只有一条事件，`/metrics` 里看不到任何分享流量——现场反馈
    「302 播放慢」时，一半的库（分享库）完全没有数据可看；限流也只有网盘侧有。
    """
    sid = 'b' * 32
    calls = []

    async def share_url(file_id, code, password, ua):
        calls.append(ua)
        return 'https://cdn.example/shared.mkv', {}

    app.state.cloud.share_url_download = share_url
    # 盘面核对（output_reconcile）会把「文件不在盘上」的老记录标成撤销，
    # 这里必须真的落一个 STRM，否则用例随机拿到 403。
    (app.state.strms / 'y.strm').write_text(
        'http://nas:9000/play115/share/' + sid + '/y.mkv', encoding='utf-8')
    with app.state.store.db:
        app.state.store.db.execute(
            "INSERT INTO share_strms(id,share_code,receive_code,file_id,pickcode,name,strm_path,token_hash,revoked,created)"
            " VALUES(?,?,?,?,?,?,?,?,0,?)",
            (sid, 'abc', '1234', '999', '', 'y.mkv', 'y.strm', 'hash-b', 1))
    plays = []
    app.state.metrics.record_play = lambda result, ms: plays.append(result)

    assert client.get('/play115/share/' + sid, follow_redirects=False).status_code == 302
    assert plays == ['ok']

    async def broken(file_id, code, password, ua):
        raise RuntimeError('boom')

    app.state.cloud.share_url_download = broken
    with pytest.raises(RuntimeError):
        client.get('/play115/share/' + sid, follow_redirects=False)
    assert plays == ['ok', 'error']          # 失败同样入桶，不再只有事件

    # 每个分享入口 60 次/分钟，第 61 次直接 429，不再消耗 115 的配额。
    app.state.play_limits.clear()
    app.state.cloud.share_url_download = share_url
    codes = [client.get('/play115/share/' + sid, follow_redirects=False).status_code
             for _ in range(60)]
    assert set(codes) == {302}, codes
    before = len(calls)
    assert client.get('/play115/share/' + sid, follow_redirects=False).status_code == 429
    assert len(calls) == before              # 被限流的那次没有打到 115


def test_readiness_detects_stopped_or_recovering_secondary_lane(app, client):
    engine=app.state.organizer
    class DeadWorker:
        def done(self): return True
    old=engine.extra_workers
    try:
        engine.extra_workers=[DeadWorker()]
        assert client.get('/health/ready').status_code==503
        engine.extra_workers=old
        engine.lane_faults['sync']=True
        assert client.get('/health/ready').status_code==503
        engine.lane_faults['sync']=False
        assert client.get('/health/ready').status_code==200
    finally:
        engine.extra_workers=old
        engine.lane_faults.clear()
