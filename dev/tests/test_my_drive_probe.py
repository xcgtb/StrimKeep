"""Exercise real ffprobe via the bounded relay, plus classification and append workflows."""

import shutil
import socket
import subprocess

import httpx
import pytest
from dev.tests.test_cloud_workbench import cloud
from dev.tests.test_my_drive import client
from dev.tests.test_my_drive_organize import Catalog
from app.cloud import media_probe, settings, recognition, workflow, categories, jobs, store, directories, naming
from app.cloud.errors import CloudError


@pytest.mark.parametrize("country,group", [("CN", "国产"), ("HK", "国产"), ("TW", "国产"), ("US", "欧美"), ("GB", "欧美"), ("DE", "欧美"), ("JP", "日韩"), ("KR", "日韩"), ("IN", "其他")])
def test_region_groups_match_screenshot_directories(country, group, cloud):
    cfg = settings.save({"classification_enabled": True, "region_classification": True, "region_mode": "grouped"})
    for kind, category in (("movie", "电影"), ("tv", "剧集")):
        media = {"kind": kind, "year": "2024", **categories.decide(cfg, {"origin_country": [country]}, kind)}
        assert categories.prefix(cfg, media) == category+"/"+group+category


def test_custom_groups_priority_animation_and_manual_override(cloud):
    cfg = settings.save({"classification_enabled": True, "region_classification": True, "region_mode": "grouped", "animation_movie_folder": True, "region_groups": [{"name": "韩国", "countries": ["KR"]}, {"name": "国产", "countries": ["CN"]}]})
    media = categories.decide(cfg, {"origin_country": ["CN", "KR"]}, "tv")
    assert media["subcategory"] == "韩国剧集"
    animation = {"genre_ids": [16], "origin_country": ["CN"]}
    assert categories.decide(cfg, animation, "movie")["subcategory"] == "动画电影"
    assert categories.decide(cfg, animation, "tv")["subcategory"] == "国产动漫"
    assert categories.decide(cfg, animation, "movie", {"category": "动漫"})["subcategory"] == "国产动漫"


@pytest.mark.parametrize("rules", [[], [{"name": "其他", "countries": ["CN"]}], [{"name": "../x", "countries": ["CN"]}], [{"name": "国产", "countries": ["CN", "CN"]}], [{"name": "A", "countries": ["CN"]}, {"name": "B", "countries": ["CN"]}], [{"name": "A", "countries": ["cn"]}]])
def test_invalid_region_rules_cannot_be_saved(rules, cloud):
    with pytest.raises(CloudError):
        settings.save({"region_groups": rules})


def test_precreate_grouped_tree_twice_reuses_ids_and_matches_recognition(cloud, client, monkeypatch):
    monkeypatch.setattr(recognition, "tmdb", lambda: Catalog())
    cfg = settings.save({"classification_enabled": True, "enabled_categories": ["电影", "剧集"], "region_classification": True, "region_mode": "grouped", "animation_movie_folder": True})
    results = []
    for _ in range(2):
        response = client.post("/api/cloud/categories/create", json={"account_id": cloud.id, "target_cid": "2", "confirmed": True})
        assert response.status_code == 202
        jid = response.json()["job_id"]
        jobs.execute(jid)
        assert store.job(jid)["state"] == "success"
        results.append(store.job(jid)["result"]["directories"])
    assert results[0] == results[1]
    assert {r["name"] for r in results[0]} == set(categories.directory_paths(cfg))
    assert len([w for w in cloud.remote.writes if w[0] == "mkdir"]) == 11
    media = recognition.recognize("Series.2024.S01E01.mkv")
    assert media["directory"].startswith("剧集/国产剧集/")
    preview = client.post("/api/cloud/naming/preview", json={"settings": {}}).json()
    assert preview["items"][1]["directory"].startswith("剧集/国产剧集/")


def test_later_episodes_reuse_show_and_season_but_new_season_is_created(cloud, monkeypatch):
    monkeypatch.setattr(recognition, "tmdb", lambda: Catalog())
    settings.save({"classification_enabled": True, "region_classification": True, "region_mode": "grouped", "naming_style": "template"})
    cloud.remote.files["4"]["name"] = "Series.2024.S01E01.mkv"
    cloud.remote.files["5"]["name"] = "Series.2024.S01E01.zh.srt"
    def organize():
        plan = workflow.plan(cloud.id, "1", "2")
        jid = workflow.apply(plan["id"], True, False)
        jobs.execute(jid)
        assert store.job(jid)["state"] == "success"
    organize()
    season1 = cloud.remote.info("4")["parent_id"]
    show = cloud.remote.info(season1)["parent_id"]
    before = len([w for w in cloud.remote.writes if w[0] == "mkdir"])
    cloud.remote.files["6"] = {**cloud.remote.info("4"), "id": "6", "parent_id": "1", "name": "Series.2024.S01E02.mkv"}
    organize()
    assert cloud.remote.info("6")["parent_id"] == season1
    assert len([w for w in cloud.remote.writes if w[0] == "mkdir"]) == before
    cloud.remote.files["7"] = {**cloud.remote.info("4"), "id": "7", "parent_id": "1", "name": "Series.2024.S02E01.mkv"}
    organize()
    season2 = cloud.remote.info("7")["parent_id"]
    assert season2 != season1 and cloud.remote.info(season2)["parent_id"] == show
    assert len([w for w in cloud.remote.writes if w[0] == "mkdir"]) == before+1


def probe_data():
    return {"streams": [{"codec_type": "video", "codec_name": "hevc", "width": 3840, "height": 1600, "pix_fmt": "yuv420p10le", "avg_frame_rate": "24000/1001", "color_transfer": "smpte2084"}, {"codec_type": "audio", "codec_name": "eac3", "channels": 6, "channel_layout": "5.1(side)"}], "format": {"duration": "2700"}}


def test_probe_actual_fields_override_filename_and_preserve_source_release(cloud):
    fields = media_probe.summarize(probe_data())
    cfg = settings.save({"naming_style": "template", "movie_file_template": "{title}.{resolution}.{codec}.{dynamic_range}.{fps}.{bitdepth}.{audio_codec}.{audio_layout}-{release_group}{ext}"})
    media = recognition.recognize("Movie.2024.1080p.WEB-DL.H264-GROUP.mkv", naming_config=cfg, probe={"status": "success", "fields": fields})
    assert media["name"] == "Movie.2160p.H265.HDR10.23.976fps.10bit.DDP.5.1-GROUP.mkv"
    assert media["naming_fields"]["source"] == "WEB-DL"
    assert media["media_info"]["status"] == "success"
    settings.save({"rename_enabled": False})
    assert recognition.recognize("Movie.2024.1080p.mkv", probe={"status": "success", "fields": fields})["name"] == "Movie.2024.1080p.mkv"


def test_full_template_does_not_leave_release_group_separator_when_empty():
    assert naming.render("{title}.{audio_codec}.{audio_layout}-{release_group}{ext}", {"title": "Movie", "audio_codec": "AAC", "audio_layout": "", "release_group": "", "ext": ".mkv"}) == "Movie.AAC.mkv"


def test_webrip_source_is_not_promoted_to_webdl_and_interlacing_is_preserved():
    assert recognition.source_label({"source": "Web"}, "Movie.WEBRip.mkv") == "WEBRip"
    assert recognition.source_label({"source": "Web"}, "Movie.WEB-DL.mkv") == "WEB-DL"
    data = probe_data()
    data["streams"][0].update(width=1920, height=1080, field_order="tt")
    assert media_probe.summarize(data)["resolution"] == "1080i"


@pytest.mark.parametrize("transfer,side,expected", [("smpte2084", [], "HDR10"), ("arib-std-b67", [], "HLG"), ("bt709", [], "SDR"), (None, [], ""), ("smpte2084", [{"side_data_type": "DOVI configuration record"}], "DV"), ("smpte2084", [{"side_data_type": "HDR Dynamic Metadata SMPTE2094-40 (HDR10+)"}], "HDR10+")])
def test_dynamic_range_is_only_confirmed_when_evidence_exists(transfer, side, expected):
    data = probe_data()
    data["streams"][0].update(color_transfer=transfer, side_data_list=side)
    assert media_probe.summarize(data)["dynamic_range"] == expected


def test_probe_cache_uses_fingerprint_not_filename(cloud, monkeypatch):
    calls = []
    monkeypatch.setattr(media_probe, "probe_url", lambda link, size: calls.append(size) or media_probe.summarize(probe_data()))
    row = cloud.remote.info("4")
    assert media_probe.extract(cloud.id, cloud.remote, row)["status"] == "success"
    cloud.remote.files["4"]["name"] = "renamed.mkv"
    assert media_probe.extract(cloud.id, cloud.remote, cloud.remote.info("4"))["status"] == "cached"
    cloud.remote.files["4"]["sha1"] = "B"*40
    assert media_probe.extract(cloud.id, cloud.remote, cloud.remote.info("4"))["status"] == "success"
    assert len(calls) == 2 and cloud.remote.downloads == 2


def test_probe_failure_skips_without_unknown_move_or_can_explicitly_fallback(cloud, client, monkeypatch):
    def broken(*args):
        raise CloudError("ffprobe 提取失败，保留源文件", 502)
    monkeypatch.setattr(media_probe, "extract", broken)
    settings.save({"ffprobe_enabled": True, "unknown_enabled": True})
    directories.save(cloud.id, {"unknown": "3"}, 0)
    plan = workflow.plan(cloud.id, "1", "2")
    assert not plan["items"] and not plan["unknown_items"] and "ffprobe" in plan["skipped"][0]["reason"]
    assert not cloud.remote.writes
    settings.save({"ffprobe_failure": "filename"})
    plan = workflow.plan(cloud.id, "1", "2")
    assert plan["items"][0]["media"]["media_info"]["status"] == "fallback"
    jid = workflow.apply(plan["id"], True, False)
    jobs.execute(jid)
    assert store.job(jid)["result"]["warnings"]


def test_enabled_probe_plan_organize_and_sync_use_actual_fields(cloud, monkeypatch):
    monkeypatch.setattr(media_probe, "probe_url", lambda *args: media_probe.summarize(probe_data()))
    settings.save({"ffprobe_enabled": True, "naming_style": "template", "movie_file_template": "{title}.{resolution}.{codec}{ext}"})
    plan = workflow.plan(cloud.id, "1", "2")
    assert plan["items"][0]["media"]["name"] == "Movie.2160p.H265.mkv"
    jid = workflow.apply(plan["id"], True)
    jobs.execute(jid)
    assert store.job(jid)["state"] == "success"
    assert cloud.remote.info("4")["name"] == "Movie.2160p.H265.mkv"
    assert cloud.remote.info("5")["name"] == "Movie.2160p.H265.zh.srt"
    assert list(cloud.root.rglob("Movie.2160p.H265.strm"))
    payload = {"account_id": cloud.id, "account_revision": store.account(cloud.id)["revision"], "source_cid": "2"}
    sync = jobs.enqueue("sync", payload)
    jobs.execute(sync)
    assert store.job(sync)["state"] == "success" and cloud.remote.downloads == 1


def test_probe_setting_change_invalidates_preview(cloud):
    plan = workflow.plan(cloud.id, "1", "2")
    settings.save({"ffprobe_enabled": True})
    with pytest.raises(CloudError, match="重新预览"):
        workflow.apply(plan["id"], True)
    assert not cloud.remote.writes


@pytest.mark.parametrize('tail_padding', [False, True])
def test_real_ffprobe_reads_mp4_via_range_relay_without_exposing_headers(tmp_path, monkeypatch, tail_padding):
    if not shutil.which("ffprobe") or not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg tooling absent")
    movie = tmp_path/"fixture.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=320x240:r=25", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono", "-t", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(movie)], check=True, timeout=15)
    contents = movie.read_bytes()
    if tail_padding:
        # Keep chunk offsets valid: pad AFTER mdat, immediately before tail moov.
        # Models multi-GB MP4s whose metadata is far beyond the first 8 MiB.
        import struct
        offset = 0
        while contents[offset+4:offset+8] != b'moov':
            length = struct.unpack('>I', contents[offset:offset+4])[0]
            assert length >= 8
            offset += length
            assert offset < len(contents)
        padding = media_probe.CHUNK_BYTES + 65536
        contents = contents[:offset] + struct.pack('>I4s', padding, b'free') + bytes(padding-8) + contents[offset:]
        assert len(contents) > media_probe.CHUNK_BYTES
    requests = []
    original_client = httpx.Client
    def send(request):
        requests.append(request)
        assert str(request.url) == "https://download.115cdn.com/fixture"
        assert request.headers["cookie"] == "private"
        start, end = map(int, request.headers["range"].removeprefix("bytes=").split("-"))
        return httpx.Response(206, stream=httpx.ByteStream(contents[start:end+1]), headers={"Content-Range": f"bytes {start}-{end}/{len(contents)}"})
    monkeypatch.setattr(httpx, "Client", lambda **kw: original_client(transport=httpx.MockTransport(send), **kw))
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))])
    fields = media_probe.probe_url({"url": "https://download.115cdn.com/fixture", "headers": {"Cookie": "private"}}, len(contents))
    assert fields["width"] == "320" and fields["height"] == "240"
    assert fields["video_codec"] == "H264" and fields["fps"] == "25fps" and fields["bitdepth"] == "8bit" and fields["audio_codec"] == "AAC"
    assert requests and sum(len(r.headers["range"]) > 0 for r in requests) >= 1
    assert "private" not in str(fields)


def test_relay_rejects_private_dns(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))])
    with pytest.raises(CloudError, match="CDN 地址异常"):
        media_probe.probe_url({"url": "https://download.115cdn.com/source", "headers": {}}, 100)


@pytest.mark.parametrize("response_code,content_range", [(302, None), (206, "bytes 1-99/100"), (200, None)])
def test_relay_rejects_redirect_wrong_range_or_ignored_large_range(monkeypatch, response_code, content_range):
    if not media_probe.available():
        pytest.skip("ffprobe absent")
    original_client = httpx.Client
    seen = []
    def send(request):
        seen.append(request)
        headers = {"location": "http://127.0.0.1/private"} if response_code == 302 else {}
        if content_range:
            headers["content-range"] = content_range
        return httpx.Response(response_code, stream=httpx.ByteStream(b"invalid"), headers=headers)
    monkeypatch.setattr(httpx, "Client", lambda **kw: original_client(transport=httpx.MockTransport(send), **kw))
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))])
    with pytest.raises(CloudError):
        media_probe.probe_url({"url": "https://download.115cdn.com/source", "headers": {"Cookie": "private"}}, media_probe.CHUNK_BYTES+1)
    assert seen and all(r.url.host == "download.115cdn.com" for r in seen)


def test_relay_byte_budget_prevents_any_upstream_read(monkeypatch):
    if not media_probe.available():
        pytest.skip("ffprobe absent")
    monkeypatch.setattr(media_probe, "MAX_BYTES", 0)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))])
    def unexpected(**kw):
        pytest.fail("exhausted probe must not contact CDN")
    monkeypatch.setattr(httpx, "Client", unexpected)
    with pytest.raises(CloudError):
        media_probe.probe_url({"url": "https://download.115cdn.com/source", "headers": {}}, 100)


def test_identity_conflict_never_falls_back_to_filename(cloud, monkeypatch):
    settings.save({"ffprobe_enabled": True, "ffprobe_failure": "filename"})
    def changed(*args):
        raise CloudError("下载地址与源文件身份不符", 409)
    monkeypatch.setattr(media_probe, "extract", changed)
    planned = workflow.plan(cloud.id, "1", "2")
    assert not planned["items"] and planned["skipped"] and not cloud.remote.writes
