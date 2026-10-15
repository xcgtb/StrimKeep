"""Allowlisted filename templates, with no expression evaluation."""

import re
from pathlib import PurePosixPath
from string import Formatter
from app.cloud.errors import CloudError

DEFAULT_TEMPLATES = {
    "movie_directory_template": "{title} ({year}) [tmdb-{tmdb_id}]",
    "tv_directory_template": "{title} ({year}) [tmdb-{tmdb_id}]",
    "season_directory_template": "Season {season:02d}",
    "movie_file_template": "{title}.{year}.{resolution}.{source}.{codec}{ext}",
    "tv_file_template": "{title}.{year}.S{season:02d}E{episode:02d}.{resolution}.{source}.{codec}{ext}",
}
FIELDS = {
    "title", "original_title", "year", "tmdb_id", "season", "episode",
    "episode_title", "resolution", "source", "codec", "video_codec",
    "audio_codec", "dynamic_range", "fps", "bitdepth", "release_group",
    "ext", "original_name", "category", "region",
    "width", "height", "audio_channels", "audio_layout", "duration",
}
NUMBERS = {"season", "episode"}


def fields(template):
    if not isinstance(template, str) or not template.strip() or len(template) > 500:
        raise CloudError("命名模板不能为空或超过 500 字符")
    if any(ord(c) < 32 or c in '/\\<>:"|?*' for c in template):
        # Colon is allowed only in season/episode format specifications.
        cleaned = re.sub(r"\{(?:season|episode):(?:02d?|03d?)\}", "", template)
        if any(ord(c) < 32 or c in '/\\<>:"|?*' for c in cleaned):
            raise CloudError("模板应为单个目录名或文件名，不能含路径和非法字符")
    result = set()
    try:
        for literal, key, spec, conversion in Formatter().parse(template):
            if key is None:
                continue
            if key not in FIELDS or conversion or (spec and (key not in NUMBERS or spec not in {"02", "02d", "03", "03d"})):
                raise CloudError("模板含未知字段或格式，只允许列出的变量和季集数字格式")
            result.add(key)
    except ValueError:
        raise CloudError("模板括号不匹配；字面大括号请写成 {{ 和 }}") from None
    if not result:
        raise CloudError("命名模板至少包含一个变量")
    return result


def validate(config):
    for key in DEFAULT_TEMPLATES:
        used = fields(config[key])
        if key.endswith("file_template") and "ext" not in used:
            raise CloudError("视频文件模板必须包含 {ext}，保持真实扩展名")
        if key.startswith("movie_") and used & NUMBERS:
            raise CloudError("电影模板不能使用季集号")
        if key == "season_directory_template" and "season" not in used:
            raise CloudError("季目录模板必须包含 {season}")


def clean(value):
    value = re.sub(r'[\\/\x00-\x1f<>:"|?*]', " ", str(value))
    value = re.sub(r"\s+", " ", value).strip(" .")
    if not value or value in {".", ".."} or len(value.encode()) > 200:
        raise CloudError("模板生成的名称为空或超过文件名限制")
    if re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", value):
        raise CloudError("模板生成了文件系统保留名称")
    return value


def filename(value):
    """Preserve readable titles and caller-authored template separators."""
    return clean(re.sub(r"\.{2,}", ".", clean(value)))


def normalize_fields(values):
    result = dict(values)
    source = str(result.get("source") or "")
    source = {"ultra hd blu-ray": "BluRay", "blu-ray": "BluRay"}.get(source.casefold(), source)
    if source == "BluRay" and re.search(r"(?i)(?:^|[. _-])remux(?:$|[. _-])", str(result.get("original_name") or "")):
        source += " REMUX"
    result["source"] = source
    result["codec"] = {"h265": "H.265", "h264": "H.264", "hevc": "H.265", "avc": "H.264"}.get(str(result.get("codec") or "").casefold(), result.get("codec") or "")
    depth = str(result.get("bitdepth") or "")
    result["bitdepth"] = re.sub(r"(?i)^(\d+)[ -]?bit$", r"\1-bit", depth)
    dynamic = str(result.get("dynamic_range_label") or result.get("dynamic_range") or "")
    if dynamic.casefold() == "dv" or dynamic.casefold().startswith("dolby vision"):
        profile = re.search(r"(?i)profile\s+([1-9]\d?)\b", dynamic) or re.search(r"(?i)(?:dovi|dv|dolby[. _-]*vision)[. _-]*p([1-9]\d?)(?:$|[. _-])", str(result.get("original_name") or ""))
        dynamic = "DoVi" + (" P" + profile[1] if profile else "")
    elif dynamic == "HDR / HLG":
        dynamic = "HLG" if re.search(r"(?i)\bHLG\b", str(result.get("original_name") or "")) else "HDR"
    result["dynamic_range"] = dynamic
    result["audio_codec"] = str(result.get("audio_codec_label") or result.get("audio_codec") or "")
    return result


def standard_filename(values, *, compact=False):
    values = normalize_fields(values)
    title = values["title"] + " (" + values["year"] + ")"
    if values["kind"] == "tv":
        title += f" - S{int(values['season']):02d}E{int(values['episode']):02d}"
    keys = ("resolution", "codec") if compact else ("resolution", "source", "dynamic_range", "codec", "bitdepth", "fps")
    parts = [str(values[k]) for k in keys if values.get(k)]
    if not compact and values.get("audio_codec"):
        parts.append(str(values["audio_codec"]) + (" " + str(values["audio_layout"]) if values.get("audio_layout") else ""))
    stem = title + (" - " + ".".join(parts) if parts else "")
    if not compact and values.get("release_group"):
        stem += "-" + str(values["release_group"])
    return filename(stem + values["ext"])


def render(template, values):
    fields(template)
    output = []
    for literal, key, spec, conversion in Formatter().parse(template):
        output.append(literal)
        if key is not None:
            value = values.get(key)
            if value is None or value == "":
                if key in NUMBERS:
                    raise CloudError("模板需要的季集信息缺失", 409)
                output.append("")
            elif key in NUMBERS:
                output.append(format(int(value), spec if spec.endswith('d') else spec+'d' if spec else 'd'))
            else:
                output.append(str(value))
    value = "".join(output)
    value = re.sub(r"([._-])\1+", r"\1", value)
    if "ext" in values:
        value = re.sub(r"[._-]+(?="+re.escape(values["ext"])+r"$)", "", value)
    value = re.sub(r"\s+", " ", value).strip(" ._-")
    return clean(value)


def paths(config, values):
    kind = values["kind"]
    prefix = "tv" if kind == "tv" else "movie"
    directory = render(config[prefix+"_directory_template"], values)
    if kind == "tv":
        directory += "/" + render(config["season_directory_template"], values)
    if config["rename_enabled"] and config["naming_style"] == "template":
        name = filename(render(config[prefix+"_file_template"], values))
        if PurePosixPath(name).suffix.lower() != values["ext"].lower():
            raise CloudError("模板不能改变视频的真实扩展名")
    else:
        name = values["original_name"]
    return {"directory": directory, "name": name}


def preview(config, kind="tv"):
    from app.cloud import categories
    values = {
        "kind": kind, "title": "金色" if kind == "tv" else "我不是药神",
        "original_title": "Example", "year": "2026" if kind == "tv" else "2018",
        "tmdb_id": "123456", "season": 1, "episode": 1, "episode_title": "第一集",
        "resolution": "2160p", "source": "WEB-DL", "codec": "H265",
        "video_codec": "H265", "audio_codec": "DD+", "dynamic_range": "HDR10",
        "fps": "24", "bitdepth": "10bit", "release_group": "Example",
        "ext": ".mkv", "original_name": "original.S01E01.mkv", "region": "中国大陆", "width": "3840", "height": "2160", "audio_channels": "6", "audio_layout": "5.1", "duration": "2700.000",
    }
    category = "剧集" if kind == "tv" else "电影"
    if config["classification_enabled"] and category not in categories.enabled(config):
        category = categories.enabled(config)[0]
    classification = categories.decide(config, {"origin_country": ["CN"]}, kind, {"category": category} if config["classification_enabled"] and category in categories.enabled(config) else None)
    values.update(classification)
    result = paths(config, values)
    return {"kind": kind, "values": values, "directory": categories.prefix(config, values)+"/"+result["directory"], "name": result["name"], "sample": True}
