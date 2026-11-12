"""TMDB 404 不该停整批；离线重复提交要明确拒绝。

背景（0.9.33）：
- job 647 因为一部片在 TMDB 返回 404，整批整理被 halt。
- 同一链接重复提交离线时没有任何提示，只是被 115 回「任务已存在」后转核查。
"""
import asyncio
import sqlite3
import time

from strimkeep.cloud115 import CloudError
from strimkeep.offline_api import OfflinePostProcess


class Store:
    def __init__(self, conn):
        self.db = conn
        self.events = []
        self.values = {}

    def event(self, kind, message):
        self.events.append((kind, message))

    def get(self, key, default=None):
        return self.values.get(key, default)

    def directory_label(self, cid, fallback=''):
        return fallback or ''

    def set_meta(self, key, value):
        self.values[key] = value


def make_db():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.executescript('''
        CREATE TABLE offline_submissions(id INTEGER PRIMARY KEY, urls TEXT, target_cid TEXT,
                                         target_label TEXT, before_ids TEXT, submitted_at REAL,
                                         last_checked REAL, state TEXT, message TEXT,
                                         organize_job_id INTEGER, organize INTEGER,
                                         attempts INTEGER NOT NULL DEFAULT 0, stalled_at REAL);
    ''')
    return conn


def engine_on(conn):
    obj = OfflinePostProcess.__new__(OfflinePostProcess)
    obj.store = Store(conn)
    obj.engine = obj
    return obj


def test_duplicate_link_is_rejected_before_submitting():
    conn = make_db()
    conn.execute("INSERT INTO offline_submissions VALUES (1,?,'100','','[]',?,0,'submitted','',NULL,1,0,NULL)",
                 ('["ed2k://|file|A.mkv|1|ABC|/"]', time.time()))
    engine = engine_on(conn)
    try:
        asyncio.run(engine.submit(['ed2k://|file|A.mkv|1|ABC|/'], '100', '目录', origin='web'))
    except CloudError as exc:
        assert exc.code == 'OFFLINE_DUPLICATE'
        assert '#1' in str(exc)
    else:
        raise AssertionError('重复链接必须被拒绝')
    assert conn.execute("SELECT COUNT(*) FROM offline_submissions").fetchone()[0] == 1


def test_finished_link_can_be_submitted_again():
    """已结束的提交不挡路，允许重新提交（比如上次失败想再试一次）。"""
    conn = make_db()
    conn.execute("INSERT INTO offline_submissions VALUES (1,?,'100','','[]',?,0,'expired','',NULL,1,0,NULL)",
                 ('["ed2k://|file|A.mkv|1|ABC|/"]', time.time()))
    engine = engine_on(conn)
    engine.engine = type('E', (), {'cloud': type('C', (), {
        'children': staticmethod(lambda cid: asyncio.sleep(0, result=[])),
        'offline_add': staticmethod(lambda urls, cid: asyncio.sleep(0, result=None)),
    })()})()
    try:
        asyncio.run(engine.submit(['ed2k://|file|A.mkv|1|ABC|/'], '100', '目录', origin='web'))
    except CloudError as exc:
        assert exc.code != 'OFFLINE_DUPLICATE'
