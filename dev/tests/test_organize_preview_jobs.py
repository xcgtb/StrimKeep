"""Slow previews survive HTTP boundaries; only confirmed jobs mutate media."""
import time
from unittest.mock import Mock

import pytest

from dev.tests.test_cloud_workbench import cloud
from dev.tests.test_my_drive import client
from app.cloud import accounts, jobs, plan_preview, store, workflow
from app.cloud.errors import CloudError


def start(client, cloud, **options):
    response = client.post('/api/cloud/plans/scan', json={
        'account_id': cloud.id, 'source_cid': '1', 'target_cid': '2', **options})
    assert response.status_code == 202, response.text
    return response.json()['job_id']


def test_slow_preview_returns_before_remote_reads_and_can_be_recovered_and_applied(cloud, client, monkeypatch):
    original = workflow.recognize_source
    clock = time.time
    called = []
    def slow(*args, **kwargs):
        called.append(True)
        # Simulate recognition outlasting the former 120 second HTTP deadline.
        monkeypatch.setattr(time, 'time', lambda: clock() + 195)
        return original(*args, **kwargs)
    monkeypatch.setattr(workflow, 'recognize_source', slow)
    jid = start(client, cloud)
    assert not called and not cloud.remote.writes
    jobs.execute(jid)
    detail = client.get('/api/cloud/jobs/' + jid).json()['job']
    assert detail['state'] == 'success', detail
    assert detail['preview_input']['account_id'] == cloud.id
    pid = detail['result']['plan_id']
    response = client.get('/api/cloud/plans/' + pid)
    assert response.status_code == 200
    planned = response.json()['plan']
    assert planned['items'] and planned['expires_in'] > 890
    assert not cloud.remote.writes
    assert not list(cloud.root.rglob('*.strm'))
    # Reopening the saved receipt does not scan, submit, or move anything.
    assert client.get('/api/cloud/plans/' + pid).json()['plan']['id'] == pid
    assert len(called) == 1
    assert client.post('/api/cloud/plans/apply', json={'plan_id': pid}).status_code == 400
    applied = client.post('/api/cloud/plans/apply', json={'plan_id': pid, 'confirmed': True})
    assert applied.status_code == 202
    assert client.post('/api/cloud/plans/apply', json={'plan_id': pid, 'confirmed': True}).status_code == 409
    jobs.execute(applied.json()['job_id'])
    assert store.job(applied.json()['job_id'])['state'] == 'success'
    assert any(w[0] == 'move' for w in cloud.remote.writes)
    assert list(cloud.root.rglob('*.strm'))
    assert client.get('/api/cloud/plans/' + pid).status_code == 409
    assert not detail['steps']  # No write steps or ingestion notices for a preview.


def test_duplicate_active_scans_reuse_one_job_and_different_options_conflict(cloud, client):
    jid = start(client, cloud)
    assert start(client, cloud) == jid
    with pytest.raises(CloudError, match='不同选项'):
        plan_preview.start(cloud.id, '1', '3')
    with store.db() as con:
        assert con.execute("SELECT count(*) FROM cloud_jobs WHERE kind='organize_preview'").fetchone()[0] == 1
    assert not cloud.remote.writes


@pytest.mark.parametrize('during', [False, True])
def test_stop_does_not_publish_a_plan_or_mutate_files(cloud, client, monkeypatch, during):
    jid = start(client, cloud)
    if during:
        original = cloud.remote.list
        def listing(cid):
            jobs.request_stop(jid)
            return original(cid)
        monkeypatch.setattr(cloud.remote, 'list', listing)
    else:
        assert client.post('/api/cloud/jobs/' + jid + '/stop', json={'confirmed': True}).status_code == 200
    jobs.execute(jid)
    assert store.job(jid)['state'] == 'stopped'
    with store.db() as con:
        assert con.execute('SELECT count(*) FROM cloud_plans').fetchone()[0] == 0
    assert not cloud.remote.writes


def test_changed_authorization_cannot_publish_or_reopen_an_old_plan(cloud, client):
    jid = start(client, cloud)
    jobs.execute(jid)
    pid = store.job(jid)['result']['plan_id']
    pending = start(client, cloud)
    accounts.save_verified('UID=123_A1_1; CID=new; SEID=new', cloud.remote)
    jobs.execute(pending)
    assert store.job(pending)['state'] == 'failed'
    assert client.get('/api/cloud/plans/' + pid).status_code == 409
    assert not cloud.remote.writes


def test_authorization_changed_during_recognition_discards_the_preview(cloud, client, monkeypatch):
    original = workflow.recognize_source
    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        accounts.save_verified('UID=123_A1_1; CID=changed; SEID=changed', cloud.remote)
        return result
    monkeypatch.setattr(workflow, 'recognize_source', changed)
    jid = start(client, cloud)
    jobs.execute(jid)
    assert store.job(jid)['state'] == 'failed'
    with store.db() as con:
        assert con.execute('SELECT count(*) FROM cloud_plans').fetchone()[0] == 0
    assert not cloud.remote.writes


def test_scan_failure_and_expired_plan_have_explicit_receipts(cloud, client, monkeypatch):
    jid = start(client, cloud)
    jobs.execute(jid)
    pid = store.job(jid)['result']['plan_id']
    with store.db() as con:
        con.execute('UPDATE cloud_plans SET expires=0 WHERE id=?', (pid,))
    assert client.get('/api/cloud/plans/' + pid).status_code == 409
    failed = start(client, cloud)
    monkeypatch.setattr(cloud.remote, 'list', Mock(side_effect=CloudError('目录读取失败', 502)))
    jobs.execute(failed)
    result = store.job(failed)
    assert result['state'] == 'failed' and not result['result'].get('plan_id')
    assert not cloud.remote.writes


def test_scan_and_result_require_authenticated_same_origin_session(cloud, client):
    client.headers['Origin'] = 'https://evil.example'
    response = client.post('/api/cloud/plans/scan', json={
        'account_id': cloud.id, 'source_cid': '1', 'target_cid': '2'})
    assert response.status_code == 403
    client.cookies.clear()
    assert client.get('/api/cloud/plans/' + 'a' * 32).status_code == 401
