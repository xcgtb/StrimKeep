"""媒体信息补扫不许对同一个文件反复入队。

背景（0.9.16）：补扫原来的跳过条件是「有 sidecar **且** Emby 有视频流」。
sidecar 在、Emby 还没解析出视频流时，每 5 分钟重新入队一次——现场队列 2609 行里
484 行是重复，每轮固定报「为 15 条缺失 sidecar 的受管 STRM 入队」。

0.9.48：跳过之后 Emby 就再没人管了（现场 70 条 STRM 在 Emby 里恒为 0 条流），
所以「sidecar 在、Emby 没有视频流」改成交给 probe_emby() 的独立记账（emby_state）
继续追；补扫只负责「Emby 有流就标已确认」和「没有记账行就补一条」，既不新增重复行，
也不重置 probe_emby 的退避。
"""
import sqlite3
import time

from strimkeep.native_media_info import NativeMediaInfo


class Store:
    def __init__(self, conn):
        self.db = conn
        self.events = []

    def event(self, kind, message):
        self.events.append((kind, message))


class Delivery:
    @staticmethod
    def play_ids(item):
        return ['p1']

    @staticmethod
    def matches(item, target):
        return True


class Engine:
    def __init__(self, root):
        self.root = str(root)


def make_db(rows, emby_state='pending', emby_next_due=0):
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.executescript('''
        CREATE TABLE native_media_info_queue(job_id INTEGER, item_id TEXT, target TEXT,
                                             connection TEXT, state TEXT, attempts INTEGER, next_due REAL,
                                             emby_state TEXT NOT NULL DEFAULT 'pending',
                                             emby_attempts INTEGER NOT NULL DEFAULT 0,
                                             emby_next_due REAL NOT NULL DEFAULT 0);
        CREATE TABLE playbacks(id TEXT, relative_path TEXT, revoked INTEGER);
        CREATE TABLE share_strms(id TEXT, strm_path TEXT, revoked INTEGER);
        INSERT INTO playbacks VALUES ('p1','a.strm',0);
    ''')
    for state in rows:
        conn.execute("INSERT INTO native_media_info_queue(job_id,item_id,target,connection,state,"
                     "attempts,next_due,emby_state,emby_next_due) VALUES (0,'1',?,'fp',?,0,0,?,?)",
                     ('{"relative":"a.strm","pid":"p1"}', state, emby_state, emby_next_due))
    return conn


def engine_on(tmp_path, conn):
    engine = NativeMediaInfo.__new__(NativeMediaInfo)
    engine.store = Store(conn)
    engine.engine = Engine(tmp_path)
    engine.delivery = Delivery()
    engine.queued = []

    def enqueue(job_id, item, target, fingerprint):
        engine.queued.append(target['relative'])
        return True

    engine.enqueue = enqueue
    return engine


def touch(tmp_path, with_sidecar):
    (tmp_path / 'a.strm').write_text('http://x/play115/p1', encoding='utf-8')
    if with_sidecar:
        (tmp_path / 'a-media-info.json').write_text('{}', encoding='utf-8')


def item():
    return {'Id': 'e1', 'Path': '/media/a.strm', 'MediaStreams': [{'Type': 'Video'}]}


def test_sidecar_present_and_already_done_is_not_requeued(tmp_path):
    touch(tmp_path, with_sidecar=True)
    engine = engine_on(tmp_path, make_db(['done']))
    assert engine._sweep_files([item()], 'fp') == 0
    assert engine.queued == []


def test_sidecar_present_without_a_done_row_is_queued_once(tmp_path):
    """sidecar 在、连记账行都没有（被清理过）：补一条让本地/Emby 两半都重走一次。"""
    touch(tmp_path, with_sidecar=True)
    engine = engine_on(tmp_path, make_db([]))
    assert engine._sweep_files([{'Id': 'e1', 'Path': '/media/a.strm'}], 'fp') == 1
    assert engine.queued == ['a.strm']


def test_missing_sidecar_is_still_queued(tmp_path):
    touch(tmp_path, with_sidecar=False)
    engine = engine_on(tmp_path, make_db(['done']))
    assert engine._sweep_files([item()], 'fp') == 1
    assert engine.queued == ['a.strm']


def test_already_queued_is_skipped(tmp_path):
    touch(tmp_path, with_sidecar=False)
    engine = engine_on(tmp_path, make_db(['queued']))
    assert engine._sweep_files([item()], 'fp') == 0
    assert engine.queued == []



def test_sidecar_present_with_emby_empty_is_left_to_probe_emby(tmp_path):
    """sidecar 在、Emby 还没解析出视频流：补扫不建行、也不重置 probe_emby 的退避。"""
    touch(tmp_path, with_sidecar=True)
    scheduled = time.time() + 3600
    engine = engine_on(tmp_path, make_db(['done'], emby_state='pending', emby_next_due=scheduled))
    assert engine._sweep_files([{'Id': 'e1', 'Path': '/media/a.strm'}], 'fp') == 0
    assert engine.queued == []
    row = engine.store.db.execute(
        'SELECT emby_state,emby_next_due FROM native_media_info_queue').fetchone()
    assert row['emby_state'] == 'pending' and row['emby_next_due'] == scheduled

    # Emby 后来自己探出来了：顺手标成已确认（连 gave_up 的也算），不是只跳过。
    engine = engine_on(tmp_path, make_db(['done'], emby_state='gave_up', emby_next_due=scheduled))
    assert engine._sweep_files([item()], 'fp') == 0
    assert engine.store.db.execute(
        'SELECT emby_state FROM native_media_info_queue').fetchone()['emby_state'] == 'confirmed'


def test_twin_in_another_library_already_has_streams_is_confirmed(tmp_path):
    """同名文件在另一个库已有视频流：Emby 视为同一部片，本条不再追（现场 7 条白跑）。"""
    (tmp_path / '分享影视库').mkdir()
    (tmp_path / '分享影视库' / 'a.strm').write_text('http://x/play115/p1', encoding='utf-8')
    (tmp_path / '分享影视库' / 'a-media-info.json').write_text('{}', encoding='utf-8')
    conn = make_db(['done'], emby_state='pending')
    conn.execute("INSERT INTO playbacks VALUES ('p1','分享影视库/a.strm',0)")
    # 记账行的 relative 必须和真实 STRM 一致（make_db 默认写的是顶层 a.strm）。
    conn.execute('UPDATE native_media_info_queue SET target=?',
                 ('{"relative":"分享影视库/a.strm","pid":"p1"}',))
    engine = engine_on(tmp_path, conn)
    ours = {'Id': 'e1', 'Path': '/media/分享影视库/a.strm', 'MediaStreams': []}
    twin = {'Id': 'twin', 'Path': '/media/媒体库/a.strm',
            'MediaStreams': [{'Type': 'Video', 'Codec': 'hevc'}]}
    assert engine._sweep_files([ours, twin], 'fp') == 0
    assert engine.queued == []
    assert engine.store.db.execute(
        'SELECT emby_state FROM native_media_info_queue').fetchone()['emby_state'] == 'confirmed'
    assert any('另一个库的同名条目已有视频流' in message for _, message in engine.store.events)

