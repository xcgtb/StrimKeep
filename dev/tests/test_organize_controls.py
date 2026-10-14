"""Filtering, safe stop boundaries and read-only eight-dimensional comparisons."""

import copy
import json
import subprocess
import time
import pytest
from dev.tests.test_cloud_workbench import cloud
from dev.tests.test_my_drive import client
from app.cloud import file_filters, quality8, settings, workflow, recognition, jobs, store, strm
from app.cloud.errors import CloudError


def test_filtering_precedes_tmdb_probe_and_does_not_archive_unknown(cloud, monkeypatch):
    settings.save({"min_video_mb": 1, "ffprobe_enabled": True})
    def unexpected(*args, **kw):
        pytest.fail("filtered videos must not reach TMDB or ffprobe")
    monkeypatch.setattr(recognition, "recognize", unexpected)
    planned = workflow.plan(cloud.id, "1", "2")
    assert not planned["items"] and not planned["unknown_items"] and not planned["skipped"]
    assert planned["filtered"][0]["file_id"] == "4" and not cloud.remote.writes


def test_size_threshold_uses_video_bytes_and_zero_is_unlimited(cloud):
    assert workflow.plan(cloud.id, "1", "2")["items"]
    cloud.remote.files["4"]["size"] = 1048576
    settings.save({"min_video_mb": 1})
    assert workflow.plan(cloud.id, "1", "2")["items"]
    cloud.remote.files["4"]["size"] -= 1
    assert workflow.plan(cloud.id, "1", "2")["filtered"]


@pytest.mark.parametrize("patch", [{"video_extensions": [".strm"]}, {"video_extensions": [".MKV"]}, {"video_extensions": ["mkv"]}, {"video_extensions": [".mkv", ".mkv"]}, {"metadata_extensions": [".mkv"]}, {"min_video_mb": -1}, {"min_video_mb": True}, {"ignore_regex": ["("]}])
def test_invalid_filters_rejected(patch, cloud):
    with pytest.raises(CloudError):
        settings.save(patch)


def test_keyword_regex_and_extension_filters_keep_source_files(cloud):
    settings.save({"ignore_keywords": ["MOVIE"]})
    assert workflow.plan(cloud.id, "1", "2")["filtered"][0]["reason"] == "命中忽略关键词"
    settings.save({"ignore_keywords": [], "ignore_regex": [r"\.1080p\."]})
    assert workflow.plan(cloud.id, "1", "2")["filtered"][0]["reason"] == "命中忽略正则"
    settings.save({"ignore_regex": [], "video_extensions": [".mp4"]})
    assert workflow.plan(cloud.id, "1", "2")["filtered"][0]["reason"] == "未选择的视频扩展名"
    cloud.remote.files["6"] = {**cloud.remote.info("4"), "id": "6", "name": "Movie.strm"}
    assert any(i["reason"] == "STRM 不是源视频" for i in workflow.plan(cloud.id, "1", "2")["filtered"])
    assert not cloud.remote.writes


def test_regex_deadline_aborts_before_any_writes(cloud, monkeypatch):
    settings.save({"ignore_regex": ["Movie"]})
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("regex", 3)
    monkeypatch.setattr(file_filters.subprocess, "run", timeout)
    with pytest.raises(CloudError, match="正则执行超时"):
        workflow.plan(cloud.id, "1", "2")
    assert not cloud.remote.writes


def test_metadata_selection_and_ambiguous_excluded_video_do_not_steal_subtitle(cloud):
    cfg = settings.save({"metadata_extensions": [".nfo"]})
    assert not workflow.related_companions(list(cloud.remote.files.values()), cloud.remote.info("4"), cfg)
    cfg = settings.save({"metadata_extensions": [".srt"], "video_extensions": [".mp4"]})
    cloud.remote.files["6"] = {**cloud.remote.info("4"), "id": "6", "name": "Movie.2024.1080p.mp4"}
    assert not workflow.related_companions(list(cloud.remote.files.values()), cloud.remote.info("6"), cfg)


def test_filter_settings_change_blocks_old_plan(cloud):
    planned = workflow.plan(cloud.id, "1", "2")
    settings.save({"ignore_keywords": ["trailer"]})
    with pytest.raises(CloudError, match="重新预览"):
        workflow.apply(planned["id"], True)


def test_queued_stop_is_durable_idempotent_and_never_executes(cloud, client):
    planned = workflow.plan(cloud.id, "1", "2")
    jid = workflow.apply(planned["id"], True)
    assert client.post(f"/api/cloud/jobs/{jid}/stop", json={}).status_code == 400
    assert client.post(f"/api/cloud/jobs/{jid}/stop", json={"confirmed": 1}).status_code == 400
    assert client.post(f"/api/cloud/jobs/{jid}/stop", json={"confirmed": True}).status_code == 200
    assert client.post(f"/api/cloud/jobs/{jid}/stop", json={"confirmed": True}).status_code == 200
    assert store.job(jid)["stop_requested"] and store.job(jid)["state"] == "stopped"
    jobs.execute(jid)
    assert not cloud.remote.writes
    assert client.get(f"/api/cloud/jobs/{jid}").json()["job"]["state"] == "stopped"


def test_stop_during_first_video_finishes_subtitle_strm_then_leaves_next_video(cloud, monkeypatch):
    cloud.remote.files["6"] = {**cloud.remote.info("4"), "id": "6", "name": "Movie.2024.2160p.mkv"}
    planned = workflow.plan(cloud.id, "1", "2")
    assert len(planned["items"]) == 2
    jid = workflow.apply(planned["id"], True)
    move = cloud.remote.move
    def stop_after_video(fid, parent):
        move(fid, parent)
        if fid == "4":
            jobs.request_stop(jid)
    monkeypatch.setattr(cloud.remote, "move", stop_after_video)
    jobs.execute(jid)
    result = store.job(jid)
    assert result["state"] == "stopped"
    assert cloud.remote.info("4")["parent_id"] == cloud.remote.info("5")["parent_id"] != "1"
    assert cloud.remote.info("6")["parent_id"] == "1"
    assert len(result["result"]["generated"]) == 1 and result["result"]["remaining"][0]["file_id"] == "6"
    assert len(list(cloud.root.rglob("*.strm"))) == 1
    again = workflow.plan(cloud.id, "1", "2")
    assert [i["source"]["id"] for i in again["items"]] == ["6"]


def test_stop_does_not_hide_uncertain_remote_write(cloud, monkeypatch):
    plan = workflow.plan(cloud.id, "1", "2")
    jid = workflow.apply(plan["id"], True)
    def uncertain(fid, parent):
        jobs.request_stop(jid)
        raise CloudError("远端结果未知", 502, uncertain=True)
    monkeypatch.setattr(cloud.remote, "move", uncertain)
    jobs.execute(jid)
    assert store.job(jid)["state"] == "uncertain"


def test_stop_during_automatic_recognition_pauses_rule_and_can_be_enabled(cloud, client, monkeypatch):
    payload = {"account_id": cloud.id, "account_revision": store.account(cloud.id)["revision"], "source_cid": "1", "target_cid": "2", "interval_seconds": 300, "rule_id": "rule"}
    with store.db() as con:
        con.execute("INSERT INTO cloud_rules VALUES(?,?,?,?,?)", ("rule", store.encode(payload), 1, 0, time.time()))
    jid = jobs.enqueue("workflow", payload)
    recognize = recognition.recognize
    def request_stop(*args, **kwargs):
        jobs.request_stop(jid)
        return recognize(*args, **kwargs)
    monkeypatch.setattr(recognition, "recognize", request_stop)
    jobs.execute(jid)
    assert store.job(jid)["state"] == "stopped" and not cloud.remote.writes
    assert client.get("/api/cloud/rules").json()["items"][0]["enabled"] is False
    assert client.post("/api/cloud/rules/rule/enabled", json={"enabled": True}).status_code == 200
    assert client.get("/api/cloud/rules").json()["items"][0]["enabled"] is True


def test_stop_during_receive_reconciles_once_without_starting_organize(cloud, monkeypatch):
    preview = workflow.preview_share(cloud.id, "abcdef")
    jid = workflow.transfer_submit(preview["preview_id"], ["999"], "3", True, "2")
    receive = cloud.remote.receive
    def stop(*args):
        receive(*args)
        jobs.request_stop(jid)
    monkeypatch.setattr(cloud.remote, "receive", stop)
    jobs.execute(jid)
    job = store.job(jid)
    assert job["state"] == "stopped" and job["result"]["stage_id"]
    assert len([w for w in cloud.remote.writes if w[0] == "receive"]) == 1
    assert not [w for w in cloud.remote.writes if w[0] in ("rename", "move")]


def test_quality8_size_is_real_video_bytes_and_order_is_independent(cloud):
    rules = copy.deepcopy(quality8.DEFAULT)
    a = quality8.facts({"name": "Movie.1080p.mkv", "size": 200}, {})
    b = quality8.facts({"name": "Movie.1080p.mkv", "size": 100}, {})
    result = quality8.compare(a, b, rules)
    assert result["result"] == 1 and result["decided_by"] == "size"
    assert len(result["dimensions"]) == 8 and result["preview_only"]
    a["resolution"] = "720p"
    assert quality8.compare(a, b, rules)["result"] == -1
    rules.insert(0, rules.pop(-1))
    assert quality8.compare(a, b, rules)["decided_by"] == "size"
    from app.domain.quality import COVER_RULE_ORDER
    assert len(COVER_RULE_ORDER) == 7 and "size" not in COVER_RULE_ORDER


def test_quality8_measured_information_overrides_filename_and_unknown_is_not_non_dv():
    facts = quality8.facts({"name": "Movie.720p.SDR.mkv", "size": 99}, {"media_info": {"fields": {"resolution": "2160p", "dynamic_range": "DV", "audio_codec": "DD", "fps": "23.976fps"}}})
    assert facts["resolution"] == "2160p / 4K" and quality8.bucket(facts) == "DV"
    assert facts["audio"] == "AC3 / DD"
    other = quality8.facts({"name": "Movie.HDR10.mkv", "size": 50}, {})
    assert quality8.compare(facts, other, quality8.DEFAULT, True)["coexist"]
    unknown = quality8.facts({"name": "Movie.mkv", "size": 50}, {})
    assert not quality8.compare(facts, unknown, quality8.DEFAULT, True)["coexist"]


def test_quality_preview_compares_registered_and_incoming_without_writes(cloud):
    first = workflow.plan(cloud.id, "1", "2")
    jid = workflow.apply(first["id"], True)
    jobs.execute(jid)
    cloud.remote.files["6"] = {**cloud.remote.info("4"), "id": "6", "parent_id": "1", "name": "Movie.2024.2160p.mkv", "size": 200}
    settings.save({"quality8_enabled": True})
    before = list(cloud.remote.writes)
    plan = workflow.plan(cloud.id, "1", "2")
    comparisons = plan["quality8"]["comparisons"]
    assert len(comparisons) == 1 and comparisons[0]["existing_id"] == "4"
    assert comparisons[0]["result"] == 1 and comparisons[0]["decided_by"] == "resolution"
    assert before == cloud.remote.writes and cloud.remote.info("4")
    cloud.remote.files["4"]["sha1"] = "B"*40
    assert workflow.plan(cloud.id, "1", "2")["quality8"]["comparisons"][0]["blocked"]


def test_quality_preview_shows_conflicting_incoming_candidates_but_does_not_choose(cloud):
    cloud.remote.files["6"] = {**cloud.remote.info("4"), "id": "6", "size": 300}
    settings.save({"quality8_enabled": True})
    plan = workflow.plan(cloud.id, "1", "2")
    assert len(plan["items"]) == 1 and len(plan["skipped"]) == 1
    assert plan["quality8"]["comparisons"][0]["decided_by"] == "size"
    assert not cloud.remote.writes


def test_quality_strategy_rejects_missing_dimensions_and_promoted_unknown(cloud):
    with pytest.raises(CloudError):
        settings.save({"quality8_rules": quality8.DEFAULT[:7]})
    bad = copy.deepcopy(quality8.DEFAULT)
    bad[2]["tiers"].insert(0, bad[2]["tiers"].pop())
    with pytest.raises(CloudError):
        settings.save({"quality8_rules": bad})


def test_original_quality_facts_survive_custom_rename_and_later_strm_sync(cloud):
    cloud.remote.files["4"]["name"] = "Movie.2024.2160p.WEB-DL.DV.TrueHD.Atmos-TEAM.mkv"
    settings.save({"naming_style": "template", "movie_file_template": "{title}{ext}"})
    plan = workflow.plan(cloud.id, "1", "2")
    jid = workflow.apply(plan["id"], True)
    jobs.execute(jid)
    assert store.job(jid)["state"] == "success"
    assert cloud.remote.info("4")["name"] == "Movie.mkv"
    payload = {"account_id": cloud.id, "account_revision": store.account(cloud.id)["revision"], "source_cid": "2"}
    sync = jobs.enqueue("sync", payload)
    jobs.execute(sync)
    with store.db() as con:
        raw = con.execute("SELECT payload FROM cloud_assets WHERE account_id=? AND file_id='4'", (cloud.id,)).fetchone()
    media = json.loads(raw[0])["media"]
    facts = quality8.facts(cloud.remote.info("4"), media)
    assert facts["release_group"] == "TEAM" and facts["source"] == "WEB-DL"
    assert facts["audio"] == "TrueHD Atmos / DTS:X" and quality8.bucket(facts) == "DV"
