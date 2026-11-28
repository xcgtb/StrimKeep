"""Dedicated background worker for StrimKeep.

Owns Organizer lanes, scheduler, channel monitor, Telegram bot and Emby media
coverage. Inventory scheduled scans stay with the Web API installer because
they depend on app.state functions registered there.
"""
import asyncio
import logging
import json
import os
import signal
import sqlite3
import time
from pathlib import Path

from fastapi import FastAPI

from . import __version__, commands
from .cloud115 import Cloud115
from .housekeeping import housekeeping_loop
from .leases import reap_stale_jobs
from .library_covers import LibraryCovers
from .media_info_api import inject_loop
from .network import ServiceHTTP
from .organizer import Organizer
from . import share_service
from .store import Store


class _App:
    pass


def heartbeat_payload(engine, background=None):
    """Worker health visible to Web: heartbeat alone cannot detect dead lanes."""
    return {
        'at': time.time(),
        'worker_alive': bool(engine.worker and not engine.worker.done()),
        'scheduler_alive': bool(engine.clock and not engine.clock.done()),
        'lane_faults': {name: bool(value) for name, value in engine.lane_faults.items()},
        'background': {name: not task.done() for name, task in (background or {}).items()},
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


async def _command_queue_io(stop, operation, *args):
    delay = 0.25
    last_log = None
    while True:
        try:
            return await asyncio.to_thread(operation, *args)
        except sqlite3.OperationalError:
            now = time.monotonic()
            if last_log is None or now - last_log >= 60:
                logging.getLogger(__name__).warning(
                    'Command queue %s could not reach SQLite; retrying', operation.__name__, exc_info=True)
                last_log = now
            try:
                await asyncio.wait_for(stop.wait(), delay)
                return None
            except TimeoutError:
                delay = min(5, delay * 2)


async def command_loop(store, engine, stop, kinds=commands.KINDS, inventory=None):
    """Consume Web-submitted commands one at a time."""
    while not stop.is_set():
        row = await _command_queue_io(stop, commands.claim, store, kinds)
        if row is None:
            try:
                await asyncio.wait_for(stop.wait(), 1)
            except TimeoutError:
                pass
            continue
        try:
            payload = json.loads(row['payload'] or '{}')
            if row['kind'] == 'share_check':
                result = await share_service.check_alive(engine)
            elif row['kind'] == 'share_revoke':
                result = await share_service.revoke(engine, payload['id'], payload)
            elif row['kind'] == 'share_prune':
                result = await share_service.prune_orphans(engine)
            elif row['kind'] in commands.INVENTORY_CLEANUP:
                result = await inventory.state.inventory_cleanup_command(row['kind'], payload)
            elif row['kind'] == 'strm_rebuild':
                result = await engine.rebuild.run(dry_run=bool(payload.get('dry_run')))
            elif row['kind'] == 'strm_rebuild_rollback':
                result = await engine.rebuild.rollback(payload.get('backup_id') or None)
            elif row['kind'] == commands.LIBRARY_COVER:
                result = await engine.library_covers.apply(payload)
            elif row['kind'] == commands.RETIRED_COVER:
                result = {'message': '影片封面补齐已停用。'}
            elif row['kind'] == 'inventory_compare':
                async def report(data):
                    await asyncio.to_thread(commands.progress, store, row['id'], data)
                result = await inventory.state.inventory_compare(report)
            elif row['kind'] == 'channel_check':
                rows = await engine.channels.tick(force=True)
                source_map = {source['name']: source for source in engine.channels.sources()}
                result = {'checked': len(rows), 'queued': sum(count for _, count in rows),
                          'channels': [{'name': name, 'queued': count,
                                        'state': source_map.get(name, {}).get('state', ''),
                                        'error': source_map.get(name, {}).get('error', '')}
                                       for name, count in rows]}
            else:
                raise RuntimeError('未知的后台命令：%s' % row['kind'])
            # Retry the database write alone. The operation may already have changed remote files.
            await _command_queue_io(stop, commands.finish, store, row['id'], result)
        except Exception as exc:
            error = str(getattr(exc, 'detail', None) or exc)[:300] or type(exc).__name__
            await _command_queue_io(stop, commands.finish, store, row['id'], None, error)


async def heartbeat_loop(store, engine, background, stop, *, interval=5):
    path = store.root / 'data' / 'worker-heartbeat'
    temporary = path.with_suffix('.tmp')
    pending = None
    last_error = None
    try:
        while not stop.is_set():
            temporary.write_text(str(os.getpid()), encoding='ascii')
            temporary.replace(path)
            if pending is None or pending.done():
                if pending is not None:
                    try:
                        pending.result()
                    except Exception:
                        now = time.monotonic()
                        if last_error is None or now - last_error >= 60:
                            last_error = now
                            logging.getLogger(__name__).warning('Worker health status could not be written', exc_info=True)
                payload = heartbeat_payload(engine, background)
                pending = asyncio.create_task(asyncio.to_thread(store.set_meta, 'worker_heartbeat', payload))
            try:
                await asyncio.wait_for(stop.wait(), interval)
            except TimeoutError:
                continue
    finally:
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)


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
    engine.library_covers = LibraryCovers(store, http)
    await asyncio.to_thread(commands.recover_running, store)
    scan_app = inventory_app(store, engine, strm_root.resolve(), http)
    from .inventory_operations_api import inventory_delete_loop
    background = {name: asyncio.create_task(coro, name=name) for name, coro in {
        'emby_coverage': engine.emby.coverage_loop(),
        'media_info': inject_loop(app, store),
        'housekeeping': housekeeping_loop(app, store),
        'operations': command_loop(store, engine, stop, commands.OPERATIONS, scan_app),
        'cover_operations': command_loop(store, engine, stop, commands.COVER_OPERATIONS),
        'inventory_compare': command_loop(store, engine, stop, ('inventory_compare',), scan_app),
        'inventory_delete': inventory_delete_loop(store, scan_app.state.inventory_run_delete),
        'channel_check': command_loop(store, engine, stop, ('channel_check',)),
        'library_covers': engine.library_covers.schedule_loop(),
        'inventory_schedule': scan_app.state.inventory_scan_loop(),
    }.items()}
    background['cloud_activity'] = engine.cloud_activity_task
    store.event('system', f'StrimKeep worker {__version__} started.')
    last_reap = 0.0
    last_prune = 0.0
    heartbeat = asyncio.create_task(heartbeat_loop(store, engine, background, stop))
    try:
        while not stop.is_set():
            if heartbeat.done():
                await heartbeat
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
        stop.set()
        for task in background.values():
            task.cancel()
        if background:
            await asyncio.gather(*background.values(), return_exceptions=True)
        await asyncio.gather(heartbeat, return_exceptions=True)
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
