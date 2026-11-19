"""Bounded, prioritized executor for synchronous SQLite/IO work.

Borrowed from MoviePilot's DatabaseWorker and Celery's queue isolation:
interactive work (page/API requests) and background work (SSE rebuild, scans,
maintenance) use separate pools and separate capacities. A burst of background
work cannot starve interactive requests; both queues are bounded so overload
fails fast instead of growing without limit.
"""
import asyncio
import concurrent.futures
import threading
from contextvars import copy_context
from typing import Callable, TypeVar

T = TypeVar('T')


class Pool:
    def __init__(self, name, max_workers, capacity):
        if max_workers < 1 or capacity < max_workers:
            raise ValueError('invalid db worker capacity')
        self.name = name
        self.max_workers = max_workers
        self.capacity = capacity
        self.lock = threading.Lock()
        self.loop = None
        self.executor = None
        self.queued = 0
        self.running = 0
        self.rejected = 0

    def stats(self):
        with self.lock:
            return {'max_workers': self.max_workers, 'capacity': self.capacity,
                    'queued': self.queued, 'running': self.running,
                    'rejected': self.rejected}

    async def run(self, operation: Callable[[], T]) -> T:
        loop = asyncio.get_running_loop()
        with self.lock:
            executor = self.executor
            if executor is None or self.loop is not loop:
                return await asyncio.to_thread(operation)
            if self.queued + self.running >= self.capacity:
                self.rejected += 1
                raise RuntimeError('database worker overloaded: %s' % self.name)
            self.queued += 1
        context = copy_context()
        started = {'value': False}

        def execute():
            with self.lock:
                if not started['value']:
                    started['value'] = True
                    self.queued = max(0, self.queued - 1)
                    self.running += 1
            try:
                return context.run(operation)
            finally:
                with self.lock:
                    self.running = max(0, self.running - 1)

        future = executor.submit(execute)
        wrapped = loop.create_future()

        def complete(done):
            def publish():
                with self.lock:
                    if not started['value']:
                        # Cancelled before a worker thread picked it up.
                        started['value'] = True
                        self.queued = max(0, self.queued - 1)
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


class DbWorker:
    def __init__(self, interactive_workers=3, interactive_capacity=24,
                 background_workers=2, background_capacity=32):
        self.interactive = Pool('interactive', interactive_workers, interactive_capacity)
        self.background = Pool('background', background_workers, background_capacity)

    async def start(self):
        loop = asyncio.get_running_loop()
        for pool in (self.interactive, self.background):
            with pool.lock:
                if pool.executor is not None:
                    if pool.loop is not loop:
                        raise RuntimeError('db worker cannot cross event loops')
                    continue
                pool.loop = loop
                pool.executor = concurrent.futures.ThreadPoolExecutor(
                    max_workers=pool.max_workers,
                    thread_name_prefix='sk-db-%s' % pool.name)

    def stats(self):
        return {'interactive': self.interactive.stats(),
                'background': self.background.stats()}

    async def run(self, operation, *, priority='interactive'):
        pool = self.background if priority == 'background' else self.interactive
        return await pool.run(operation)

    async def shutdown(self):
        for pool in (self.interactive, self.background):
            with pool.lock:
                executor = pool.executor
                pool.executor = None
            if executor is not None:
                executor.shutdown(wait=True, cancel_futures=True)
