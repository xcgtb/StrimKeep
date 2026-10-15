"""Direct destination receipts must never claim the destination's other files."""

import copy
import json
import pytest
from app.cloud import jobs, store, workflow
from app.cloud.errors import CloudError
from dev.tests.test_cloud_workbench import cloud  # noqa: F401


def submit(cloud, organize=False):
    preview = workflow.preview_share(cloud.id, "abcdefghi")
    return workflow.transfer_submit(preview["preview_id"], ["999"], "3", True, "2" if organize else None)


def test_direct_receive_has_no_wrapper_and_ignores_other_arrivals(cloud, monkeypatch):
    remote = cloud.remote
    remote.files["80"] = {**remote.info("4"), "id": "80", "parent_id": "3", "name": "Old.2024.mkv"}
    receive = remote.receive
    def arrive(*args):
        receive(*args)
        remote.files["81"] = {**remote.info("4"), "id": "81", "parent_id": "3", "name": "Other.2024.mkv"}
    monkeypatch.setattr(remote, "receive", arrive)
    jid = submit(cloud)
    jobs.execute(jid)
    job = store.job(jid)
    assert job["state"] == "success", job
    assert len(job["result"]["files"]) == 1
    assert job["result"]["files"][0]["parent_id"] == "3"
    assert remote.writes == [("receive", "21", "3")]
    assert {r["id"] for r in remote.list("3")} == {"21", "80", "81"}


def test_nested_receipt_organizes_only_new_video_and_subtitle(cloud, monkeypatch):
    remote = cloud.remote
    remote.files["80"] = {**remote.info("4"), "id": "80", "parent_id": "3", "name": "Old.2024.mkv"}
    old = copy.deepcopy(remote.files["80"])
    original_page = remote.share_page
    def page(*args):
        response = original_page(*args)
        response["items"] = [{**response["items"][0], "name": "分享作品", "is_dir": True, "size": 0, "sha1": ""}]
        return response
    def receive(code, password, ids, target):
        folder = remote.mkdir(target, "分享作品")
        nested = remote.mkdir(folder, "视频")
        remote.files["90"] = {**remote.info("4"), "id": "90", "parent_id": nested}
        remote.files["91"] = {**remote.info("5"), "id": "91", "parent_id": nested}
        remote.writes.append(("receive", folder, target))
    monkeypatch.setattr(remote, "share_page", page)
    monkeypatch.setattr(remote, "receive", receive)
    jid = submit(cloud, True)
    jobs.execute(jid)
    job = store.job(jid)
    assert job["state"] == "success", job
    assert {f["id"] for f in job["result"]["files"]} == {"90", "91"}
    assert remote.files["80"] == old
    assert remote.info("90")["parent_id"] == remote.info("91")["parent_id"]
    assert remote.info("90")["parent_id"] not in ("3", "22")
    assert len(list(cloud.root.rglob("*.strm"))) == 1
    assert not any(r["name"].startswith("转存待整理-") for r in remote.files.values())


def test_existing_same_name_blocks_receive_without_merging_or_writes(cloud):
    remote = cloud.remote
    remote.files["80"] = {**remote.info("4"), "id": "80", "parent_id": "3"}
    jid = submit(cloud)
    jobs.execute(jid)
    assert store.job(jid)["state"] == "failed"
    assert "同名" in store.job(jid)["message"]
    assert not remote.writes


def test_timeout_reconciliation_and_continuation_exclude_old_and_later_files(cloud):
    remote = cloud.remote
    remote.files["80"] = {**remote.info("4"), "id": "80", "parent_id": "3", "name": "Old.2024.mkv"}
    remote.receive_uncertain = True
    jid = submit(cloud, True)
    jobs.execute(jid)
    remote.files["81"] = {**remote.info("4"), "id": "81", "parent_id": "3", "name": "Later.2024.mkv"}
    receipt = jobs.verify_transfer(jid)
    assert len(receipt["files"]) == 1
    next_job = jobs.continue_transfer(jid, True)
    jobs.execute(next_job["job_id"])
    assert store.job(next_job["job_id"])["state"] == "success"
    assert remote.info("80")["parent_id"] == remote.info("81")["parent_id"] == "3"
    assert len([w for w in remote.writes if w[0] == "receive"]) == 1
    assert not any(r["name"].startswith("转存待整理-") for r in remote.files.values())


def test_ambiguous_new_receipts_stay_uncertain_and_never_replay(cloud, monkeypatch):
    remote = cloud.remote
    receive = remote.receive
    def ambiguous(*args):
        receive(*args)
        remote.files["80"] = {**remote.info("21"), "id": "80"}
    monkeypatch.setattr(remote, "receive", ambiguous)
    jid = submit(cloud)
    jobs.execute(jid)
    assert store.job(jid)["state"] == "uncertain"
    with pytest.raises(CloudError):
        jobs.verify_transfer(jid)
    jobs.execute(jid)
    assert len([w for w in remote.writes if w[0] == "receive"]) == 1


def test_legacy_staged_timeout_remains_verifiable_after_upgrade(cloud):
    remote = cloud.remote
    jid = submit(cloud, True)
    stage = remote.mkdir("3", "转存待整理-" + jid[:12])
    remote.receive("abcdefghi", "", ["999"], stage)
    intent = {"stage_id": stage, "source_ids": ["999"]}
    with store.db() as con:
        con.execute("UPDATE cloud_jobs SET state='uncertain' WHERE id=?", (jid,))
        con.execute("INSERT INTO cloud_steps VALUES(?,?,?,?,?,?)", (jid, "receive", "dispatched", json.dumps(intent), "{}", 0))
    receipt = jobs.verify_transfer(jid)
    assert receipt["stage_id"] == stage and len(receipt["files"]) == 1
    next_job = jobs.continue_transfer(jid, True)
    jobs.execute(next_job["job_id"])
    assert store.job(next_job["job_id"])["state"] == "success"


def test_virtual_root_does_not_require_fs_info_zero(cloud, monkeypatch):
    info = cloud.remote.info
    def no_root(fid):
        assert fid != "0", "virtual root has no fs_info endpoint"
        return info(fid)
    monkeypatch.setattr(cloud.remote, "info", no_root)
    preview = workflow.preview_share(cloud.id, "abcdefghi")
    jid = workflow.transfer_submit(preview["preview_id"], ["999"], "0", True)
    jobs.execute(jid)
    assert store.job(jid)["state"] == "success"
    assert cloud.remote.writes == [("receive", "21", "0")]


def test_changed_destination_cannot_be_reconciled_as_original_target(cloud):
    cloud.remote.receive_uncertain = True
    jid = submit(cloud)
    jobs.execute(jid)
    cloud.remote.files["3"]["parent_id"] = "1"
    with pytest.raises(CloudError, match="身份已变化"):
        jobs.verify_transfer(jid)
    assert store.job(jid)["state"] == "uncertain"


def test_invisible_receipt_does_not_claim_existing_contents_or_repeat_request(cloud, monkeypatch):
    cloud.remote.files["80"] = {**cloud.remote.info("4"), "id": "80", "parent_id": "3", "name": "Old.mkv"}
    def invisible(code, password, ids, target):
        cloud.remote.writes.append(("receive-invisible", target))
    monkeypatch.setattr(cloud.remote, "receive", invisible)
    jid = submit(cloud, True)
    jobs.execute(jid)
    assert store.job(jid)["state"] == "uncertain"
    with pytest.raises(CloudError):
        jobs.verify_transfer(jid)
    jobs.execute(jid)
    assert cloud.remote.writes == [("receive-invisible", "3")]


def test_read_failure_after_receive_remains_reconcilable_without_resend(cloud, monkeypatch):
    listing = cloud.remote.list
    def fail_after_write(parent):
        if cloud.remote.writes and parent == "3":
            raise CloudError("目录读取失败", 502)
        return listing(parent)
    monkeypatch.setattr(cloud.remote, "list", fail_after_write)
    jid = submit(cloud)
    jobs.execute(jid)
    assert store.job(jid)["state"] == "uncertain"
    monkeypatch.setattr(cloud.remote, "list", listing)
    result = jobs.verify_transfer(jid)
    assert result["files"][0]["id"] == "21"
    assert cloud.remote.writes == [("receive", "21", "3")]
