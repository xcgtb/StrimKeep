"""Real SQLite/STRM plus stateful remote: ID deletion and no ambiguous fallback."""

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from dev.tests.test_cloud_workbench import cloud
from app import engine, wash, plan_guard, cloud_bridge, governance as legacy_governance
from app.cloud import (
    governance,
    provider115,
    open115,
    settings,
    store,
    strm,
    recognition,
    jobs,
    workflow,
)
from app.cloud.errors import CloudError


@pytest.fixture
def bound(cloud, monkeypatch):
    row = cloud.remote.info("4")
    asset = strm.generate(
        cloud.id,
        row,
        recognition.recognize(row["name"]),
        companions=[cloud.remote.info("5")],
        provider=cloud.remote,
    )
    from app.cloud import accounts

    accounts.save_authorization(
        "123",
        {},
        {
            "open": open115.tokens(
                {
                    "access_token": "test-access",
                    "refresh_token": "test-refresh",
                    "expires_in": 7200,
                },
                "123456",
            )
        },
    )
    settings.save({"official_delete_enabled": True})
    deleted = {}
    proof_fail = set()

    def delete(fid):
        deleted[fid] = cloud.remote.files.pop(fid)
        cloud.remote.writes.append(("delete", fid))

    def proof(fid, source, acknowledged=False):
        if fid in proof_fail:
            raise CloudError("状态暂不可核实", 502)
        if fid in deleted and fid not in cloud.remote.files:
            return {
                "file_id": fid,
                "acknowledged": acknowledged,
                "parent_complete_absent": True,
            }
        return None

    provider = SimpleNamespace(
        profile=cloud.remote.profile,
        require=lambda op: None,
        info=cloud.remote.info,
        delete=delete,
        removed_receipt=proof,
    )
    monkeypatch.setattr(provider115, "for_account", lambda aid: provider)
    monkeypatch.setattr(engine, "L_ROOT", cloud.root)
    monkeypatch.setattr(
        engine, "cloud_videos", Mock(side_effect=AssertionError("no path fallback"))
    )
    return SimpleNamespace(
        asset=asset,
        path=Path(asset["local_path"]),
        cloud=cloud,
        provider=provider,
        deleted=deleted,
        proof_fail=proof_fail,
    )


def clean(bound, local=True, dry=False):
    # The API path does not depend on a present CD2 mount.
    return wash.safe_delete_files(
        [bound.path],
        bound.cloud.root,
        bound.cloud.root / "unmounted" if local else None,
        dry,
    )


def test_registered_video_and_explicit_companions_delete_by_id_without_mount(bound):
    remote = bound.cloud.remote
    remote.files["6"] = {
        **remote.info("4"),
        "id": "6",
        "name": "Movie.2024.1080p.copy.mkv",
    }
    result = clean(bound)
    assert result["strm_removed"] == 1 and result["cloud_removed"] == 1
    assert result["cloud_sidecars_removed"] == 1 and not result["errors"]
    assert (
        set(bound.deleted) == {"4", "5"}
        and "6" in remote.files
        and not bound.path.exists()
    )
    assert store.asset(bound.asset["id"])["state"] == "revoked"


def test_share_bound_reference_only_deletes_nas_and_revokes_playback(
    bound, monkeypatch
):
    monkeypatch.setattr(
        open115, "for_account", Mock(side_effect=AssertionError("no Open call"))
    )
    result = clean(bound, False)
    assert result["strm_removed"] == 1 and result["cloud_removed"] == 0
    assert set(bound.cloud.remote.files) >= {"4", "5"} and not bound.deleted


@pytest.mark.parametrize(
    "change",
    [
        "edited_strm",
        "wrong_uid",
        "sha",
        "companion",
        "directory",
        "disabled",
        "no_cookie",
    ],
)
def test_identity_or_permission_change_keeps_nas_and_never_guesses_source(
    bound, change, monkeypatch
):
    if change == "edited_strm":
        bound.path.write_text("http://external/video.mkv")
    elif change == "wrong_uid":
        with store.db() as con:
            con.execute(
                "UPDATE cloud_accounts SET uid='999' WHERE id=?", (bound.cloud.id,)
            )
    elif change == "sha":
        bound.cloud.remote.files["4"]["sha1"] = "B" * 40
    elif change == "companion":
        bound.cloud.remote.files["5"]["size"] = 101
    elif change == "directory":
        bound.cloud.remote.files["4"]["is_dir"] = True
    elif change == "disabled":
        settings.save({"official_delete_enabled": False})
    else:
        monkeypatch.setattr(
            provider115, "for_account", Mock(side_effect=CloudError("需要 Cookie", 409))
        )
    result = clean(bound)
    assert result["strm_removed"] == 0 and result["errors"] and bound.path.exists()
    assert not bound.deleted
    engine.cloud_videos.assert_not_called()


def test_all_group_members_are_preflighted_before_any_remote_mutation(bound):
    bound.cloud.remote.files["5"]["name"] = "renamed.srt"
    assert clean(bound)["errors"] and not bound.deleted and bound.path.exists()


def test_acknowledged_delete_then_unreadable_receipt_keeps_strm_and_retry_does_not_resend(
    bound,
):
    bound.proof_fail.add("4")
    assert (
        clean(bound)["errors"] and bound.path.exists() and set(bound.deleted) == {"4"}
    )
    assert governance.record(bound.cloud.id, "4")["state"] == "acknowledged"
    assert clean(bound)["errors"] and len(bound.cloud.remote.writes) == 1
    bound.proof_fail.clear()
    governance.verify(bound.asset["id"])
    result = clean(bound)
    assert result["strm_removed"] == 1 and result["cloud_removed"] == 0
    assert [r for r in bound.cloud.remote.writes if r == ("delete", "4")] == [
        ("delete", "4")
    ]


def test_uncertain_but_proven_write_can_finish_without_second_request(bound):
    original = bound.provider.delete

    def timeout(fid):
        original(fid)
        raise CloudError("超时", 502, uncertain=True)

    bound.provider.delete = timeout
    assert clean(bound)["strm_removed"] == 1
    assert len(bound.cloud.remote.writes) == 2


def test_uncertain_without_exact_proof_keeps_nas_and_never_retries(bound):
    bound.provider.delete = Mock(
        side_effect=CloudError("网络结果未知", 502, uncertain=True)
    )
    assert clean(bound)["errors"] and bound.path.exists()
    assert clean(bound)["errors"] and bound.path.exists()
    bound.provider.delete.assert_called_once_with("4")
    with pytest.raises(CloudError, match="仅明确拒绝"):
        governance.reset_rejected(bound.asset["id"])


def test_explicit_rejection_can_only_reset_after_identity_is_proven(bound):
    original = bound.provider.delete
    bound.provider.delete = Mock(side_effect=CloudError("拒绝", 502))
    assert clean(bound)["errors"] and bound.path.exists()
    assert clean(bound)["errors"]
    bound.provider.delete.assert_called_once()
    assert governance.reset_rejected(bound.asset["id"])["reset"] == ["4"]
    bound.provider.delete = original
    assert clean(bound)["strm_removed"] == 1


def test_an_unselected_reference_protects_same_physical_source(bound):
    asset = bound.asset
    with store.db() as con:
        con.execute(
            "INSERT INTO cloud_assets VALUES(?,?,?,?,?,?,?,?)",
            (
                store.ident(),
                bound.cloud.id,
                "4",
                str(bound.cloud.root / "other.strm"),
                store.encode(asset["payload"]),
                asset["nonce"],
                "active",
                1,
            ),
        )
    assert clean(bound)["errors"] and not bound.deleted


def test_dry_run_never_dispatches_or_records_deletion(bound):
    assert clean(bound, dry=True)["strm_removed"] == 1
    assert (
        governance.record(bound.cloud.id, "4") is None
        and bound.path.exists()
        and not bound.deleted
    )


def test_plan_guard_rejects_missing_and_changed_source_snapshot(bound):
    path = str(bound.path)
    snapshots = {path: plan_guard.fingerprint(path, (bound.cloud.root,))}
    sources = cloud_bridge.snapshots([path], bound.cloud.root, True)
    action = {
        "kind": "loc",
        "files": [path],
        "file_guard": {
            "version": plan_guard.VERSION,
            "targets": [path],
            "retained": [],
            "cloud_sources": sources,
        },
    }
    assert not plan_guard.validate(action, snapshots, (bound.cloud.root,))
    old = copy.deepcopy(action)
    del old["file_guard"]["cloud_sources"]
    assert plan_guard.validate(old, snapshots, (bound.cloud.root,))
    settings.save({"official_delete_enabled": False})
    assert plan_guard.validate(action, snapshots, (bound.cloud.root,))


def test_single_title_confirmations_are_bounded_single_use_and_source_sensitive(bound):
    ready = cloud_bridge.capture([bound.path], bound.cloud.root, True)
    assert ready["cloud_sources"][0]["file_id"] == "4"
    cloud_bridge.confirm(
        ready["source_preview_id"], [bound.path], bound.cloud.root, True
    )
    with pytest.raises(ValueError):
        cloud_bridge.confirm(
            ready["source_preview_id"], [bound.path], bound.cloud.root, True
        )
    next_preview = cloud_bridge.capture([bound.path], bound.cloud.root, True)
    bound.path.write_text("altered")
    with pytest.raises(ValueError):
        cloud_bridge.confirm(
            next_preview["source_preview_id"], [bound.path], bound.cloud.root, True
        )


def test_legacy_unbound_reference_uses_original_mount_chain(cloud, monkeypatch):
    path = cloud.root / "legacy.strm"
    path.write_text("http://old-upstream/file")
    video = cloud.root / "original.mkv"
    video.write_bytes(b"video")
    monkeypatch.setattr(engine, "cloud_videos", lambda *args: ([video], "exact"))
    result = wash.safe_delete_files([path], cloud.root, cloud.root)
    assert result["strm_removed"] == 1 and result["cloud_removed"] == 1
    assert not video.exists() and not path.exists()


@pytest.mark.parametrize(
    "response",
    [
        {"state": False, "code": 40140125},
        {"state": False, "code": 10014},
        {"state": False, "code": 123456},
        {"state": True, "data": {}},
    ],
)
def test_only_explicit_file_absence_receipts_can_prove_delete(response):
    provider = object.__new__(open115.OpenProvider)
    provider.client = SimpleNamespace(fs_info=lambda *a, **k: response)
    provider.list = Mock(return_value=[])
    source = {"parent_id": "1"}
    if response.get("state") is True:
        assert provider.removed_receipt("4", source, True) is None
    else:
        with pytest.raises(CloudError):
            provider.removed_receipt("4", source, True)
    provider.list.assert_not_called()


def test_removed_receipt_needs_exact_error_and_complete_parent_list():
    provider = object.__new__(open115.OpenProvider)
    provider.client = SimpleNamespace(
        fs_info=lambda *a, **k: {"state": False, "code": 20018}
    )
    provider.list = Mock(return_value=[])
    result = provider.removed_receipt("4", {"parent_id": "1"}, True)
    assert result["not_found_code"] == 20018 and result["acknowledged"]
    provider.list.return_value = [{"id": "4"}]
    assert provider.removed_receipt("4", {"parent_id": "1"}, True) is None


def test_transfer_continuation_queues_organize_once_without_resending_receive(cloud):
    cloud.remote.receive_uncertain = True
    preview = workflow.preview_share(cloud.id, "https://115.com/s/abcdefghi")
    jid = workflow.transfer_submit(preview["preview_id"], ["999"], "3", True, "2", True)
    jobs.execute(jid)
    assert store.job(jid)["state"] == "uncertain"
    jobs.verify_transfer(jid)
    next_job = jobs.continue_transfer(jid, True)
    assert jobs.continue_transfer(jid, True) == next_job
    cloud.remote.receive_uncertain = False
    jobs.execute(next_job["job_id"])
    assert store.job(next_job["job_id"])["state"] == "success"
    assert len([x for x in cloud.remote.writes if x[0] == "receive"]) == 1
    progress = store.job(next_job["job_id"])["progress"]
    assert progress["total"] == progress["processed"] == 1


def test_sidecar_with_two_possible_video_owners_is_not_automatically_bound(cloud):
    rows = [cloud.remote.info("4"), cloud.remote.info("5")]
    rows.append({**rows[0], "id": "6", "name": "Movie.2024.1080p.mp4"})
    assert workflow.related_companions(rows, rows[0]) == []


def test_legacy_cli_imports_reuse_engine_and_storage_singletons(bound):
    import os
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[2]
    script = """
import sys,json
from pathlib import Path
sys.path.insert(0,str(Path.cwd()/'app'))
import engine,storage,cloud_bridge
value=cloud_bridge.preview(sys.argv[1],sys.argv[2],True)
assert sys.modules['app.engine'] is engine
assert sys.modules['app.storage'] is storage
assert value['file_id']=='4' and value['uid']=='123'
print('CLI identity verified')
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(bound.path), str(bound.cloud.root)],
        cwd=root,
        env={
            **os.environ,
            "AGENT_DATA": str(bound.cloud.state.parent),
            "L_ROOT": str(bound.cloud.root),
        },
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr + result.stdout


def test_governance_plan_captures_source_identity_without_changing_quality_rules(
    bound, monkeypatch
):
    monkeypatch.setattr(engine, "S_ROOT", bound.cloud.root / "share")
    action = SimpleNamespace(files=[bound.path], guard_paths=[], kind="loc", meta={})
    snapshots, guards = legacy_governance._execution_snapshots([action])
    assert guards[0]["cloud_sources"][str(bound.path)]["file_id"] == "4"
    assert action.meta["cloud_sources"][0]["media_identity"]["tmdb_id"] == "44"
    assert snapshots[str(bound.path)]["sha256"]


@pytest.mark.parametrize("changed", ["disconnected", "altered", "copied"])
def test_share_cleanup_does_not_depend_on_remote_account_or_binding_health(
    bound, monkeypatch, changed
):
    monkeypatch.setattr(
        open115, "for_account", Mock(side_effect=AssertionError("no remote"))
    )
    if changed == "disconnected":
        with store.db() as con:
            con.execute("DELETE FROM cloud_accounts WHERE id=?", (bound.cloud.id,))
            con.execute(
                "UPDATE cloud_assets SET state='disconnected' WHERE id=?",
                (bound.asset["id"],),
            )
    elif changed == "altered":
        bound.path.write_text("manually edited")
    else:
        copy_path = bound.path.with_name("copy.strm")
        copy_path.write_bytes(bound.path.read_bytes())
        bound.path = copy_path
    preview = cloud_bridge.capture([bound.path], bound.cloud.root, False)
    cloud_bridge.confirm(
        preview["source_preview_id"], [bound.path], bound.cloud.root, False
    )
    assert clean(bound, False)["strm_removed"] == 1 and not bound.deleted


def test_partial_group_explicit_rejection_can_resume_only_rejected_member(bound):
    original = bound.provider.delete

    def reject_sidecar(fid):
        if fid == "5":
            raise CloudError("权限拒绝", 502)
        return original(fid)

    bound.provider.delete = reject_sidecar
    assert (
        clean(bound)["errors"] and bound.path.exists() and set(bound.deleted) == {"4"}
    )
    assert governance.reset_rejected(bound.asset["id"])["reset"] == ["5"]
    bound.provider.delete = original
    assert clean(bound)["strm_removed"] == 1
    assert bound.cloud.remote.writes == [("delete", "4"), ("delete", "5")]


def test_nas_change_during_official_delete_preserves_new_local_content(bound):
    original = bound.provider.delete

    def change_local(fid):
        original(fid)
        if fid == "5":
            bound.path.write_text("new NAS content")

    bound.provider.delete = change_local
    result = clean(bound)
    assert result["strm_removed"] == 0 and result["errors"]
    assert bound.path.read_text() == "new NAS content" and set(bound.deleted) == {
        "4",
        "5",
    }


def test_movie_delete_api_requires_the_single_use_source_confirmation(
    bound, monkeypatch
):
    from app.routers import library

    execute = Mock(return_value={"status": "success"})
    monkeypatch.setattr(library, "_delete_title_files", execute)
    body = {"target": "local", "tmdb_id": "44", "name": "Movie", "dry_run": True}
    preview = library._movie_delete(body)
    assert preview["cloud_sources"][0]["file_id"] == "4"
    assert library._movie_delete({**body, "dry_run": False})["status"] == "error"
    execute.assert_not_called()
    assert (
        library._movie_delete(
            {
                **body,
                "dry_run": False,
                "source_preview_id": preview["source_preview_id"],
            }
        )["status"]
        == "success"
    )
    assert (
        library._movie_delete(
            {
                **body,
                "dry_run": False,
                "source_preview_id": preview["source_preview_id"],
            }
        )["status"]
        == "error"
    )
    execute.assert_called_once()


def test_preview_checks_cookie_state_without_network(
    bound, monkeypatch
):
    from app.cloud import accounts

    monkeypatch.setattr(
        open115, "for_account", Mock(side_effect=AssertionError("no HTTP preview"))
    )
    accounts.save_authorization("123", {}, {"cookie": ""})
    value = governance.preview(bound.path, bound.cloud.root)
    assert value["delete_configured"] and not value["enabled"]
    assert value["authorization_state"] == "not_connected"
    with pytest.raises(ValueError):
        cloud_bridge.capture([bound.path], bound.cloud.root, True)


def test_unreadable_special_strm_never_blocks_read_or_falls_back_to_source_path(
    cloud, monkeypatch
):
    import os

    pipe = cloud.root / "pipe.strm"
    os.mkfifo(pipe)
    source = Mock(side_effect=AssertionError("no path fallback"))
    monkeypatch.setattr(engine, "cloud_videos", source)
    result = wash.safe_delete_files([pipe], cloud.root, cloud.root)
    assert result["strm_removed"] == 0 and result["errors"]
    source.assert_not_called()
