"""Dedicated background worker for StrimKeep.

Owns Organizer lanes, scheduler, channel monitor, Telegram bot and Emby media
coverage. Inventory scheduled scans stay with the Web API installer because
they depend on app.state functions registered there.
"""
import asyncio
import json
import os
import signal
import time
from pathlib import Path

from fastapi import FastAPI

from . import __version__, commands
from .cloud115 import Cloud115
from .housekeeping import housekeeping_loop
from .leases import reap_stale_jobs
from .media_info_api import inject_loop
from .network import ServiceHTTP
from .organizer import Organizer
from . import share_service
from .store import Store


class _App:
    pass


def heartbeat_payload(engine):
    """Worker health visible to Web: heartbeat alone cannot detect dead lanes."""
    return {
        'at': time.time(),
        'worker_alive': bool(engine.worker and not engine.worker.done()),
        'scheduler_alive': bool(engine.clock and not engine.clock.done()),
        'lane_faults': {name: bool(value) for name, value in engine.lane_faults.items()},
        'closing': bool(engine.closing),
    }


def inventory_app(store, engine, strm_root, http):
    """Build the inventory installers without serving HTTP.

    Scheduled governance scans live in install_inventory(), so the Worker needs
    the same closures. A private FastAPI app gives them app.state without
    duplicating the scan/plan logic.
    """
    private = FastAPI()
    private.state.store = store
    private.state.strms = strm_root
    private.state.organizer = engine
    private.state.http = http
    private.state.cloud = engine.cloud

    async def admin():
        return True

    from .inventory_api import install_inventory
    from .inventory_operations_api import install_inventory_operations
    install_inventory(private, store, admin)
    install_inventory_operations(private, store, admin)
    return private


async def command_loop(store, engine, stop, kinds=commands.KINDS):
    """Consume Web-submitted commands one at a time."""
    while not stop.is_set():
        row = await asyncio.to_thread(commands.claim, store, kinds)
        if row is None:
            await asyncio.sleep(1)
            continue
        try:
            payload = json.loads(row['payload'] or '{}')
            if row['kind'] == 'share_check':
                result = await share_service.check_alive(engine)
            elif row['kind'] == 'strm_rebuild':
                result = await engine.rebuild.run(dry_run=bool(payload.get('dry_run')))
            elif row['kind'] == 'strm_rebuild_rollback':
                result = await engine.rebuild.rollback(payload.get('backup_id') or None)
            elif row['kind'] == commands.COVER_SWEEP:
                result = await engine.cover.run()
                if result.get('error'):
                    raise RuntimeError(result['error'])
            else:
                raise RuntimeError('未知的后台命令：%s' % row['kind'])
            await asyncio.to_thread(commands.finish, store, row['id'], result)
        except Exception as exc:
            await asyncio.to_thread(commands.finish, store, row['id'], None,
                                    str(exc)[:300] or type(exc).__name__)


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
    engine.cover.queue_daily = True
    await asyncio.to_thread(commands.recover_running, store)
    scan_app = inventory_app(store, engine, strm_root.resolve(), http)
    background = [
        asyncio.create_task(engine.emby.coverage_loop()),
        asyncio.create_task(inject_loop(app, store)),
        asyncio.create_task(housekeeping_loop(app, store)),
        asyncio.create_task(command_loop(store, engine, stop, commands.OPERATIONS)),
        asyncio.create_task(command_loop(store, engine, stop, (commands.COVER_SWEEP,))),
        asyncio.create_task(scan_app.state.inventory_scan_loop()),
    ]
    store.event('system', f'StrimKeep worker {__version__} started.')
    last_reap = 0.0
    last_prune = 0.0
    try:
        while not stop.is_set():
            store.set_meta('worker_heartbeat', heartbeat_payload(engine))
            now = time.monotonic()
            if now - last_reap >= 120:
                last_reap = now
                try:
                    await asyncio.to_thread(reap_stale_jobs, store)
                except Exception:
                    pass
            if now - last_prune >= 3600:
                last_prune = now
                try:
                    await asyncio.to_thread(commands.prune, store)
                except Exception:
                    pass
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
