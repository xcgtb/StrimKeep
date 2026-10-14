"""Web settings shared by the workbench and optional playback listener."""

import os
from pathlib import Path
from urllib.parse import urlsplit
from app import state_store, storage
from app.cloud.errors import CloudError

DEFAULTS = {
    "public_url": "",
    "output_root": "/media/local",
    "emby_output_root": "",
    "proxy_enabled": False,
    "proxy_port": 8322,
    "nfo_enabled": True,
    "artwork_enabled": False,
    "notify_enabled": False,
    "open_app_id": "",
    "naming_style": "standard",
    "movie_folder": "电影",
    "tv_folder": "电视剧",
    "official_delete_enabled": False,
}


def path():
    return storage._diagnostic_dir() / "cloud_workbench_settings.json"


def get():
    result = dict(DEFAULTS)
    result.update(state_store.read(path(), {}))
    if not result["open_app_id"]:
        result["open_app_id"] = os.environ.get("STRIMKEEP_115_APP_ID", "").strip()
    return result


def save(value):
    if not isinstance(value, dict) or set(value) - set(DEFAULTS):
        raise CloudError("配置字段无效")
    result = get()
    result.update(value)
    from app.cloud.provider115 import numeric

    if result["open_app_id"]:
        result["open_app_id"] = numeric(result["open_app_id"])
    if result["naming_style"] not in ("standard", "compact", "original"):
        raise CloudError("命名样式无效")
    for field in ("movie_folder", "tv_folder"):
        text = result[field]
        if (
            not isinstance(text, str)
            or not text.strip()
            or text != text.strip()
            or text in (".", "..")
            or any(ord(c) < 32 or c in '/\\<>:"|?*' for c in text)
            or text.startswith(".")
            or text.endswith(".")
            or len(text.encode()) > 80
        ):
            raise CloudError("归档分类应为一个有效的目录名")
    raw_url = str(result["public_url"])
    try:
        parsed = urlsplit(raw_url)
        parsed.port
    except ValueError:
        raise CloudError("播放地址中的主机或端口无效") from None
    if len(raw_url) > 2048 or any(ord(c) <= 32 for c in raw_url):
        raise CloudError("播放地址包含空白或无效字符")
    if result["public_url"] and (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise CloudError("播放地址应为 http(s)://主机:端口，可带部署子路径")
    for key in ("output_root", "emby_output_root"):
        raw = str(result[key])
        if raw and (
            not raw.startswith("/") or "\x00" in raw or ".." in Path(raw).parts
        ):
            raise CloudError("请填写有效的绝对目录路径")
    if result["output_root"] in ("", "/", "/data", "/media"):
        raise CloudError("请选择用于存放 STRM 的独立目录")
    try:
        port = int(result["proxy_port"])
    except (ValueError, TypeError):
        raise CloudError("反代端口应为数字") from None
    if not 1024 <= port <= 65535 or port == 8321:
        raise CloudError("反代端口必须为空闲端口且不同于管理端口")
    result["proxy_port"] = port
    for key in (
        "proxy_enabled",
        "nfo_enabled",
        "artwork_enabled",
        "notify_enabled",
        "official_delete_enabled",
    ):
        if not isinstance(result[key], bool):
            raise CloudError("开关配置必须为布尔值")
    state_store.save(path(), result)
    return result
