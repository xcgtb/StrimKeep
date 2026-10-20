"""Exact media identities and bounded episode reads shared by deletion entries.

Paths only prove library ownership; they never prove that two Emby IDs are one
work. Cross-member reads require a strong, equal TMDB/name/year identity.
"""
import re
import stat

_ID = re.compile(r'^[A-Za-z0-9_-]{1,128}$')


def member_ids(row, field):
    ids = [str(i) for i in row.get(field) or [row.get('id')] if i]
    if not ids or len(ids) > 12 or len(set(ids)) != len(ids) or any(not _ID.fullmatch(i) for i in ids):
        raise ValueError('媒体成员 ID 无效，请重新同步片库')
    return ids


def _items(response, label, limit=None):
    if not isinstance(response, dict) or not isinstance(response.get('Items'), list):
        raise ValueError('Emby %s响应不完整，保留原状态' % label)
    rows = response['Items']
    total = response.get('TotalRecordCount')
    if (isinstance(total, bool) or not isinstance(total, int) or total != len(rows)
            or (limit is not None and len(rows) >= limit)
            or any(not isinstance(row, dict) for row in rows)):
        raise ValueError('Emby %s列表被截断或无效，保留原状态' % label)
    return rows


def mapped_path(engine, path, *, file=False):
    lib = engine.EMBY_PATHS.lib_of(path or '', fallback=False)
    converted = engine.emby_path_to_container(path)
    if lib not in ('local', 'share') or converted is None:
        raise ValueError('媒体路径无法严格映射到目标库')
    root = engine.EMBY_PATHS.container_root(path)
    if not root.is_dir() or not engine._inside(converted, root) or converted.is_symlink():
        raise ValueError('STRM 根目录不可见或路径无效，保留原状态')
    if file and converted.suffix.lower() != '.strm':
        raise ValueError('媒体路径不是目标库 STRM')
    return converted, lib


def file_exists(path):
    try:
        mode = path.stat().st_mode
    except FileNotFoundError:
        return False
    # Permission, I/O and mount errors must not be interpreted as absence.
    if not stat.S_ISREG(mode):
        raise ValueError('STRM 不是普通文件，保留原状态')
    return True


def metadata(engine, members, kind, expected=None, allow_missing=False):
    members = member_ids({'id': members[0] if members else None, 'ids': members}, 'ids')
    rows = _items(engine.emby_request('/Items', {
        'Ids': ','.join(members), 'Recursive': 'true', 'IncludeItemTypes': kind,
        'Fields': 'Path,ProviderIds,ProductionYear,ImageTags,Genres,CommunityRating',
        'Limit': len(members) + 1}, timeout=15, retries=0), '作品身份')
    found, signatures = {}, set()
    for row in rows:
        ident = str(row.get('Id') or '')
        if ident not in members or ident in found or row.get('Type') != kind:
            raise ValueError('Emby 作品成员身份不匹配，保留原状态')
        mapped_path(engine, row.get('Path'), file=kind == 'Movie')
        name = str(row.get('Name') or '').strip().casefold()
        year = str(row.get('ProductionYear') or '')
        tid = str((row.get('ProviderIds') or {}).get('Tmdb') or '')
        if not name:
            raise ValueError('Emby 作品名称缺失，保留原状态')
        if expected:
            for actual, value in ((name, str(expected.get('name') or '').strip().casefold()),
                                  (year, str(expected.get('year') or '')),
                                  (tid, str(expected.get('tmdb_id') or ''))):
                if value and actual != value:
                    raise ValueError('关联成员的片名、年份或 TMDB 不一致')
        if len(members) > 1 and (not tid.isascii() or not tid.isdecimal() or not year.isdecimal()):
            raise ValueError('跨库成员缺少可信 TMDB、片名或年份')
        signatures.add((tid, name, year))
        found[ident] = row
    if len(signatures) > 1:
        raise ValueError('关联成员的片名、年份或 TMDB 不一致')
    if not allow_missing and len(found) != len(members):
        raise ValueError('Emby 未完整返回关联作品，保留原状态')
    return found


def episodes(engine, members, meta, *, target=None, require_files=False):
    """Read each member, verify mixed results, and deduplicate exact STRM paths."""
    result, seen_ids, seen_paths = [], {}, {}
    for sid in members:
        if sid not in meta:
            continue
        rows = _items(engine.emby_request('/Items', {
            'ParentId': sid, 'Recursive': 'true', 'IncludeItemTypes': 'Episode',
            'Fields': 'Path,SeriesId,ParentIndexNumber,IndexNumber,IndexNumberEnd',
            'Limit': 5000}, timeout=15, retries=0), '分集', limit=5000)
        for row in rows:
            owner = str(row.get('SeriesId') or '')
            if owner not in meta or row.get('Type', 'Episode') != 'Episode':
                raise ValueError('Emby 返回其他作品分集，禁止删除或覆盖缓存')
            path, lib = mapped_path(engine, row.get('Path'), file=True)
            _, owner_lib = mapped_path(engine, meta[owner]['Path'])
            owner_root = engine.emby_path_to_container(meta[owner]['Path'])
            if lib != owner_lib or not engine._inside(path, owner_root):
                raise ValueError('分集身份与剧目录不匹配，保留原状态')
            ident, key = str(row.get('Id') or ''), str(path)
            if ident and ident in seen_ids and seen_ids[ident] != (owner, key):
                raise ValueError('Emby 分集 ID 对应多个路径，保留原状态')
            if key in seen_paths and seen_paths[key] != owner:
                raise ValueError('同一 STRM 对应不同电视剧成员')
            if ident:
                seen_ids[ident] = (owner, key)
            exists = file_exists(path)
            if not exists:
                if require_files and (target is None or target == lib):
                    raise ValueError('预览发现文件已不存在或挂载不可见，请同步后重新预览')
                continue
            if key not in seen_paths:
                seen_paths[key] = owner
                result.append(row)
    return result


def series_files(engine, row, *, require_files=False):
    members = row['members']
    meta = metadata(engine, members, 'Series', row)
    live = episodes(engine, members, meta, target=row['target'], require_files=require_files)
    files = sorted({str(engine.emby_path_to_container(ep['Path'])) for ep in live
                    if engine.EMBY_PATHS.lib_of(ep['Path'], fallback=False) == row['target']})
    primary = next((str(ep['SeriesId']) for ep in live
                    if engine.EMBY_PATHS.lib_of(ep['Path'], fallback=False) == row['target']), None)
    return files, primary
