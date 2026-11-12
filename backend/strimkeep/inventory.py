"""Project STRM inventory into works.

库存事实来自目录扫描（``inventory_fs``），台账只提供「这份输出对应哪个网盘/
分享来源」的索引。没有登记的文件用合成编号进入投影，``registered=False``，
删除时按来源可核实性分别处理。
"""
import re
from pathlib import Path, PurePosixPath

from .inventory_fs import is_synthetic
from .media import EPISODE_TAG

_ID = re.compile(r'(?i)\{tmdb-(\d+)\}')
_BRACKETS = re.compile(r'\[.*?\]|\{.*?\}|\(.*?\)')
_YEAR = re.compile(r'\((\d{4})\)')


def _title_identity(folder):
    year = _YEAR.search(folder)
    base = re.sub(r'\s+', ' ', _BRACKETS.sub('', folder)).strip() or folder
    norm = re.sub(r'\s+', ' ', re.sub(r'[._:\-]+', ' ', base)).strip().casefold()
    return norm, year.group(1) if year else ''


def project(rows, root, match_strategy='title_year'):
    """把注册的 STRM 投影成「作品」。身份口径可切换（1.2.12 match_strategy）：

    - ``title_year``（默认）：规范化剧名 + 年份，TMDB 只作证据；
    - ``tmdb``：优先用 TMDB 编号配对；同一编号被多个「剧名+年份」使用时
      自动回退到剧名+年份，避免整理器写错编号把两部片合并。
    """
    root = Path(root)
    rows = list(rows)
    if not root.is_dir() or root.is_symlink():
        raise ValueError('STRM 根目录不可用')
    if match_strategy not in ('title_year', 'tmdb'):
        match_strategy = 'title_year'
    identity = root.stat()
    pending, issues, seen_paths = [], [], set()
    for side, record_id, relative in rows:
        p = PurePosixPath(relative or '')
        if (side not in ('cloud', 'share') or not relative or p.is_absolute()
                or any(x in ('', '.', '..') for x in p.parts) or '\\' in relative
                or p.suffix.casefold() != '.strm'):
            issues.append({'side': side, 'id': record_id, 'reason': 'unsafe_path'})
            continue
        if relative in seen_paths:
            issues.append({'side': side, 'id': record_id, 'reason': 'duplicate_path'})
            continue
        seen_paths.add(relative)
        path = root.joinpath(*p.parts)
        try:
            if any(part.is_symlink() for part in (path, *list(path.parents)[:len(p.parts)-1])):
                raise ValueError('symlink')
            if not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
                raise ValueError('missing')
            directory_mtime = path.parent.stat().st_mtime
            file_info = path.stat()
        except (OSError, ValueError):
            issues.append({'side': side, 'id': record_id, 'reason': 'missing_or_unsafe'})
            continue
        ids = {int(match) for part in p.parts for match in _ID.findall(part)}
        if not ids:
            issues.append({'side': side, 'id': record_id, 'reason': 'identity_unresolved'})
            continue
        matches = list(EPISODE_TAG.finditer(p.stem))
        if len(matches) > 1:
            issues.append({'side': side, 'id': record_id, 'reason': 'episode_ambiguous'})
            continue
        match = matches[0] if matches else None
        if not match and any(re.fullmatch(r'(?i)season[ ._-]*\d+', part) for part in p.parts[:-1]):
            issues.append({'side': side, 'id': record_id, 'reason': 'episode_unresolved'})
            continue
        kind = 'tv' if match else 'movie'
        folder = next((part for part in reversed(p.parts[:-1]) if _ID.search(part)), p.stem)
        title = _ID.sub('', folder).strip(' .')
        title_identity = _title_identity(folder)
        entry = {'id': record_id, 'path': relative, 'name': p.name, 'directory_mtime': directory_mtime,
                 'registered': not is_synthetic(record_id),
                 'file_size': file_info.st_size, 'file_mtime_ns': file_info.st_mtime_ns}
        if match:
            season, first = int(match[1]), int(match[2])
            last = int(match[3]) if match[3] else first
            if first < 0 or last < first or last - first > 30 or (first == 0 and last != 0):
                issues.append({'side': side, 'id': record_id, 'reason': 'episode_invalid'})
                continue
            entry.update(season=season, episodes=list(range(first, last + 1)))
        pending.append({'kind': kind, 'ids': ids, 'title': title, 'title_identity': title_identity,
                        'side': side, 'entry': entry})


    # TMDB 优先时先找冲突：同一编号被多个身份使用 → 这些作品回退到「剧名+年份」
    conflicted = set()
    if match_strategy == 'tmdb':
        owners = {}
        for record in pending:
            for tmdb_id in record['ids']:
                owners.setdefault((record['kind'], tmdb_id), set()).add(record['title_identity'])
        conflicted = {key for key, titles in owners.items() if len(titles) > 1}

    items = {}
    for record in pending:
        key = (record['kind'], ('title',) + record['title_identity'])
        source = 'title_year'
        if match_strategy == 'tmdb':
            tmdb_id = min(record['ids'])
            if (record['kind'], tmdb_id) not in conflicted:
                key, source = (record['kind'], ('tmdb', tmdb_id)), 'tmdb'
        item = items.setdefault(key, {'kind': record['kind'], 'title': record['title'],
                                      'year': record['title_identity'][1] or None,
                                      'identity_source': source,
                                      'fallback': source == 'title_year' and match_strategy == 'tmdb',
                                      'tmdb_ids': set(), 'cloud': [], 'share': []})
        item['tmdb_ids'].update(record['ids'])
        item[record['side']].append(record['entry'])

    conflicts = []
    for item in items.values():
        tmdb_ids = sorted(item.pop('tmdb_ids') or {0})
        item['tmdb_id'] = tmdb_ids[0]
        item['tmdb_ids'] = tmdb_ids
    by_tmdb = {}
    for item in items.values():
        for tmdb_id in item['tmdb_ids']:
            by_tmdb.setdefault((item['kind'], tmdb_id), []).append(item)
    for (kind, tmdb_id), works in sorted(by_tmdb.items()):
        if len(works) < 2:
            continue
        conflicts.append({'kind': kind, 'tmdb': tmdb_id, 'count': len(works),
                          'identities': [{'title': work['title'], 'year': work.get('year'),
                                          'tmdb_id': work['tmdb_id']} for work in works]})
    after = root.stat()
    if (identity.st_dev, identity.st_ino) != (after.st_dev, after.st_ino):
        raise ValueError('STRM 根目录在读取期间发生变化')
    paths = {(side, str(record_id)): relative for side, record_id, relative in rows}
    for issue in issues:
        issue['path'] = paths.get((issue['side'], str(issue['id'])), '')
    return {'items': sorted(items.values(), key=lambda v: (v['title'].casefold(), v['tmdb_id'])),
            'issues': issues, 'issue_count': len(issues), 'tmdb_conflicts': conflicts,
            'match_strategy': match_strategy, 'scope': 'strm_files',
            # 「完整」只表示这次扫描跑完了：单个文件解析不了是它自己的问题，
            # 不再升级成「整库不可用」（那会把片库映射和双库治理一起拦掉）。
            'complete': True}


def registered_rows(store):
    cloud = [('cloud', r['file_id'], r['relative_path']) for r in
             store.db.execute('SELECT file_id,relative_path FROM organized_files')]
    share = [('share', r['id'], r['strm_path']) for r in
             store.db.execute("SELECT id,strm_path FROM share_strms WHERE revoked=0 AND strm_path LIKE ?", ('分享影视库/%',))]
    return cloud + share


def work_folder(relative):
    """取文件所属的作品目录（含 `{tmdb-…}` 的那一层）；判断不了返回 None。"""
    parts = PurePosixPath(str(relative or '')).parts
    found = [index for index, part in enumerate(parts[:-1]) if _ID.search(part)]
    if len(found) != 1:
        return None
    return PurePosixPath(*parts[:found[0] + 1]).as_posix()


def work_complete(snapshot, tmdb_id):
    """Only this work's own unresolved evidence blocks it.

    目录是库存事实之后，别的作品目录里有个解析不了的文件（例如未识别整包）
    与这部作品无关，不能因此把它的缺集统计标成「待核实」。同一个 TMDB 被两个
    身份使用时仍然只能算未核实——那种情况下没法判断是哪一部。
    """
    owners = [item for item in snapshot.get('items', [])
              if tmdb_id in (item.get('tmdb_ids') or [item.get('tmdb_id')])]
    if len(owners) != 1:
        return False
    folders = {work_folder(row.get('path')) for side in ('cloud', 'share')
               for row in owners[0][side]}
    folders.discard(None)
    if not folders:
        return False
    for issue in snapshot.get('issues', []):
        path = str(issue.get('path') or '')
        if work_folder(path) in folders:
            return False
        ids = {int(value) for value in _ID.findall(path)}
        if tmdb_id in ids:
            return False
    return True


def missing_season(official_episodes, item, season, today):
    """Only aired episodes with valid TMDB dates are counted as missing."""
    expected, unknown = set(), []
    for entry in official_episodes:
        number, air = entry.get('episode_number'), entry.get('air_date')
        if type(number) is not int or number <= 0:
            continue
        try:
            from datetime import date
            airdate = date.fromisoformat(air)
        except (TypeError, ValueError):
            unknown.append(number)
            continue
        if airdate <= today:
            expected.add(number)
    result = {'expected': sorted(expected), 'unknown_air_date': sorted(set(unknown))}
    for side in ('cloud', 'share'):
        present = {ep for row in item[side] if row.get('season') == season for ep in row['episodes']}
        result[side] = {'present': sorted(present), 'missing': sorted(expected - present)}
    return result
