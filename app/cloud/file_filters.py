"""Organizer selection; regex matching runs in a time-limited child process."""

import json
import re
import subprocess
import sys
from pathlib import PurePosixPath
from app.cloud.errors import CloudError

KEYS = ("video_extensions", "metadata_extensions", "min_video_mb", "ignore_keywords", "ignore_regex")


def validate(config):
    for key in ("video_extensions", "metadata_extensions"):
        values = config[key]
        if not isinstance(values, list) or not 1 <= len(values) <= 40 or any(not isinstance(v, str) or not re.fullmatch(r"\.[a-z0-9]{1,12}", v) or v == ".strm" for v in values) or len(set(values)) != len(values):
            raise CloudError("扩展名应为不重复的小写列表，例如 .mkv；不能包含 .strm")
    if set(config["video_extensions"]) & set(config["metadata_extensions"]):
        raise CloudError("视频与附属文件扩展名不能重叠")
    size = config["min_video_mb"]
    if isinstance(size, bool) or not isinstance(size, (float, int)) or not 0 <= size <= 1000000:
        raise CloudError("最小视频大小应为 0 至 1000000 MB，0 表示不限制")
    for key, count, length in (("ignore_keywords", 100, 80), ("ignore_regex", 20, 160)):
        values = config[key]
        if not isinstance(values, list) or len(values) > count or any(not isinstance(v, str) or not v.strip() or len(v) > length or any(ord(c) < 32 for c in v) for v in values):
            raise CloudError("忽略关键词或正则列表无效")
    try:
        for value in config["ignore_regex"]:
            re.compile(value, re.I)
    except re.error:
        raise CloudError("忽略正则语法错误，请检查表达式") from None


def regex_matches(patterns, rows, with_rules=False):
    if not patterns or not rows:
        return {} if with_rules else set()
    # No user expressions execute as Python; expensive regex is killed as a process.
    code = "import json,re,sys;p,rows=json.load(sys.stdin);rules=[re.compile(v,re.I) for v in p];json.dump([(i,next(p[j] for j,r in enumerate(rules) if r.search(n))) for i,n in rows if any(r.search(n) for r in rules)],sys.stdout)"
    try:
        process = subprocess.run([sys.executable, "-I", "-c", code], input=json.dumps([patterns, [(r["id"], r["name"]) for r in rows]]), capture_output=True, text=True, timeout=3, check=False)
        if process.returncode:
            raise ValueError()
        matches = dict(json.loads(process.stdout))
        return matches if with_rules else set(matches)
    except Exception:
        raise CloudError("忽略正则执行超时或失败；未移动文件，请简化表达式", 409) from None


def select(rows, config):
    from app.cloud.recognition import VIDEO
    accepted, filtered = [], []
    for row in rows:
        extension = PurePosixPath(row["name"]).suffix.lower()
        reason, rule = "", ""
        if extension not in config["video_extensions"]:
            if extension in VIDEO or extension == ".strm":
                reason = "未选择的视频扩展名" if extension != ".strm" else "STRM 不是源视频"
                rule = '当前扩展名 '+extension+'；允许 '+', '.join(config['video_extensions'])
            else:
                continue
        elif row["size"] < config["min_video_mb"]*1048576:
            reason = "小于最小视频大小"
            rule = '最小 '+str(config['min_video_mb'])+' MB；实际 '+str(round(row['size']/1048576, 3))+' MB'
        elif any(v.casefold() in row["name"].casefold() for v in config["ignore_keywords"]):
            reason = "命中忽略关键词"
            rule = next(v for v in config['ignore_keywords'] if v.casefold() in row['name'].casefold())
        if reason:
            filtered.append({"file_id": row["id"], "name": row["name"], "reason": reason, "rule": rule, "action": "filtered"})
        else:
            accepted.append(row)
    matches = regex_matches(config["ignore_regex"], accepted, with_rules=True)
    for row in accepted:
        if row["id"] in matches:
            filtered.append({"file_id": row["id"], "name": row["name"], "reason": "命中忽略正则", "rule": matches[row['id']], "action": "filtered"})
    return [r for r in accepted if r["id"] not in matches], filtered
