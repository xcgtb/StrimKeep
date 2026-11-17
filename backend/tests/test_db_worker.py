import asyncio
import threading

import pytest

from strimkeep.db_worker import DbWorker


def test_db_worker_runs_sync_work_off_event_loop():
    async def run():
        worker = DbWorker()
        await worker.start()
        loop_thread = threading.get_ident()
        def work():
            return threading.get_ident()
        thread_id = await worker.run(work)
        assert thread_id != loop_thread
        await worker.shutdown()
    asyncio.run(run())


def test_db_worker_rejects_overload():
    async def run():
        worker = DbWorker(max_workers=1, capacity=1)
        await worker.start()
        gate = threading.Event()
        def block():
            gate.wait(5)
            return 1
        task = asyncio.create_task(worker.run(block))
        await asyncio.sleep(0.05)
        with pytest.raises(RuntimeError, match='overloaded'):
            await worker.run(lambda: 2)
        gate.set()
        assert await task == 1
        await worker.shutdown()
    asyncio.run(run())
