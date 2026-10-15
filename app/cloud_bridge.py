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
    if owned(path):
        return service().preview(path, root, local)
    if local:
        from importlib import import_module
        engine = import_module('.engine', __package__) if __package__ else import_module('engine')
        if Path(root) != Path(engine.L_ROOT) and Path(root) in governance_roots(True):
            return {'mode':'115_file_id','blocked':'自生成库的引用未登记来源，禁止按路径兜底删除'}
    return None


def snapshots(paths, root, local=True):
    return {
        str(p): value for p in paths if (value := preview(p, root, local)) is not None
    }


def governance_roots(local=None):
    """Explicitly opted-in generated roots; never infer a broad parent directory."""
    if not __package__:
        service()
    from importlib import import_module
    engine = import_module('.engine', __package__) if __package__ else import_module('engine')
    from app.cloud import settings
    cfg = settings.get()
    result = []
    for is_local, legacy, key in ((True,engine.L_ROOT,'output_root'),(False,engine.S_ROOT,'share_output_root')):
        if local is not None and local != is_local:
            continue
        roots = [Path(legacy)]
        candidate = Path(cfg[key])
        other = Path(cfg['share_output_root' if is_local else 'output_root'])
        other_legacy = Path(engine.S_ROOT if is_local else engine.L_ROOT)
        if cfg['governance_generated_enabled']:
            if (not candidate.is_absolute() or candidate.is_symlink() or '..' in candidate.parts
                or candidate == other or candidate.is_relative_to(other) or other.is_relative_to(candidate)):
                raise ValueError('自生成 STRM 治理根目录无效或相互重叠，已停止')
            if candidate.is_relative_to(other_legacy) or other_legacy.is_relative_to(candidate):
                raise ValueError('自生成目录与另一类原治理库重叠，已停止')
            if not candidate.is_dir():
                raise ValueError('自生成 STRM 治理根目录不可读，已停止，不能当作空库')
            if not candidate.is_relative_to(roots[0]):
                if roots[0].is_relative_to(candidate):
                    raise ValueError('自生成目录包含原治理根目录，已停止以避免扩大删除范围')
                roots.append(candidate)
        result.extend(roots)
    return tuple(dict.fromkeys(result))


def governance_root(path, local):
    roots = [r for r in governance_roots(local) if Path(path).is_relative_to(r)]
    if not roots:
        raise ValueError('路径不属于当前启用的治理库')
    return max(roots, key=lambda r:len(r.parts))


def governance_library(root):
    """Combine library indexes only during an explicit governance scan; no API scan."""
    import copy
    from collections import defaultdict
    from importlib import import_module
    engine = import_module('.engine', __package__) if __package__ else import_module('engine')
    base = engine._get_lib(root)
    local = Path(root) == Path(engine.L_ROOT)
    roots = governance_roots(local)
    extras = [engine._get_lib(r) for r in roots if r != Path(root)]
    if not extras:
        return base
    merged = copy.copy(base)
    merged.mov = defaultdict(list, {k:list(v) for k,v in base.mov.items()})
    merged.tv = defaultdict(lambda:defaultdict(list), {k:defaultdict(list,{sn:list(v) for sn,v in seasons.items()}) for k,seasons in base.tv.items()})
    for key in ('meta','key_tmdb_movie','key_tmdb_tv','path_tmdb'):
        setattr(merged,key,dict(getattr(base,key)))
    for key in ('by_base','tmdb_refs'):
        setattr(merged,key,defaultdict(set,{k:set(v) for k,v in getattr(base,key).items()}))
    for extra in extras:
        for key, values in extra.mov.items(): merged.mov[key].extend(values)
        for key, seasons in extra.tv.items():
            for sn, values in seasons.items(): merged.tv[key][sn].extend(values)
        for key in ('meta','key_tmdb_movie','key_tmdb_tv','path_tmdb'):
            getattr(merged,key).update(getattr(extra,key))
        for key in ('by_base','tmdb_refs'):
            for k,v in getattr(extra,key).items(): getattr(merged,key)[k].update(v)
        merged.strm_count += extra.strm_count
    return merged


def governance_delete(files, local, dry_run=False):
    from importlib import import_module
    engine = import_module('.engine', __package__) if __package__ else import_module('engine')
    # Preflight every root before deleting any group. Extra roots are only for
    # registered local sources; foreign references cannot reach CD2 fallback.
    grouped = {}
    for path in files:
        root = governance_root(path, local)
        legacy = Path(engine.L_ROOT if local else engine.S_ROOT)
        if root != legacy and not owned(path):
            raise ValueError('自生成输出目录含未登记引用，请移出或完成来源绑定后重新预览')
        grouped.setdefault(root,[]).append(path)
    result = {}
    for root, members in grouped.items():
        row = engine.safe_delete_files(members,root,engine.CLOUD_L_ROOT if local else None,dry_run)
        for key,value in row.items():
            if isinstance(value,list):result.setdefault(key,[]).extend(value)
            elif type(value) is int:result[key]=result.get(key,0)+value
    return result


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
