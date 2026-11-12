"""结项不许补发历史入库通知。

背景（0.9.15）：把 job 218 从 partial 改成 completed 之后，它 258 条「新片入库」
通知被集中补发（每条间隔 1.1 秒，刷了六分钟）。结项只是把账本收口，不是「这批刚入库」。
"""
import asyncio
import json
import sqlite3

from strimkeep.notifications import Notifications


class Store:
    def __init__(self, conn):
        self.db = conn
        self.events = []

    def get(self, key, default=None):
        return default

    def event(self, kind, message):
        self.events.append((kind, message))


def make_db(stage, notify_state='pending'):
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.executescript('''
        CREATE TABLE organize_jobs(id INTEGER PRIMARY KEY, state TEXT, stage TEXT, message TEXT,
                                   notify_state TEXT, config TEXT);
        CREATE TABLE organize_items(id INTEGER PRIMARY KEY, job_id INTEGER, state TEXT, plan TEXT);
        CREATE TABLE notifications(id INTEGER PRIMARY KEY, at REAL, level TEXT, job_id INTEGER,
                                   event_key TEXT, title TEXT, message TEXT, read_at REAL,
                                   telegram_state TEXT, attempts INTEGER, next_try REAL, photo TEXT);
        CREATE TABLE media_notice_groups(event_key TEXT PRIMARY KEY, job_id INTEGER, items TEXT);
        CREATE TABLE media_notice_outbox(job_id INTEGER PRIMARY KEY);
        CREATE TABLE strm_group_outbox(job_id INTEGER, group_key TEXT, items TEXT);
        CREATE TABLE strm_reported_items(job_id INTEGER, item_id INTEGER);
        CREATE TABLE telegram_message_refs(event_key TEXT, chat_id TEXT, message_id INTEGER, photo INTEGER, edited TEXT, attempts INTEGER, next_try REAL);
    ''')
    conn.execute("INSERT INTO organize_jobs VALUES (218,'completed',?, '', ?, ?)",
                 (stage, notify_state, json.dumps({'job_kind': 'organize'})))
    conn.execute("INSERT INTO organize_items VALUES (1,218,'done',?)",
                 (json.dumps({'relative_path': '媒体库/电影/x/x.strm', 'details': {'id': 1},
                              'hint': {'media_type': 'movie'}}),))
    return conn


def engine_on(conn):
    engine = Notifications.__new__(Notifications)
    engine.store = Store(conn)
    engine.published = []

    async def media_report(row):
        engine.published.append(row['id'])

    engine.media_report = media_report
    return engine


def test_settled_job_does_not_replay_media_notices():
    conn = make_db('已自动结项')
    engine = engine_on(conn)
    asyncio.run(engine._job(dict(conn.execute('SELECT * FROM organize_jobs WHERE id=218').fetchone())))
    assert engine.published == []                       # 一条都不补发
    assert conn.execute('SELECT notify_state FROM organize_jobs WHERE id=218').fetchone()[0] == 'not_needed'


def test_manual_settle_stage_is_also_silent():
    conn = make_db('已人工结项')
    engine = engine_on(conn)
    asyncio.run(engine._job(dict(conn.execute('SELECT * FROM organize_jobs WHERE id=218').fetchone())))
    assert engine.published == []


def test_normal_completion_still_reports():
    conn = make_db('批次结束')
    engine = engine_on(conn)
    asyncio.run(engine._job(dict(conn.execute('SELECT * FROM organize_jobs WHERE id=218').fetchone())))
    assert engine.published == [218]                    # 正常跑完的通知照发
