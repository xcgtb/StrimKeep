"""Complete offline intake with a durable, strict, read-only completion gate."""

import json
from unittest.mock import Mock

import pytest
from dev.tests.test_cloud_workbench import cloud
from dev.tests.test_cloud_receiving import MAGNET, ED2K, receiving, run
from app.cloud import directories, jobs, offline_handoff as handoff, receiving as receive, store, workflow
from app.cloud.errors import CloudError


def submit(cloud, *, organize=True, text=MAGNET):
    jid = receive.apply(receive.preview(cloud.id, text, organize=organize)['id'], True)
    assert run(jid)['state'] == 'success'
    return jid


def task(*, status=2, target='3', hash_value='a'*40):
    return {'info_hash': hash_value, 'wp_path_id': target, 'status': status}


def remote_list(cloud, monkeypatch, rows):
    monkeypatch.setattr(workflow, 'verify_account', lambda *args: cloud.remote)
    monkeypatch.setattr(cloud.remote, 'offline_tasks', lambda page: {'items': list(rows), 'page_count': 1})


def test_optin_creates_durable_receipt_and_debounces_completed_download(receiving, monkeypatch):
    jid = submit(receiving)
    with store.db() as con:
        entry = con.execute('SELECT info_hash,state,account_id,source_cid,target_cid FROM cloud_offline_handoffs').fetchone()
    assert entry == ('a'*40, 'waiting', receiving.id, '3', '2')
    assert 'magnet:' not in str(entry)
    remote_list(receiving, monkeypatch, [task(status=1)])
    assert handoff.poll_one() == 0
    with store.db() as con:
        con.execute('UPDATE cloud_offline_handoffs SET next_check=0')
    remote_list(receiving, monkeypatch, [task()])
    assert handoff.poll_one() == 1
    assert handoff.poll_one() == 0
    with store.db() as con:
        rows = con.execute("SELECT id,payload FROM cloud_jobs WHERE kind='workflow'").fetchall()
    assert len(rows) == 1
    payload = json.loads(rows[0][1])
    assert payload['source_cid'] == '3' and payload['target_cid'] == '2'
    assert payload['expected_directory_revision'] == directories.get(receiving.id)['revision']
    assert handoff.records(receiving.id)[0]['child_job'] == rows[0][0]
    assert receiving.remote.offline_add.call_count == 1


def test_no_auto_without_optin_and_no_hash_does_not_guess(receiving):
    submit(receiving, organize=False)
    with store.db() as con:
        assert not con.execute('SELECT 1 FROM cloud_offline_handoffs').fetchone()
    receiving.remote.offline_add.return_value = {'accepted': True, 'info_hash': '', 'completed': False}
    jid = receive.apply(receive.preview(receiving.id, MAGNET, organize=True)['id'], True)
    receipt = run(jid)
    assert receipt['state'] == 'needs_review'
    with store.db() as con:
        assert not con.execute('SELECT 1 FROM cloud_offline_handoffs').fetchone()
    assert 'needs_review' in receipt['result']['accepted'][0]['handoff']['state']
    assert '哈希' in receipt['result']['errors'][0]['reason']


@pytest.mark.parametrize('rows',[
    [task(status=0)], [task(status=-1)], [task(target='9')],
    [task(hash_value='b'*40)],
    [task(),task(status=1,hash_value='b'*40)],
    [task(),task()],
])
def test_incomplete_mismatched_ambiguous_or_other_incomplete_tasks_never_run(receiving, monkeypatch, rows):
    submit(receiving)
    remote_list(receiving, monkeypatch, rows)
    assert handoff.poll_one() == 0
    with store.db() as con:
        assert not con.execute("SELECT 1 FROM cloud_jobs WHERE kind='workflow'").fetchone()


def test_credential_change_blocks_forever_and_does_not_poll_remote(receiving, monkeypatch):
    submit(receiving)
    with store.db() as con:
        con.execute('UPDATE cloud_accounts SET revision=revision+1 WHERE id=?',(receiving.id,))
    remote = Mock(side_effect=AssertionError('stale account must not access cloud'))
    monkeypatch.setattr(workflow, 'verify_account', remote)
    assert handoff.poll_one() == 0
    assert handoff.records(receiving.id)[0]['state']=='blocked'
    remote.assert_not_called()


def test_directory_change_blocks_handoff(receiving, monkeypatch):
    submit(receiving)
    dirs = directories.get(receiving.id)
    directories.save(receiving.id, {'target': '0'}, dirs['revision'])
    remote = Mock(side_effect=AssertionError('old intake directory should not poll'))
    monkeypatch.setattr(workflow, 'verify_account', remote)
    assert handoff.poll_one() == 0
    assert handoff.records(receiving.id)[0]['state']=='blocked'
    remote.assert_not_called()


def test_change_after_enqueued_refuses_organize(receiving, monkeypatch):
    submit(receiving)
    remote_list(receiving, monkeypatch, [task()])
    assert handoff.poll_one() == 1
    dirs=directories.get(receiving.id)
    directories.save(receiving.id, {'target': '0'}, dirs['revision'])
    with store.db() as con:
        child=con.execute("SELECT id FROM cloud_jobs WHERE kind='workflow'").fetchone()[0]
    assert run(child)['state']=='failed'
    assert not receiving.remote.writes


def test_source_parent_nesting_blocked_before_queue(receiving, monkeypatch):
    submit(receiving)
    remote_list(receiving,monkeypatch,[task()])
    receiving.remote.files['2']['parent_id'] = '3'
    assert handoff.poll_one() == 0
    with store.db() as con:
        assert not con.execute("SELECT 1 FROM cloud_jobs WHERE kind='workflow'").fetchone()


def test_receipts_redact_hash_and_urls(receiving):
    submit(receiving)
    info=handoff.records(receiving.id)
    assert info and info[0]['task_hash']=='aaaaaaaa…'
    assert MAGNET not in str(info)
    assert 'a'*40 not in str(info)


def test_safely_stop_organizer_pauses_pending_download_handoffs(receiving,monkeypatch):
    from app.cloud import intake_automation
    submit(receiving)
    result=intake_automation.stop_all(receiving.id)
    assert result['offline_paused']==1
    assert handoff.records(receiving.id)[0]['state']=='paused'
    remote_list(receiving,monkeypatch,[task()])
    assert handoff.poll_one()==0
    with store.db() as con:
        assert not con.execute("SELECT 1 FROM cloud_jobs WHERE kind='workflow'").fetchone()
