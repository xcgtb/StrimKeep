"""账本自动对账 + 任务自动结项。

背景（0.9.15）：用户在 115 里手动删除/移回待整理/重命名之后，账本一直挂着要人来点完结。
账本应该自己对账：文件没了、已入库、分享失效、已移回待整理 → 自己闭环。
判定只认 file_id，所以手动重命名不影响。
"""
import asyncio
import sqlite3

from strimkeep.cloud115 import CloudError
from strimkeep.organizer import Organizer


class Store:
    def __init__(self, conn, roots=('100',)):
        self.db = conn
        self.roots = list(roots)
        self.events = []

    def get(self, key, default=None):
        if key == 'organize_config':
            return {'source_cids': self.roots}
        return default

    def event(self, kind, message):
        self.events.append((kind, message))


class Cloud:
    def __init__(self, parents=None, missing=(), broken=()):
        self.parents = parents or {}
        self.missing = set(missing)
        self.broken = set(broken)
        self.calls = []

    async def file(self, fid):
        self.calls.append(fid)
        if fid in self.missing:
            raise CloudError('源文件不存在或无法访问。', 'FILE_NOT_FOUND', 404)
        if fid in self.broken:
            raise CloudError('115 限流', 'RATE_LIMITED', 429)
        return {'id': fid, 'name': 'x.mkv', 'parent_id': self.parents.get(fid, '999')}


def make_db():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.executescript('''
        CREATE TABLE organize_jobs(id INTEGER PRIMARY KEY, state TEXT, message TEXT, stage TEXT,
                                   updated REAL, notify_state TEXT);
        CREATE TABLE organize_items(id INTEGER PRIMARY KEY, job_id INTEGER, file_id TEXT, state TEXT,
                                    plan TEXT, step TEXT, message TEXT);
        CREATE TABLE organized_files(file_id TEXT);
        CREATE TABLE share_strms(file_id TEXT, revoked INTEGER);
        INSERT INTO organize_jobs VALUES (9,'partial','旧批次','批次结束',0,'verified');
    ''')
    return conn


def engine_on(conn, cloud):
    engine = Organizer.__new__(Organizer)
    engine.store = Store(conn)
    engine.cloud = cloud

    def update_job(job_id, **fields):
        keys = [k for k in ('state', 'message', 'stage') if k in fields]
        if not keys:
            return
        conn.execute('UPDATE organize_jobs SET %s WHERE id=?' % ','.join('%s=?' % k for k in keys),
                     (*[fields[k] for k in keys], job_id))
        conn.commit()

    engine.update_job = update_job
    return engine


def add_item(conn, iid, fid, state='pending', message='TMDB 请求失败。'):
    conn.execute('INSERT INTO organize_items VALUES (?,?,?,?,?,?,?)',
                 (iid, 9, fid, state, '{}', '待处理', message))


def test_file_already_archived_closes_without_network():
    conn = make_db()
    add_item(conn, 1, 'f1')
    conn.execute("INSERT INTO organized_files VALUES ('f1')")
    cloud = Cloud()
    engine = engine_on(conn, cloud)
    assert asyncio.run(engine.reconcile_ledger(now=10 ** 9)) == 1
    state, step, message = conn.execute('SELECT state,step,message FROM organize_items WHERE id=1').fetchone()
    assert (state, step) == ('skipped', '文件已归档')
    assert 'TMDB 请求失败' in message            # 原始原因保留
    assert cloud.calls == []                     # 本地就能判，不打 115


def test_share_revoked_and_live_are_both_closed():
    conn = make_db()
    add_item(conn, 1, 'gone-share')
    add_item(conn, 2, 'live-share')
    conn.execute("INSERT INTO share_strms VALUES ('gone-share',1)")
    conn.execute("INSERT INTO share_strms VALUES ('live-share',0)")
    engine = engine_on(conn, Cloud())
    assert asyncio.run(engine.reconcile_ledger(now=10 ** 9)) == 2
    steps = dict(conn.execute('SELECT file_id,step FROM organize_items').fetchall())
    assert steps == {'gone-share': '分享已失效', 'live-share': '文件已入库'}


def test_parent_zero_means_the_file_is_gone():
    conn = make_db()
    add_item(conn, 1, 'f1')
    engine = engine_on(conn, Cloud(parents={'f1': '0'}))
    assert asyncio.run(engine.reconcile_ledger(now=10 ** 9)) == 1
    assert conn.execute('SELECT step FROM organize_items WHERE id=1').fetchone()[0] == '源文件已不在网盘'


def test_file_moved_back_to_the_scan_root_closes_the_old_row():
    conn = make_db()
    add_item(conn, 1, 'f1')
    engine = engine_on(conn, Cloud(parents={'f1': '100'}))
    assert asyncio.run(engine.reconcile_ledger(now=10 ** 9)) == 1
    assert '已人工移回待整理' in conn.execute('SELECT step FROM organize_items WHERE id=1').fetchone()[0]


def test_file_still_elsewhere_is_left_alone():
    conn = make_db()
    add_item(conn, 1, 'f1')
    engine = engine_on(conn, Cloud(parents={'f1': '888'}))
    assert asyncio.run(engine.reconcile_ledger(now=10 ** 9)) == 0
    assert conn.execute('SELECT state FROM organize_items WHERE id=1').fetchone()[0] == 'pending'


def test_rate_limit_stops_the_pass_and_touches_nothing():
    conn = make_db()
    for i in range(1, 6):
        add_item(conn, i, 'f%d' % i)
    engine = engine_on(conn, Cloud(broken={'f1', 'f2', 'f3', 'f4', 'f5'}))
    assert asyncio.run(engine.reconcile_ledger(now=10 ** 9)) == 0
    assert conn.execute("SELECT COUNT(*) FROM organize_items WHERE state='pending'").fetchone()[0] == 5
    assert len(engine.cloud.calls) == 3           # 连续 3 次异常就停手


def test_recent_jobs_are_not_touched():
    conn = make_db()
    conn.execute("UPDATE organize_jobs SET updated=?", (10 ** 9,))
    add_item(conn, 1, 'f1')
    engine = engine_on(conn, Cloud(missing={'f1'}))
    assert asyncio.run(engine.reconcile_ledger(now=10 ** 9 + 60)) == 0


def test_settle_jobs_closes_a_job_with_no_pending_items():
    conn = make_db()
    conn.execute("INSERT INTO organize_items VALUES (1,9,'f1','done','{}','归档及 STRM 完成','')")
    engine = engine_on(conn, Cloud())
    assert engine.settle_jobs() == [9]
    job = dict(conn.execute('SELECT state,stage,message FROM organize_jobs WHERE id=9').fetchone())
    assert job['state'] == 'completed' and job['stage'] == '已自动结项'
    assert '旧批次' in job['message'] and '自动结项' in job['message']


def test_settle_jobs_leaves_jobs_with_work_or_failed_notify():
    conn = make_db()
    conn.execute("INSERT INTO organize_items VALUES (1,9,'f1','pending','{}','待处理','')")
    engine = engine_on(conn, Cloud())
    assert engine.settle_jobs() == []
    assert conn.execute('SELECT state FROM organize_jobs WHERE id=9').fetchone()[0] == 'partial'

    conn.execute("UPDATE organize_items SET state='done'")
    conn.execute("UPDATE organize_jobs SET notify_state='failed'")
    assert engine.settle_jobs() == []
    assert conn.execute('SELECT state FROM organize_jobs WHERE id=9').fetchone()[0] == 'partial'
