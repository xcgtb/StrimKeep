"""Web settings shared by the workbench and optional playback listener."""

import os
import copy
import time
import threading
from pathlib import Path
from urllib.parse import urlsplit
from app import state_store, storage
from app.cloud.errors import CloudError
from app.cloud.naming import DEFAULT_TEMPLATES
from app.cloud.categories import REGION_GROUPS
from app.cloud.recognition import VIDEO, SIDECAR
from app.cloud.quality8 import DEFAULT as QUALITY8_DEFAULT

DEFAULTS = {
    "public_url": "",
    "external_url": "",
    "output_root": "/strm/local",
    "emby_output_root": "",
    "share_output_root": "/strm/share",
    "share_emby_output_root": "",
    "proxy_enabled": False,
    "proxy_port": 8322,
    "nfo_enabled": True,
    "artwork_enabled": False,
    "notify_enabled": False,
    "notify_transfer": True,
    "notify_organize": True,
    "notify_strm": True,
    "notify_strm_details": False,
    "notify_channel": True,
    "notify_maintenance": True,
    "emby_refresh_enabled": True,
    "ingest_retry_seconds": 60,
    "ingest_max_attempts": 120,
    "open_app_id": "",
    "naming_style": "standard",
    "movie_folder": "电影",
    "tv_folder": "电视剧",
    "official_delete_enabled": False,
    "governance_generated_enabled": False,
    "rename_enabled": True,
    "classification_enabled": False,
    "enabled_categories": ["电影", "剧集", "动漫", "纪录片", "综艺"],
    "custom_categories": [],
    "region_classification": False,
    "year_classification": False,
    "unknown_enabled": False,
    "region_mode": "country",
    "region_groups": REGION_GROUPS,
    "animation_movie_folder": False,
    "ffprobe_enabled": False,
    "ffprobe_failure": "skip",
    "cleanup_empty_directories": False,
    "strm_delay_seconds": 0,
    "operation_delay_seconds": 1.5,
    "generate_strm_enabled": True,
    "video_extensions": sorted(VIDEO),
    "metadata_extensions": sorted(SIDECAR),
    "min_video_mb": 0,
    # STRM 全量同步（定时）：空字符串=不排期；last_run 由调度器回写
    "strm_scan_cron": "",
    "strm_scan_timezone": "Asia/Shanghai",
    "strm_scan_last_run": 0,
    "strm_scan_last_enqueued": 0,
    "strm_scan_start_mode": "immediate",
    "strm_scan_anchor": 0,
    "strm_scan_pending": False,
    "strm_reconcile_enabled": True,
    # 飞牛媒体探测拦截：命中 Lavf/60.3.100 的直连播放请求返回 403（不含 Emby 反代）
    "strm_block_feiniu_ua": False,
    "ignore_keywords": [],
    "ignore_regex": [],
    "quality8_enabled": False,
    "quality8_mode": "preview",
    "quality8_rules": QUALITY8_DEFAULT,
    "quality8_coexist_dv": True,
    "share_quality_enabled": False,
    "share_quality_mode": "preview",
    "share_quality_rules": QUALITY8_DEFAULT,
    "share_quality_coexist_dv": True,
    **DEFAULT_TEMPLATES,
}
_LOCK = threading.RLock()


def path():
    return storage._diagnostic_dir() / "cloud_workbench_settings.json"


def get():
    result = copy.deepcopy(DEFAULTS)
    result.update(state_store.read(path(), {}))
    if not result["open_app_id"]:
        result["open_app_id"] = os.environ.get("STRIMKEEP_115_APP_ID", "").strip()
    return result


def save(value):
    if not isinstance(value, dict) or set(value) - set(DEFAULTS):
        raise CloudError("配置字段无效")
    with _LOCK:
        previous = get()
        result = validate({**previous, **value})
        if any(previous[k] != result[k] for k in ('strm_scan_cron','strm_scan_timezone','strm_scan_start_mode')):
            result['strm_scan_anchor'] = time.time()
            result['strm_scan_pending'] = bool(result['strm_scan_cron']) and result['strm_scan_start_mode'] == 'immediate'
        state_store.save(path(), result)
        return result


def record_scan(config, now, enqueued=False):
    """A completed scheduler tick cannot reset a concurrently edited schedule."""
    with _LOCK:
        stored = state_store.read(path(), {})
        current = {**copy.deepcopy(DEFAULTS), **stored}
        keys = ('strm_scan_cron','strm_scan_timezone','strm_scan_start_mode','strm_scan_anchor')
        if any(current[k] != config.get(k, DEFAULTS[k]) for k in keys):
            return False
        return state_store.compare_and_save(path(), stored, {**current, 'strm_scan_last_run': now,
                    'strm_scan_last_enqueued': now if enqueued else current['strm_scan_last_enqueued'], 'strm_scan_pending': False})


def validate(result):
    from app.cloud.provider115 import numeric
    from app.cloud import schedules

    cron = result["strm_scan_cron"]
    if not isinstance(cron, str):
        raise CloudError("STRM 全量同步 Cron 应为字符串")
    schedules.zone(result["strm_scan_timezone"])
    if cron.strip():
        schedules.cron_next(cron, result["strm_scan_timezone"], 0)
    result["strm_scan_cron"] = cron.strip()
    for key in ('strm_scan_last_run','strm_scan_last_enqueued'):
        if type(result[key]) not in (int, float) or not 0 <= result[key] < float('inf'):
            raise CloudError("STRM 上次同步时间无效")
    if result['strm_scan_start_mode'] not in ('immediate','scheduled'):
        raise CloudError('定时同步启动方式无效')
    if type(result['strm_scan_anchor']) not in (int,float) or not 0 <= result['strm_scan_anchor'] < float('inf'):
        raise CloudError('定时同步起算时间无效')

    if result["open_app_id"]:
        result["open_app_id"] = numeric(result["open_app_id"])
    if result["naming_style"] not in ("standard", "compact", "original", "template"):
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
    for url_key in ("public_url", "external_url"):
        raw_url = str(result[url_key])
        try:
            parsed = urlsplit(raw_url)
            parsed.port
        except ValueError:
            raise CloudError("播放地址中的主机或端口无效") from None
        if len(raw_url) > 2048 or any(ord(c) <= 32 for c in raw_url):
            raise CloudError("播放地址包含空白或无效字符")
        if result[url_key] and (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise CloudError("播放地址应为 http(s)://主机:端口，可带部署子路径")
    for key in ("output_root", "emby_output_root", "share_output_root", "share_emby_output_root"):
        raw = str(result[key])
        if raw and (
            not raw.startswith("/") or "\x00" in raw or ".." in Path(raw).parts
        ):
            raise CloudError("请填写有效的绝对目录路径")
    if result["output_root"] in ("", "/", "/data", "/media"):
        raise CloudError("请选择用于存放 STRM 的独立目录")
    if result["share_output_root"] in ("", "/", "/data", "/media"):
        raise CloudError("请选择用于存放分享 STRM 的独立目录")
    for left, right in (("output_root", "share_output_root"), ("emby_output_root", "share_emby_output_root")):
        if result[left] and result[right]:
            a, b = Path(result[left]), Path(result[right])
            if a.is_relative_to(b) or b.is_relative_to(a):
                raise CloudError("本地库与分享库输出目录不能相同或相互包含")
    try:
        port = int(result["proxy_port"])
    except (ValueError, TypeError):
        raise CloudError("反代端口应为数字") from None
    if not 1024 <= port <= 65535 or port == 8321:
        raise CloudError("反代端口必须为空闲端口且不同于管理端口")
    result["proxy_port"] = port
    for key in (
        "proxy_enabled",
        "strm_block_feiniu_ua",
        "strm_scan_pending", "strm_reconcile_enabled",
        "nfo_enabled",
        "artwork_enabled",
        "notify_enabled",
        "notify_transfer", "notify_organize", "notify_strm", "notify_strm_details",
        "notify_channel", "notify_maintenance", "emby_refresh_enabled",
        "official_delete_enabled",
        "governance_generated_enabled",
        "rename_enabled", "classification_enabled", "region_classification",
        "year_classification", "unknown_enabled",
        "animation_movie_folder", "ffprobe_enabled",
        "quality8_enabled", "quality8_coexist_dv",
        "share_quality_enabled", "share_quality_coexist_dv",
        "cleanup_empty_directories", "generate_strm_enabled",
    ):
        if not isinstance(result[key], bool):
            raise CloudError("开关配置必须为布尔值")
    if result["ffprobe_failure"] not in ("skip", "filename"):
        raise CloudError("媒体信息提取失败策略无效")
    if result["quality8_mode"] not in ("preview", "upgrade"):
        raise CloudError("整理八维模式无效")
    if result["quality8_mode"] == "upgrade" and not result["quality8_enabled"]:
        raise CloudError("请先启用整理八维策略，再选择执行洗版")
    if result['share_quality_mode'] not in ('preview','upgrade') or result['share_quality_mode'] == 'upgrade' and not result['share_quality_enabled']:
        raise CloudError('请先启用分享库独立比较，再选择执行洗版')
    if type(result["strm_delay_seconds"]) not in (int, float) or not 0 <= result["strm_delay_seconds"] <= 86400:
        raise CloudError("STRM 联动延时应为 0 至 86400 秒")
    if type(result["operation_delay_seconds"]) not in (int,float) or not 0 <= result["operation_delay_seconds"] <= 60:
        raise CloudError("操作间隔应为 0 至 60 秒")
    for key, low, high in (("ingest_retry_seconds", 10, 600), ("ingest_max_attempts", 1, 720)):
        if type(result[key]) is not int or not low <= result[key] <= high:
            raise CloudError("入库核验间隔或次数超出允许范围")
    from app.cloud import naming, categories
    naming.validate(result)
    categories.validate(result)
    from app.cloud import file_filters, quality8
    file_filters.validate(result)
    quality8.validate(result["quality8_rules"])
    quality8.validate(result["share_quality_rules"])
    return result
