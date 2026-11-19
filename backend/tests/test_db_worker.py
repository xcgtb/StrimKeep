import asyncio
import threading

from strimkeep.db_worker import DbWorker


def test_db_worker_priority_pools_are_isolated():
    async def run():
        worker = DbWorker()
        await worker.start()
        assert worker.interactive.max_workers == 3
        assert worker.background.max_workers == 2
        assert worker.interactive.executor is not worker.background.executor
        result = await worker.run(lambda: 'i')
        assert result == 'i'
        result = await worker.run(lambda: 'b', priority='background')
        assert result == 'b'
        await worker.shutdown()
    asyncio.run(run())


def test_db_worker_background_overload_does_not_touch_interactive():
    async def run():
        worker = DbWorker(interactive_workers=1, interactive_capacity=2,
                          background_capacity=1, background_workers=1)
        await worker.start()
        gate = threading.Event()

        def block():
            gate.wait(5)
            return 1
        task = asyncio.create_task(worker.run(block, priority='background'))
        await asyncio.sleep(0.05)
        try:
            await worker.run(lambda: 2, priority='background')
        except RuntimeError as exc:
            assert 'background' in str(exc)
        else:
            raise AssertionError('background pool should reject overload')
        assert await worker.run(lambda: 3, priority='interactive') == 3
        gate.set()
        assert await task == 1
        await worker.shutdown()
    asyncio.run(run())


def test_db_worker_reports_running_and_queued():
    async def run():
        worker = DbWorker(interactive_workers=1, interactive_capacity=4)
        await worker.start()
        gate = threading.Event()

        def block():
            gate.wait(5)
            return 1
        task = asyncio.create_task(worker.run(block))
        await asyncio.sleep(0.05)
        stats = worker.stats()['interactive']
        assert stats['running'] == 1 and stats['queued'] == 0
        gate.set()
        assert await task == 1
        await asyncio.sleep(0.05)
        stats = worker.stats()['interactive']
        assert stats['running'] == 0 and stats['queued'] == 0
        await worker.shutdown()
    asyncio.run(run())


def test_metrics_exposes_db_worker_pools(tmp_path):
    from strimkeep.metrics import Metrics
    from strimkeep.store import Store

    store = Store(tmp_path / 'data', 'admin', 'testpassword123')
    metrics = Metrics(store)
    app = type('App', (), {'version': 'test', 'state': type('S', (), {})()})()
    app.state.db_worker = DbWorker()
    body = metrics.render(app)
    assert 'strimkeep_db_worker_capacity{pool="interactive"} 24' in body
    assert 'strimkeep_db_worker_capacity{pool="background"} 32' in body
    assert 'strimkeep_db_worker_rejected_total{pool="interactive"} 0' in body
    store.close()

