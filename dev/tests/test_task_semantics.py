# -*- coding: utf-8 -*-
"""契约：执行槽状态与业务任务状态是两套词，不可混用。

app/tasks.py        执行槽（内存）：running / success / error / cancelled
app/cloud/states.py 业务任务（落库）：queued / running / success / stopped / failed / uncertain / needs_review

这条测试挡的是回归：有人把执行槽的 error 当成业务失败写进界面，
或者给业务状态新加词却忘了同步 states 模块。
"""
from app import tasks
from app.cloud import states


def test_task_name_stays_backward_compatible():
    """旧代码和测试仍写 tasks.Task(...)，别名必须指向同一个类。"""
    assert tasks.Task is tasks.ExecSlot


def test_exec_slot_starts_running_with_declared_status():
    slot = tasks.ExecSlot('probe', source='test', exclusive=False)
    assert slot.status in tasks.EXEC_STATUSES
    assert slot.status == 'running'


def test_exec_status_vocabulary_is_complete():
    assert tasks.EXEC_STATUSES == frozenset({'running', 'success', 'error', 'cancelled'})


def test_every_job_label_is_a_known_job_state():
    assert set(states.JOB_STATE_LABELS) <= states.JOB_STATES


def test_job_states_partition_into_active_and_terminal():
    assert states.ACTIVE_JOB_STATES | states.TERMINAL_JOB_STATES == states.JOB_STATES
    assert not (states.ACTIVE_JOB_STATES & states.TERMINAL_JOB_STATES)


def test_the_two_vocabularies_are_not_interchangeable():
    """互相渗透的典型错误：把 error 当业务终态，或把 needs_review 写进执行槽。"""
    assert 'error' not in states.JOB_STATES
    assert 'needs_review' not in tasks.EXEC_STATUSES
    assert 'uncertain' not in tasks.EXEC_STATUSES


def test_uncertain_is_terminal_and_never_auto_retried():
    """写操作结果未知时必须停在终态等人处理，不允许自动重放。"""
    assert 'uncertain' in states.TERMINAL_JOB_STATES
    assert 'uncertain' not in states.ACTIVE_JOB_STATES
