"""Shared prerequisites for manual, transferred and scheduled media workflows."""

import tempfile
from urllib.parse import urlsplit
from app.cloud import settings, recognition, directories
from app.cloud.errors import CloudError


def output_ready(kind="personal"):
    """Check before remote writes, and again when a queued task starts."""
    from app.cloud import strm
    config = settings.get()
    parsed = urlsplit(config["public_url"])
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise CloudError("请在「STRM 生成 → 输出与播放地址」保存播放器可访问的服务地址", 409)
    base = strm.root(kind)
    try:
        # Access flags alone do not prove that a Docker mount permits writes.
        with tempfile.TemporaryFile(dir=base) as probe:
            probe.write(b"strimkeep-output-check")
            probe.flush()
    except OSError:
        raise CloudError("STRM 输出目录不可写，请检查「STRM 生成 → 输出与播放地址」和 Docker 挂载权限", 409) from None
    return str(base)


def prepare(generate=True):
    recognition.tmdb()
    if generate and settings.get()["generate_strm_enabled"]:
        output_ready()


def status(account_id):
    """Local configuration checks only; no cloud mutation or network health claim."""
    config = settings.get()
    roots = directories.get(account_id)["directories"]
    checks = []

    def add(key, label, page, module, action):
        try:
            value = action()
            checks.append(dict(key=key, label=label, ready=True, detail=str(value or "已配置"), page=page, module=module))
        except CloudError as error:
            checks.append(dict(key=key, label=label, ready=False, detail=str(error), page=page, module=module))

    def folder(role):
        row = roots.get(role)
        if not row:
            raise CloudError("尚未选择目录", 409)
        return row["name"]

    def tmdb_configured():
        from app import engine
        # The dashboard must not instantiate Tmdb and load its entire cache.
        if not (engine.RUNTIME_CFG.get("tmdb_key") or "").strip():
            raise CloudError("请在规则设置中保存 TMDB 密钥", 409)
        return "密钥已配置，连接以实际识别结果为准"

    add("transfer", "转存暂存目录", "transfer", "receive", lambda: folder("transfer"))
    add("source", "待整理来源", "organize", "organize-directories", lambda: folder("source"))
    add("target", "网盘归档目录", "organize", "organize-directories", lambda: folder("target"))
    add("tmdb", "TMDB 识别", "settings", "", tmdb_configured)
    add("output", "NAS STRM 输出", "strm", "strm-output", lambda: output_ready())
    required = {"source", "target", "tmdb"} | ({"output"} if config["generate_strm_enabled"] else set())
    return {"checks": checks, "ready": all(c["ready"] for c in checks if c["key"] in required),
            "generate": config["generate_strm_enabled"], "public_url": config["public_url"],
            "output_root": config["output_root"], "naming_style": config["naming_style"]}
