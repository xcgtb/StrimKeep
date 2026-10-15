"""Generated names agree across cloud videos, subtitles and persisted STRM output."""

import re
from pathlib import Path

import pytest

from app.cloud import jobs, naming, recognition, settings, store, strm, workflow
from dev.tests.test_cloud_workbench import cloud


@pytest.mark.parametrize("style", ["standard", "compact", "template"])
def test_generated_names_remove_title_and_media_whitespace(cloud, monkeypatch, style):
    class Movie:
        def get(self, *args, **kwargs):
            return {"id": 9479, "title": "圣诞夜惊魂", "release_date": "1993-10-01"}

    monkeypatch.setattr(recognition, "tmdb", lambda: Movie())
    settings.save({"naming_style": style, "movie_file_template": "{title} {year}.{source}.{fps}.{audio_layout}-{release_group}{ext}"})
    media = recognition.recognize(
        "The Nightmare Before Christmas.1993.2160p.Ultra HD BluRay.REMUX.HDR.HEVC.DTS-HD MA 7.1-WF.mkv",
        {"kind": "movie", "tmdb_id": "9479"},
        probe={"status": "success", "fields": {"resolution": "2160p", "video_codec": "H265", "audio_codec": "DTS", "dynamic_range": "HDR10", "fps": "23.976fps", "bitdepth": "10bit", "audio_layout": "7.1"}},
    )
    assert not re.search(r"\s", media["name"])
    assert "23.976fps" in media["name"] and "7.1-WF.mkv" in media["name"]
    assert media["title"] == "圣诞夜惊魂"
    assert media["directory"].endswith("圣诞夜惊魂 (1993) [tmdb-9479]")
    if style == "standard":
        assert media["name"] == "圣诞夜惊魂.1993.[tmdb-9479].2160p.UHD.BluRay.HDR10.H265.23.976fps.10bit.DTS.7.1-WF.mkv"
    if style == "compact":
        assert "tmdb" not in media["name"]


@pytest.mark.parametrize("patch", [{"naming_style": "original"}, {"rename_enabled": False}])
def test_explicit_original_name_settings_preserve_spaces(cloud, patch):
    settings.save(patch)
    name = "Movie 2024 1080p.mkv"
    assert recognition.recognize(name)["name"] == name


def test_template_preview_uses_the_same_dot_normalization(cloud):
    cfg = settings.save({"naming_style": "template", "tv_file_template": "{title} {year} S{season:02d}E{episode:02d} {resolution} {audio_layout}{ext}"})
    example = naming.preview(cfg)
    assert example["name"] == "金色.2026.S01E01.2160p.5.1.mkv"
    assert naming.filename("Movie\tName\u3000..23.976fps.DTS-HD MA.7.1.mkv") == "Movie.Name.23.976fps.DTS-HD.MA.7.1.mkv"


def test_organize_publishes_matching_video_subtitle_strm_and_nfo_names(cloud):
    settings.save({"naming_style": "template", "movie_file_template": "{title} {year} {resolution}{ext}"})
    plan = workflow.plan(cloud.id, "1", "2")
    jid = workflow.apply(plan["id"], True)
    jobs.execute(jid)
    assert store.job(jid)["state"] == "success"
    video = cloud.remote.info("4")
    subtitle = cloud.remote.info("5")
    assert video["name"] == "Movie.2024.1080p.mkv"
    assert subtitle["name"] == "Movie.2024.1080p.zh.srt"
    path = Path(strm.list_assets()[0]["path"])
    assert path.stem == Path(video["name"]).stem
    assert path.is_file() and path.with_suffix(".nfo").is_file() and path.with_suffix(".zh.srt").is_file()


def test_rescan_after_output_change_writes_new_root_without_remote_mutations(cloud):
    plan = workflow.plan(cloud.id, "1", "2")
    jid = workflow.apply(plan["id"], True)
    jobs.execute(jid)
    old_files = {p: p.read_bytes() for p in cloud.root.rglob("*") if p.is_file()}
    generated = cloud.root.parent / "strm" / "local"
    generated.mkdir(parents=True)
    settings.save({"output_root": str(generated)})
    before = list(cloud.remote.writes)
    jid = jobs.enqueue("sync", {"account_id": cloud.id, "account_revision": store.account(cloud.id)["revision"], "source_cid": "2"})
    jobs.execute(jid)
    job = store.job(jid)
    assert job["state"] == "success", job
    assert len(job["result"]["generated"]) == 1
    assert all(Path(p).is_relative_to(generated) and Path(p).is_file() for p in job["result"]["generated"])
    assert cloud.remote.writes == before
    assert all(p.read_bytes() == content for p, content in old_files.items())
