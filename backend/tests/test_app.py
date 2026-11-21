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


def test_task_center_offline_attention_reads_older_active_and_review_records(app, client):
    import time
    submitted_at = time.time()
    with app.state.store.db:
        app.state.store.db.executemany(
            'INSERT INTO offline_submissions(urls,target_cid,submitted_at,state) VALUES(?,?,?,?)',
            [('[]', '123', submitted_at, state)
             for state in ['submitted', 'needs_review'] + ['completed'] * 55 + ['submitting'] * 20])
    response = client.get('/api/v1/offline/submissions/attention')
    assert response.status_code == 200, response.text
    summary = response.json()
    assert summary['active']['total'] == 21 and len(summary['active']['items']) == 20
    assert summary['review']['total'] == 1 and summary['review']['items'][0]['state'] == 'needs_review'
    assert 'urls' not in summary['active']['items'][0]


def test_mapping_page_reports_actual_strm_scan_roots(app, client):
    app.state.store.set_many({'organize_config': {'archive_cid': '123', 'directory_labels': {'123': '115 网盘 / 测试媒体'}}})
    root = app.state.strms
    outsider = root / '另一个 Emby 目录' / '作品 (2024) {tmdb-16}' / '作品.strm'
    outsider.parent.mkdir(parents=True)
    outsider.write_text('https://example.invalid/stream')
    path = '/api/v1/inventory/mapping/page?media=all&refresh=1'
    first = client.get(path).json()
    assert first['total'] == 0
    assert first['scan_sources'] == [
        {'side': 'cloud', 'path': str(root / '测试媒体'), 'exists': False, 'strm_count': 0},
        {'side': 'share', 'path': str(root / '分享影视库'), 'exists': False, 'strm_count': 0},
    ]
    in_scope = root / '测试媒体' / '作品 (2024) {tmdb-17}' / '作品.strm'
    in_scope.parent.mkdir(parents=True)
    in_scope.write_text('https://example.invalid/stream')
    result = client.get(path).json()
    assert result['total'] == 1 and result['rows'][0]['tmdb_id'] == 17
    assert result['scan_sources'][0]['exists'] and result['scan_sources'][0]['strm_count'] == 1


def test_playback_cleanup_preserves_moved_legacy_strm(app, client):
    kept = sample(client)['id']
    app.state.store.db.execute(
        "INSERT INTO playbacks VALUES(?,?,?,?,?,?,0,0)",
        ('orphan-id', '999', 'pick', 'x', 'missing.strm', 'hash'))
    app.state.store.db.commit()
    moved = app.state.organizer.root / 'moved' / 'episode.strm'
    moved.parent.mkdir(parents=True)
    moved.write_text('https://example.test/play/' + kept)
    response = client.post('/api/v1/playbacks/cleanup?include_active=true')
    assert response.status_code == 200 and response.json()['removed'] == 1
    ids = {row['id'] for row in app.state.store.db.execute('SELECT id FROM playbacks')}
    assert kept in ids and 'orphan-id' not in ids


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


def test_system_health_reports_checks(client, app):
    import time
    app.state.store.set_meta('worker_heartbeat', {
        'at': time.time(), 'worker_alive': True, 'scheduler_alive': True,
        'lane_faults': {}, 'closing': False})
    response = client.get('/api/v1/system/health')
    assert response.status_code == 200
    data = response.json()
    assert data['overall'] in ('ok', 'warn', 'error')
    keys = {item['key'] for item in data['checks']}
    assert {'database', 'worker', 'commands', 'disk_data', 'disk_strm',
            'backup', 'tmdb', 'emby', 'cloud115', 'failures'} <= keys
    assert all(item['status'] in ('ok', 'warn', 'error') for item in data['checks'])


def test_dismiss_unrecognized_items_closes_job(app, client):
    jobs = app.state.store.jobs
    job_id = jobs.create({'job_kind': 'organize'}, state='queued')
    jobs.insert_item(job_id, 'file-1', {
        'id': 'file-1', 'name': 'unknown.mkv', 'parent_id': '1', 'size': 1}, state='discovered')
    item = app.state.store.db.execute(
        'SELECT * FROM organize_items WHERE job_id=?', (job_id,)).fetchone()
    jobs.update_item(item['id'], state='review', step='未识别：无法识别',
                     message='缺少可确认的标题或年份。')
    jobs.update(job_id, state='partial', stage='等待人工处理')

    response = client.post(f'/api/v1/organize/jobs/{job_id}/dismiss-unrecognized', json={})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data['dismissed'] == 1
    assert data['closed'] is True
    assert data['job']['state'] == 'completed'
    item = app.state.store.db.execute(
        'SELECT id,state,step FROM organize_items WHERE job_id=?', (job_id,)).fetchone()
    assert item['state'] == 'skipped'
    assert item['step'] == '已人工忽略'
    assert data['job']['unidentified'] == 0
    detail = client.get(f'/api/v1/organize/jobs/{job_id}').json()
    assert detail['items'][0]['unidentified'] is False
    assert client.post(f'/api/v1/organize/jobs/{job_id}/dismiss-unrecognized', json={}).status_code == 409

    # Old records used a label that contained the search keyword.
    jobs.update_item(item['id'], step='人工忽略未识别', message='未识别项不再处理')
    detail = client.get(f'/api/v1/organize/jobs/{job_id}').json()
    assert detail['job']['unidentified'] == 0
    assert detail['items'][0]['unidentified'] is False


def test_manual_match_creates_single_file_job(app, client, monkeypatch):
    jobs = app.state.store.jobs
    job_id = jobs.create({
        'job_kind': 'organize', 'source_cids': ['1'], 'archive_cid': '2',
        'pending_cid': '3', 'retries': 0, 'retry_interval': 0,
        'upgrade_policy': {'mode': 'archive'}, 'strm_options': {},
        'classify_enabled': False,
    }, state='queued')
    jobs.insert_item(job_id, 'file-1', {
        'id': 'file-1', 'name': 'unknown.mkv', 'parent_id': '1', 'size': 123},
        state='discovered')
    item = app.state.store.db.execute(
        'SELECT * FROM organize_items WHERE job_id=?', (job_id,)).fetchone()
    jobs.update_item(item['id'], state='review', step='未识别：无法识别',
                     message='缺少可确认的标题或年份。')
    jobs.update(job_id, state='partial')

    async def fake_tmdb(path, params=None):
        assert path == 'movie/603'
        return {'id': 603, 'title': '黑客帝国', 'release_date': '1999-03-31'}

    monkeypatch.setattr(app.state.organizer, 'tmdb', fake_tmdb)
    set_manifest = jobs.set_manifest

    def check_worker_visibility(new_id, manifest):
        # A separate WAL reader models the Worker polling between writes.
        with sqlite3.connect(app.state.store.path) as reader:
            assert reader.execute('SELECT 1 FROM organize_jobs WHERE id=?', (new_id,)).fetchone() is None
        return set_manifest(new_id, manifest)

    monkeypatch.setattr(jobs, 'set_manifest', check_worker_visibility)
    response = client.post(
        f'/api/v1/organize/jobs/{job_id}/items/{item["id"]}/manual-match',
        json={'tmdb_id': 603, 'media_type': 'movie'})
    assert response.status_code == 200, response.text
    data = response.json()
    new_id = data['job']['id']
    assert new_id != job_id
    new_job = app.state.store.db.execute(
        'SELECT * FROM organize_jobs WHERE id=?', (new_id,)).fetchone()
    config = json.loads(new_job['config'])
    assert config['only_file_ids'] == ['file-1']
    assert config['manual_hint']['tmdb_id'] == 603
    assert config['manual_hint']['identity_source'] == 'manual'
    assert config['pending_cid'] == ''
    assert new_job['manifest'] == 1
    rows = app.state.store.db.execute(
        'SELECT * FROM organize_items WHERE job_id=?', (new_id,)).fetchall()
    assert len(rows) == 1
    assert rows[0]['state'] == 'discovered'
    source = app.state.store.db.execute(
        'SELECT step FROM organize_items WHERE id=?', (item['id'],)).fetchone()
    assert source['step'] == '已转手动匹配'
    assert client.post(
        f'/api/v1/organize/jobs/{job_id}/items/{item["id"]}/manual-match',
        json={'tmdb_id': 603, 'media_type': 'movie'}).status_code == 409


def test_build_plan_uses_manual_hint_without_identify(app, client, monkeypatch):
    organizer = app.state.organizer
    job_id = organizer.jobs.create({
        'job_kind': 'organize', 'source_cids': ['1'], 'archive_cid': '2',
        'pending_cid': '', 'retries': 0, 'retry_interval': 0,
        'upgrade_policy': {'mode': 'archive'}, 'strm_options': {},
        'ffprobe_enabled': False, 'classify_enabled': False,
        'rename_enabled': True, 'folder_language': 'zh', 'filename_language': 'zh',
    }, state='running')

    async def fake_tmdb(path, params=None):
        assert path == 'movie/603'
        return {'id': 603, 'title': '黑客帝国', 'release_date': '1999-03-31'}

    async def fake_file(file_id):
        return {'id': file_id, 'name': '测试视频.mkv', 'parent_id': '1', 'size': 123}

    async def fail_identify(hint):
        raise AssertionError('手动匹配不应再走文件名 TMDB 检索')

    monkeypatch.setattr(organizer, 'tmdb', fake_tmdb)
    monkeypatch.setattr(organizer, 'identify', fail_identify)
    monkeypatch.setattr(organizer.cloud, 'file', fake_file)
    original = {'id': 'file-1', 'name': '测试视频.mkv', 'parent_id': '1', 'size': 123}
    plan = asyncio.run(organizer.build_plan(
        job_id, original,
        manual={'query': '黑客帝国', 'year': 1999, 'media_type': 'movie',
                'tmdb_id': 603, 'identity_source': 'manual'}))
    assert plan['details']['id'] == 603
    assert plan['hint']['identity_source'] == 'manual'
    assert '黑客帝国' in plan['target_name']
    assert plan['media_origin'] == 'filename'
