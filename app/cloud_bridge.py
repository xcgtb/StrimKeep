"""Lazy source identities for the Web and legacy CLI deletion entry points."""

from pathlib import Path
import sys
import os
import stat

if __package__:
    from . import storage
else:
    import storage


def service():
    if not __package__:
        # Keep CLI state/config singleton ownership when importing package services.
        parent = str(Path(__file__).resolve().parents[1])
        if parent not in sys.path:
            sys.path.insert(0, parent)
        import app

        for name in ("engine", "config", "storage", "state_store"):
            module = sys.modules.get(name)
            if module is not None:
                sys.modules.setdefault("app." + name, module)
                setattr(app, name, sys.modules["app." + name])
    from app.cloud import governance

    return governance


def owned(path):
    """Include altered registered files, so they cannot fall back to path deletion."""
    with storage._transaction() as con:
        exists = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='cloud_assets'"
        ).fetchone()
        if (
            exists
            and con.execute(
                "SELECT 1 FROM cloud_assets WHERE local_path=?", (str(path),)
            ).fetchone()
        ):
            return True
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
                return (
                    True  # An unverifiable input must not enter source-path fallback.
                )
            return b"/api/cloud/play/" in stream.read(65537)
    except OSError:
        return True


def preview(path, root, local=True):
    return service().preview(path, root, local) if owned(path) else None


def snapshots(paths, root, local=True):
    return {
        str(p): value for p in paths if (value := preview(p, root, local)) is not None
    }


# Source-bound single-title confirmations; legacy source paths keep their original flow.
import threading
import time
import uuid

_confirmations = {}
_confirmation_lock = threading.RLock()


def capture(files, root, local):
    from importlib import import_module

    guard = (
        import_module(".plan_guard", __package__)
        if __package__
        else import_module("plan_guard")
    )
    sources = snapshots(files, root, local)
    if not sources:
        return {}
    if any(v.get("blocked") or not v.get("enabled") for v in sources.values()):
        raise ValueError("115 来源未完成身份或官方删除配置，请在工作台处理后重新预览")
    key = uuid.uuid4().hex
    with _confirmation_lock:
        now = time.monotonic()
        for ident in list(_confirmations):
            if _confirmations[ident]["expires"] < now:
                del _confirmations[ident]
        while len(_confirmations) >= 100:
            del _confirmations[next(iter(_confirmations))]
        _confirmations[key] = {
            "expires": now + 300,
            "root": str(root),
            "local": local,
            "paths": {str(p): guard.fingerprint(p, (root,)) for p in files},
            "sources": sources,
        }
    return {"source_preview_id": key, "cloud_sources": list(sources.values())}


def confirm(key, files, root, local):
    if not key and not any(owned(p) for p in files):
        return
    from importlib import import_module

    guard = (
        import_module(".plan_guard", __package__)
        if __package__
        else import_module("plan_guard")
    )
    with _confirmation_lock:
        old = _confirmations.pop(key, None)
    if (
        not old
        or old["expires"] < time.monotonic()
        or old["root"] != str(root)
        or old["local"] != local
        or old["paths"] != {str(p): guard.fingerprint(p, (root,)) for p in files}
        or old["sources"] != snapshots(files, root, local)
    ):
        raise ValueError("115 来源或确认快照变化、过期，请重新预览后确认删除")
