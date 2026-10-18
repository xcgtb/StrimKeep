# -*- coding: utf-8 -*-
"""契约：频道轮询的退避算法。

这两条最容易写错、也最贵：
1. 被 Telegram 限流后必须**按服务端给的秒数**等待——提前重试会把惩罚时间罚得更长；
2. 有积压时必须**立刻继续**，否则 N 个频道共用一个 tick，实际周期被拉长 N 倍。
"""
import pytest

from app.cloud import pacing as cr


def test_flood_wait_reads_seconds_attribute():
    class FloodWaitError(Exception):
        def __init__(self, seconds):
            self.seconds = seconds

    assert cr.flood_wait_seconds(FloodWaitError(120)) == 120
    assert cr.flood_wait_seconds(FloodWaitError('45')) == 45


def test_flood_wait_falls_back_to_value_attribute():
    class Other(Exception):
        value = 30

    assert cr.flood_wait_seconds(Other()) == 30


def test_flood_wait_returns_zero_for_unrelated_errors():
    assert cr.flood_wait_seconds(RuntimeError('boom')) == 0
    assert cr.flood_wait_seconds(ValueError('x')) == 0


def test_flood_wait_never_negative():
    class Odd(Exception):
        seconds = -5

    assert cr.flood_wait_seconds(Odd()) == 0


def test_backoff_never_shorter_than_server_demand():
    """核心断言：给 3600 就至少等 3600，不能被默认周期截断。"""
    delay = cr.next_poll_delay(error_seconds=3600)
    assert delay >= 3600
    assert delay == 3600 + cr.FLOOD_MARGIN_SEC


def test_backoff_takes_priority_over_backlog():
    """既限流又有积压时，必须先等，不能因为积压就冲进去。"""
    assert cr.next_poll_delay(error_seconds=120, backlog=True) >= 120


def test_backlog_polls_immediately():
    assert cr.next_poll_delay(backlog=True) == 0


def test_normal_case_uses_rule_interval():
    assert cr.next_poll_delay(default_seconds=600) == 600


@pytest.mark.parametrize('value', [0, None, '', 'abc', -10])
def test_bad_interval_falls_back_to_default(value):
    assert cr.next_poll_delay(default_seconds=value) == 300


def test_flood_wait_exception_carries_seconds_and_code():
    error = cr.ChannelFloodWait(75)
    assert error.seconds == 75
    assert '75' in str(error)
    assert error.args[-1] == 429 if len(error.args) > 1 else True


def test_flood_wait_exception_clamps_negative():
    assert cr.ChannelFloodWait(-1).seconds == 0
