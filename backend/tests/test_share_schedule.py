"""分享整理与存活检测的定时入口：对齐（整理 5 分钟一轮、存活检测每天 03:00）。"""
import asyncio
import time
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from strimkeep import organizer as organizer_module
from strimkeep.organizer import Organizer
from strimkeep.store import Store
from test_organize import setup


def test_next_check_due_is_anchored_to_three_am(tmp_path):
    store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
    fake = SimpleNamespace(store=store)
    zone = ZoneInfo('Asia/Shanghai')
    early = datetime(2026, 10, 29, 1, 0, tzinfo=zone).timestamp()
    late = datetime(2026, 10, 29, 4, 0, tzinfo=zone).timestamp()

    due_early = datetime.fromtimestamp(Organizer.next_check_due(fake, 24, early), zone)
    due_late = datetime.fromtimestamp(Organizer.next_check_due(fake, 24, late), zone)

    assert (due_early.day, due_early.hour, due_early.minute) == (29, 3, 0)
    assert (due_late.day, due_late.hour) == (30, 3)
    store.close()


def test_share_check_runs_once_per_due_time(tmp_path, monkeypatch):
    async def run():
        store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
        store.set_many({'share_settings': {'share_check_hours': 24}})
        calls = []

        async def check_alive(engine):
            calls.append(engine)

        monkeypatch.setattr(organizer_module.share_service, 'check_alive', check_alive)

        class FakeEngine:
            store = None

            def __init__(self):
                self._share_checked = 0.0
                self._share_check_task = None

            share_check_tick = Organizer.share_check_tick
            start_share_check = Organizer.start_share_check
            next_check_due = Organizer.next_check_due
            next_share_check_due = Organizer.next_share_check_due

        engine = FakeEngine()
        engine.store = store
        await Organizer.share_check_tick(engine)          # 第一次只落计划，不跑
        assert calls == [] and float(store.get('share_check_due') or 0) > 0
        due = float(store.get('share_check_due') or 0)
        store.set_many({'share_check_due': time.time() - 10})   # 把计划拨到过去，模拟到点
        await Organizer.share_check_tick(engine)
        # 0.9.56 起检测在后台 task 里跑：让出一拍再断言，且计划应当立刻顺延
        assert float(store.get('share_check_due') or 0) > time.time()
        await asyncio.sleep(0)
        assert len(calls) == 1
        store.close()
    asyncio.run(run())


def test_share_check_runs_in_background_and_never_blocks_the_caller(tmp_path, monkeypatch):
    """0.9.56：82 个分享串行取链实测 41 秒，不能压在请求/调度上。

    请求最多等 `wait` 秒；超时就返回 pending，后台继续跑完并写事件。
    每次调用都是一次新的检测（不缓存结果），所以只有第一次慢。
    """
    async def run():
        store, cloud, http, engine, _ = setup(tmp_path)
        calls = []

        async def slow_once(_engine):
            calls.append(1)
            if len(calls) == 1:
                await asyncio.sleep(0.3)
            return {'alive': 1, 'dead': [], 'dead_count': 0, 'skipped': 0}

        monkeypatch.setattr(organizer_module.share_service, 'check_alive', slow_once)
        started = time.monotonic()
        result = await engine.start_share_check(wait=0.05)
        elapsed = time.monotonic() - started
        assert elapsed < 0.25, '调用方不能被检测拖住（实测用了 %.2fs）' % elapsed
        assert result.get('started') is True and result.get('pending') is True

        again = await engine.start_share_check(wait=0.05)
        assert again.get('started') is False, '同一时刻只允许一次检测'

        await asyncio.sleep(0.35)                       # 后台跑完
        assert calls == [1]
        third = await engine.start_share_check(wait=0.05)
        assert third.get('dead_count') == 0, '下一次调用应当立刻拿到结果'
        assert calls == [1, 1]
        await http.aclose(); store.close()
    asyncio.run(run())


def test_share_scan_only_enqueues_when_pending_files_exist(tmp_path, monkeypatch):
    async def run():
        store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
        root = tmp_path / 'strm'
        pending = root / organizer_module.share_service.PENDING_NAME
        pending.mkdir(parents=True)
        enqueued = []

        def fake_enqueue(engine, origin='web', secret=None, channel_claim=None):
            enqueued.append(origin)
            return 7

        monkeypatch.setattr(organizer_module.share_service, 'enqueue_pending_organize', fake_enqueue)
        engine = SimpleNamespace(store=store, root=root, _share_scanned=0.0)

        await Organizer.share_scan_tick(engine)            # 空目录：不排任务
        assert enqueued == []

        (pending / 'a.strm').write_text('http://x/play/1', encoding='utf-8')
        engine._share_scanned = 0.0
        await Organizer.share_scan_tick(engine)
        assert enqueued == ['schedule']

        # 冷却期内不重复排队
        (pending / 'b.strm').write_text('http://x/play/2', encoding='utf-8')
        await Organizer.share_scan_tick(engine)
        assert enqueued == ['schedule']
        store.close()
    asyncio.run(run())


def test_share_scan_can_be_switched_off(tmp_path, monkeypatch):
    async def run():
        store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
        store.set_many({'share_scan_minutes': 0})
        root = tmp_path / 'strm'
        pending = root / organizer_module.share_service.PENDING_NAME
        pending.mkdir(parents=True)
        (pending / 'a.strm').write_text('x', encoding='utf-8')
        enqueued = []
        monkeypatch.setattr(organizer_module.share_service, 'enqueue_pending_organize',
                            lambda *a, **k: enqueued.append(a))
        engine = SimpleNamespace(store=store, root=root, _share_scanned=0.0)
        await Organizer.share_scan_tick(engine)
        assert enqueued == []
        store.close()
    asyncio.run(run())


def test_share_scan_waits_for_running_share_job(tmp_path, monkeypatch):
    async def run():
        store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
        root = tmp_path / 'strm'
        pending = root / organizer_module.share_service.PENDING_NAME
        pending.mkdir(parents=True)
        (pending / 'a.strm').write_text('x', encoding='utf-8')
        with store.db:
            store.db.execute("INSERT INTO organize_jobs(state,config,created,updated) VALUES('running',?,?,?)",
                             ('{"job_kind":"share_organize"}', 0, 0))
        enqueued = []
        monkeypatch.setattr(organizer_module.share_service, 'enqueue_pending_organize',
                            lambda *a, **k: enqueued.append(a))
        engine = SimpleNamespace(store=store, root=root, _share_scanned=0.0)
        await Organizer.share_scan_tick(engine)
        assert enqueued == []
        store.close()
    asyncio.run(run())


def test_next_cron_due_follows_the_expression(tmp_path):
    """Cron 模式：下一次执行按表达式算，不再锚定 03:00。"""
    store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
    fake = SimpleNamespace(store=store)
    zone = ZoneInfo('Asia/Shanghai')
    now = datetime(2026, 10, 29, 1, 0, tzinfo=zone).timestamp()

    due = datetime.fromtimestamp(Organizer.next_cron_due(fake, '0 3 * * *', now), zone)
    assert (due.day, due.hour, due.minute) == (29, 3, 0)

    every_six = datetime.fromtimestamp(Organizer.next_cron_due(fake, '0 */6 * * *', now), zone)
    assert (every_six.day, every_six.hour, every_six.minute) == (29, 6, 0)
    store.close()


def test_share_check_runs_on_cron_schedule_even_with_zero_hours(tmp_path, monkeypatch):
    """Cron 模式不受「小时=0 即关闭」影响，按表达式跑一轮后顺延。"""
    async def run():
        store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
        store.set_many({'share_settings': {'share_check_hours': 0, 'share_check_mode': 'cron',
                                           'share_check_cron': '0 3 * * *'}})
        calls = []

        async def check_alive(engine):
            calls.append(engine)

        monkeypatch.setattr(organizer_module.share_service, 'check_alive', check_alive)

        class FakeEngine:
            store = None

            def __init__(self):
                self._share_checked = 0.0
                self._share_check_task = None

            share_check_tick = Organizer.share_check_tick
            start_share_check = Organizer.start_share_check
            next_check_due = Organizer.next_check_due
            next_share_check_due = Organizer.next_share_check_due
            next_cron_due = Organizer.next_cron_due

        engine = FakeEngine()
        engine.store = store
        await Organizer.share_check_tick(engine)
        assert calls == [] and float(store.get('share_check_due') or 0) > 0
        store.set_many({'share_check_due': time.time() - 10})
        await Organizer.share_check_tick(engine)
        await asyncio.sleep(0)          # 0.9.56 起检测在后台 task 里跑
        assert len(calls) == 1
        assert float(store.get('share_check_due') or 0) > time.time()
        store.close()
    asyncio.run(run())


def test_interval_mode_still_closes_on_zero_hours(tmp_path):
    """间隔模式保持原语义：小时=0 关闭，不写计划时间。"""
    async def run():
        store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
        store.set_many({'share_settings': {'share_check_hours': 0, 'share_check_mode': 'interval'}})
        calls = []

        async def check_alive(engine):
            calls.append(engine)

        class FakeEngine:
            store = None

            def __init__(self):
                self._share_checked = 0.0
                self.share_service = SimpleNamespace(check_alive=check_alive)

            share_check_tick = Organizer.share_check_tick
            next_check_due = Organizer.next_check_due
            next_share_check_due = Organizer.next_share_check_due

        engine = FakeEngine()
        engine.store = store
        await Organizer.share_check_tick(engine)
        assert calls == []
        assert float(store.get('share_check_due') or 0) == 0
        store.close()
    asyncio.run(run())


def test_share_check_records_failure_without_repeating_due_job(tmp_path, monkeypatch):
    async def run():
        store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
        store.set_many({'share_settings': {'share_check_hours': 24}, 'share_check_due': time.time() - 1})
        calls = []

        async def check_alive(engine):
            calls.append(engine)
            raise RuntimeError('检测服务不可用')

        monkeypatch.setattr(organizer_module.share_service, 'check_alive', check_alive)

        class FakeEngine:
            def __init__(self):
                self._share_check_task = None

            share_check_tick = Organizer.share_check_tick
            start_share_check = Organizer.start_share_check
            next_check_due = Organizer.next_check_due
            next_share_check_due = Organizer.next_share_check_due

        engine = FakeEngine()
        engine.store = store
        await engine.share_check_tick()
        await engine.share_check_tick()
        await asyncio.sleep(0)          # 0.9.56 起检测在后台 task 里跑
        assert calls == [engine]
        assert float(store.get('share_check_due')) > time.time()
        event = store.db.execute("SELECT message FROM events WHERE kind='share_check' ORDER BY id DESC LIMIT 1").fetchone()
        assert event and '检测服务不可用' in event[0]
        store.close()
    asyncio.run(run())
