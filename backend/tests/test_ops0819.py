"""0.8.19 运维改动：终态收口、转存密钥生命周期、任务视图批量统计、媒体库路径自检。

每条用例都对应一个现场问题（背景见 docs/releases/RELEASE_0.8.19.md）：

1. 任务结束后 `organize_items` 里还有 `discovered` 行（0.8.18 现场 294 条），
   它们所属任务早已结束，却继续污染任务中心的「未处理 / 未识别」统计；
2. 转存任务的 `settings['transfer-secret:<job>']` 在任务结束后永不释放（现场 21 条），
   等于多留一份加密的分享码/提取码；
3. `/api/v1/tasks` 与 `/api/v1/tasks/events` 对每条任务各跑 2~3 条统计查询
   （30 个任务时每轮约 62 次全表扫描，每 2 秒一次）；
4. Emby 媒体库路径漂移只有在 30 分钟核对超时后才会暴露（0.6.5 现场踩过）。

这些用例走真实代码路径（Store / Organizer.update_job / FastAPI 路由 / EmbyDelivery），
只有 Emby 的 HTTP 用 httpx.MockTransport。
"""
import asyncio
import json
import time

import httpx
from fastapi.testclient import TestClient

from strimkeep.emby import EmbyDelivery
from strimkeep.main import create_app
from strimkeep.store import Store
from test_app import FakeCloud

PASSWORD = 'test-password-1234'


# ---------------------------------------------------------------- 测试脚手架

def new_store(tmp_path):
    return Store(tmp_path / 'data', 'admin', PASSWORD)


def add_job(store, state='running', kind='organize', config=None):
    payload = {'job_kind': kind, **(config or {})}
    with store.db:
        return store.db.execute(
            'INSERT INTO organize_jobs(state,config,created,updated) VALUES(?,?,?,?)',
            (state, json.dumps(payload), 1.0, 1.0)).lastrowid


def add_item(store, job_id, file_id, state, step=''):
    with store.db:
        store.db.execute(
            'INSERT INTO organize_items(job_id,file_id,original,state,step) VALUES(?,?,?,?,?)',
            (job_id, file_id, json.dumps({'id': file_id, 'name': file_id + '.mkv'}), state, step))


def item_states(store, job_id):
    return {row['file_id']: row['state'] for row in store.db.execute(
        'SELECT file_id,state FROM organize_items WHERE job_id=?', (job_id,))}


def setting_keys(store):
    return {row['key'] for row in store.db.execute('SELECT key FROM settings')}


def event_messages(store, kind='emby'):
    return [row['message'] for row in store.db.execute(
        'SELECT message FROM events WHERE kind=? ORDER BY id', (kind,))]


# ------------------------------------------------- 1) 任务结束时的条目收口

def test_terminal_job_settles_leftover_items(tmp_path, monkeypatch):
    monkeypatch.setenv('ADMIN_USERNAME', 'admin')
    monkeypatch.setenv('ADMIN_PASSWORD', PASSWORD)
    app = create_app(tmp_path / 'data', tmp_path / 'strm', FakeCloud)
    with TestClient(app):
        store = app.state.store
        job = add_job(store, state='running')
        add_item(store, job, '1', 'discovered')
        add_item(store, job, '2', 'planned')
        add_item(store, job, '3', 'done')
        add_item(store, job, '4', 'review')
        app.state.organizer.update_job(job, state='completed')
        # 只有「还没开始动云端」的两种状态被归一；已经出结果的一律保留。
        assert item_states(store, job) == {'1': 'skipped', '2': 'skipped',
                                           '3': 'done', '4': 'review'}
        row = store.db.execute(
            "SELECT step,message FROM organize_items WHERE job_id=? AND file_id='1'",
            (job,)).fetchone()
        assert row['step'] == '任务结束未处理' and row['message'] == '任务结束未处理'


def test_running_job_keeps_unprocessed_items(tmp_path, monkeypatch):
    """运行中的任务不能收口，否则继续执行时会漏掉还没处理的文件。"""
    monkeypatch.setenv('ADMIN_USERNAME', 'admin')
    monkeypatch.setenv('ADMIN_PASSWORD', PASSWORD)
    app = create_app(tmp_path / 'data', tmp_path / 'strm', FakeCloud)
    with TestClient(app):
        store = app.state.store
        job = add_job(store, state='queued')
        add_item(store, job, '1', 'discovered')
        app.state.organizer.update_job(job, state='running', stage='识别中')
        app.state.organizer.update_job(job, stage='归档中')
        assert item_states(store, job) == {'1': 'discovered'}


def test_startup_reconcile_is_scoped_and_idempotent(tmp_path):
    store = new_store(tmp_path)
    finished = add_job(store, state='completed')
    partial = add_job(store, state='partial')
    running = add_job(store, state='running')
    for job in (finished, partial, running):
        add_item(store, job, 'f%d' % job, 'discovered')
    assert store.reconcile_terminal_items() == 2
    assert item_states(store, finished)['f%d' % finished] == 'skipped'
    assert item_states(store, partial)['f%d' % partial] == 'skipped'
    assert item_states(store, running)['f%d' % running] == 'discovered'
    assert store.reconcile_terminal_items() == 0          # 第二次运行没有任何变更


# ------------------------------------------------- 2) 转存密钥的生命周期

def test_transfer_secret_released_when_job_ends(tmp_path):
    store = new_store(tmp_path)
    finished = add_job(store, kind='transfer')
    active = add_job(store, kind='transfer')
    with store.db:
        store.db.executemany('INSERT OR REPLACE INTO settings VALUES(?,?)',
                             [('transfer-secret:%d' % finished, 'x'),
                              ('transfer-secret:%d' % active, 'y')])
    result = store.settle_terminal_job(finished)
    assert result['secrets'] == 1
    assert setting_keys(store) == {'transfer-secret:%d' % active}


def test_prune_drops_finished_transfer_secrets_and_caps_update_keys(tmp_path):
    store = new_store(tmp_path)
    finished = add_job(store, state='completed', kind='transfer')
    active = add_job(store, state='running', kind='transfer')
    with store.db:
        store.db.executemany('INSERT OR REPLACE INTO settings VALUES(?,?)',
                             [('transfer-secret:%d' % finished, 'x'),
                              ('transfer-secret:%d' % active, 'y')])
        store.db.executemany('INSERT OR REPLACE INTO settings VALUES(?,?)',
                             [('transfer-update:k%d' % index, 'v') for index in range(205)])
    result = store.prune()
    keys = setting_keys(store)
    assert 'transfer-secret:%d' % finished not in keys     # 所属任务已结束 -> 清理
    assert 'transfer-secret:%d' % active in keys           # 仍在跑 -> 保留
    assert len([key for key in keys if key.startswith('transfer-update:')]) == 200
    assert result['transfer_secrets'] == 1 and result['stale_updates'] == 5


# ------------------------------------------------- 3) 任务视图的批量统计

def test_batch_stats_match_per_row_queries(tmp_path, monkeypatch):
    """HTTP 返回的计数必须与「逐条查询」的老实现完全一致，否则前端数字会跳。"""
    monkeypatch.setenv('ADMIN_USERNAME', 'admin')
    monkeypatch.setenv('ADMIN_PASSWORD', PASSWORD)
    app = create_app(tmp_path / 'data', tmp_path / 'strm', FakeCloud)
    with TestClient(app) as client:
        store = app.state.store
        first = add_job(store, state='completed')
        second = add_job(store, state='partial')
        add_item(store, first, 'a', 'review', '未识别：风格不符')
        add_item(store, first, 'b', 'skipped', '保留已有更好版本')
        add_item(store, second, 'c', 'done', '')
        add_item(store, second, 'd', 'skipped', '命中忽略关键词')
        csrf = client.post('/api/v1/auth/login',
                           json={'username': 'admin', 'password': PASSWORD}).json()['csrf']
        client.headers['X-CSRF-Token'] = csrf
        listed = {item['id']: item
                  for item in client.get('/api/v1/tasks').json()['active']
                  + client.get('/api/v1/tasks').json()['history']}
        for job_id in (first, second):
            single = app.state.job_public(app.state.organizer.job(job_id))   # 逐条查询路径
            batched = listed[job_id]                                        # 批量查询路径
            for field in ('counts', 'total', 'unidentified', 'filtered_items', 'filtered'):
                assert batched[field] == single[field], field
        assert listed[first]['unidentified'] == 1 and listed[first]['filtered_items'] == 1
        assert listed[second]['unidentified'] == 0 and listed[second]['filtered_items'] == 1


def test_job_item_stats_counts_queries_per_call(tmp_path, monkeypatch):
    """空列表 0 条查询、2 个任务 2 条查询：批量统计的查询数与任务数无关。

    这是 /api/v1/tasks/events 每 2 秒一轮的关键指标（改造前：每任务 2 条）。
    """
    monkeypatch.setenv('ADMIN_USERNAME', 'admin')
    monkeypatch.setenv('ADMIN_PASSWORD', PASSWORD)
    app = create_app(tmp_path / 'data', tmp_path / 'strm', FakeCloud)
    store = app.state.store
    first = add_job(store, state='running')
    second = add_job(store, state='running')
    add_item(store, first, 'a', 'review', '未识别：风格不符')
    add_item(store, second, 'b', 'skipped', '低于最小体积')
    real = store.db
    calls = []

    class Recorder:
        def execute(self, sql, *args):
            calls.append(' '.join(str(sql).split()))
            return real.execute(sql, *args)

    store.db = Recorder()
    try:
        assert app.state.job_item_stats([]) == {'unidentified': {}, 'filtered': {}}
        assert calls == []
        stats = app.state.job_item_stats([first, second])
    finally:
        store.db = real
    assert len(calls) == 2
    assert stats == {'unidentified': {first: 1}, 'filtered': {second: 1}}


# ------------------------------------------------- 4) Emby 媒体库路径自检

class _Engine:
    """最小 engine：EmbyDelivery 只用到 store / http / closing。"""

    def __init__(self, store, http):
        self.store, self.http, self.closing = store, http, False


def _delivery(tmp_path, payload):
    store = new_store(tmp_path)
    store.set_many({'emby_url': 'http://emby.local:8096', 'emby_key': 'k'})

    def handler(request):
        assert request.url.path == '/emby/Library/VirtualFolders', request.url.path
        return httpx.Response(200, json=payload)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return store, EmbyDelivery(_Engine(store, http)), http


def test_emby_coverage_selfcheck_flags_uncovered_library(tmp_path):
    """库根不在 STRM 目录下 -> 明确写一条「未通过」，而不是等 30 分钟核对超时。"""
    store, delivery, http = _delivery(tmp_path, [
        {'Name': '影片', 'ItemId': '1', 'Locations': ['/media/媒体库/电影']}])
    try:
        report = asyncio.run(delivery.coverage_selfcheck())
    finally:
        asyncio.run(http.aclose())
    assert report['covered'] is False and report['libraries'] == []
    messages = event_messages(store)
    assert len(messages) == 1 and '自检未通过' in messages[0] and '/strimkeep' in messages[0]


def test_emby_coverage_selfcheck_accepts_library_inside_strm_root(tmp_path):
    """README 推荐的配置是库指向 /strimkeep/<分类>，属于「覆盖」，不能误报。"""
    store, delivery, http = _delivery(tmp_path, [
        {'Name': '影片', 'ItemId': '1', 'Locations': ['/strimkeep/影视媒体库/电影']}])
    try:
        report = asyncio.run(delivery.coverage_selfcheck())
        again = asyncio.run(delivery.coverage_selfcheck())    # 同一天：直接复用上次结论
    finally:
        asyncio.run(http.aclose())
    assert report['covered'] is True and report['libraries'] == ['影片']
    assert again == report
    assert len(event_messages(store)) == 1                    # 每天只写一条结论


def test_emby_coverage_selfcheck_stays_silent_when_library_read_fails(tmp_path):
    """读不到库信息（Emby 未配置/网络失败）时不下结论：不能把「读不到」写成「配错了」。"""
    store = new_store(tmp_path)
    store.set_many({'emby_url': 'http://emby.local:8096', 'emby_key': 'k'})

    def handler(request):
        return httpx.Response(500, json={'error': 'boom'})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    delivery = EmbyDelivery(_Engine(store, http))
    try:
        assert asyncio.run(delivery.coverage_selfcheck()) is None
    finally:
        asyncio.run(http.aclose())
    assert event_messages(store) == []



