"""0.8.21 回归：一线程一连接、线程内读写、热点端点、事件循环延迟度量。

对应 0.8.20 的 SIGSEGV 教训与 0.8.21 的改造（背景见 docs/releases/RELEASE_0.8.21.md）：

1. `Store.db` 变成「当前线程的连接」，工作线程里照常写 `store.db.execute(...)`；
2. 多个线程并发写不会崩、不会丢数据（这正是 0.8.20 崩掉的场景）；
3. 线程里能看到主线程已提交的数据（同一 WAL 库，不同连接）；
4. `/api/v1/tasks`、`/api/v1/logs`、`/api/v1/dashboard` 在改成 `to_thread` 之后仍然正确；
5. 慢请求仍然写入操作记录，供排障使用。
"""
import asyncio
import json
import time

from fastapi.testclient import TestClient

from strimkeep.main import create_app
from strimkeep.store import Store
from test_app import FakeCloud

PASSWORD = 'test-password-1234'


# ------------------------------------------------- 1) 一线程一连接

def test_store_gives_each_thread_its_own_connection(tmp_path):
    store = Store(tmp_path / 'data', 'admin', PASSWORD)
    main_connection = store.db

    async def worker():
        def run():
            connection = store.db
            with connection:
                connection.execute('INSERT INTO events(at,kind,message) VALUES(?,?,?)',
                                   (time.time(), 'system', 'from-worker'))
            return connection
        return await asyncio.to_thread(run)

    worker_connection = asyncio.run(worker())
    # 工作线程拿到的是另一条连接；主线程的连接没有变化
    assert worker_connection is not main_connection
    assert store.db is main_connection
    # 工作线程提交的数据，主线程（另一条连接）能读到
    messages = [row[0] for row in store.db.execute('SELECT message FROM events')]
    assert messages == ['from-worker']
    store.close()


def test_concurrent_threaded_writes_do_not_crash(tmp_path):
    """30 个线程同时写：0.8.20 在共用连接上会 SIGSEGV，这里必须全部落库。"""
    store = Store(tmp_path / 'data', 'admin', PASSWORD)

    async def scenario():
        def write(index):
            with store.db:
                store.db.execute('INSERT INTO events(at,kind,message) VALUES(?,?,?)',
                                 (time.time(), 'system', 'w%02d' % index))
            return index
        return await asyncio.gather(*(asyncio.to_thread(write, index) for index in range(30)))

    assert sorted(asyncio.run(scenario())) == list(range(30))
    assert store.db.execute('SELECT count(*) FROM events').fetchone()[0] == 30
    store.close()


def test_threaded_reads_see_committed_writes(tmp_path):
    store = Store(tmp_path / 'data', 'admin', PASSWORD)
    with store.db:
        store.db.execute("INSERT INTO settings VALUES('k','\"v\"')")

    async def read():
        return await asyncio.to_thread(
            lambda: store.db.execute("SELECT value FROM settings WHERE key='k'").fetchone()[0])

    assert asyncio.run(read()) == '"v"'
    store.close()


# ------------------------------------------------- 3) 改成工作线程后的端点

def test_emby_play_ids_match_previous_semantics():
    """`play_ids()` 抽出来之后，`matches()` 的判定必须与 0.8.20 完全一致。"""
    from strimkeep.emby import EmbyDelivery

    # 分享库的入口是 /play115/share/<sid>/…（0.8.21 之后只有这个前缀会被认成
    # share/<sid>；/play/share/… 会被 parse_play_path 当成 id="share"）。
    item = {'Id': '1',
            'Path': 'http://nas:9000/play/abc123/%E5%90%8D.mkv',
            'MediaSources': [{'Path': 'http://nas:9000/play115/share/sid9/name.mkv'}]}
    assert EmbyDelivery.play_ids(item) == {'abc123', 'share/sid9'}
    assert EmbyDelivery.matches(item, {'pid': 'abc123', 'relative': 'x.strm'})
    assert EmbyDelivery.matches(item, {'pid': 'share/sid9', 'relative': 'x.strm'})
    assert not EmbyDelivery.matches(item, {'pid': 'other', 'relative': 'x.strm'})
    # 没有播放入口时退回路径后缀比对（保持老语义）
    plain = {'Id': '2', 'Path': '/media/影视媒体库/电影/a.strm'}
    assert EmbyDelivery.play_ids(plain) == set()
    assert EmbyDelivery.matches(plain, {'pid': 'p', 'relative': '影视媒体库/电影/a.strm'})
    assert not EmbyDelivery.matches(plain, {'pid': 'p', 'relative': '别的目录/b.strm'})

def test_task_and_log_endpoints_still_work(tmp_path, monkeypatch):
    monkeypatch.setenv('ADMIN_USERNAME', 'admin')
    monkeypatch.setenv('ADMIN_PASSWORD', PASSWORD)
    app = create_app(tmp_path / 'data', tmp_path / 'strm', FakeCloud)
    with TestClient(app) as client:
        store = app.state.store
        with store.db:
            job = store.db.execute(
                "INSERT INTO organize_jobs(state,config,created,updated) VALUES('running','{}',1,1)"
            ).lastrowid
            store.db.execute("INSERT INTO organize_items(job_id,file_id,original,state,step)"
                             " VALUES(?,'f1','{}','review','未识别：测试')", (job,))
            store.db.execute('INSERT INTO events(at,kind,message) VALUES(?,?,?)',
                             (time.time(), 'organize', '线程读日志测试'))
        csrf = client.post('/api/v1/auth/login',
                           json={'username': 'admin', 'password': PASSWORD}).json()['csrf']
        client.headers['X-CSRF-Token'] = csrf
        tasks = client.get('/api/v1/tasks')
        assert tasks.status_code == 200
        listed = tasks.json()['active']
        assert [item['id'] for item in listed] == [job]
        assert listed[0]['unidentified'] == 1
        logs = client.get('/api/v1/logs?q=' + '线程读日志')
        assert logs.status_code == 200
        assert any('线程读日志测试' in row['message'] for row in logs.json()['items'])
        dashboard = client.get('/api/v1/dashboard')
        assert dashboard.status_code == 200
        assert 'version' in dashboard.json()['system']
