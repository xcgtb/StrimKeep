"""Bounded executor for synchronous SQLite/IO work.

Borrowed from MoviePilot's DatabaseWorker: a fixed-size thread pool with a hard
queue capacity so a burst of requests cannot grow unbounded and stall the
event loop. StrimKeep's Store already creates one SQLite connection per thread;
this worker only controls *where* the synchronous work runs.
"""
import asyncio
import concurrent.futures
import threading
from contextvars import copy_context
from typing import Callable, TypeVar

T = TypeVar('T')

MAX_WORKERS = 4
CAPACITY = 64


class DbWorker:
    def __init__(self, max_workers: int = MAX_WORKERS, capacity: int = CAPACITY):
        if max_workers < 1 or capacity < max_workers:
            raise ValueError('invalid db worker capacity')
        self.max_workers = max_workers
        self.capacity = capacity
        self._lock = threading.Lock()
        self._loop = None
        self._executor = None
        self._queued = 0
        self._running = 0
        self._rejected = 0

    async def start(self):
        loop = asyncio.get_running_loop()
        with self._lock:
            if self._executor is not None:
                if self._loop is not loop:
                    raise RuntimeError('db worker cannot cross event loops')
                return
            self._loop = loop
            self._executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=self.max_workers, thread_name_prefix='strimkeep-db')

    def stats(self):
        with self._lock:
            return {'max_workers': self.max_workers, 'capacity': self.capacity,
                    'queued': self._queued, 'running': self._running,
                    'rejected': self._rejected}

    async def run(self, operation: Callable[[], T]) -> T:
        loop = asyncio.get_running_loop()
        with self._lock:
            executor = self._executor
            if executor is None or self._loop is not loop:
                # Tests create apps outside lifespan; fall back to asyncio's pool.
                return await asyncio.to_thread(operation)
            if self._queued + self._running >= self.capacity:
                self._rejected += 1
                raise RuntimeError('database worker overloaded')
            self._queued += 1
        context = copy_context()
        future = executor.submit(context.run, operation)
        wrapped = loop.create_future()

        def complete(done):
            def publish():
                with self._lock:
                    if self._queued > 0:
                        self._queued -= 1
                    else:
                        self._running = max(0, self._running - 1)
                if done.cancelled():
                    wrapped.cancel()
                elif done.exception():
                    wrapped.set_exception(done.exception())
                else:
                    wrapped.set_result(done.result())
            try:
                loop.call_soon_threadsafe(publish)
            except RuntimeError:
                pass
        future.add_done_callback(complete)
        return await asyncio.shield(wrapped)

    async def shutdown(self):
        with self._lock:
            executor = self._executor
            self._executor = None
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=True)
