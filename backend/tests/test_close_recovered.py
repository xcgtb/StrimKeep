"""历史积压自动闭环：同一文件后来整理成功的，旧批次不再挂账。

背景（0.9.9）：现场 231 条 review/pending 里有 99 条是「未识别目录里的 08.mp4 后来又
整理成功」这种历史残留，没有任何人工价值，却让任务中心一直显示「部分未完成」。
"""
import sqlite3

from strimkeep.organizer import Organizer


class Store:
    def __init__(self, conn):
        self.db = conn
        self.events = []

    def event(self, kind, message):
        self.events.append((kind, message))


def engine_on(conn):
    engine = Organizer.__new__(Organizer)
    engine.store = Store(conn)
    engine.job = lambda job_id: (dict(conn.execute('SELECT * FROM organize_jobs WHERE id=?', (job_id,)).fetchone() or {}) or None)

    def update_job(job_id, **fields):
        keys = [k for k in ('state', 'message', 'stage') if k in fields]
        if not keys:
            return
        conn.execute('UPDATE organize_jobs SET %s WHERE id=?' % ','.join('%s=?' % k for k in keys),
                     (*[fields[k] for k in keys], job_id))
        conn.commit()

    engine.update_job = update_job
    return engine


def make_db():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.executescript('''
        CREATE TABLE organize_jobs(id INTEGER PRIMARY KEY, state TEXT, message TEXT, stage TEXT, notify_state TEXT);
        CREATE TABLE organize_items(id INTEGER PRIMARY KEY, job_id INTEGER, file_id TEXT, state TEXT,
                                    plan TEXT, step TEXT, message TEXT, original TEXT);
    ''')
    return conn


def test_same_file_success_later_closes_the_old_item_only():
    conn = make_db()
    conn.executescript('''
        INSERT INTO organize_jobs VALUES (9,'partial','已跳过 2 个视频。','批次结束','sent');
        INSERT INTO organize_jobs VALUES (40,'partial','已跳过 1 个视频。','批次结束','sent');
        INSERT INTO organize_items VALUES (1,9,'f1','review','{}','未识别，已转待确认目录','文件名只有集号','{"name":"08.mp4"}');
        INSERT INTO organize_items VALUES (2,9,'f2','review','{}','未识别，已转待确认目录','缺少作品线索','{"name":"09.mp4"}');
        INSERT INTO organize_items VALUES (3,40,'f3','done','{}','归档及 STRM 完成','','{"name":"08.mp4"}');
    ''')
    engine = engine_on(conn)
    assert engine.close_recovered_items() == 1
    state, step, message = conn.execute('SELECT state,step,message FROM organize_items WHERE id=1').fetchone()
    assert state == 'skipped'                    # 落到终态，任务中心的待处理计数才会减少
    assert step == '已由后续批次补齐'
    assert '文件名只有集号' in message           # 原始原因仍可追溯
    assert conn.execute('SELECT step FROM organize_items WHERE id=2').fetchone()[0] == '未识别，已转待确认目录'
    assert engine.job(9)['state'] == 'partial'  # 还有别的未完成，不结项
    assert engine.job(40)['state'] == 'partial'  # 没有被闭环的批次不碰


def test_older_success_never_closes_a_newer_item():
    conn = make_db()
    conn.executescript('''
        INSERT INTO organize_jobs VALUES (40,'completed','已整理。','批次结束','sent');
        INSERT INTO organize_jobs VALUES (41,'partial','部分未完成。','批次结束','sent');
        INSERT INTO organize_items VALUES (1,40,'f1','done','{}','完成','','{"name":"08.mp4"}');
        INSERT INTO organize_items VALUES (2,41,'f2','review','{}','未识别','缺线索','{"name":"08.mp4"}');
    ''')
    engine = engine_on(conn)
    assert engine.close_recovered_items() == 0
    assert engine.job(41)['state'] == 'partial'


def test_job_with_failed_notify_is_not_closed():
    conn = make_db()
    conn.executescript('''
        INSERT INTO organize_jobs VALUES (9,'partial','Emby 通知失败。','批次结束','failed');
        INSERT INTO organize_jobs VALUES (40,'completed','已整理。','批次结束','sent');
        INSERT INTO organize_items VALUES (1,9,'f1','review','{}','未识别','缺线索','{"name":"08.mp4"}');
        INSERT INTO organize_items VALUES (2,40,'f2','done','{}','完成','','{"name":"08.mp4"}');
    ''')
    engine = engine_on(conn)
    assert engine.close_recovered_items() == 1
    assert conn.execute('SELECT step FROM organize_items WHERE id=1').fetchone()[0] == '已由后续批次补齐'
    assert engine.job(9)['state'] == 'partial'   # 通知失败要人工看，不自动结项
