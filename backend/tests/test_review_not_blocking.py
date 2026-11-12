"""未识别是正常结果，不该让任务变成「部分未完成」；任务结束后立刻对账。

背景（0.9.39/0.9.40）：用户抱怨「一出错就要人工干预，干预完成功了还要报错」。
根因：任务结束判定把 `review`（未识别）当成失败；人工点结项又只改任务状态不改条目。
0.9.40 再补：任务一结束就开始对账，用户在未识别目录删了文件，账本立刻跟着闭环。
"""
import asyncio
import sqlite3
import time

from strimkeep.cloud115 import CloudError
from strimkeep.organizer import Organizer


class Store:
    def __init__(self, conn):
        self.db = conn
        self.events = []

    def event(self, kind, message):
        self.events.append((kind, message))

    def get(self, key, default=None):
        return default

    def directory_label(self, cid, fallback=''):
        return fallback or ''


class Cloud:
    def __init__(self, missing=()):
        self.missing = set(missing)
        self.calls = []

    async def file(self, fid):
        self.calls.append(fid)
        if fid in self.missing:
            raise CloudError('源文件不存在或无法访问。', 'FILE_NOT_FOUND', 404)
        return {'id': fid, 'name': 'x.mkv', 'parent_id': '888'}


def make_db(states):
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.executescript('''
        CREATE TABLE organize_jobs(id INTEGER PRIMARY KEY, state TEXT, message TEXT, stage TEXT,
                                   updated REAL, notify_state TEXT);
        CREATE TABLE organize_items(id INTEGER PRIMARY KEY, job_id INTEGER, file_id TEXT, state TEXT,
                                    plan TEXT, step TEXT, message TEXT, original TEXT);
        CREATE TABLE organized_files(file_id TEXT);
        CREATE TABLE share_strms(file_id TEXT, revoked INTEGER);
        INSERT INTO organize_jobs VALUES (9,'partial','旧批次','批次结束',0,'verified');
    ''')
    for i, st in enumerate(states, start=1):
        conn.execute("INSERT INTO organize_items VALUES (?,9,?,?,'{}','待处理','','{}')",
                     (i, 'f%d' % i, st))
    return conn


def engine_on(conn):
    engine = Organizer.__new__(Organizer)
    engine.store = Store(conn)
    engine.cloud = Cloud()

    def update_job(job_id, **fields):
        keys = [k for k in ('state', 'message', 'stage', 'notify_state') if k in fields]
        if not keys:
            return
        conn.execute('UPDATE organize_jobs SET %s WHERE id=?' % ','.join('%s=?' % k for k in keys),
                     (*[fields[k] for k in keys], job_id))
        conn.commit()

    engine.update_job = update_job
    return engine


def test_only_review_items_do_not_block_settling():
    """只剩未识别条目时，任务应该自己结项，而不是永远显示部分未完成。"""
    conn = make_db(['review', 'review'])
    engine = engine_on(conn)
    assert engine.settle_jobs() == [9]
    assert conn.execute('SELECT state FROM organize_jobs WHERE id=9').fetchone()[0] == 'completed'


def test_pending_items_still_block_settling():
    conn = make_db(['review', 'pending'])
    engine = engine_on(conn)
    assert engine.settle_jobs() == []
    assert conn.execute('SELECT state FROM organize_jobs WHERE id=9').fetchone()[0] == 'partial'


def test_finished_jobs_are_reconciled_without_waiting_a_day():
    """任务结束后立刻对账：用户在未识别目录删了文件，账本马上闭环。"""
    conn = make_db(['review'])
    conn.execute("UPDATE organize_jobs SET state='completed', updated=?", (time.time(),))
    engine = engine_on(conn)
    engine.cloud = Cloud(missing={'f1'})
    assert asyncio.run(engine.reconcile_ledger(now=time.time())) == 1
    assert conn.execute('SELECT step FROM organize_items WHERE id=1').fetchone()[0] == '源文件已不在网盘'


def test_running_jobs_still_wait_a_day():
    conn = make_db(['review'])
    conn.execute("UPDATE organize_jobs SET state='running', updated=?", (time.time(),))
    engine = engine_on(conn)
    engine.cloud = Cloud(missing={'f1'})
    assert asyncio.run(engine.reconcile_ledger(now=time.time())) == 0
