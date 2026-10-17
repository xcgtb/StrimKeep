"""Opt-in, bounded batch deletion for Mapping's television series.

The preview is immutable, short lived and single-use. A local batch uses the
existing safe_delete_files chain: it CAN delete source videos. No webhook event
is ever treated as deletion authorization.
"""
from __future__ import annotations

import threading
import time
import uuid

from . import deletion_progress, engine, library_pagination, library_response, cloud_bridge

_lock = threading.RLock()
_previews = {}
_TTL = 300
_PREVIEWS = 20
_MAX_LOCAL = 20
_MAX_SHARE = 600
_BATCH_SHARE = 60
_MAX_FILES = 3000  # Files per verified batch
_MAX_SHARE_FILES = 18000  # Total files in one share-only operation
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
            raise ValueError('请选择 1 至 %d 部不同的作品' % maximum)
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
            raise ValueError('%s库单次选择上限 %d 部，请缩小筛选' %
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
    from . import library_identity
    if not row['tmdb_id'].isdecimal() or not row['name'] or row.get('year') is None:
        raise ValueError('缺少可信的 TMDB、片名或年份，请先同步片库')
    files, primary = library_identity.series_files(engine, row, require_files=True)
    if len(files) > _MAX_FILES:
        raise ValueError('本批次 STRM 过多，请缩小范围')
    return files, primary


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


def _batches(records, target):
    """Keep each share-only unit within 60 titles AND 3000 proven STRM paths."""
    if target != 'share':
        return [records]
    groups, group, files = [], [], 0
    for row in records:
        count = int(row.get('count') or 0)
        if not 1 <= count <= _MAX_FILES:
            raise ValueError('单部作品 STRM 超出单批核验上限')
        if group and (len(group) >= _BATCH_SHARE or files + count > _MAX_FILES):
            groups.append(group)
            group, files = [], 0
        group.append(row)
        files += count
    if group:
        groups.append(group)
    return groups


# Asynchronous *read-only* preview avoids FRP/browser timeouts on hundreds of items.
_preview_jobs = {}
_preview_jobs_lock = threading.Lock()
_PREVIEW_JOB_TTL = 1200


def start_preview(body):
    with _preview_jobs_lock:
        now = time.monotonic()
        for key in list(_preview_jobs):
            info = _preview_jobs[key]
            if info['status'] != 'running' and now - info['updated'] > _PREVIEW_JOB_TTL:
                del _preview_jobs[key]
        if any(row['status'] == 'running' for row in _preview_jobs.values()):
            return _error('已有批量预览正在核验，请等待完成')
        # Only lightweight progress/results live here; detailed STRM previews
        # remain in the existing bounded, single-use _previews collection.
        while len(_preview_jobs) >= 24:
            oldest = min(_preview_jobs, key=lambda k: _preview_jobs[k]['updated'])
            del _preview_jobs[oldest]
        key = uuid.uuid4().hex
        _preview_jobs[key] = {'status': 'running', 'processed': 0, 'total': 0,
                              'updated': now, 'result': None}

    started_at = time.monotonic()
    def progress(processed, total):
        if time.monotonic() - started_at > 720:
            raise TimeoutError('预览核验超过 12 分钟，已停止；未删除任何文件')
        with _preview_jobs_lock:
            info = _preview_jobs.get(key)
            if info:
                info.update(processed=processed, total=total, updated=time.monotonic())

    def work():
        try:
            result = preview(body, progress=progress)
        except Exception as exc:
            result = _error('批量预览异常：' + str(exc)[:180])
        with _preview_jobs_lock:
            info = _preview_jobs.get(key)
            if info:
                info.update(status='done', result=result, updated=time.monotonic())

    try:
        threading.Thread(target=work, name='mapping-batch-preview', daemon=True).start()
    except Exception as exc:
        with _preview_jobs_lock:
            _preview_jobs.pop(key, None)
        return _error('无法启动预览：' + str(exc))
    return {'status': 'accepted', 'job_id': key}


def preview_status(job_id):
    if not isinstance(job_id, str) or len(job_id) != 32 or any(c not in '0123456789abcdef' for c in job_id):
        return _error('无效预览任务编号')
    with _preview_jobs_lock:
        row = _preview_jobs.get(job_id)
        if not row:
            return _error('预览任务已过期或服务器已重启，请重新发起')
        result = dict(row)
    result.pop('updated', None)
    return result


def preview(body, progress=None):
    try:
        revision, records, target, maximum = _checked_rows(body or {})
        total = 0
        ready = []
        if progress:
            progress(0, len(records))
        for row in records:
            files, primary = _collect(row)
            if not files or not primary:
                raise ValueError('《%s》没有核实到目标库 STRM，请刷新片库筛选' % row['name'])
            root = engine.L_ROOT if target == 'local' else engine.S_ROOT
            sources = cloud_bridge.snapshots(files, root, target == 'local')
            if any(v.get('blocked') or not v.get('enabled') for v in sources.values()):
                raise ValueError('115 来源尚未完成身份或官方删除授权，请在网盘工作台处理后重新预览')
            row.update(paths=files, primary=primary, count=len(files), cloud_sources=sources)
            total += len(files)
            max_files = _MAX_FILES if target == 'local' else _MAX_SHARE_FILES
            if total > max_files:
                raise ValueError('批量 STRM 超出总量安全上限，请缩小选择')
            ready.append(row)
            if progress:
                progress(len(ready), len(records))
        if revision != library_response._key():
            raise ValueError('预览期间片库事实发生变化，请重新预览')
        groups = _batches(ready, target)
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
                'batch_count': len(groups), 'batch_size': _BATCH_SHARE if target == 'share' else maximum,
                'series': [{'name': r['name'], 'count': r['count']} for r in ready[:25]],
                'cloud_sources': [v for r in ready for v in r.get('cloud_sources',{}).values()][:25],
                'notice': '本地库会按原治理逻辑联动删除源视频；分享库仅清理 NAS STRM'}
    except Exception as exc:
        return _error(str(exc))


def _run(records, target, progress, delete_files):
    processed = removed = cloud = retained = failed = 0
    errors = []
    total = len(records)
    groups = _batches(records, target)
    batch_lookup = {id(item): index for index, group in enumerate(groups, 1) for item in group}
    for row in records:
        # One worker / one preview / one confirmation. Never replay a batch.
        batch_index = batch_lookup[id(row)]
        try:
            current, primary = _collect(row)
            if current != row['paths'] or primary != row['primary']:
                raise ValueError('源文件或库归属与预览不同，本部跳过，请重新预览')
            root = engine.L_ROOT if target == 'local' else engine.S_ROOT
            sources = cloud_bridge.snapshots(current, root, target == 'local')
            if sources != row.get('cloud_sources', {}):
                raise ValueError('115 来源身份或授权配置与预览不同，请重新预览')
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
                  'error_count': failed, 'current_file': row['name'],
                  'batch_index': batch_index, 'batch_total': len(groups)})
    return {'status': 'success', 'target': target, 'total': total, 'processed': processed,
            'strm_removed': removed, 'cloud_removed': cloud, 'retained': retained,
            'failed': failed, 'errors': errors[:20], 'batch_total': len(groups)}


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
