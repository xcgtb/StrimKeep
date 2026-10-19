"""Real saved-directory, account-lifecycle and busy-worker regression scenarios."""
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from dev.tests.test_cloud_workbench import cloud
from dev.tests.test_cloud_receiving import receiving, channel, msg
from app.cloud import (accounts, channel_reader, credentials, directories,
                       emby_library, jobs, maintenance, receiving as intake,
                       services, settings, store, strm_scan, workflow)


def test_saved_directory_drives_one_scheduled_sync_with_real_sqlite(receiving):
    directories.save(receiving.id, {'strm': '2'}, 1)
    assert directories.get(receiving.id)['directories']['strm'] == {'id': '2', 'name': '归档'}
    settings.save({'strm_scan_cron': '0 0 * * *'})
    first = strm_scan.tick(settings.get(), 1_700_000_000)
    assert len(first) == 1
    job = store.job(first[0]['job_id'])
    with store.db() as con:
        payload = json.loads(con.execute('SELECT payload FROM cloud_jobs WHERE id=?', (job['id'],)).fetchone()[0])
    assert job['kind'] == 'sync' and payload['source_cid'] == '2'
    assert payload['account_revision'] == store.account(receiving.id)['revision']
    assert not strm_scan.tick(settings.get(), 1_700_000_060)
    # A later period must not stack another scan behind a still-pending scan.
    assert not strm_scan.tick(settings.get(), 1_700_086_500)
    assert settings.get()['strm_scan_last_run'] == 1_700_086_500
    with store.db() as con:
        assert con.execute("SELECT count(*) FROM cloud_jobs WHERE kind='sync'").fetchone()[0] == 1
        con.execute("UPDATE cloud_jobs SET state='success' WHERE id=?", (job['id'],))
    assert len(strm_scan.tick(settings.get(), 1_700_173_000)) == 1
    assert receiving.remote.writes == []


def test_disconnect_clears_live_services_and_channels_but_retains_history(receiving):
    intake.save(receiving.id, {'tg_enabled': True}, 0)
    rule = channel(receiving)
    incoming = intake.capture(msg(), authorized=True)[0]
    with store.db() as con:
        con.execute("INSERT INTO cloud_channel_cursors VALUES(?,10,1,0,'')", (rule['id'],))
        con.execute("INSERT INTO cloud_service_settings VALUES(?,?,?,?,?,?)",
                    (receiving.id, 'telegram_reader', '{}', 1, 1, 1))
    finished = jobs.enqueue('sync', {'account_id': receiving.id})
    with store.db() as con:
        con.execute("UPDATE cloud_jobs SET state='success' WHERE id=?", (finished,))
    store.disconnect_account(receiving.id)
    with store.db() as con:
        assert con.execute("SELECT count(*) FROM cloud_service_settings WHERE account_id=?", (receiving.id,)).fetchone()[0] == 0
        assert con.execute("SELECT count(*) FROM cloud_channels WHERE account_id=?", (receiving.id,)).fetchone()[0] == 0
        assert con.execute("SELECT count(*) FROM cloud_channel_cursors WHERE id=?", (rule['id'],)).fetchone()[0] == 0
        state, result = con.execute("SELECT state,result FROM cloud_inbox WHERE id=?", (incoming,)).fetchone()
        assert state == 'processed' and '账号已断开' in result
    assert store.job(finished)['state'] == 'success'
    assert intake.capture(msg(mid=2), authorized=True) == []
    assert channel_reader.poll_one() is False
    new = accounts.save_authorization('999', {}, {'cookie': 'UID=999_A1_1; CID=x; SEID=y'})
    directories.save(new, {'transfer': '3'}, 0)
    assert intake.save(new, {'tg_enabled': True}, 0)['config']['tg_enabled']


def test_legacy_orphan_services_no_longer_block_or_capture_new_account(receiving):
    with store.db() as con:
        con.execute("INSERT INTO cloud_service_settings VALUES(?,?,?,?,?,?)",
                    ('deleted-account', 'receiving', '{"tg_enabled":true}', 1, 1, 1))
        con.execute("INSERT INTO cloud_inbox(id,event_key,account_id,source,payload,state,created) VALUES(?,?,?,?,?,'queued',?)",
                    ('old-inbox', 'old-event', 'deleted-account', 'telegram', '{}', 1))
    assert intake.save(receiving.id, {'tg_enabled': True}, 0)['config']['tg_enabled']
    captured = intake.capture(msg(), authorized=True)
    assert len(captured) == 1
    intake.schedule()
    with store.db() as con:
        assert con.execute("SELECT state FROM cloud_inbox WHERE id='old-inbox'").fetchone()[0] == 'processed'
        assert not con.execute("SELECT 1 FROM cloud_jobs WHERE json_extract(payload,'$.account_id')='deleted-account'").fetchone()
        assert con.execute("SELECT account_id FROM cloud_inbox WHERE id=?", (captured[0],)).fetchone()[0] == receiving.id


def test_capture_rechecks_account_after_sealing_before_insert(receiving, monkeypatch):
    intake.save(receiving.id, {'tg_enabled': True}, 0)
    real_seal = credentials.seal
    def disconnect_before_insert(value):
        sealed = real_seal(value)
        store.disconnect_account(receiving.id)
        return sealed
    monkeypatch.setattr(credentials, 'seal', disconnect_before_insert)
    assert intake.capture(msg(), authorized=True) == []
    with store.db() as con:
        assert not con.execute('SELECT 1 FROM cloud_inbox').fetchone()


@pytest.mark.parametrize('scheduler_failure', [False, True])
def test_busy_worker_keeps_scheduling_without_starting_another_write(cloud, monkeypatch, scheduler_failure):
    jobs.enqueue('sync', {'account_id': cloud.id, 'source_cid': '2'})
    stop = threading.Event()
    monkeypatch.setattr(jobs, '_STOP', stop)
    clock = [0.0]
    monkeypatch.setattr(jobs.time, 'monotonic', lambda: clock[0])
    def wait(_):
        clock[0] += 1
        if clock[0] >= 6:
            stop.set()
        return False
    spawn = Mock(return_value=SimpleNamespace(done=SimpleNamespace(wait=wait)))
    monkeypatch.setattr(jobs.tasks.manager, 'spawn', spawn)
    readers = Mock()
    intake_dispatch = Mock()
    maintain = Mock(side_effect=[None, RuntimeError('fixture'), None] if scheduler_failure else None)
    monkeypatch.setattr(maintenance, 'schedule', maintain)
    monkeypatch.setattr(intake, 'schedule', intake_dispatch)
    monkeypatch.setattr(channel_reader, 'schedule', readers)
    monkeypatch.setattr(emby_library, 'schedule', Mock())
    jobs._loop()
    assert readers.call_count >= 2 and intake_dispatch.call_count >= 2
    assert maintain.call_count == 3
    assert spawn.call_count == 1
    assert cloud.remote.writes == []
