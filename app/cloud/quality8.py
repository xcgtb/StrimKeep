"""Independent organizer quality preview. Never authorizes replacing/deleting files."""

import copy
from app.domain.quality import COVER_TIERS_DEFAULT, recognize_dims
from app.cloud.errors import CloudError

ORDER = ["release_group", "source", "resolution", "dynamic_range", "fps", "bitdepth", "audio", "size"]
LABELS = dict(zip(ORDER, ["发布组", "资源类型", "分辨率", "动态范围", "帧率", "色深", "音频格式", "真实视频大小"]))
UNKNOWN = "其他 / 未识别"
PARSER_KEYS = {"dynamic_range": "dolby"}
DEFAULT = [{"key": k, "enabled": k not in ("release_group", "source"), "tiers": copy.deepcopy(COVER_TIERS_DEFAULT.get(PARSER_KEYS.get(k, k), [])), "groups": []} for k in ORDER]


def validate(rules):
    if not isinstance(rules, list) or len(rules) != 8 or any(not isinstance(r, dict) for r in rules):
        raise CloudError("整理八维策略必须包含八个维度")
    if sorted(str(r.get("key")) for r in rules) != sorted(ORDER):
        raise CloudError("整理八维策略维度缺失或重复")
    for r in rules:
        if set(r) != {"key", "enabled", "tiers", "groups"} or not isinstance(r["enabled"], bool):
            raise CloudError("整理八维策略字段无效")
        tiers = COVER_TIERS_DEFAULT.get(PARSER_KEYS.get(r["key"], r["key"]), [])
        if not isinstance(r["tiers"], list) or sorted(str(v) for v in r["tiers"]) != sorted(tiers) or tiers and r["tiers"][-1] != UNKNOWN:
            raise CloudError("画质档位只能重排，未识别必须置后")
        groups = r["groups"]
        if not isinstance(groups, list) or len(groups) > 100 or any(not isinstance(g, str) or not g.strip() or len(g) > 40 or any(ord(c) < 32 for c in g) for g in groups) or len({g.casefold() for g in groups}) != len(groups):
            raise CloudError("发布组列表无效")


def facts(row, media):
    source_name = media.get("quality_source_name") or media.get("naming_fields", {}).get("original_name") or row["name"]
    parsed = recognize_dims(source_name)
    result = {k: parsed[PARSER_KEYS.get(k, k)] for k in ORDER if k != "size"}
    fields = media.get("media_info", {}).get("fields", {})
    for key, field in (("resolution", "resolution"), ("dynamic_range", "dynamic_range"), ("fps", "fps"), ("bitdepth", "bitdepth"), ("audio", "audio_codec")):
        value = fields.get(field)
        if value:
            if key == "audio" and value == "DD":
                value = "AC3"
            result[key] = recognize_dims(value)[PARSER_KEYS.get(key, key)]
    result["size"] = int(row["size"])
    return result


def bucket(values):
    value = values["dynamic_range"]
    return "DV" if value.startswith("Dolby Vision") else ("unknown" if value == UNKNOWN else "non-DV")


def compare(a, b, rules, coexist=False):
    decisions, result, decided = [], 0, None
    a_bucket, b_bucket = bucket(a), bucket(b)
    parallel = coexist and {a_bucket, b_bucket} == {"DV", "non-DV"}
    for rule in rules:
        key = rule["key"]
        left, right = a[key], b[key]
        verdict, reason = "tie", "相同"
        if not rule["enabled"] or key == "release_group" and not rule["groups"]:
            verdict, reason = "skip", "维度关闭或未配置发布组"
        elif parallel:
            verdict, reason = "parallel", "杜比与非杜比分组共存"
        elif decided:
            verdict, reason = "after", "前一维已决定"
        elif key == "size" and (left <= 0 or right <= 0):
            verdict, reason = "skip", "实际视频大小缺失"
        else:
            if key == "size":
                rank_a, rank_b = -left, -right
            elif key == "release_group":
                names = [g.casefold() for g in rule["groups"]]
                rank_a = names.index(left.casefold()) if left.casefold() in names else len(names)
                rank_b = names.index(right.casefold()) if right.casefold() in names else len(names)
            else:
                tiers = rule["tiers"]
                rank_a = tiers.index(left) if left in tiers else len(tiers)
                rank_b = tiers.index(right) if right in tiers else len(tiers)
            if rank_a != rank_b:
                result, decided = (1 if rank_a < rank_b else -1), key
                verdict, reason = ("new" if result == 1 else "existing"), "按该维优先级决定"
        decisions.append({"key": key, "label": LABELS[key], "new": left, "existing": right, "verdict": verdict, "reason": reason})
    return {"result": result, "decided_by": decided, "coexist": parallel, "dimensions": decisions, "suggestion": "建议共存" if parallel else ("新版本更优" if result == 1 else "保留已有版本" if result == -1 else "平局，保留已有版本"), "preview_only": True}


def identity(media):
    return tuple(media.get(k) for k in ("kind", "tmdb_id", "season", "episode"))
