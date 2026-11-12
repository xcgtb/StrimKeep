"""0.9.60：任务状态机的守卫。

两组断言：
1. **谁能写这张表**——除 jobs.py / store.py 外，任何模块直接 UPDATE/INSERT/DELETE
   `organize_jobs` / `organize_items` 都让门禁失败。这是审计 A1「9 个写入方」的封条，
   以后再加功能只能往 jobs.py 加入口。
2. **能写成什么样**——状态词必须认、迁移必须合法、任务进终态必须收口。

第 1 组是源码扫描，不依赖运行时；第 2 组用真 Store（临时库）。
"""
import glob
import os
import re

import pytest

from strimkeep.jobs import (ITEM_STATES, JOB_STATES, JOB_TERMINAL, JobStateError, Jobs)
from strimkeep.store import Store

SOURCE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# jobs.py 是实现、store.py 保留建表与保留期清理（_prune / 退役模块）——都是运维动作，
# 不是业务状态迁移，所以在白名单里。新增白名单条目请先想清楚：它会不会写状态。
WRITER_ALLOWED = {'jobs.py', 'store.py'}
DIRECT_WRITE = re.compile(
    r'\b(?:UPDATE\s+organize_(?:jobs|items)|INSERT\s+INTO\s+organize_(?:jobs|items)'
    r'|DELETE\s+FROM\s+organize_(?:jobs|items)|REPLACE\s+INTO\s+organize_(?:jobs|items))', re.I)


def make_store(tmp_path):
    return Store(tmp_path / 'data', 'admin', 'test-password-1234')


def test_no_module_writes_job_tables_directly():
    """A1 的封条：业务模块不得绕过 jobs.py 直接写这两张表。"""
    offenders = []
    for path in sorted(glob.glob(os.path.join(SOURCE_ROOT, 'strimkeep', '*.py'))):
        name = os.path.basename(path)
        if name in WRITER_ALLOWED:
            continue
        text = open(path, encoding='utf-8').read()
        for match in DIRECT_WRITE.finditer(text):
            line = text[:match.start()].count('\n') + 1
            offenders.append('%s:%d %s' % (name, line, match.group(0)))
    assert not offenders, '以下位置绕过了 jobs.py：\n  ' + '\n  '.join(offenders)


def test_unknown_state_is_rejected(tmp_path):
    store = make_store(tmp_path)
    try:
        job_id = store.jobs.create({'job_kind': 'organize'})
        with pytest.raises(JobStateError):
            store.jobs.update(job_id, state='completd')       # 拼错的状态不许入库
        item_id = store.jobs.insert_item(job_id, 'f1', {'name': 'a.mkv'})
        with pytest.raises(JobStateError):
            store.jobs.update_item(item_id, state='finishd')
        assert store.jobs.rejected == 2                     # /metrics 能看到有人撞墙
        assert store.jobs.get(job_id)['state'] == 'queued'
    finally:
        store.close()


def test_illegal_job_transition_is_rejected(tmp_path):
    store = make_store(tmp_path)
    try:
        job_id = store.jobs.create({'job_kind': 'organize'})
        store.jobs.update(job_id, state='needs_review', stage='等待核查')
        # 待核查不能直接跳去暂停：迁移表里没这条路，写下去就是没人处理的死状态。
        with pytest.raises(JobStateError):
            store.jobs.update(job_id, state='paused')
        # 终态回 queued 是显式重开，允许（真实链路的 local-export 补齐就这么走）。
        store.jobs.finish(job_id, 'completed')
        store.jobs.update(job_id, state='queued', stage='人工重跑')
        assert store.jobs.get(job_id)['state'] == 'queued'
    finally:
        store.close()


def test_terminal_state_settles_unstarted_items(tmp_path):
    """进终态必须收口：还没开始的条目落 skipped，真实结果一律不碰。"""
    store = make_store(tmp_path)
    try:
        job_id = store.jobs.create({'job_kind': 'organize'})
        started = store.jobs.insert_item(job_id, 'a', {'name': 'a.mkv'})
        store.jobs.update_item(started, state='done', step='已归档')
        store.jobs.insert_item(job_id, 'b', {'name': 'b.mkv'})          # 停在 discovered
        store.jobs.insert_item(job_id, 'c', {'name': 'c.mkv'}, state='planned')
        result = store.jobs.finish(job_id, 'completed')
        assert result['items'] == 2                                    # discovered + planned
        states = {row['file_id']: row['state'] for row in
                  store.db.execute('SELECT file_id,state FROM organize_items WHERE job_id=?', (job_id,))}
        assert states == {'a': 'done', 'b': 'skipped', 'c': 'skipped'}
        stage = store.db.execute(
            "SELECT step FROM organize_items WHERE file_id='b'").fetchone()[0]
        assert stage == '任务结束未处理'
    finally:
        store.close()


def test_finish_requires_terminal_state(tmp_path):
    store = make_store(tmp_path)
    try:
        job_id = store.jobs.create({'job_kind': 'organize'})
        with pytest.raises(JobStateError):
            store.jobs.finish(job_id, 'running')
        assert all(state in JOB_TERMINAL for state in ('completed', 'partial', 'cancelled'))
    finally:
        store.close()


def test_close_only_accepts_unfinished_outcomes(tmp_path):
    """人工结项只能收「部分未完成 / 待核查 / 已取消」，不能拿它去改已完成的任务。"""
    store = make_store(tmp_path)
    try:
        done = store.jobs.create({'job_kind': 'organize'})
        store.jobs.finish(done, 'completed')
        with pytest.raises(JobStateError):
            store.jobs.close(done, '不该允许')
        partial = store.jobs.create({'job_kind': 'organize'})
        store.jobs.finish(partial, 'partial')
        store.jobs.close(partial, '人工确认可结项')
        row = store.jobs.get(partial)
        assert row['state'] == 'completed' and row['stage'] == '人工结项'
    finally:
        store.close()


def test_item_transitions_follow_the_table(tmp_path):
    store = make_store(tmp_path)
    try:
        job_id = store.jobs.create({'job_kind': 'organize'})
        item_id = store.jobs.insert_item(job_id, 'a', {'name': 'a.mkv'})
        store.jobs.update_item(item_id, state='archived')
        store.jobs.update_item(item_id, state='done')
        # done 可以退回 review（人工复核）与 archived（补齐输出），但不能回到 discovered。
        store.jobs.update_item(item_id, state='review')
        store.jobs.update_item(item_id, state='done')
        with pytest.raises(JobStateError):
            store.jobs.update_item(item_id, state='discovered')
        assert set(ITEM_STATES) >= {'discovered', 'planned', 'archived', 'done', 'skipped'}
    finally:
        store.close()


def test_recover_running_requeues_and_marks_notifications_unknown(tmp_path):
    store = make_store(tmp_path)
    try:
        running = store.jobs.create({'job_kind': 'organize'})
        store.jobs.update(running, state='running', stage='执行中')
        store.jobs.update(running, notify_state='inflight')
        assert store.jobs.recover_running() == 1
        row = store.jobs.get(running)
        assert row['state'] == 'queued' and row['stage'] == '重启后核查恢复'
        assert row['notify_state'] == 'unknown'
    finally:
        store.close()


def test_skip_job_items_keeps_existing_message(tmp_path):
    """人工结项只落状态与步骤，不覆盖条目原来的失败原因（可追溯）。"""
    store = make_store(tmp_path)
    try:
        job_id = store.jobs.create({'job_kind': 'organize'})
        item_id = store.jobs.insert_item(job_id, 'a', {'name': 'a.mkv'})
        store.jobs.update_item(item_id, state='review', step='识别失败', message='TMDB 未匹配')
        store.jobs.skip_job_items(job_id, '已人工结项')
        row = store.jobs.item(item_id)
        assert row['state'] == 'skipped'
        assert row['message'] == 'TMDB 未匹配'
    finally:
        store.close()


def test_state_vocabulary_is_closed():
    """词表本身要收敛：新增状态必须同时进迁移表，否则守门形同虚设。"""
    from strimkeep.jobs import ITEM_TRANSITIONS, JOB_TRANSITIONS
    assert set(JOB_TRANSITIONS) == set(JOB_STATES)
    assert set(ITEM_TRANSITIONS) == set(ITEM_STATES)
    for state, targets in JOB_TRANSITIONS.items():
        assert targets <= set(JOB_STATES), state
    missing = [state for state in JOB_STATES if state not in JOB_TRANSITIONS]
    assert not missing

