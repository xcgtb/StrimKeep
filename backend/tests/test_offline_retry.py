"""离线提交：核查失败退避、重复提交转核查、超期结项、卡住回执。

背景：
- 0.9.18：`tick()` 里任何 CloudError 都直接转 needs_review，现场 99 条
  「核查未完成：115 请求未完成」一次就判死；28 条「任务已存在」也被当失败。
- 0.9.19：超过 72 小时没认领产物的自己结项。
- 0.9.21：卡在下载中的任务每 12 小时汇总一条回执，用户才知道该去 115 换源。
"""
import asyncio
import sqlite3
import time

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
        labels = (self.values.get('organize_config') or {}).get('directory_labels') or {}
        return labels.get(str(cid)) or fallback or ''

    def meta(self, key):
        return self.values.get(key, '')

    def set_meta(self, key, value):
        self.values[key] = value


class FakeNotifications:
    def __init__(self):
        self.sent = []

    async def publish(self, key, title, message, level='info', job_id=None, test=False,
                      photo=None, chat_override=None, force=False, category=None):
        self.sent.append((title, message))
        return 'sent'


def make_db(state='submitted', attempts=0, age=0):
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.executescript('''
        CREATE TABLE offline_submissions(id INTEGER PRIMARY KEY, urls TEXT, target_cid TEXT,
                                         target_label TEXT, before_ids TEXT, submitted_at REAL,
                                         last_checked REAL, state TEXT, message TEXT,
                                         organize_job_id INTEGER, organize INTEGER,
                                         attempts INTEGER NOT NULL DEFAULT 0, stalled_at REAL);
    ''')
    urls = '["ed2k://|file|Movie.S01E01.mkv|123|ABC|/"]'
    conn.execute("INSERT INTO offline_submissions VALUES (1,?,'100','','[]',?,0,?,'',NULL,1,?,NULL)",
                 (urls, time.time() - age, state, attempts))
    return conn


def engine_on(conn):
    obj = OfflinePostProcess.__new__(OfflinePostProcess)
    obj.store = Store(conn)
    obj.INTERVAL = 60
    obj.next_run = 0
    obj.engine = obj        # report_stalled 通过 self.engine.notifications 发回执
    return obj


def test_timeout_keeps_retrying_instead_of_giving_up():
    conn = make_db()
    engine = engine_on(conn)
    row = dict(conn.execute('SELECT * FROM offline_submissions WHERE id=1').fetchone())
    engine._retry_later(row, '核查未完成：115 请求未完成')
    state, message, attempts = conn.execute(
        'SELECT state,message,attempts FROM offline_submissions WHERE id=1').fetchone()
    assert state == 'submitted'                   # 继续核查，不转人工
    assert attempts == 1
    assert '稍后重试' in message


def test_it_gives_up_only_after_the_retry_limit():
    conn = make_db(attempts=5)
    engine = engine_on(conn)
    row = dict(conn.execute('SELECT * FROM offline_submissions WHERE id=1').fetchone())
    engine._retry_later(row, '核查未完成：115 请求未完成')
    state, message, attempts = conn.execute(
        'SELECT state,message,attempts FROM offline_submissions WHERE id=1').fetchone()
    assert state == 'needs_review'                # 第 6 次才转人工
    assert attempts == 6 and '转人工' in message


def test_stale_submissions_expire_instead_of_haunting_the_ui():
    conn = make_db(state='needs_review', age=4 * 86400)
    engine = engine_on(conn)
    asyncio.run(engine.tick(now=time.time() + 1))
    state, message = conn.execute('SELECT state,message FROM offline_submissions WHERE id=1').fetchone()
    assert state == 'expired'
    assert '72 小时' in message


def test_fresh_submission_is_not_expired():
    conn = make_db(state='submitted', age=0)
    engine = engine_on(conn)
    asyncio.run(engine.tick(now=time.time() + 1))
    assert conn.execute('SELECT state FROM offline_submissions WHERE id=1').fetchone()[0] == 'submitted'


def test_stalled_tasks_get_one_summary_receipt():
    """10 分钟没下完、且本轮核查明确「下载进行中」才回执，同一个任务不重复提醒。"""
    conn = make_db(state='submitted', age=14 * 60)
    conn.execute("UPDATE offline_submissions SET message='下载进行中（任务列表 0/1 已完成）。'")
    engine = engine_on(conn)
    engine.notifications = FakeNotifications()
    now = time.time()
    assert asyncio.run(engine.report_stalled(now=now, ids=[1])) == 1
    assert len(engine.notifications.sent) == 1
    title, message = engine.notifications.sent[0]
    assert title == '⏳ 离线下载 · 超时'
    assert '📄 文件  Movie.S01E01.mkv' in message
    assert '👉 到 115 离线列表删掉换源' in message
    assert '115 网盘 / 115 网盘' not in message      # 前缀只出现一次
    # 同一个任务已经回执过，不再重复
    assert asyncio.run(engine.report_stalled(now=now + 600, ids=[1])) == 0
    assert len(engine.notifications.sent) == 1


def test_fresh_submissions_do_not_trigger_a_receipt():
    """5 分钟内的还在正常下载窗口里，不打扰。"""
    conn = make_db(state='submitted', age=5 * 60)
    conn.execute("UPDATE offline_submissions SET message='下载进行中（任务列表 0/1 已完成）。'")
    engine = engine_on(conn)
    engine.notifications = FakeNotifications()
    assert asyncio.run(engine.report_stalled(now=time.time(), ids=[1])) == 0
    assert engine.notifications.sent == []


def test_submission_not_checked_this_round_is_not_reported_as_stalled():
    """刚提交还没轮到核查的，不能猜成超时（现场误报就是这条）。"""
    conn = make_db(state='submitted', age=30 * 60)
    conn.execute("UPDATE offline_submissions SET message='已提交，等待下载完成后定位本批产物并自动整理。'")
    engine = engine_on(conn)
    engine.notifications = FakeNotifications()
    assert asyncio.run(engine.report_stalled(now=time.time(), ids=[])) == 0
    assert engine.notifications.sent == []


def test_deleted_task_is_not_reported_as_stalled():
    """115 里匹配不到任务（已被删）时只写「等待确认身份」，不算超时。"""
    conn = make_db(state='submitted', age=30 * 60)
    conn.execute("UPDATE offline_submissions SET message='等待确认离线任务身份；不会认领目录中的其他文件。'")
    engine = engine_on(conn)
    engine.notifications = FakeNotifications()
    assert asyncio.run(engine.report_stalled(now=time.time(), ids=[1])) == 0
    assert engine.notifications.sent == []


def test_receipt_uses_the_current_directory_label():
    """目录改名后回执跟着变，不是提交时的快照，也不重复加前缀。"""
    conn = make_db(state='submitted', age=14 * 60)
    conn.execute("UPDATE offline_submissions SET message='下载进行中（任务列表 0/1 已完成）。'")
    engine = engine_on(conn)
    engine.notifications = FakeNotifications()
    engine.store.values['organize_config'] = {'directory_labels': {'100': '115 网盘 / 新名字'}}
    asyncio.run(engine.report_stalled(now=time.time(), ids=[1]))
    _, message = engine.notifications.sent[0]
    assert '📂 目标  115 网盘 / 新名字' in message
    assert message.count('115 网盘') == 1


def test_task_parsing_uses_delete_file_id_as_the_product():
    """115 的 file_id 是保存目录，产物文件 ID 在 delete_file_id。

    现场就是因为把目录 ID 当产物 ID，所有离线任务都认领不到产物。
    """
    from strimkeep.offline_api import _tasks
    raw = {'tasks': [{
        'info_hash': 'abc', 'name': 'A.mkv', 'percentDone': 100, 'status_text': '下载成功',
        'display_status': 'finished', 'file_id': '3510832216693277696',
        'delete_file_id': '3556901390494205376', 'wp_path_id': '3510832216693277696',
    }]}
    items, total = _tasks(raw)
    assert total == 1
    task = items[0]
    assert task['file_id'] == '3556901390494205376'      # 产物
    assert task['dir_id'] == '3510832216693277696'       # 保存目录
    assert task['display'] == 'finished'


def test_state_is_done_only_on_real_finished_evidence():
    from strimkeep.offline_api import OfflinePostProcess as P
    assert P._state({'percent': 100, 'status': '', 'display': ''}) == 'done'
    assert P._state({'percent': 0, 'status': '下载成功', 'display': 'finished'}) == 'done'
    assert P._state({'percent': 42, 'status': '下载中', 'display': 'downloading'}) == 'running'
    assert P._state({'percent': 0, 'status': '失败', 'display': 'failed'}) == 'failed'
    # 认不出来的状态不能默认成「下载中」，否则超时回执会凭空开火
    assert P._state({'percent': 0, 'status': '神秘状态', 'display': 'whatever'}) == 'unknown'
