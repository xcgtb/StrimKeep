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


def recognize(name, override=None, naming_config=None, probe=None, context=None):
    from guessit import guessit

    from app.cloud import settings, naming as templates, categories
    naming = naming_config or settings.get()
    context = context or {}
    parsed = guessit(name)
    cn = re.search(r"第\s*(\d{1,2})\s*季.{0,20}?第\s*(\d{1,3})\s*[集话]", name)
    season = parsed.get("season")
    episode = parsed.get("episode")
    if season is None:
        season = context.get("season")
    if cn:
        season, episode = int(cn[1]), int(cn[2])
    if override:
        if override.get("season") is not None:
            season = override["season"]
        if override.get("episode") is not None:
            episode = override["episode"]
    if isinstance(season, list) or isinstance(episode, list):
        raise UnrecognizedMedia("多季或跨集视频需要人工处理")
    kind = (override or {}).get("kind") or ("tv" if episode is not None else context.get("kind") or "movie")
    if kind not in ("movie", "tv"):
        raise CloudError("作品类别无效")
    if kind == "tv" and (not isinstance(season, int) or not isinstance(episode, int)):
        raise UnrecognizedMedia("季集号无法确定，请在修正作品处填写季集号后重新预览")
    if kind == "tv" and (not 0 <= season <= 99 or not 1 <= episode <= 999):
        raise CloudError("季集号超出支持范围")
    explicit_ids = set(re.findall(r"(?i)[\[{]tmdb-(\d+)[\]}]", name))
    if len(explicit_ids) > 1:
        raise UnrecognizedMedia("文件名包含多个 TMDB 身份，请人工核对")
    selected_id = (override or {}).get("tmdb_id") or next(iter(explicit_ids), None) or context.get("tmdb_id")
    if selected_id:
        try:
            detail = tmdb().get("/" + kind + "/" + str(int(selected_id)))
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
    if naming["classification_enabled"] and not selected_id:
        try:
            complete = tmdb().get("/"+kind+"/"+str(detail["id"]))
            if str(complete.get("id")) != str(detail["id"]):
                raise ValueError()
            detail = complete
        except Exception:
            raise CloudError("TMDB 分类详情查询失败，保留源文件", 502) from None
    if selected_id and str(detail.get("id")) != str(selected_id):
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
            raise CloudError(f"TMDB 季集查询失败（作品 {tmdb_id} / S{season:02d}E{episode:02d}），保留源文件", 502, code='tmdb_episode_request', stage='tmdb_episode') from None
        if episode_detail is None:
            raise CloudError(f"TMDB 未找到所选季集（作品 {tmdb_id} / S{season:02d}E{episode:02d}）；请核对作品 ID 与季集号，保留源文件", 404, code='tmdb_episode_missing', stage='tmdb_episode')
        if not isinstance(episode_detail, dict):
            raise CloudError("TMDB 季集响应格式异常，保留源文件", 502, code='tmdb_episode_schema', stage='tmdb_episode')
        if not episode_detail.get("id") or episode_detail.get("season_number") != season or episode_detail.get("episode_number") != episode:
            raise UnrecognizedMedia("TMDB 季集信息无法核实，请检查季集号后重新预览")
    try:
        information = probe() if callable(probe) else (probe or {"status": "filename", "fields": {}})
    except CloudError as error:
        # The work/episode has already been verified; a later media-read failure
        # must not hide that receipt or suggest repeating the TMDB correction.
        error.recognition = {'title': title, 'year': year, 'kind': kind,
                             'tmdb_id': tmdb_id, 'season': season, 'episode': episode}
        raise
    probe_fields = information.get("fields", {})
    for target, source in (("screen_size", "resolution"), ("video_codec", "video_codec"), ("audio_codec", "audio_codec")):
        if probe_fields.get(source):
            parsed[target] = probe_fields[source]
    base = f"{title} ({year}) {{tmdb-{tmdb_id}}}"
    ext = PurePosixPath(name).suffix.lower()
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
    values.update({k: v for k, v in probe_fields.items() if k in ("dynamic_range_label", "audio_codec_label") and v})
    relative = f'{categories.prefix(naming, values)}/{base}'
    if kind == "tv":
        relative += f"/Season {season:02d}"
    if naming["naming_style"] == "template":
        custom = templates.paths(naming, values)
        relative = categories.prefix(naming, values) + "/" + custom["directory"]
        filename = custom["name"]
    elif naming["naming_style"] == "original":
        filename = safe_name(name)
    else:
        filename = templates.standard_filename(values, compact=naming["naming_style"] == "compact")
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
        # Reuse already-fetched series metadata; no extra season lookup just
        # for a notification, and never infer the total from incoming files.
        "season_episode_count": next((s['episode_count'] for s in (detail.get('seasons') or []) if isinstance(s, dict) and type(s.get('season_number')) is int and s['season_number'] == season and type(s.get('episode_count')) is int and 0 <= s['episode_count'] <= 10000), None) if kind == 'tv' else None,
        "directory": relative,
        "name": filename,
        "poster_path": detail.get("poster_path") or "",
        "backdrop_path": detail.get("backdrop_path") or "",
        "plot": str(detail.get("overview") or "")[:10000],
        "episode_id": str(episode_detail.get("id") or ""),
        "episode_title": str(episode_detail.get("name") or ""),
        "episode_plot": str(episode_detail.get("overview") or "")[:10000],
        "aired": str(episode_detail.get("air_date") or ""),
    }
