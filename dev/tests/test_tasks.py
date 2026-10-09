# -*- coding: utf-8 -*-
"""
tasks.py 测试：统一任务总线（Web / Bot / 定时巡检共用）。

  - 重任务互斥：检查与占位必须原子完成，并发 spawn 只能有一个通过
  - 轻量任务不占位
  - 任务日志只收本任务线程的日志
  - 运行中的任务永远不被淘汰
"""
import logging
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2] / 'app'))
import tasks  # noqa: E402


def _wait(t, timeout=5):
    assert t.done.wait(timeout), '任务没有在限定时间内结束'


class TestExclusive:
    def test_second_exclusive_task_is_rejected_until_first_finishes(self):
        m = tasks.TaskManager()
        gate = threading.Event()
        t1 = m.spawn('inter_check', gate.wait)
        try:
            m.spawn('inter_clean', lambda: None)
            assert False, '应该抛出 TaskBusy'
        except tasks.TaskBusy as e:
            assert e.current is t1
        gate.set()
        _wait(t1)
        assert m.running() is None
        t2 = m.spawn('inter_clean', lambda: {'status': 'success'})
        _wait(t2)
        assert t2.status == 'success'

    def test_concurrent_spawns_only_one_wins(self):
        """以前先查 CURRENT、等工作线程跑起来才占位，前后脚的两个请求都能通过检查"""
        m = tasks.TaskManager()
        gate = threading.Event()
        barrier = threading.Barrier(16)
        won, lost = [], []

        def racer():
            barrier.wait()
            try:
                won.append(m.spawn('inter_check', gate.wait))
            except tasks.TaskBusy:
                lost.append(1)

        ths = [threading.Thread(target=racer) for _ in range(16)]
        for th in ths: th.start()
        for th in ths: th.join(5)
        gate.set()
        for t in won: _wait(t)
        assert len(won) == 1 and len(lost) == 15

    def test_non_exclusive_task_neither_blocks_nor_is_blocked(self):
        m = tasks.TaskManager()
        gate = threading.Event()
        heavy = m.spawn('inter_check', gate.wait)
        light = m.spawn('bot_search', lambda: {'status': 'success'}, exclusive=False)
        _wait(light)
        assert light.status == 'success'
        assert m.running() is heavy
        gate.set(); _wait(heavy)


class TestResult:
    def test_busy_or_error_dict_marks_task_failed(self):
        m = tasks.TaskManager()
        t = m.spawn('inter_clean', lambda: {'status': 'busy', 'message': '已有治理任务正在执行'})
        _wait(t)
        assert t.status == 'error' and t.error == '已有治理任务正在执行'

    def test_exception_is_captured_into_task(self):
        m = tasks.TaskManager()

        def boom():
            raise ValueError('坏了')
        t = m.spawn('inter_check', boom)
        _wait(t)
        assert t.status == 'error'
        assert t.error == 'ValueError: 坏了'
        assert any('坏了' in ln for ln in t.logs), '异常栈应该进任务日志，方便网页上排查'
        assert m.running() is None

    def test_on_done_receives_finished_task(self):
        m = tasks.TaskManager()
        got = []
        seen = threading.Event()

        def cb(t):
            got.append((t.status, t.result)); seen.set()
        m.spawn('bot_logs', lambda: {'status': 'success', 'n': 1}, exclusive=False, on_done=cb)
        assert seen.wait(5)
        assert got == [('success', {'status': 'success', 'n': 1})]


class TestLogIsolation:
    def test_only_own_thread_logs_are_captured(self):
        m = tasks.TaskManager()
        lg = logging.getLogger('strimkeep')
        old_level = lg.level
        lg.setLevel(logging.INFO)
        inside = threading.Event()
        release = threading.Event()

        def job():
            lg.info('任务自己的日志')
            inside.set()
            release.wait(5)
            return {'status': 'success'}
        try:
            t = m.spawn('inter_check', job)
            assert inside.wait(5)
            other = threading.Thread(target=lambda: lg.info('别的线程的日志'))
            other.start(); other.join()
            release.set(); _wait(t)
            assert any('任务自己的日志' in ln for ln in t.logs)
            assert not any('别的线程的日志' in ln for ln in t.logs)
        finally:
            release.set()
            lg.setLevel(old_level)


class TestPrune:
    def _done(self, m, ts):
        t = tasks.Task('fake'); t.status = 'success'; t.ts = ts
        m.tasks[t.id] = t
        return t

    def test_running_task_is_never_pruned_even_if_old(self):
        m = tasks.TaskManager()
        old_running = tasks.Task('fake'); old_running.ts = time.time() - 10 * 3600
        m.tasks[old_running.id] = old_running
        stale = self._done(m, time.time() - 2 * 3600)
        fresh = self._done(m, time.time())
        m.prune()
        assert old_running.id in m.tasks
        assert stale.id not in m.tasks
        assert fresh.id in m.tasks

    def test_finished_tasks_are_capped(self):
        m = tasks.TaskManager()
        m.max_keep = 3
        now = time.time()
        ts = [self._done(m, now + i) for i in range(6)]
        m.prune()
        kept = [t for t in ts if t.id in m.tasks]
        assert [t.ts for t in kept] == [t.ts for t in ts[-3:]]


class TestPruneUsesFinishTime:
    def test_long_task_result_survives_right_after_finishing(self):
        m = tasks.TaskManager()
        t = tasks.Task('inter_check'); t.status = 'success'
        t.ts = time.time() - 3 * 3600      # 跑了 3 小时
        t.finished_at = time.time()        # 刚结束
        m.tasks[t.id] = t
        m.prune()
        assert t.id in m.tasks, '刚结束的长任务结果还要给前端取，不能按开始时间淘汰'
