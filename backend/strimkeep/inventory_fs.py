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
import os
from pathlib import Path

SYNTHETIC_PREFIX = 'fs-'


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
                          ('分享影视库/%',)):
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
        base = base_root / name
        if base.is_symlink():
            raise ValueError('库根目录不可核实')
        if not base.is_dir():
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
