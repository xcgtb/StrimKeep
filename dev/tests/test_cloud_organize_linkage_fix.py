"""Regression checks for 115 transfer -> organize wiring (isolated fake 115, no cloud writes)."""
import json
import pytest
from dev.tests.test_cloud_receiving import receiving, msg
from dev.tests.test_cloud_workbench import cloud
from dev.tests.test_my_drive import client
from app.cloud import receiving as cloud_receiving, jobs, store, intake_automation
from app.cloud.errors import CloudError


@pytest.mark.parametrize('enabled,expected_target', [(True, '2'), (False, None)])
def test_tg_private_share_uses_saved_organize_option(receiving, enabled, expected_target):
    cloud_receiving.save(receiving.id, {'tg_enabled': True, 'organize_after_transfer': enabled}, 0)
    ids = cloud_receiving.capture(msg('https://115.com/s/abcdef123'), authorized=True)
    assert len(ids) == 1
    cloud_receiving.schedule()
    parent = cloud_receiving.inbox(receiving.id)[0]['job_id']
    jobs.execute(parent)
    assert store.job(parent)['state'] == 'success'
    child = store.job(parent)['result']['child_jobs'][0]
    with store.db() as con:
        payload = json.loads(con.execute('SELECT payload FROM cloud_jobs WHERE id=?', (child,)).fetchone()[0])
    assert payload['organize_target'] == expected_target
    # Do not execute the cloud relocation in this regression; only verify intent routing.


def test_web_preview_and_direct_request_keep_selected_mode(receiving):
    link = 'https://115.com/s/abcdef123'
    preview = cloud_receiving.preview(receiving.id, link, organize=True)
    assert preview['organize_after_transfer'] is True
    parent = cloud_receiving.apply(preview['id'], True)
    jobs.execute(parent)
    child = store.job(parent)['result']['child_jobs'][0]
    with store.db() as con:
        payload = json.loads(con.execute('SELECT payload FROM cloud_jobs WHERE id=?', (child,)).fetchone()[0])
    assert payload['organize_target'] == '2'
    direct = cloud_receiving.receive(receiving.id, link, 1, 'd' * 32, True, True)
    assert direct == cloud_receiving.receive(receiving.id, link, 1, 'd' * 32, True, True)
    with pytest.raises(CloudError):
        cloud_receiving.receive(receiving.id, link, 1, 'd' * 32, True, False)
    with store.db() as con:
        direct_payload = json.loads(con.execute('SELECT payload FROM cloud_jobs WHERE id=?', (direct,)).fetchone()[0])
    assert direct_payload['organize_target'] == '2'


def test_run_saved_transfer_directory_independent_of_scan_root(receiving):
    job = intake_automation.run_now(receiving.id, 1, '3')
    assert intake_automation.run_now(receiving.id, 1, '3') == job
    with store.db() as con:
        payload = json.loads(con.execute('SELECT payload FROM cloud_jobs WHERE id=?', (job,)).fetchone()[0])
    assert payload['source_cid'] == '3' and payload['target_cid'] == '2'
    with pytest.raises(CloudError):
        intake_automation.run_now(receiving.id, 1, '2')


def test_http_api_accepts_organize_and_selected_source(receiving, client):
    r = client.post('/api/cloud/accounts/'+receiving.id+'/organizer/run',
                    json={'expected_revision': 1, 'source_cid': '3'})
    assert r.status_code == 202, r.text
    r = client.post('/api/cloud/resources/receive',
                    json={'account_id': receiving.id, 'text': 'https://115.com/s/abcdef123',
                          'expected_revision': 1, 'request_id': 'e'*32,
                          'organize': True, 'confirmed': True})
    assert r.status_code == 202, r.text


def test_full_direct_share_to_move_and_strm(receiving, monkeypatch):
    """One end-to-end queue path with stateful fake 115, real SQLite and NAS files."""
    from app.cloud import strm, workflow
    monkeypatch.setattr(workflow, "_operation_pause", lambda: None)
    jid = cloud_receiving.receive(
        receiving.id, "https://115.com/s/abcdef123", 1, "f" * 32,
        True, True,
    )
    jobs.execute(jid)
    parent = store.job(jid)
    assert parent["state"] == "success", parent
    child_id = parent["result"]["child_jobs"][0]
    jobs.execute(child_id)
    child = store.job(child_id)
    assert child["state"] == "success", child
    organization = child["result"].get("organization")
    assert organization and organization["files"] and organization["generated"], child
    transferred = child["result"]["files"][0]["id"]
    final_file = receiving.remote.info(transferred)
    assert final_file["parent_id"] != "3", final_file
    assert any(x[0] == "rename" and x[1] == transferred for x in receiving.remote.writes)
    assert any(x[0] == "move" and x[1] == transferred for x in receiving.remote.writes)
    assert len(strm.list_assets()) == 1
    assert len([x for x in receiving.remote.writes if x[0] == "receive"]) == 1
