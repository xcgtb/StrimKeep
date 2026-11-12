"""Partition the filesystem inventory into a deletable plan, failing per file.

原来的版本要求「目录与台账完全一致」才肯生成计划，一条不一致就整库拒绝——
这正是台账把片库映射和治理绑死的根因。现在的分工是：

* 目录是库存事实（``inventory_fs`` 扫出来的行），所有作品都参与判定；
* 台账只核对**来源身份**：网盘文件必须有可靠登记才能进计划，
  分享文件以本地文件为准，缺登记也能清理；
* 单个文件核不上来源，只是这个文件不进计划（并在预览里说明），
  不再牵连同一部作品的其他文件，更不牵连整个库。
"""
import re
from pathlib import PurePosixPath

from .inventory import _title_identity, project
from .inventory_sources import source_fingerprints

_ID = re.compile(r'(?i)\{tmdb-(\d+)\}')
_SEASON = re.compile(r'(?i)season[ ._-]*\d+')
_CATEGORIES = {'电影','剧集','影视','国产剧','国产剧集','欧美剧','欧美剧集','日韩剧','日韩剧集','动漫','动漫剧','综艺','纪录片','国产电影','欧美电影','日韩电影','其他电影','其他剧集'}


def path_keys(relative, roots):
    if not isinstance(relative, str) or not relative or '\\' in relative:
        return None
    p = PurePosixPath(relative)
    if p.is_absolute() or '..' in p.parts or len(p.parts) < 3 or p.parts[0] not in roots.values():
        return None
    parts = p.parts[:-1]
    tagged = [(i, name) for i, name in enumerate(parts) if _ID.search(name)]
    if len(tagged) > 1:
        return None
    if tagged:
        _, folder = tagged[0]
    elif _SEASON.fullmatch(parts[-1]):
        if len(parts) < 3: return None
        folder = parts[-2]
    else:
        folder = parts[-1]
    if folder in roots.values() or folder in _CATEGORIES or not folder or _SEASON.fullmatch(folder):
        return None
    title, year = _title_identity(folder)
    if not title:
        return None
    # A title missing its year cannot safely be distinguished from a known
    # work of the same name. Conservatively isolate all years of that title.
    keys = {('title', title, year), ('title_any', title)}
    keys.update(('tmdb', int(value)) for name in parts for value in _ID.findall(name))
    return keys


def partition(root, roots, rows, match_strategy, db, account):
    """把库存分成「可进计划」与「只有文件、来源核不上」两类，逐文件判定。"""
    rows = list(rows)
    projection = project(rows, root, match_strategy)
    unattributable, kept = [], []
    for side, record_id, path in rows:
        if path_keys(path, roots) is None:
            unattributable.append({'side': side, 'id': str(record_id), 'path': path,
                                   'reason': '作品目录无法识别，暂不纳入清理'})
            continue
        kept.append((side, record_id, path))
    supported, unsupported = [], list(unattributable)
    # 来源核验先整批做一次（3 条查询/全部文件）；只有整批失败时才退化到逐文件，
    # 用来定位到底是哪几个文件核不上。逐文件版本在 1000+ 文件时是几千次查询。
    try:
        sources = source_fingerprints(db, kept, account)
        supported = kept
    except ValueError:
        supported = []
        for side, record_id, path in kept:
            try:
                source_fingerprints(db, [(side, record_id, path)], account)
            except ValueError as exc:
                unsupported.append({'side': side, 'id': str(record_id), 'path': path,
                                    'reason': str(exc)})
                continue
            supported.append((side, record_id, path))
        sources = source_fingerprints(db, supported, account) if supported else []
    skipped = sorted(unsupported, key=lambda item: (item['side'], item['path']))
    return {'rows': sorted(supported),
            'sources': sorted(sources, key=lambda item: (item['side'], item['id'], item['path'])),
            # 展示/选择用全量投影：文件都在，只是有些不能删。
            'projection': {**projection, 'unsupported': skipped},
            'unsupported': skipped,
            'anomalies': list(projection['issues']) + skipped,
            'audit': None}
