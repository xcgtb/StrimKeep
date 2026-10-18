# -*- coding: utf-8 -*-
"""契约：STRM 全量同步的排期计算与目录提取。

排期算错会造成两种事故：该跑的时候不跑（库里长期缺 STRM），
或每拍都跑（重复扫描、打满 115 接口）。这两条都在这里钉死。
"""
import time

from app.cloud import strm_scan


def test_empty_cron_never_schedules():
    assert strm_scan.next_run('', 'Asia/Shanghai') == 0
    assert strm_scan.due('', 'Asia/Shanghai', 0) is False


def test_invalid_cron_never_schedules():
    assert strm_scan.next_run('not a cron', 'Asia/Shanghai') == 0
    assert strm_scan.next_run('0 0 * *', 'Asia/Shanghai') == 0


def test_daily_cron_lands_in_the_future():
    now = 1_700_000_000.0
    nxt = strm_scan.next_run('0 0 * * *', 'Asia/Shanghai', now)
    assert nxt > now
    assert nxt - now <= 86400 + 1


def test_never_run_is_due_once():
    assert strm_scan.due('0 0 * * *', 'Asia/Shanghai', 0, 1_700_000_000.0) is True


def test_just_ran_is_not_due_again():
    now = 1_700_000_000.0
    assert strm_scan.due('0 0 * * *', 'Asia/Shanghai', now, now + 60) is False


def test_previous_period_is_due_again():
    now = 1_700_000_000.0
    assert strm_scan.due('0 0 * * *', 'Asia/Shanghai', now, now + 86400 + 60) is True


def test_targets_accepts_list_and_dedupes(monkeypatch):
    monkeypatch.setattr(
        strm_scan.directories, 'get',
        lambda aid: {'directories': {'strm': ['100', '200', '200', ' abc ', '']}})
    assert strm_scan.targets('a1') == ['100', '200']


def test_targets_accepts_single_value(monkeypatch):
    monkeypatch.setattr(
        strm_scan.directories, 'get',
        lambda aid: {'directories': {'strm': '300'}})
    assert strm_scan.targets('a1') == ['300']


def test_missing_strm_role_yields_empty(monkeypatch):
    monkeypatch.setattr(
        strm_scan.directories, 'get',
        lambda aid: {'directories': {'target': '1'}})
    assert strm_scan.targets('a1') == []


def test_directory_lookup_failure_is_swallowed(monkeypatch):
    def boom(aid):
        raise RuntimeError('no db')

    monkeypatch.setattr(strm_scan.directories, 'get', boom)
    assert strm_scan.targets('a1') == []


def test_tick_skips_when_not_due():
    """没到点不能投递任务；这是防止「每拍都扫」的那道闸。"""
    assert strm_scan.tick({'strm_scan_cron': '', 'strm_scan_last_run': 0}, time.time()) == []
