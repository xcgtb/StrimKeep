"""文件系统才是库存事实：扫两个库根，把落地的 STRM 变成库存行。

这层的目的只有一个：**不让登记表决定「库里有什么」**。台账
（``organized_files`` / ``share_strms``）从此只做来源索引——
能在索引里找到就带上真实登记编号，找不到就用 ``fs-`` 前缀的合成编号，
含义是「本地有这个文件，来源尚未登记」。

这样三件事同时成立：

1. 台账缺行、被撤销、被清空，作品也不会从片库映射里消失；
2. 磁盘上手动放进去的 STRM 也能参与缺集统计和质量比较；
3. 单条异常只影响它自己那个文件，不再把整页治理拦掉。

删除安全不受影响：合成编号的**网盘**文件没有可靠来源证据，仍然不允许
删除它的 115 原件；分享侧以本地文件和文件指纹为准（见 ``inventory_sources``）。
"""
import hashlib
import json
import os
import stat
from pathlib import Path, PurePosixPath

from .organize_options import library_root

SYNTHETIC_PREFIX = 'fs-'
SHARE_ROOT = '分享影视库'


def library_path(root, relative):
    parts = str(relative).split('/')
    if any(part in ('', '.', '..') or '\\' in part or (len(part) > 1 and part[1] == ':') for part in parts):
        raise ValueError('库根目录名称无效')
    current = Path(root)
    for part in parts:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            return None
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError('库根目录不可核实')
    return current


def resolve_roots(root, store):
    """Resolve the written STRM path from the archive selection or verified output paths."""
    config = store.get('organize_config', {}) or {}
    if not isinstance(config, dict):
        raise ValueError('整理配置无效')
    name = library_root(config)
    roots = {'cloud': name, 'share': SHARE_ROOT}
    if name == SHARE_ROOT:
        raise ValueError('网盘库与分享库不能使用同一目录')
    if config.get('library_dir_name'):
        return roots
    configured = library_path(root, name)
    if configured is not None and next(configured.iterdir(), None) is not None:
        return roots

    archive = str(config.get('archive_cid') or '')
    labels = config.get('directory_labels') or {}
    label = labels.get(archive, '') if isinstance(labels, dict) else ''
    trail_names = [part.strip() for part in str(label).split('/')]
    if trail_names and trail_names[0] in ('115 网盘', '115网盘'):
        trail_names = trail_names[1:]
    if len(trail_names) > 1 and trail_names[0] != SHARE_ROOT and all(trail_names):
        mapped = '/'.join(trail_names)
        if library_path(root, mapped) is not None:
            return {**roots, 'cloud': mapped}

    candidates = set()
    rows = store.db.execute("""SELECT o.relative_path,o.trail FROM output_registry o
        JOIN owned_outputs f ON f.relative_path=o.relative_path AND f.account_id=o.account_id AND f.file_id=o.file_id
        WHERE o.active=1""")
    for relative, raw_trail in rows:
        path = PurePosixPath(relative)
        if path.is_absolute() or '..' in path.parts or '\\' in relative or path.suffix.lower() != '.strm':
            continue
        parts = path.parts[:-1]
        if not parts or parts[0] == SHARE_ROOT:
            continue
        try:
            trail = json.loads(raw_trail)
        except (TypeError, ValueError):
            continue
        if not isinstance(trail, list):
            continue
        trail = [str(cid) for cid in trail if str(cid) != '0']
        if archive and archive not in trail:
            continue
        depth = 1
        if archive and len(trail) == len(parts):
            depth = trail.index(archive) + 1
        elif archive and trail[0] != archive:
            continue
        candidate = '/'.join(parts[:depth])
        if candidate in candidates:
            continue
        try:
            parent = library_path(root, '/'.join(parts))
            if parent is None or not stat.S_ISREG((parent / path.name).lstat().st_mode):
                continue
        except (OSError, ValueError):
            continue
        candidates.add(candidate)
        if len(candidates) > 1:
            return roots
    return {**roots, 'cloud': next(iter(candidates))} if candidates else roots


def check_missing_registrations(root, roots, db):
    for side, statement in (
        ('cloud', 'SELECT 1 FROM organized_files LIMIT 1'),
        ('share', "SELECT 1 FROM share_strms WHERE revoked=0 AND strm_path LIKE '分享影视库/%' LIMIT 1"),
    ):
        if library_path(root, roots[side]) is None and db.execute(statement).fetchone():
            raise ValueError('库目录未找到，仍有已登记 STRM，请检查挂载：' + roots[side])


def is_synthetic(record_id):
    """合成编号 = 「磁盘上有、台账里没有」。"""
    return isinstance(record_id, str) and record_id.startswith(SYNTHETIC_PREFIX)


def synthetic_id(side, relative):
    raw = ('%s:%s' % (side, relative)).encode('utf-8')
    return SYNTHETIC_PREFIX + hashlib.sha256(raw).hexdigest()[:24]


def registry_index(db):
    """台账索引：{(side, 相对路径): 登记编号}。只做关联，不做存在性判断。"""
    index = {}
    if db is None:
        return index
    for row in db.execute('SELECT file_id,relative_path FROM organized_files'):
        index[('cloud', row['relative_path'])] = str(row['file_id'])
    for row in db.execute("SELECT id,strm_path FROM share_strms WHERE revoked=0 AND strm_path LIKE ?",
                          (SHARE_ROOT + '/%',)):
        index[('share', row['strm_path'])] = str(row['id'])
    return index


def scan(root, roots, db=None):
    """按目录扫描两库，返回 `[(side, record_id, 相对路径)]`。

    只认普通文件：符号链接、读不到的目录直接报错（fail closed），
    免得「漏扫」被当成「库里没有」。
    """
    base_root = Path(root)
    if not base_root.is_dir() or base_root.is_symlink():
        raise ValueError('STRM 根目录不可用')
    registry = registry_index(db)
    rows = []

    def onerror(exc):
        raise exc

    for side, name in roots.items():
        base = library_path(base_root, name)
        if base is None:
            continue
        for current, dirs, files in os.walk(base, followlinks=False, onerror=onerror):
            current_path = Path(current)
            dirs[:] = sorted(folder for folder in dirs
                             if not (current_path / folder).is_symlink())
            for filename in files:
                if Path(filename).suffix.casefold() != '.strm':
                    continue
                path = current_path / filename
                if path.is_symlink() or not path.is_file():
                    continue
                relative = path.relative_to(base_root).as_posix()
                record_id = registry.get((side, relative)) or synthetic_id(side, relative)
                rows.append((side, record_id, relative))
    rows.sort(key=lambda row: (row[0], row[2]))
    return rows
