import asyncio
import json

from strimkeep import commands, worker
from strimkeep.store import Store

PASSWORD = 'testpassword123'


class FakeRebuild:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    async def run(self, dry_run=False):
        self.calls.append(('run', dry_run))
        if self.fail:
            raise RuntimeError('rebuild boom')
        return {'scanned': 3, 'dry_run': dry_run, 'rewritten': 0}

    async def rollback(self, backup_id=None):
        self.calls.append(('rollback', backup_id))
        return {'restored': 1, 'backup_id': backup_id}


class FakeEngine:
    def __init__(self, fail=False):
        self.rebuild = FakeRebuild(fail)


def execute(tmp_path, kind, payload, engine, monkeypatch, fake_check=None):
    if fake_check is not None:
        monkeypatch.setattr(worker.share_service, 'check_alive', fake_check)
    store = Store(tmp_path / 'data', 'admin', PASSWORD)
    command_id = commands.submit(store, kind, payload)

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(worker.command_loop(store, engine, stop))
        try:
            for _ in range(200):
                row = await asyncio.to_thread(commands.get, store, command_id)
                if row and row['state'] in ('done', 'failed'):
                    return row
                await asyncio.sleep(0.02)
            return await asyncio.to_thread(commands.get, store, command_id)
        finally:
            stop.set()
            await task

    row = asyncio.run(scenario())
    store.close()
    return row


def test_worker_runs_share_check_command(tmp_path, monkeypatch):
    async def fake_check(_engine):
        return {'alive': 2, 'dead_count': 0, 'skipped': 0}

    row = execute(tmp_path, 'share_check', {'scope': 'file'}, FakeEngine(),
                  monkeypatch, fake_check=fake_check)
    assert row['state'] == 'done'
    assert json.loads(row['result'])['alive'] == 2


def test_worker_runs_rebuild_and_rollback(tmp_path, monkeypatch):
    engine = FakeEngine()
    row = execute(tmp_path, 'strm_rebuild', {'dry_run': True}, engine, monkeypatch)
    assert row['state'] == 'done'
    assert json.loads(row['result'])['dry_run'] is True
    assert engine.rebuild.calls == [('run', True)]

    row = execute(tmp_path, 'strm_rebuild_rollback', {'backup_id': 5}, engine, monkeypatch)
    assert row['state'] == 'done'
    assert json.loads(row['result'])['backup_id'] == 5
    assert engine.rebuild.calls[-1] == ('rollback', 5)


def test_worker_records_command_failure(tmp_path, monkeypatch):
    row = execute(tmp_path, 'strm_rebuild', {'dry_run': False},
                  FakeEngine(fail=True), monkeypatch)
    assert row['state'] == 'failed'
    assert 'rebuild boom' in row['message']


def test_watchdog_pulses_when_health_database_write_waits(tmp_path, monkeypatch):
    import os
    import threading
    from types import SimpleNamespace

    store = Store(tmp_path, 'admin', PASSWORD)
    writing, release = threading.Event(), threading.Event()
    original = store.set_meta
    def delayed(key, value):
        writing.set()
        assert release.wait(5)
        original(key, value)
    monkeypatch.setattr(store, 'set_meta', delayed)
    monkeypatch.setattr(worker, 'heartbeat_payload', lambda *_: {'at': 42})

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(worker.heartbeat_loop(store, SimpleNamespace(), {}, stop, interval=.01))
        path = tmp_path / 'data' / 'worker-heartbeat'
        try:
            for _ in range(100):
                if path.exists() and writing.is_set():
                    break
                await asyncio.sleep(.01)
            assert writing.is_set()
            initial = path.stat().st_mtime_ns
            await asyncio.sleep(.05)
            assert path.read_text() == str(os.getpid())
            assert path.stat().st_mtime_ns > initial
            assert not store.meta('worker_heartbeat')
        finally:
            release.set()
            stop.set()
            await task
    asyncio.run(scenario())
    assert store.meta_json('worker_heartbeat', {})['at'] == 42
    store.close()


def test_publication_gate_serializes_instances_and_releases_on_cancel(tmp_path):
    import pytest
    from strimkeep.leases import PublicationGate, fcntl

    if fcntl is None:
        pytest.skip('Production uses flock')
    first, second = PublicationGate(tmp_path), PublicationGate(tmp_path)

    async def scenario():
        async with first:
            waiter = asyncio.create_task(second.acquire())
            await asyncio.sleep(.06)
            assert not waiter.done()
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
        async with asyncio.timeout(2):
            async with second:
                assert second.locked()
    asyncio.run(scenario())
