"""Conservative automatic matching plus explicit TMDB selection."""

import re
from pathlib import PurePosixPath
from app.cloud.errors import CloudError, UnrecognizedMedia

VIDEO = {
    ".mkv",
    ".mp4",
    ".avi",
    ".mov",
    ".wmv",
    ".ts",
    ".m2ts",
    ".iso",
    ".webm",
    ".flv",
}
SIDECAR = {
    ".srt",
    ".ass",
    ".ssa",
    ".sub",
    ".idx",
    ".vtt",
    ".nfo",
    ".jpg",
    ".png",
    ".json",
}


def safe_name(value):
    value = re.sub(r'[\\/\x00-\x1f<>:"|?*]', " ", str(value))
    value = re.sub(r"\s+", " ", value).strip(" .")
    if not value or len(value.encode()) > 200:
        raise CloudError("文件名称为空或超过文件系统限制")
    return value


def key(value):
    return re.sub(r"[^\w]", "", str(value).casefold())


def source_label(parsed, name):
    value = parsed.get("source")
    if isinstance(value, list) or not value:
        return ""
    if str(value).casefold() == "web":
        if re.search(r"(?i)(?:^|[. _-])web[. _-]?dl(?:$|[. _-])", name):
            return "WEB-DL"
        if re.search(r"(?i)(?:^|[. _-])web[. _-]?rip(?:$|[. _-])", name):
            return "WEBRip"
        return "WEB"
    return str(value)


def tmdb():
    from app.tmdb import Tmdb

    client = Tmdb()
    if not client.key:
        raise CloudError("请先配置 TMDB 密钥", 409)
    return client


def search(query, kind="movie", year=None):
    if kind not in ("movie", "tv") or not 1 < len(query) < 150:
        raise CloudError("查询类型或名称无效")
    params = {"query": query, "include_adult": "false"}
    if year:
        params["year" if kind == "movie" else "first_air_date_year"] = str(year)
    try:
        response = tmdb().get("/search/" + kind, **params)
        return response.get("results", [])[:20]
    except CloudError:
        raise
    except Exception:
        raise CloudError("TMDB 查询失败，请检查网络和密钥", 502) from None


def recognize(name, override=None, naming_config=None, probe=None):
    from guessit import guessit

    from app.cloud import settings, naming as templates, categories
    naming = naming_config or settings.get()
    parsed = guessit(name)
    cn = re.search(r"第\s*(\d{1,2})\s*季.{0,20}?第\s*(\d{1,3})\s*[集话]", name)
    season = parsed.get("season")
    episode = parsed.get("episode")
    if cn:
        season, episode = int(cn[1]), int(cn[2])
    if override:
        if override.get("season") is not None:
            season = override["season"]
        if override.get("episode") is not None:
            episode = override["episode"]
    if isinstance(season, list) or isinstance(episode, list):
        raise UnrecognizedMedia("多季或跨集视频需要人工处理")
    kind = (override or {}).get("kind") or ("tv" if episode is not None else "movie")
    if kind not in ("movie", "tv"):
        raise CloudError("作品类别无效")
    if kind == "tv" and (not isinstance(season, int) or not isinstance(episode, int)):
        raise UnrecognizedMedia("季集号无法确定，请在修正作品处填写季集号后重新预览")
    if kind == "tv" and (not 0 <= season <= 99 or not 1 <= episode <= 999):
        raise CloudError("季集号超出支持范围")
    if override and override.get("tmdb_id"):
        try:
            detail = tmdb().get("/" + kind + "/" + str(int(override["tmdb_id"])))
        except Exception:
            raise CloudError("所选 TMDB 作品查询失败", 502) from None
    else:
        title = str(parsed.get("title") or "")
        if not title:
            raise UnrecognizedMedia("作品名称无法确定，请选择 TMDB 作品")
        matches = search(title, kind, parsed.get("year"))
        exact = [
            x
            for x in matches
            if key(title)
            in {
                key(x.get("title") or x.get("name")),
                key(x.get("original_title") or x.get("original_name")),
            }
        ]
        if parsed.get("year"):
            exact = [
                x
                for x in exact
                if str(x.get("release_date") or x.get("first_air_date") or "")[:4]
                == str(parsed["year"])
            ]
        if len(exact) != 1:
            raise UnrecognizedMedia("作品识别不唯一，请从 TMDB 候选中选择")
        detail = exact[0]
    if naming["classification_enabled"] and not (override and override.get("tmdb_id")):
        try:
            complete = tmdb().get("/"+kind+"/"+str(detail["id"]))
            if str(complete.get("id")) != str(detail["id"]):
                raise ValueError()
            detail = complete
        except Exception:
            raise CloudError("TMDB 分类详情查询失败，保留源文件", 502) from None
    if override and override.get("tmdb_id") and str(detail.get("id")) != str(override["tmdb_id"]):
        raise CloudError("TMDB 返回作品身份与所选条目不同", 502)
    title = safe_name(detail.get("title") or detail.get("name") or "")
    year = str(detail.get("release_date") or detail.get("first_air_date") or "")[:4]
    if not year.isdecimal():
        raise UnrecognizedMedia("作品缺少年份，请人工核对")
    tmdb_id = str(detail["id"])
    episode_detail = {}
    if kind == "tv":
        try:
            episode_detail = tmdb().get(f"/tv/{tmdb_id}/season/{season}/episode/{episode}")
        except Exception:
            raise CloudError("TMDB 季集查询失败，保留源文件", 502) from None
        if not isinstance(episode_detail, dict):
            raise CloudError("TMDB 季集响应不完整，保留源文件", 502)
        if not episode_detail.get("id") or episode_detail.get("season_number") != season or episode_detail.get("episode_number") != episode:
            raise UnrecognizedMedia("TMDB 季集信息无法核实，请检查季集号后重新预览")
    information = probe() if callable(probe) else (probe or {"status": "filename", "fields": {}})
    probe_fields = information.get("fields", {})
    for target, source in (("screen_size", "resolution"), ("video_codec", "video_codec"), ("audio_codec", "audio_codec")):
        if probe_fields.get(source):
            parsed[target] = probe_fields[source]
    base = f"{title} ({year}) [tmdb-{tmdb_id}]"
    ext = PurePosixPath(name).suffix.lower()
    tags = []
    for field in ("screen_size", "video_codec", "source", "audio_codec"):
        value = parsed.get(field)
        if value and not isinstance(value, list):
            tags.append(safe_name(str(value)))
    for field in ("dynamic_range", "fps", "bitdepth"):
        if probe_fields.get(field):
            tags.append(safe_name(probe_fields[field]))
    quality = " - " + " ".join(tags) if tags else ""
    stem = base + (f" - S{season:02d}E{episode:02d}" if kind == "tv" else "") + quality
    if naming["naming_style"] == "compact":
        stem = (
            f"{title} ({year})"
            + (f" - S{season:02d}E{episode:02d}" if kind == "tv" else "")
            + quality
        )
    filename = (
        safe_name(name)
        if naming["naming_style"] == "original"
        else safe_name(stem + ext)
    )
    classification = categories.decide(naming, detail, kind, override)
    values = {
        "kind": kind, "title": title, "original_title": detail.get("original_title") or detail.get("original_name") or title,
        "year": year, "tmdb_id": tmdb_id, "season": season, "episode": episode,
        "episode_title": episode_detail.get("name", ""), "resolution": str(parsed.get("screen_size") or ""),
        "source": source_label(parsed, name), "codec": str(parsed.get("video_codec") or ""),
        "video_codec": str(parsed.get("video_codec") or ""), "audio_codec": str(parsed.get("audio_codec") or ""),
        "release_group": str(parsed.get("release_group") or ""), "ext": ext, "original_name": name,
        **classification,
    }
    from app.domain.quality import recognize_dims
    dimensions = recognize_dims(name)
    for target, source in (("dynamic_range", "dolby"), ("fps", "fps"), ("bitdepth", "bitdepth")):
        value = dimensions[source]
        values[target] = "" if value == "其他 / 未识别" else value
    values.update({k: v for k, v in probe_fields.items() if k in templates.FIELDS and v})
    relative = f'{categories.prefix(naming, values)}/{base}'
    if kind == "tv":
        relative += f"/Season {season:02d}"
    if naming["naming_style"] == "template":
        custom = templates.paths(naming, values)
        relative = categories.prefix(naming, values) + "/" + custom["directory"]
        filename = custom["name"]
    if not naming["rename_enabled"]:
        filename = str(name)
    return {
        **classification,
        "media_info": information,
        "naming_fields": {k:v for k,v in values.items() if k in templates.FIELDS},
        "kind": kind,
        "tmdb_id": tmdb_id,
        "title": title,
        "year": year,
        "season": season,
        "episode": episode,
        "directory": relative,
        "name": filename,
        "poster_path": detail.get("poster_path") or "",
        "plot": str(detail.get("overview") or "")[:10000],
        "episode_id": str(episode_detail.get("id") or ""),
        "episode_title": str(episode_detail.get("name") or ""),
        "episode_plot": str(episode_detail.get("overview") or "")[:10000],
        "aired": str(episode_detail.get("air_date") or ""),
    }
