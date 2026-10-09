# -*- coding: utf-8 -*-
import app.morning as morning


class _Cfg:
    def __init__(self):
        self.marked = []
    def mark_morning_report_sent(self, value):
        self.marked.append(value)


class _Eng:
    def __init__(self):
        self.sent = []
    def notify_telegram(self, text):
        self.sent.append(text)
        return True


def _run(mark_sent):
    cfg = _Cfg(); eng = _Eng()
    old_cfg, old_eng, old_build = morning._cfg, morning._eng, morning.build_morning_report
    try:
        morning._cfg = cfg
        morning._eng = lambda: eng
        morning.build_morning_report = lambda items, force_refresh=False: 'cached'
        assert morning.send_morning_report(['stats'], force_refresh=False, mark_sent=mark_sent) is True
        return cfg, eng
    finally:
        morning._cfg, morning._eng, morning.build_morning_report = old_cfg, old_eng, old_build


def test_manual_send_does_not_mark_daily_send():
    cfg, eng = _run(False)
    assert cfg.marked == []
    assert eng.sent == ['cached']


def test_scheduled_send_marks_daily_send():
    cfg, eng = _run(True)
    assert len(cfg.marked) == 1
    assert eng.sent == ['cached']
