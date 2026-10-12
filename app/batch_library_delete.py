"""Opt-in, bounded batch deletion for Mapping's television series.

The preview is immutable, short lived and single-use. A local batch uses the
existing safe_delete_files chain: it CAN delete source videos. No webhook event
is ever treated as deletion authorization.
"""
from __future__ import annotations

import threading
import time
import uuid

from . import deletion_progress, engine, library_pagination, library_response

_lock = threading.RLock()
_previews = {}
_TTL = 300
_PREVIEWS = 20
_MAX_LOCAL = 20
_MAX_SHARE = 60
_MAX_FILES = 3000
_ALLOWED_STATUS = frozenset(('all', 'aligned', 'missing', 'extra', 'ongoing', 'unmatched', 'no_tmdb', 'pending'))


def _error(message):
    return {'status': 'error', 'message': message}


def _target(body):
    target = body.get('target')
    if target not in ('local', 'share') or body.get('scope') != target:
        raise ValueError('请先切换到仅本地或仅分享，并明确选择目标库')
    return target


def _checked_rows(body):
    target = _target(body)
    media = body.get('media', 'series')
    if media not in ('series', 'movies'):
        raise ValueError('无效媒体类型')
    status = body.get('filter', 'all')
    category = body.get('category', '')
    keyword = body.get('keyword', '')
    mode = body.get('category_mode', 'seg')
    selection = body.get('selection')
    maximum = _MAX_LOCAL if target == 'local' else _MAX_SHARE
    if status not in _ALLOWED_STATUS or mode not in ('seg', 'sub') or selection not in ('all', 'selected'):
        raise ValueError('筛选条件或选择方式无效')
    if (not isinstance(category, str) or not isinstance(keyword, str) or
            len(category) > 80 or len(keyword) > 120):
        raise ValueError('分类或搜索内容无效')
    ids = body.get('movie_ids' if media == 'movies' else 'series_ids', [])
    if selection == 'selected':
        if (not isinstance(ids, list) or not 1 <= len(ids) <= maximum or
                any(not isinstance(x, str) or not x or len(x) > 128 for x in ids) or
                len(set(ids)) != len(ids)):
            raise ValueError('请选择 1 至 %d 部不同的电视剧' % maximum)
    elif ids not in ([], None):
        raise ValueError('全选请求不得额外指定作品编号')
    with library_pagination._lock:
        if not library_pagination._read():
            raise ValueError('片库数据尚未就绪，请先完成一次片库映射')
        revision = library_response._key()
        rows = library_pagination._filtered(media, status, target, category, mode, keyword.strip().lower())
        allowed = {str(row['id']): row for row in rows if row.get('id') and row.get('in_' + target)}
        if selection == 'selected':
            if any(sid not in allowed for sid in ids):
                raise ValueError('所选作品已不属于当前筛选结果，请刷新后重新选择')
            selected = [allowed[sid] for sid in ids]
        else:
            selected = list(allowed.values())
        if not selected:
            raise ValueError('当前筛选条件下没有可清理的电视剧')
        if len(selected) > maximum:
            raise ValueError('%s库单次上限 %d 部，请缩小筛选或分批多选' %
                             ('本地' if target == 'local' else '分享', maximum))
        records = []
        member_seen = set()
        for row in selected:
            members = [str(x) for x in (row.get('ids' if media == 'movies' else 'series_ids') or [row['id']]) if x]
            if (not members or len(members) > 12 or len(set(members)) != len(members) or
                    any(len(x) > 128 for x in members) or member_seen.intersection(members)):
                raise ValueError('发现无效或重复的电视剧成员 ID，拒绝执行批量操作')
            member_seen.update(members)
            records.append({'id': str(row['id']), 'name': str(row.get('name') or '')[:180],
                            'year': row.get('year'), 'tmdb_id': str(row.get('tmdb_id') or ''),
                            'members': members, 'target': target, 'media': media})
        if revision != library_response._key():
            raise ValueError('片库快照刚发生变化，请重新预览')
    return revision, records, target, maximum


def _collect(row):
    """Validate exact identity and collect only proven STRM files in requested library.

    Emby may ignore ParentId and return mixed same-title members. We NEVER
    delete a path unless it has an explicit SeriesId belonging to the verified
    same-title group and maps under the selected root.
    """
    if row.get('media') == 'movies':
        return _collect_movie(row)
    members = row['members']
    tid = row['tmdb_id']
    if not tid.isdecimal() or not row['name'] or row.get('year') is None:
        raise ValueError('《%s》缺少可信的 TMDB/剧名/年份，请用单剧删除核实' % row['name'])
    meta = engine.emby_request('/Items', {
        'Ids': ','.join(members), 'Recursive': 'true', 'IncludeItemTypes': 'Series',
        'Fields': 'Path,ProviderIds,ProductionYear', 'Limit': len(members) + 1,
    }, timeout=15, retries=0)
    if not isinstance(meta, dict) or not isinstance(meta.get('Items'), list) or not isinstance(meta.get('TotalRecordCount'), int):
        raise ValueError('Emby 电视剧身份响应不完整')
    rows = meta['Items']
    if len(rows) != len(members) or meta['TotalRecordCount'] != len(members):
        raise ValueError('Emby 未完整返回关联电视剧，无法证明跨库身份')
    seen = set()
    for item in rows:
        sid = str(item.get('Id') or '')
        if sid not in members or sid in seen or item.get('Type') != 'Series':
            raise ValueError('Emby 电视剧成员身份不匹配')
        seen.add(sid)
        if (str(item.get('Name') or '').casefold() != row['name'].casefold() or
                str(item.get('ProductionYear') or '') != str(row['year']) or
                str((item.get('ProviderIds') or {}).get('Tmdb') or '') != tid):
            raise ValueError('关联 Series ID 的片名、年份或 TMDB 不一致')
        if engine.EMBY_PATHS.lib_of(item.get('Path') or '', fallback=False) not in ('local', 'share'):
            raise ValueError('电视剧所在库路径不可核实')
    root = engine.L_ROOT if row['target'] == 'local' else engine.S_ROOT
    paths = {}
    primary = None
    for sid in members:
        resp = engine.emby_request('/Items', {'ParentId': sid, 'Recursive': 'true',
            'IncludeItemTypes': 'Episode', 'Fields': 'Path,SeriesId', 'Limit': 5000}, timeout=15, retries=0)
        if not isinstance(resp, dict) or not isinstance(resp.get('Items'), list) or not isinstance(resp.get('TotalRecordCount'), int):
            raise ValueError('Emby 分集查询不完整')
        episodes = resp['Items']
        if resp['TotalRecordCount'] != len(episodes) or len(episodes) >= 5000:
            raise ValueError('Emby 分集列表被截断，禁止批量删除')
        for episode in episodes:
            eid = str(episode.get('SeriesId') or '')
            if eid not in members:
                raise ValueError('Emby 返回其他作品分集，禁止批量删除')
            if eid != sid:
                continue  # A cross-library result; its own member pass will inspect it.
            path = episode.get('Path') or ''
            if engine.EMBY_PATHS.lib_of(path, fallback=False) != row['target']:
                continue
            conv = engine.emby_path_to_container(path)
            if conv is None or not engine._inside(conv, root) or conv.suffix.lower() != '.strm':
                raise ValueError('分集路径无法严格映射到目标库 STRM')
            if not conv.is_file():
                raise ValueError('预览发现文件已不存在或挂载不可见，拒绝按过期状态删除')
            paths[str(conv)] = conv
            primary = primary or sid
            if len(paths) > _MAX_FILES:
                raise ValueError('本批次 STRM 过多，请缩小范围')
    return sorted(paths), primary


def _collect_movie(row):
    members, tid = row['members'], row['tmdb_id']
    if not tid.isdecimal() or not row['name'] or row.get('year') is None:
        raise ValueError('电影缺少可核实的 TMDB、片名或年份')
    response = engine.emby_request('/Items', {'Ids': ','.join(members), 'Recursive': 'true',
        'IncludeItemTypes': 'Movie', 'Fields': 'Path,ProviderIds,ProductionYear', 'Limit': len(members) + 1}, timeout=15, retries=0)
    if not isinstance(response, dict) or response.get('TotalRecordCount') != len(members) or not isinstance(response.get('Items'), list) or len(response['Items']) != len(members):
        raise ValueError('电影成员响应不完整，禁止批量删除')
    root = engine.L_ROOT if row['target'] == 'local' else engine.S_ROOT
    paths, seen = set(), set()
    for item in response['Items']:
        ident = str(item.get('Id') or '')
        if ident not in members or ident in seen or item.get('Type') != 'Movie' or str(item.get('Name') or '').casefold() != row['name'].casefold() or str(item.get('ProductionYear') or '') != str(row['year']) or str((item.get('ProviderIds') or {}).get('Tmdb') or '') != tid:
            raise ValueError('电影成员身份不一致，禁止批量删除')
        seen.add(ident)
        path = item.get('Path') or ''
        lib = engine.EMBY_PATHS.lib_of(path, fallback=False)
        if lib not in ('local', 'share'):
            raise ValueError('电影库路径无法核实')
        if lib != row['target']:
            continue
        converted = engine.emby_path_to_container(path)
        if converted is None or converted.suffix.lower() != '.strm' or not engine._inside(converted, root) or not converted.is_file():
            raise ValueError('电影路径无法严格核实为目标库 STRM')
        paths.add(str(converted))
    # Do not remove extra files that Emby did not explicitly prove.
    found = {str(p) for p in engine.find_movie_strms_by_tmdb(root, tid)}
    if paths != found:
        raise ValueError('电影目录文件与 Emby 版本不一致，请单片核实')
    return sorted(paths), members[0]


def preview(body):
    try:
        revision, records, target, maximum = _checked_rows(body or {})
        total = 0
        ready = []
        for row in records:
            files, primary = _collect(row)
            if not files or not primary:
                raise ValueError('《%s》没有核实到目标库 STRM，请刷新片库筛选' % row['name'])
            row.update(paths=files, primary=primary, count=len(files))
            total += len(files)
            if total > _MAX_FILES:
                raise ValueError('批量 STRM 超出安全上限，请缩小选择')
            ready.append(row)
        if revision != library_response._key():
            raise ValueError('预览期间片库事实发生变化，请重新预览')
        key = uuid.uuid4().hex
        with _lock:
            now = time.monotonic()
            for ident in list(_previews):
                if _previews[ident]['expires'] < now:
                    del _previews[ident]
            while len(_previews) >= _PREVIEWS:
                del _previews[next(iter(_previews))]
            _previews[key] = {'revision': revision, 'records': ready, 'target': target,
                              'expires': now + _TTL, 'used': False}
        return {'status': 'success', 'preview_id': key, 'target': target,
                'count': len(ready), 'strm_count': total, 'max_titles': maximum,
                'series': [{'name': r['name'], 'count': r['count']} for r in ready[:25]],
                'notice': '本地库会按原治理逻辑联动删除源视频；分享库仅清理 NAS STRM'}
    except Exception as exc:
        return _error(str(exc))


def _run(records, target, progress, delete_files):
    processed = removed = cloud = retained = failed = 0
    errors = []
    total = len(records)
    for row in records:
        try:
            current, primary = _collect(row)
            if current != row['paths'] or primary != row['primary']:
                raise ValueError('源文件或库归属与预览不同，本部跳过，请重新预览')
            from pathlib import Path
            files = [Path(p) for p in current]
            result = (delete_files(files, target, row['name'], '电影', tmdb_id=row['tmdb_id'])
                      if row.get('media') == 'movies' else delete_files(files, target, row['name'], '剧集', series_id=primary))
            if result.get('status') != 'success':
                raise ValueError(str(result.get('message') or '删除失败'))
            num = int(result.get('strm_removed') or 0)
            cld = int(result.get('cloud_removed') or 0)
            removed += num
            cloud += cld
            retained += max(0, len(current) - num)
            if (result.get('errors') or []) or num < len(current):
                failed += 1
                errors.append(row['name'] + ': ' + '; '.join(str(x) for x in (result.get('errors') or ['部分 STRM 未删除'])[:2]))
        except Exception as exc:
            failed += 1
            errors.append(row['name'] + ': ' + str(exc)[:200])
        processed += 1
        progress({'phase': 'deleting', 'total': total, 'processed': processed,
                  'strm_removed': removed, 'cloud_removed': cloud, 'retained': retained,
                  'error_count': failed, 'current_file': row['name']})
    return {'status': 'success', 'target': target, 'total': total, 'processed': processed,
            'strm_removed': removed, 'cloud_removed': cloud, 'retained': retained,
            'failed': failed, 'errors': errors[:20]}


def execute(body, delete_files):
    body = body or {}
    target = body.get('target')
    if target not in ('local', 'share') or body.get('confirm') is not True:
        return _error('未确认批量删除的目标库')
    phrase = '删除本地库及源文件' if target == 'local' else '删除分享库'
    if body.get('confirmation') != phrase:
        return _error('请重新确认删除范围，特别是本地库源视频清理')
    key = body.get('preview_id')
    if not isinstance(key, str) or len(key) != 32:
        return _error('无效预览编号')
    with _lock:
        p = _previews.get(key)
        if not p or p['used'] or p['expires'] < time.monotonic():
            return _error('该预览已过期或执行过，请重新预览')
        if p['target'] != target or p['revision'] != library_response._key():
            return _error('目标库或片库事实发生变化，请重新预览')
        records = [dict(item, paths=list(item['paths']), members=list(item['members'])) for item in p['records']]
        reply = deletion_progress.submit(body.get('operation_id'), ('batch-series', target, key),
            lambda progress: _run(records, target, progress, delete_files))
        if reply.get('status') == 'accepted':
            p['used'] = True
        return reply
