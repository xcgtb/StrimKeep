"""Eventually visible receipts must not resend receive or bypass identity checks."""

import copy
import pytest
from app.cloud import jobs, store, workflow, recognition
from app.cloud.errors import CloudError
from dev.tests.test_cloud_workbench import cloud  # noqa: F401
from dev.tests.test_my_drive_organize import Catalog

REAL_PAUSE = workflow._transfer_receipt_pause


def episodes(cloud, monkeypatch, generate=False):
    remote = cloud.remote
    selected = [
        {**remote.info("4"), "id": str(900 + n),
         "name": f"绝命毒师 (2008) - S05E{n:02d} - 4K.WEB-DL.mkv"}
        for n in range(1, 4)
    ]
    monkeypatch.setattr(remote, "share_page", lambda *a: {
        "items": copy.deepcopy(selected), "total": len(selected), "has_more": False, "offset": 0,
    })
    def receive(code, password, ids, target):
        for n, source in enumerate(selected, 1):
            remote.files[str(20 + n)] = {**source, "id": str(20 + n), "parent_id": target}
        remote.counter = 20 + len(selected)
        remote.writes.append(("receive", tuple(ids), target))
    monkeypatch.setattr(remote, "receive", receive)
    preview = workflow.preview_share(cloud.id, "abcdefghi")
    return workflow.transfer_submit(preview["preview_id"], [r["id"] for r in selected], "3", True, "2", generate)


@pytest.fixture(autouse=True)
def no_real_wait(cloud, monkeypatch):
    pauses = []
    monkeypatch.setattr(workflow, "_transfer_receipt_pause", lambda job_id, delay, error: pauses.append(delay))
    return pauses


def test_episode_batch_waits_for_all_files_before_organizing(cloud, monkeypatch, no_real_wait):
    jid = episodes(cloud, monkeypatch)
    listing, reads, planned = cloud.remote.list, [], []
    def delayed(parent):
        rows = listing(parent)
        if parent == "3" and cloud.remote.writes:
            reads.append(parent)
            return rows[:min(len(reads) - 1, 3)]
        return rows
    monkeypatch.setattr(cloud.remote, "list", delayed)
    # Verify the hand-off to the existing organizer without involving TMDB/network.
    def plan(*args, **kwargs):
        planned.append(kwargs["source_ids"])
        return {"id": "verified-plan"}
    monkeypatch.setattr(workflow, "plan", plan)
    monkeypatch.setattr(workflow, "organize", lambda *a: {"errors": [], "skipped": []})
    jobs.execute(jid)
    job = store.job(jid)
    assert job["state"] == "success", job
    assert planned == [["21", "22", "23"]]
    assert len(job["result"]["files"]) == 3
    assert len(no_real_wait) == 3
    assert len(cloud.remote.writes) == 1


def test_invisible_batch_is_bounded_and_can_be_reconciled_without_resend(cloud, monkeypatch, no_real_wait):
    jid = episodes(cloud, monkeypatch)
    listing = cloud.remote.list
    monkeypatch.setattr(cloud.remote, "list", lambda parent: [] if parent == "3" else listing(parent))
    jobs.execute(jid)
    assert store.job(jid)["state"] == "uncertain"
    assert "0/3" in store.job(jid)["message"]
    assert len(no_real_wait) == len(workflow.TRANSFER_RECEIPT_DELAYS)
    assert sum(no_real_wait) == 60
    jobs.execute(jid)
    assert len(cloud.remote.writes) == 1
    monkeypatch.setattr(cloud.remote, "list", listing)
    receipt = jobs.verify_transfer(jid)
    assert len(receipt["files"]) == 3 and receipt["continuation_required"]
    assert len(cloud.remote.writes) == 1


def test_partial_batch_with_wrong_fingerprint_stops_without_wait_or_organize(cloud, monkeypatch, no_real_wait):
    jid = episodes(cloud, monkeypatch)
    listing = cloud.remote.list
    def wrong(parent):
        rows = listing(parent)
        return [{**rows[0], "sha1": "B" * 40}] if parent == "3" and rows else rows
    monkeypatch.setattr(cloud.remote, "list", wrong)
    jobs.execute(jid)
    assert store.job(jid)["state"] == "uncertain"
    assert not no_real_wait
    assert len(cloud.remote.writes) == 1


def test_stop_during_visibility_wait_preserves_unverified_receipt(cloud, monkeypatch):
    jid = episodes(cloud, monkeypatch)
    listing = cloud.remote.list
    monkeypatch.setattr(cloud.remote, "list", lambda parent: [] if parent == "3" else listing(parent))
    def stop(job_id, delay, error):
        jobs.request_stop(job_id)
        raise error
    monkeypatch.setattr(workflow, "_transfer_receipt_pause", stop)
    jobs.execute(jid)
    assert store.job(jid)["state"] == "uncertain"
    assert len(cloud.remote.writes) == 1
    monkeypatch.setattr(cloud.remote, "list", listing)
    assert len(jobs.verify_transfer(jid)["files"]) == 3


def test_complete_listing_is_not_read_again_before_saving_receipt(cloud, monkeypatch, no_real_wait):
    preview = workflow.preview_share(cloud.id, "abcdefghi")
    jid = workflow.transfer_submit(preview["preview_id"], ["999"], "3", True)
    listing, reads = cloud.remote.list, []
    def one_snapshot(parent):
        if parent == "3" and cloud.remote.writes:
            reads.append(parent)
            if len(reads) > 1:
                raise CloudError("第二次目录读取失败", 502)
        return listing(parent)
    monkeypatch.setattr(cloud.remote, "list", one_snapshot)
    jobs.execute(jid)
    assert store.job(jid)["state"] == "success"
    assert reads == ["3"] and not no_real_wait


def test_delayed_season_really_renames_moves_and_generates_all_strm(cloud, monkeypatch):
    class SeriesCatalog(Catalog):
        def get(self, path, **params):
            data = super().get(path, **params)
            for item in data.get("results", [data]):
                if "first_air_date" in item:
                    item.update(name="绝命毒师", first_air_date="2008-01-20", release_date="2008-01-20")
            return data
    monkeypatch.setattr(recognition, "tmdb", lambda: SeriesCatalog())
    jid = episodes(cloud, monkeypatch, generate=True)
    cloud.remote.files["80"] = {**cloud.remote.info("4"), "id": "80", "parent_id": "3", "name": "Existing.mkv"}
    old = copy.deepcopy(cloud.remote.files["80"])
    listing, reads = cloud.remote.list, []
    def delayed(parent):
        rows = listing(parent)
        if parent == "3" and any(w[0] == "receive" for w in cloud.remote.writes):
            reads.append(parent)
            # One episode at a time becomes visible; unrelated content stays put.
            return [r for r in rows if r["id"] == "80"] + [r for r in rows if r["id"] != "80"][:len(reads)]
        return rows
    monkeypatch.setattr(cloud.remote, "list", delayed)
    jobs.execute(jid)
    job = store.job(jid)
    assert job["state"] == "success", job
    assert len(job["result"]["organization"]["generated"]) == 3
    files = list(cloud.root.rglob("*.strm"))
    assert len(files) == 3 and all("Season 05" in str(p) for p in files)
    for n in range(1, 4):
        row = cloud.remote.info(str(20 + n))
        assert row["parent_id"] != "3" and f"S05E{n:02d}" in row["name"]
    assert cloud.remote.info("80") == old
    assert len([w for w in cloud.remote.writes if w[0] == "receive"]) == 1


def test_real_pause_observes_stop_after_one_poll_without_marking_verified(cloud, monkeypatch):
    jid = episodes(cloud, monkeypatch)
    listing, slept = cloud.remote.list, []
    monkeypatch.setattr(cloud.remote, "list", lambda parent: [] if parent == "3" else listing(parent))
    monkeypatch.setattr(workflow, "_transfer_receipt_pause", REAL_PAUSE)
    def stop_after_poll(seconds):
        slept.append(seconds)
        jobs.request_stop(jid)
    monkeypatch.setattr(workflow.time, "sleep", stop_after_poll)
    jobs.execute(jid)
    job = jobs.details(jid)
    assert job["state"] == "uncertain" and len(slept) == 1 and slept[0] <= 1
    assert job["steps"][0]["state"] == "dispatched"
    assert len(cloud.remote.writes) == 1
