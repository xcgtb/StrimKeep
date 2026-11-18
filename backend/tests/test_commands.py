import asyncio
import time

from strimkeep import commands
from strimkeep.store import Store

PASSWORD = 'testpassword123'


def make_store(tmp_path):
    return Store(tmp_path / 'data', 'admin', PASSWORD)


def test_submit_claim_finish_roundtrip(tmp_path):
    store = make_store(tmp_path)
    command_id = commands.submit(store, 'share_check', {'scope': 'file'})
    row = commands.claim(store)
    assert row['id'] == command_id and row['state'] == 'running'
    assert commands.claim(store) is None
    commands.finish(store, command_id, {'alive': 3})
    done = commands.get(store, command_id)
    assert done['state'] == 'done'
    assert '"alive": 3' in done['result']
    store.close()


def test_wait_returns_result_and_timeout(tmp_path):
    store = make_store(tmp_path)
    done_id = commands.submit(store, 'share_check', {})
    commands.claim(store)
    commands.finish(store, done_id, {'alive': 1})
    assert asyncio.run(commands.wait(store, done_id, timeout=1)) == {'alive': 1}

    pending_id = commands.submit(store, 'share_check', {})
    assert asyncio.run(commands.wait(store, pending_id, timeout=0.2, interval=0.05)) is None
    store.close()


def test_failed_command_is_reported(tmp_path):
    store = make_store(tmp_path)
    command_id = commands.submit(store, 'share_check', {})
    commands.claim(store)
    commands.finish(store, command_id, None, 'boom')
    result = asyncio.run(commands.wait(store, command_id, timeout=1))
    assert result['failed'] is True and 'boom' in result['message']
    store.close()


def test_recover_running_requeues_once_then_fails(tmp_path):
    store = make_store(tmp_path)
    command_id = commands.submit(store, 'share_check', {})
    commands.claim(store)
    assert commands.recover_running(store) == 1
    assert commands.get(store, command_id)['state'] == 'queued'
    commands.claim(store)
    assert commands.recover_running(store) == 1
    failed = commands.get(store, command_id)
    assert failed['state'] == 'failed' and '后台重启' in failed['message']
    store.close()


def test_worker_ready_reads_dict_and_legacy(tmp_path):
    store = make_store(tmp_path)
    assert not commands.worker_ready(store)
    store.set_meta('worker_heartbeat', {'at': time.time(), 'lane_faults': {}})
    assert commands.worker_ready(store)
    store.set_meta('worker_heartbeat', {'at': time.time(), 'lane_faults': {'organize': True}})
    assert not commands.worker_ready(store)
    store.set_meta('worker_heartbeat', time.time())
    assert commands.worker_ready(store)
    store.set_meta('worker_heartbeat', time.time() - 100)
    assert not commands.worker_ready(store)
    store.close()


def test_running_and_prune(tmp_path):
    store = make_store(tmp_path)
    command_id = commands.submit(store, 'share_check', {})
    assert commands.running(store, 'share_check')['id'] == command_id
    commands.claim(store)
    commands.finish(store, command_id, {})
    with store.db:
        store.db.execute('UPDATE worker_commands SET finished=? WHERE id=?',
                         (time.time() - commands.KEEP_SECONDS - 10, command_id))
    assert commands.prune(store) == 1
    assert commands.get(store, command_id) is None
    store.close()
