"""Dedicated background worker for StrimKeep.

Owns Organizer lanes, scheduler, channel monitor, Telegram bot and Emby media
coverage. Inventory scheduled scans stay with the Web API installer because
they depend on app.state functions registered there.
"""
import asyncio
import os
import signal
import time
from pathlib import Path

from . import __version__
from .cloud115 import Cloud115
from .housekeeping import housekeeping_loop
from .media_info_api import inject_loop
from .network import ServiceHTTP
from .organizer import Organizer
from .store import Store


class _App:
    pass


async def run_worker():
    root = Path(os.environ.get('STRIMKEEP_DATA_DIR', '/data'))
    strm_root = Path(os.environ.get('STRIMKEEP_STRM_DIR', '/strm'))
    strm_root.mkdir(parents=True, exist_ok=True)
    store = Store(root, os.environ.get('ADMIN_USERNAME', ''), os.environ.get('ADMIN_PASSWORD', ''))
    cloud = Cloud115(store)
    http = ServiceHTTP(store)
    engine = Organizer(store, cloud, http, strm_root)
    lock = root / 'data/worker.lock'
    lock.parent.mkdir(parents=True, exist_ok=True)
    handle = lock.open('a')
    import fcntl
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        await cloud.close()
        await http.aclose()
        store.close()
        print('worker already running', flush=True)
        return 0
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    app = _App()
    app.state = _App()
    app.state.store = store
    app.state.strms = strm_root.resolve()
    app.state.organizer = engine
    engine.start()
    background = [
        asyncio.create_task(engine.emby.coverage_loop()),
        asyncio.create_task(inject_loop(app, store)),
        asyncio.create_task(housekeeping_loop(app, store)),
    ]
    store.event('system', f'StrimKeep worker {__version__} started.')
    try:
        while not stop.is_set():
            store.set_meta('worker_heartbeat', time.time())
            await asyncio.sleep(5)
    finally:
        for task in background:
            task.cancel()
        if background:
            await asyncio.gather(*background, return_exceptions=True)
        await engine.close()
        await cloud.close()
        await http.aclose()
        handle.close()
        store.close()
    return 0


def main():
    return asyncio.run(run_worker())


if __name__ == '__main__':
    raise SystemExit(main())
