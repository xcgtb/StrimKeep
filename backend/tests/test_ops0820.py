"""0.8.20 回归：重建预览/执行/回滚、Emby 交付摘列、任务视图兼容、SQLite PRAGMA。

对应用户可见的变化（背景见 docs/releases/RELEASE_0.8.20.md）：

1. `POST /api/v1/strm/rebuild?dry_run=1` 只预览不写盘；
2. 执行前落快照，`POST /api/v1/strm/rebuild/rollback` 能还原旧字节；
3. `emby_deliveries` 新增 total/confirmed 摘列，`job_public` 不再解析整段 payload；
4. `Store` 打开即设置 busy_timeout / synchronous（WAL 下的写并发保护）；
5. `/api/v1/tasks/events` 的变更检测不改变首帧内容。
"""
import json
import time

from fastapi.testclient import TestClient

from strimkeep.main import create_app
from strimkeep.store import Store
from test_app import FakeCloud

PASSWORD = 'test-password-1234'
ACCOUNT = '1'
BASE = 'http://nas:9000'
FID = '9001'
PICK = 'pc9001'
SHA1 = 'a' * 40
SIZE = 12345
NAME = '影片 (2020).mkv'
RELATIVE = '电影/影片 (2020)/影片.strm'
STALE = 'http://old-host:9000/play/deadbeef\n'


def managed_app(tmp_path, monkeypatch, stale=STALE):
    """建一个「有一条受管输出、磁盘上是旧地址」的应用，返回 (app, client, path)。"""
    monkeypatch.setenv('ADMIN_USERNAME', 'admin')
    monkeypatch.setenv('ADMIN_PASSWORD', PASSWORD)
    app = create_app(tmp_path / 'data', tmp_path / 'strm', FakeCloud)
    client = TestClient(app)
    client.__enter__()
    store = app.state.store
    store.set_many({'cookie': 'UID=%s; CID=t; SEID=t' % ACCOUNT, 'playback_base': BASE})
    source = {'id': FID, 'pickcode': PICK, 'sha1': SHA1, 'size': SIZE,
              'name': NAME, 'parent_id': '2'}
    with store.db:
        store.db.execute(
            'INSERT OR REPLACE INTO output_registry VALUES(?,?,?,?,?,?,?,?,1,?)',
            (ACCOUNT, FID, json.dumps(source), json.dumps([ACCOUNT, '2']), 'pid9001',
             store.crypto.encrypt(b'').decode(), RELATIVE, '{}', time.time()))
    path = (tmp_path / 'strm' / RELATIVE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(stale, encoding='utf-8')
    return app, client, path


def login(client):
    csrf = client.post('/api/v1/auth/login',
                       json={'username': 'admin', 'password': PASSWORD}).json()['csrf']
    client.headers['X-CSRF-Token'] = csrf
    return csrf


# ------------------------------------------------- 1) 重建：预览 / 执行 / 回滚

def test_rebuild_dry_run_reports_without_touching_files(tmp_path, monkeypatch):
    app, client, path = managed_app(tmp_path, monkeypatch)
    try:
        login(client)
        before = path.read_text(encoding='utf-8')
        result = client.post('/api/v1/strm/rebuild?dry_run=1').json()
        assert result['dry_run'] is True and result['rewritten'] == 1
        assert result['scanned'] == 1 and result['snapshot'] is None
        assert path.read_text(encoding='utf-8') == before          # 一个字节都没改
        assert client.get('/api/v1/strm/rebuild/status').json()['state'] == 'done'
    finally:
        client.__exit__(None, None, None)


def test_rebuild_counts_both_newline_styles_as_current(tmp_path, monkeypatch):
    """地址已是目标值时，`url` 与 `url + 换行` 都算最新，不能被重建改写。

    现场 949 条台账里 879 条是无换行形态（publication/sync 写的），只有
    write_strm() 写的才带换行；重建必须两种都接受，否则会整棵树重写。
    """
    from strimkeep.playaddr import play115_url
    url = play115_url(BASE, PICK, FID, SHA1, SIZE, NAME)
    app, client, path = managed_app(tmp_path, monkeypatch, stale=url)
    try:
        login(client)
        result = client.post('/api/v1/strm/rebuild?dry_run=1').json()
        assert (result['rewritten'], result['unchanged']) == (0, 1)
        path.write_text(url + '\n', encoding='utf-8')
        result = client.post('/api/v1/strm/rebuild').json()
        assert (result['rewritten'], result['unchanged'], result['snapshot']) == (0, 1, None)
        assert path.read_text(encoding='utf-8') == url + '\n'
    finally:
        client.__exit__(None, None, None)


def test_rebuild_rewrites_address_and_keeps_a_rollback_snapshot(tmp_path, monkeypatch):
    app, client, path = managed_app(tmp_path, monkeypatch)
    try:
        login(client)
        result = client.post('/api/v1/strm/rebuild').json()
        assert result['rewritten'] == 1 and result['snapshot']
        written = path.read_text(encoding='utf-8')
        assert written.startswith(BASE + '/play115/' + PICK + '/')
        assert written.endswith('\n')
        store = app.state.store
        files = json.loads(store.db.execute(
            'SELECT files FROM output_registry WHERE file_id=?', (FID,)).fetchone()['files'])
        assert RELATIVE in files
        backup = store.db.execute('SELECT * FROM strm_rebuild_backups ORDER BY id DESC LIMIT 1').fetchone()
        assert backup['rows'] == 1 and RELATIVE in json.loads(backup['files'])
        # 回滚：字节回到旧地址，台账指纹同步
        rolled = client.post('/api/v1/strm/rebuild/rollback').json()
        assert rolled['restored'] == 1
        assert path.read_text(encoding='utf-8') == STALE
        files = json.loads(store.db.execute(
            'SELECT files FROM output_registry WHERE file_id=?', (FID,)).fetchone()['files'])
        assert files[RELATIVE] != ''
    finally:
        client.__exit__(None, None, None)


def test_rebuild_keeps_only_recent_snapshots(tmp_path, monkeypatch):
    app, client, path = managed_app(tmp_path, monkeypatch)
    try:
        rebuild = app.state.organizer.rebuild
        for _ in range(7):
            rebuild.snapshot({RELATIVE: STALE})
        count = app.state.store.db.execute(
            'SELECT count(*) FROM strm_rebuild_backups').fetchone()[0]
        assert count == 5
    finally:
        client.__exit__(None, None, None)


def test_rebuild_rollback_without_snapshot_reports_404(tmp_path, monkeypatch):
    app, client, path = managed_app(tmp_path, monkeypatch)
    try:
        login(client)
        assert client.post('/api/v1/strm/rebuild/rollback').status_code == 404
    finally:
        client.__exit__(None, None, None)


# ------------------------------------------------- 2) Emby 交付摘列

def test_emby_delivery_summary_columns_follow_payload(tmp_path):
    from types import SimpleNamespace
    from strimkeep.emby import EmbyDelivery

    store = Store(tmp_path / 'data', 'admin', PASSWORD)
    engine = SimpleNamespace(store=store, http=None, closing=False)
    delivery = EmbyDelivery(engine)
    payload = {'targets': [{'confirmed': True}, {'confirmed': False}, {'confirmed': False}]}
    with store.db:
        store.db.execute("INSERT INTO emby_deliveries(job_id,state,payload,next_due,deadline) "
                         "VALUES(1,'accepted','{}',0,0)")
    delivery.save(1, payload=json.dumps(payload))
    row = delivery.row(1)
    assert (row['total'], row['confirmed']) == (3, 1)
    delivery.save(1, payload=json.dumps({'targets': [{'confirmed': True}]}))
    row = delivery.row(1)
    assert (row['total'], row['confirmed']) == (1, 1)


def test_job_public_prefers_summary_columns_and_falls_back_to_payload(tmp_path, monkeypatch):
    """摘列存在时以列为准（不解析 payload）；列还是 0 时按 payload 计算。"""
    monkeypatch.setenv('ADMIN_USERNAME', 'admin')
    monkeypatch.setenv('ADMIN_PASSWORD', PASSWORD)
    app = create_app(tmp_path / 'data', tmp_path / 'strm', FakeCloud)
    with TestClient(app):
        store = app.state.store
        with store.db:
            job = store.db.execute(
                "INSERT INTO organize_jobs(state,config,created,updated) VALUES('running','{}',1,1)"
            ).lastrowid
            payload = {'targets': [{'confirmed': index == 0} for index in range(9)]}
            store.db.execute(
                "INSERT INTO emby_deliveries(job_id,state,payload,next_due,deadline,total,confirmed)"
                " VALUES(?,'accepted',?,0,0,?,?)", (job, json.dumps(payload), 9, 4))
        row = app.state.organizer.job(job)
        item = app.state.job_public(row)
        assert (item['emby']['total'], item['emby']['confirmed']) == (9, 4)
        with store.db:
            store.db.execute('UPDATE emby_deliveries SET total=0,confirmed=0 WHERE job_id=?', (job,))
        item = app.state.job_public(row)
        assert (item['emby']['total'], item['emby']['confirmed']) == (9, 1)


# ------------------------------------------------- 3) 存储层

def test_store_sets_wal_pragmas(tmp_path):
    store = Store(tmp_path / 'data', 'admin', PASSWORD)
    assert store.db.execute('PRAGMA journal_mode').fetchone()[0].lower() == 'wal'
    assert store.db.execute('PRAGMA busy_timeout').fetchone()[0] == 5000
    assert store.db.execute('PRAGMA synchronous').fetchone()[0] == 1     # 1 = NORMAL


def test_prune_in_thread_uses_its_own_connection(tmp_path):
    """维护 prune 在线程里必须用独立连接：共用连接跨线程会在 C 层 SIGSEGV。

    0.8.20 首次验证就是这样崩的（`asyncio.to_thread(store.prune)` 直接
    Fatal Python error: Segmentation fault，栈在 store.prune）。这条用例至少
    保证入口存在、能跑通、且不影响主连接的配置。
    """
    store = Store(tmp_path / 'data', 'admin', PASSWORD)
    old = time.time() - 400 * 86400
    with store.db:
        store.db.execute('INSERT INTO events(at,kind,message) VALUES(?,?,?)', (old, 'system', 'old'))
        store.db.execute('INSERT INTO events(at,kind,message) VALUES(?,?,?)', (time.time(), 'system', 'fresh'))
    result = store.prune_in_thread()
    assert result['events'] == 1
    messages = [row[0] for row in store.db.execute('SELECT message FROM events ORDER BY id')]
    assert messages == ['fresh']
    assert store.db.execute('PRAGMA busy_timeout').fetchone()[0] == 5000



def test_emby_deliveries_has_summary_columns(tmp_path):
    store = Store(tmp_path / 'data', 'admin', PASSWORD)
    columns = {row[1] for row in store.db.execute('PRAGMA table_info(emby_deliveries)')}
    assert {'total', 'confirmed'} <= columns

