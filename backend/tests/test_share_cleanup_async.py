import asyncio
import threading

from strimkeep import share_service


def test_empty_pending_cleanup_stays_beneath_root(tmp_path):
    root = tmp_path / "pending"
    leaf = root / "share" / "season"
    leaf.mkdir(parents=True)
    share_service.cleanup_empty_pending(leaf, root)
    assert root.is_dir()
    assert not leaf.exists()


def test_cleanup_thread_does_not_block_loop_and_finishes_before_cancellation(monkeypatch):
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def slow_cleanup(*args):
        started.set()
        release.wait(timeout=3)
        finished.set()

    monkeypatch.setattr(share_service, 'cleanup_empty_pending', slow_cleanup)

    async def scenario():
        task = asyncio.create_task(share_service.cleanup_empty_pending_async('unused', 'unused'))
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done()
        release.set()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert finished.is_set()

    asyncio.run(scenario())
