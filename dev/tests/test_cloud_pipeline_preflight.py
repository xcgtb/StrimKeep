"""Exercise failures before mutations, queue-time drift, and explicit organize-only mode."""

import pytest
from app.cloud import pipeline, settings, workflow, jobs, store, recognition
from app.cloud.errors import CloudError
from dev.tests.test_cloud_workbench import cloud


@pytest.mark.parametrize("patch", [{"public_url": ""}, {"output_root": "/does-not-exist/strm"}])
def test_apply_rejects_missing_output_without_moving_or_consuming_plan(cloud, patch):
    plan = workflow.plan(cloud.id, "1", "2")
    settings.save(patch)
    with pytest.raises(CloudError):
        workflow.apply(plan["id"], True)
    assert cloud.remote.writes == []
    with store.db() as con:
        assert con.execute("SELECT state FROM cloud_plans WHERE id=?", (plan["id"],)).fetchone()[0] == "pending"


def test_worker_rechecks_output_if_configuration_changes_in_queue(cloud):
    plan = workflow.plan(cloud.id, "1", "2")
    jid = workflow.apply(plan["id"], True)
    settings.save({"public_url": ""})
    jobs.execute(jid)
    assert store.job(jid)["state"] == "failed"
    assert cloud.remote.writes == []
    assert cloud.remote.info("4")["parent_id"] == "1"


@pytest.mark.parametrize("enabled,generate", [(False, True), (True, False)])
def test_explicit_organize_only_can_move_without_playback_configuration(cloud, enabled, generate):
    settings.save({"public_url": "", "generate_strm_enabled": enabled})
    plan = workflow.plan(cloud.id, "1", "2")
    jid = workflow.apply(plan["id"], True, generate)
    jobs.execute(jid)
    result = store.job(jid)
    assert result["state"] == "success", result
    assert result["result"]["files"] and not result["result"]["generated"]


def test_transfer_worker_does_not_receive_before_output_is_ready(cloud):
    first = workflow.preview_share(cloud.id, "https://115.com/s/abcdef123")
    jid = workflow.transfer_submit(first["preview_id"], ["999"], "3", True, "2")
    settings.save({"public_url": ""})
    jobs.execute(jid)
    assert store.job(jid)["state"] == "failed"
    assert cloud.remote.writes == []


def test_plain_transfer_does_not_require_tmdb_or_strm(cloud, monkeypatch):
    monkeypatch.setattr(recognition, "tmdb", lambda: (_ for _ in ()).throw(CloudError("缺少 TMDB")))
    settings.save({"public_url": ""})
    first = workflow.preview_share(cloud.id, "https://115.com/s/abcdef123")
    jid = workflow.transfer_submit(first["preview_id"], ["999"], "3", True)
    jobs.execute(jid)
    assert store.job(jid)["state"] == "success"
    assert any(w[0] == "receive" for w in cloud.remote.writes)


def test_readiness_reports_each_missing_dependency_without_remote_writes(cloud, monkeypatch):
    from app import engine
    monkeypatch.setattr(engine, "RUNTIME_CFG", {**engine.RUNTIME_CFG, "tmdb_key": "fixture"})
    settings.save({"public_url": ""})
    value = pipeline.status(cloud.id)
    assert not value["ready"]
    checks = {c["key"]: c for c in value["checks"]}
    assert checks["tmdb"]["ready"]
    assert checks["output"]["page"] == "strm"
    assert checks["output"]["module"] == "strm-output"
    assert not checks["source"]["ready"]
    assert cloud.remote.writes == []


def test_unwritable_mount_is_blocked_before_cloud_operations(cloud, monkeypatch):
    def denied(**kwargs):
        raise PermissionError("read-only mount")
    monkeypatch.setattr(pipeline.tempfile, "TemporaryFile", denied)
    with pytest.raises(CloudError, match="不可写"):
        pipeline.output_ready()
    assert cloud.remote.writes == []


def test_output_checks_leave_no_probe_files(cloud):
    before = list(cloud.root.iterdir())
    assert pipeline.output_ready() == str(cloud.root)
    assert list(cloud.root.iterdir()) == before


def test_independent_generation_does_not_write_to_upstream_governance_mount(cloud):
    generated = cloud.root.parent / "strimkeep-output" / "local"
    generated.mkdir(parents=True)
    settings.save({"output_root": str(generated)})
    plan = workflow.plan(cloud.id, "1", "2")
    jid = workflow.apply(plan["id"], True)
    jobs.execute(jid)
    result = store.job(jid)
    assert result["state"] == "success", result
    assert list(generated.rglob("*.strm"))
    assert not list(cloud.root.iterdir())
    assert all(i["path"].startswith(str(generated)+"/") for i in result["result"]["generated"])


def test_new_independent_defaults_do_not_reset_an_existing_output_setting(cloud):
    assert settings.DEFAULTS["output_root"] == "/strm/local"
    assert settings.DEFAULTS["share_output_root"] == "/strm/share"
    settings.save({"output_root": "/media/local", "share_output_root": "/media/share"})
    assert settings.get()["output_root"] == "/media/local"
    assert settings.get()["share_output_root"] == "/media/share"
