"""Generated names agree across cloud videos, subtitles and persisted STRM output."""

import re
from pathlib import Path

import pytest

from app.cloud import categories, jobs, media_probe, naming, recognition, settings, store, strm, workflow
from dev.tests.test_cloud_workbench import cloud


@pytest.mark.parametrize("style", ["standard", "compact", "template"])
def test_upstream_names_preserve_title_and_join_media_information(cloud, monkeypatch, style):
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
    if style != "compact":
        assert "23.976fps" in media["name"] and "7.1-WF.mkv" in media["name"]
    assert media["title"] == "圣诞夜惊魂"
    assert media["directory"].endswith("圣诞夜惊魂 (1993) " + ("[tmdb-9479]" if style == "template" else "{tmdb-9479}"))
    if style == "standard":
        assert media["name"] == "圣诞夜惊魂 (1993) - 2160p.BluRay REMUX.HDR10.H.265.10-bit.23.976fps.DTS 7.1-WF.mkv"
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
    assert example["name"] == "金色 2026 S01E01 2160p 5.1.mkv"
    assert naming.filename("Movie\tName\u3000..23.976fps.DTS-HD MA.7.1.mkv") == "Movie Name .23.976fps.DTS-HD MA.7.1.mkv"


def test_organize_publishes_matching_video_subtitle_strm_and_nfo_names(cloud):
    settings.save({"naming_style": "template", "movie_file_template": "{title} {year} {resolution}{ext}"})
    plan = workflow.plan(cloud.id, "1", "2")
    jid = workflow.apply(plan["id"], True)
    jobs.execute(jid)
    assert store.job(jid)["state"] == "success"
    video = cloud.remote.info("4")
    subtitle = cloud.remote.info("5")
    assert video["name"] == "Movie 2024 1080p.mkv"
    assert subtitle["name"] == "Movie 2024 1080p.zh.srt"
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


def test_visible_upstream_dolby_example_keeps_profile_and_audio_layout():
    fields = media_probe.summarize({"streams": [
        {"codec_type": "video", "codec_name": "hevc", "width": 3840, "height": 2160, "pix_fmt": "yuv420p10le", "avg_frame_rate": "24000/1001", "side_data_list": [{"side_data_type": "DOVI configuration record", "dv_profile": 7}]},
        {"codec_type": "audio", "codec_name": "truehd", "channels": 8, "channel_layout": "7.1"},
    ]})
    assert fields["dynamic_range"] == "DV" and fields["dynamic_range_label"] == "DoVi P7"
    values = {"kind": "movie", "title": "走私", "year": "2023", "tmdb_id": "783110", "source": "Ultra HD Blu-ray", "original_name": "Smugglers.2023.2160p.BluRay.REMUX.DoVi.P7-WF.mkv", "release_group": "WF", "ext": ".mkv", **fields}
    assert naming.standard_filename(values) == "走私 (2023) - 2160p.BluRay REMUX.DoVi P7.H.265.10-bit.23.976fps.TrueHD 7.1-WF.mkv"


def test_probe_audio_profile_is_preserved_without_inventing_atmos():
    fields = media_probe.summarize({"streams": [
        {"codec_type": "video", "codec_name": "hevc", "width": 1920, "height": 1080, "side_data_list": [{"side_data_type": "DOVI configuration record"}]},
        {"codec_type": "audio", "codec_name": "dts", "profile": "DTS-HD MA", "channel_layout": "5.1(side)"},
    ]})
    assert fields["audio_codec_label"] == "DTS-HD MA"
    assert fields["dynamic_range_label"] == "DoVi"  # no unverified P7 suffix
    assert "Atmos" not in str(fields)
    assert naming.normalize_fields({"dynamic_range": "Dolby Vision Profile 7"})["dynamic_range"] == "DoVi P7"
    assert naming.normalize_fields({"dynamic_range": "HDR / HLG", "original_name": "Movie.HDR.mkv"})["dynamic_range"] == "HDR"


@pytest.mark.parametrize("category,kind,genre,country,expected", [
    ("电影", "movie", 0, "CN", "电影/国产电影"),
    ("电影", "movie", 16, "US", "电影/动画电影"),
    ("剧集", "tv", 0, "JP", "剧集/日韩剧集"),
    ("动漫", "tv", 16, "US", "动漫/欧美动漫"),
    ("纪录片", "movie", 99, "ZZ", "纪录片/其他纪录片"),
    ("综艺", "tv", 10764, "CN", "综艺/国产综艺"),
    ("儿童节目", "tv", 10762, "CN", "儿童节目/国产儿童节目"),
    ("演唱会", "movie", 0, "CN", "演唱会/国产演唱会"),
])
def test_upstream_grouped_layout_uses_existing_classification_rules(cloud, category, kind, genre, country, expected):
    cfg = settings.save({"classification_enabled": True, "region_classification": True, "region_mode": "grouped", "animation_movie_folder": True, "enabled_categories": categories.DEFAULT + categories.OPTIONAL})
    media = categories.decide(cfg, {"genres": [{"id": genre}], "origin_country": [country]}, kind, {"category": category} if category == "演唱会" else None)
    media.update(kind=kind,year="2023")
    assert categories.prefix(cfg, media) == expected
