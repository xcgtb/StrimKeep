"""Revalidate one work from a confirmed plan without rescanning both libraries."""
import os
import re
from pathlib import Path, PurePosixPath

from .inventory import registered_rows
from .inventory_snapshot import _fingerprint, _identity
from .inventory_sources import source_fingerprints
from .organize_options import library_root
from .quality import public_policy

_ID = re.compile(r'(?i)\{tmdb-(\d+)\}')


def _work(relative):
    parts = PurePosixPath(relative).parts
    found = [(i, m.group(1)) for i, part in enumerate(parts[:-1]) for m in _ID.finditer(part)]
    if len(found) != 1:
        raise ValueError('作品身份无法核实')
    index, tmdb = found[0]
    return tmdb, PurePosixPath(*parts[:index+1]).as_posix()


def _folder_of(relative):
    try:
        return _work(relative)[1]
    except (TypeError, ValueError):
        return None


def _work_name(relative):
    """作品目录名（含 `{tmdb-…}` 的那一层），两侧共用同一个名字。"""
    folder = _folder_of(relative)
    return PurePosixPath(folder).name if folder else None


def verify_work(engine, expected, action, work_paths=None):
    """Check plan targets, retained evidence and STRM membership for this work.

    目录是事实：作品 STRM 必须在磁盘上、指纹没变、没有新冒出来的文件。
    台账只核对网盘侧来源：分享侧缺登记不影响清理（计划里本来就只有本地文件）。
    """
    from .inventory_api import inventory_config_value
    store, root = engine.store, Path(engine.root)
    config = store.get('organize_config', {}) or {}
    roots = {'cloud': library_root(config), 'share': '分享影视库'}
    policy = {'upgrade': public_policy(store.get('upgrade_policy')),
              'inventory': inventory_config_value(store.get('inventory_config', {})), 'guard_revision': 4}
    if roots != expected.get('roots') or policy != expected.get('policy'):
        raise ValueError('库目录或治理规则已改变')
    if root.is_symlink() or any((root / name).is_symlink() for name in roots.values()):
        raise ValueError('库根目录不可核实')
    identity = {'root': _identity(root), **{side:_identity(root / name) for side, name in roots.items()}}
    if identity != expected.get('identity'):
        raise ValueError('库根目录已改变')
    work_id, work_folder = _work(action['path'])
    work_name = PurePosixPath(work_folder).name
    if work_paths is None:
        files = [item for item in expected['files'] if _work_name(item['path']) == work_name]
    else:
        files = [item for item in expected['files'] if item['path'] in work_paths]
    if not files:
        raise ValueError('计划作品无法核实')
    keys = {(v['side'], v['id'], v['path']) for v in files}
    plan_paths = {v['path'] for v in files}
    folders = {_work(v['path'])[1] for v in files}
    names = {PurePosixPath(folder).name for folder in folders}
    disk = set()

    def onerror(exc):
        raise exc

    for folder in folders:
        base = root.joinpath(*PurePosixPath(folder).parts)
        if base.is_symlink() or not base.is_dir():
            raise ValueError('作品目录已变化')
        for current, subdirs, filenames in os.walk(base, followlinks=False, onerror=onerror):
            for name in subdirs:
                if (Path(current) / name).is_symlink():
                    raise ValueError('作品目录存在符号链接')
            for name in filenames:
                if not name.lower().endswith('.strm'):
                    continue
                path = Path(current) / name
                if path.is_symlink() or not path.is_file():
                    raise ValueError('作品文件不可核实')
                disk.add(path.relative_to(root).as_posix())
    missing = plan_paths - disk
    if missing:
        raise ValueError('待删文件已不在磁盘上：%s' % sorted(missing)[0])
    # 作品的网盘登记必须与计划一致（登记变了，说明来源可能换了）；
    # 分享侧以目录为准，登记缺行不阻止清理。
    registered = [(side, str(record_id), path) for side, record_id, path in registered_rows(store)
                  if _work_name(path) in names]
    registered_paths = {path for _, _, path in registered}
    allowed_extra = registered_paths | {str(item.get('path') or '')
                                        for item in expected.get('unsupported') or ()
                                        if isinstance(item, dict)}
    extra = disk - plan_paths
    if extra - allowed_extra:
        raise ValueError('作品目录成员已变化：出现了无法核实的新文件 %s'
                         % sorted(extra - allowed_extra)[0])
    if {path for side, _, path in registered if side == 'cloud'} != {v['path'] for v in files if v['side'] == 'cloud'}:
        raise ValueError('网盘登记已变化')
    account = engine.index.account({})
    actual_sources = source_fingerprints(store.db, [(v['side'], v['id'], v['path']) for v in files], account)
    expected_sources = [v for v in expected['sources'] if (v['side'], v['id'], v['path']) in keys]
    source_key = lambda v: (v['side'], v['id'], v['path'])
    if sorted(actual_sources, key=source_key) != sorted(expected_sources, key=source_key):
        raise ValueError('作品来源身份已变化')
    for item in files:
        actual = _fingerprint(root, item['path'])
        if any(actual.get(k) != item.get(k) for k in ('sha256','size','mtime_ns','device','inode')):
            raise ValueError('待删文件或保留依据已变化')
    if identity != {'root': _identity(root), **{side:_identity(root / name) for side, name in roots.items()}}:
        raise ValueError('核对期间库根目录发生变化')
    return True
