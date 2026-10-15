"""Directory identity and type-specific names, based on the supplied upstream log."""
import re
from unittest.mock import Mock
import pytest
from app.cloud import jobs, naming, recognition, settings, store, workflow
from app.cloud.errors import CloudError
from dev.tests.test_cloud_workbench import cloud


class TV:
    key = "fixture"
    def __init__(self):
        self.calls = []
    def get(self, path, **params):
        self.calls.append(path)
        if "/episode/" in path:
            season, episode = map(int, re.search(r"/season/(\d+)/episode/(\d+)", path).groups())
            return {"id": 1000+episode, "name": "第"+str(episode)+"集", "season_number": season, "episode_number": episode}
        return {"id": int(path.rsplit("/", 1)[1]), "name": "征途", "first_air_date": "2026-01-01"}


def nested(cloud):
    folder = cloud.remote.mkdir("1", "征途 (2026) {tmdb-336207}")
    season = cloud.remote.mkdir(folder, "Season 1")
    cloud.remote.move("4", season)
    cloud.remote.move("5", season)
    cloud.remote.files["4"]["name"] = "S01E01.2026.2160p.WEB-DL.H265.mkv"
    cloud.remote.files["5"]["name"] = "S01E01.2026.2160p.WEB-DL.H265.zh.srt"
    return folder, season


def test_parent_id_drives_real_plan_video_subtitle_and_strm(cloud, monkeypatch):
    folder, season = nested(cloud)
    client = TV()
    monkeypatch.setattr(recognition, "tmdb", lambda: client)
    plan = workflow.plan(cloud.id, "1", "2")
    assert len(plan["items"]) == 1 and not plan["skipped"]
    media = plan["items"][0]["media"]
    assert media["name"] == "征途.2026.S01E01.第1集.2160p.WEB-DL.H.265.mkv"
    assert media["tmdb_id"] == "336207"
    assert media["recognition_context"]["directory"] == "征途 (2026) {tmdb-336207}"
    assert not any("search" in p for p in client.calls)
    jid = workflow.apply(plan["id"], True)
    jobs.execute(jid)
    assert store.job(jid)["state"] == "success"
    assert cloud.remote.info("5")["name"] == media["name"][:-4]+".zh.srt"
    assert len(list(cloud.root.rglob("征途.2026.S01E01.第1集.*.strm"))) == 1


def test_parent_id_and_season_are_used_when_scanning_season_itself(cloud, monkeypatch):
    _, season = nested(cloud)
    cloud.remote.files["4"]["name"] = "E01.2160p.mkv"
    monkeypatch.setattr(recognition, "tmdb", lambda: TV())
    jid = jobs.enqueue("sync", {"account_id": cloud.id, "account_revision": 1, "source_cid": season})
    jobs.execute(jid)
    assert store.job(jid)["state"] == "success", store.job(jid)
    assert len(store.job(jid)["result"]["generated"]) == 1


def test_explicit_file_and_manual_identity_take_priority_over_parent(cloud, monkeypatch):
    nested(cloud)
    monkeypatch.setattr(recognition, "tmdb", lambda: TV())
    row = cloud.remote.info("4")
    row["name"] = "Other.S02E03.[tmdb-55].mkv"
    media = workflow.recognize_source(cloud.id, cloud.remote, row, {}, settings.get())
    assert (media["tmdb_id"], media["season"], media["episode"]) == ("55", 2, 3)
    media = workflow.recognize_source(cloud.id, cloud.remote, row, {"4": {"kind": "tv", "tmdb_id": "66", "season": 0, "episode": 7}}, settings.get())
    assert (media["tmdb_id"], media["season"], media["episode"]) == ("66", 0, 7)


def test_sibling_work_identities_never_leak_via_directory_cache(cloud, monkeypatch):
    nested(cloud)
    other = cloud.remote.mkdir("1", "另一部 (2026) [tmdb-55]")
    cloud.remote.files["90"] = {**cloud.remote.info("4"), "id": "90", "parent_id": other}
    monkeypatch.setattr(recognition, "tmdb", lambda: TV())
    cache = {}
    a = workflow.recognize_source(cloud.id, cloud.remote, cloud.remote.info("4"), {}, settings.get(), cache)
    b = workflow.recognize_source(cloud.id, cloud.remote, cloud.remote.info("90"), {}, settings.get(), cache)
    assert a["tmdb_id"] == "336207" and b["tmdb_id"] == "55"


def test_parent_listing_is_cached_for_all_episodes_in_one_plan(cloud, monkeypatch):
    _, season = nested(cloud)
    cloud.remote.files["90"] = {**cloud.remote.info("4"), "id": "90", "name": "S01E02.2160p.mkv"}
    monkeypatch.setattr(recognition, "tmdb", lambda: TV())
    original = cloud.remote.info
    seen = Mock(side_effect=original)
    monkeypatch.setattr(cloud.remote, "info", seen)
    plan = workflow.plan(cloud.id, "1", "2")
    assert len(plan["items"]) == 2
    assert len([c for c in seen.call_args_list if c.args == (season,)]) == 1


def test_ambiguous_directory_identity_is_not_guessed_or_mutated(cloud, monkeypatch):
    folder, _ = nested(cloud)
    cloud.remote.files[folder]["name"] = "冲突 {tmdb-55} [tmdb-66]"
    before = list(cloud.remote.writes)
    plan = workflow.plan(cloud.id, "1", "2")
    assert not plan["items"] and "多个 TMDB" in plan["skipped"][0]["reason"]
    assert cloud.remote.writes == before


def test_upstream_tv_log_example_keeps_audio_spaces_and_movie_format():
    values = {"kind": "tv", "title": "征途", "year": "2026", "season": 1, "episode": 1, "episode_title": "第1集", "resolution": "2160p", "dynamic_range": "SDR", "codec": "H265", "bitdepth": "10bit", "fps": "60fps", "audio_codec": "AAC", "audio_layout": "2.0", "ext": ".mkv"}
    assert naming.standard_filename(values) == "征途.2026.S01E01.第1集.2160p.SDR.H.265.10-bit.60fps.AAC 2.0.mkv"
    assert naming.standard_filename(values, compact=True) == "征途.2026.S01E01.2160p.H.265.mkv"
    assert naming.standard_filename({**values, "kind": "movie"}).startswith("征途 (2026) - 2160p.")


def test_parent_identity_change_after_preview_stops_before_rename_or_move(cloud, monkeypatch):
    folder, season = nested(cloud)
    monkeypatch.setattr(recognition, "tmdb", lambda: TV())
    plan = workflow.plan(cloud.id, "1", "2")
    cloud.remote.files[folder]["name"] = "另一部 {tmdb-55}"
    before = list(cloud.remote.writes)
    jid = workflow.apply(plan["id"], True)
    jobs.execute(jid)
    assert store.job(jid)["state"] == "needs_review"
    assert "父目录" in store.job(jid)["result"]["errors"][0]["reason"]
    assert cloud.remote.writes == before
