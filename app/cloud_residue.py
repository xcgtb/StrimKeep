# -*- coding: utf-8 -*-
"""Explicit, previewed cleanup of cloud sidecar-only directories via exact mapping."""
if __package__:
    from . import state_store as _state
else:
    import state_store as _state
import secrets
import stat
import threading
import time
from pathlib import Path

try:
    from . import wash, logger as audit_logger
except ImportError:
    import wash
    import logger as audit_logger

_PREVIEWS = {}
_LOCK = threading.Lock()
_TTL = 600


def _local_location(raw):
    d, root, tag, ancestors = wash._residue_location(raw)
    if tag != 'local': raise ValueError('分享库不关联删除 115 源目录')
    if d == root or d.name in wash._eng()._CATEGORY_NAMES or not wash._eng()._RE_DIR_TMDB.search(d.name):
        raise ValueError('只能检查本地库内带 TMDB 标记的媒体目录')
    return d, root, ancestors


def _configured_cloud_root():
    root = Path(wash._eng().CLOUD_L_ROOT)
    if not root.is_absolute() or '..' in root.parts:
        raise ValueError('115 挂载根配置无效')
    for other in [Path(wash._eng().L_ROOT), Path(wash._eng().S_ROOT)]:
        if root == other or root in other.parents or other in root.parents:
            raise ValueError('115 挂载根与 STRM 库重叠，不能清理')
    return root


def _cloud_root():
    root = _configured_cloud_root()
    wash._directory_ancestors(root, root)
    return root


def _cloud_location(local, local_root):
    root = _cloud_root()
    d = root / local.relative_to(local_root)
    ancestors = {}; current = root
    for part in [None, *d.relative_to(root).parts]:
        if part is not None: current = current / part
        st = current.lstat()
        if not stat.S_ISDIR(st.st_mode): raise ValueError('115 路径经过软链接或不是普通目录: ' + str(current))
        ancestors[str(current)] = wash._residue_signature(st)
    if root.resolve(strict=True) != root or d.resolve(strict=True) != d:
        raise ValueError('115 路径祖先包含软链接')
    return d, root, ancestors


def _snapshot(d, root, ancestors):
    inventory = wash._residue_inventory(d)
    if not inventory['.']['safe']: raise ValueError(inventory['.']['reason'])
    return {'inventory': inventory, 'ancestors': {p: sig[:3] for p, sig in ancestors.items()}}


def _facts(raw):
    local, local_root, local_ancestors = _local_location(raw)
    local_facts = _snapshot(local, local_root, local_ancestors)
    cloud, cloud_root, cloud_ancestors = _cloud_location(local, local_root)
    cloud_facts = _snapshot(cloud, cloud_root, cloud_ancestors)
    return {'local': str(local), 'local_root': str(local_root),
            'cloud': str(cloud), 'cloud_root': str(cloud_root),
            'local_facts': local_facts, 'cloud_facts': cloud_facts}, cloud_ancestors


def preview_cloud_residue(raw):
    """No mutation of NAS/cloud media; short-lived server-held confirmation evidence."""
    try:
        facts, _ = _facts(raw)
        info = facts['cloud_facts']['inventory']['.']
        token = secrets.token_hex(24); now = time.monotonic()
        with _LOCK:
            for key in list(_PREVIEWS):
                if _PREVIEWS[key]['expires'] <= now: _PREVIEWS.pop(key)
            while len(_PREVIEWS) >= 50: _PREVIEWS.pop(next(iter(_PREVIEWS)))
            _PREVIEWS[token] = {'facts': facts, 'expires': now + _TTL}
        files = sorted(rel for rel, row in facts['cloud_facts']['inventory'].items() if row['kind'] == 'file')
        return {'status': 'success', 'token': token, 'expires_seconds': _TTL,
                'local_path': facts['local'], 'cloud_path': facts['cloud'],
                'file_count': info['file_count'], 'extensions': info['extensions'],
                'files': files[:50], 'files_truncated': len(files) > 50,
                'message': '本地和 115 对应目录均无媒体或未知文件；仅清理预览的 115 残留，NAS 文件保留'}
    except (OSError, ValueError) as error:
        return {'status': 'error', 'message': '115 残留不可清理: ' + str(error)}


def clean_cloud_residue(token, confirmed=False):
    """确认后直接清理，失败逐项说明；不产生任何备份。"""
    if confirmed is not True:
        return {'status': 'error', 'message': '必须确认预览中的 115 残留清理'}
    try:
        with wash.mutation_lock():
            with _LOCK: preview = _PREVIEWS.pop(str(token or ''), None)
            if not preview or preview['expires'] <= time.monotonic():
                raise ValueError('预览已过期、已使用或服务已重启，请重新预览')
            facts = preview['facts']
            current, ancestors = _facts(facts['local'])
            if current != facts: raise ValueError('本地或 115 内容/路径已变化，请重新预览')
            source, root = Path(facts['cloud']), Path(facts['cloud_root'])
            result = wash._remove_residue(source, root, facts['cloud_facts']['inventory'], ancestors)
            wash.log.info('115 残留直接清理: %s; 删除附属文件 %s 个; 未完成 %s 项',
                          source, result['files_removed'], len(result['errors']))
            return {'status': 'success' if result['removed'] else 'partial',
                    'count': int(result['removed']), 'files_removed': result['files_removed'],
                    'cloud_path': str(source), 'errors': result['errors'],
                    'message': '115 残留清理完成，无备份；NAS 残留可在当前列表执行清理' if result['removed'] else '部分项目未删除，请查看路径和原因'}
    except wash.MutationBusy as error:
        return {'status': 'busy', 'message': str(error)}
    except (OSError, ValueError) as error:
        return {'status': 'error', 'count': 0, 'files_removed': 0, 'errors': [], 'message': str(error)}


def _optional_directory(raw, root):
    """库根必须存在；允许已删除的媒体目录，仍拒绝越界及软链接。"""
    path, root = Path(str(raw or '')), Path(root)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('必须提供库内绝对路径，不能包含 ..')
    relative = path.relative_to(root)
    wash._directory_ancestors(root, root)
    current = root
    for part in relative.parts:
        current = current / part
        try: st = current.lstat()
        except FileNotFoundError: return path, None
        if not stat.S_ISDIR(st.st_mode):
            raise ValueError('路径经过软链接或不是普通目录: ' + str(current))
    return path, wash._directory_ancestors(path, root)


def scan_directory_cleanup(raw='', scope='directory', limit=100):
    """只扫描 NAS；按已配置 CD2 根生成对应路径，执行时逐项核对并删除。"""
    started = time.monotonic()
    wash.log.info('目录扫描开始（仅 NAS）: %s', raw or '整个媒体库（本地＋分享）')
    result = wash.scan_all_empty_dirs(limit=limit) if scope == 'all' else wash.scan_empty_dirs(raw, limit=limit)
    if result['status'] != 'success': return result
    try:
        eng = wash._eng()
        cloud_root = _configured_cloud_root() if any(x['lib'] == 'local' for x in result['preview']) else None
        for item in result['preview']:
            item['local_present'] = True
            item['nas_file_count'] = item['file_count']
            item['cloud_checked'] = False
            item['cloud_present'] = None
            item['cloud_file_count'] = None
            item['cloud_reason'] = ''
            item['cloud_path'] = str(cloud_root / Path(item['path']).relative_to(eng.L_ROOT)) if item['lib'] == 'local' else ''
            item['rule'] = '清理 NAS 附属文件与空目录，并通过 CD2 清理对应 115 残留' if item['lib'] == 'local' else '只清理 NAS 附属文件与空目录'
        result['cloud_checked'] = False
        wash.log.info('目录扫描完成（仅 NAS）: 目录 %s; 命中 %s; 耗时 %.1f 秒',
                      result['folders'], result['hits'], time.monotonic() - started)
        return result
    except (OSError, ValueError) as error:
        return {'status': 'error', 'message': '目录清理扫描失败: ' + str(error)}


def _cleanup_target(raw, claimed_tag):
    eng = wash._eng(); path = Path(str(raw or ''))
    for tag, root in [('local', Path(eng.L_ROOT)), ('share', Path(eng.S_ROOT))]:
        if path == root or root in path.parents:
            if tag != claimed_tag: raise ValueError('条目所属库与路径不一致')
            if path == root or path.name in eng._CATEGORY_NAMES or not eng._RE_DIR_TMDB.search(path.name):
                raise ValueError('只能清理带 TMDB 标记的媒体目录，不能清理库根或分类')
            path, ancestors = _optional_directory(path, root)
            return path, root, ancestors
    raise ValueError('路径必须在本地/分享库内')


def clean_directory_cleanup(items):
    """先清预览中的对应 115，再清 NAS；失败跳过，返回各处实际删除数量。"""
    result = {'status': 'success', 'count': 0, 'nas_count': 0, 'cloud_count': 0,
              'files_removed': 0, 'cloud_files_removed': 0, 'errors': [], 'already_absent': 0}
    removed_nas = []; seen = set(); audit_items = []
    count_keys = ('count', 'nas_count', 'cloud_count', 'files_removed', 'cloud_files_removed', 'already_absent')
    if not isinstance(items, list) or len(items) > 1000:
        return {'status': 'error', 'message': '清理条目必须为列表，最多 1000 项'}
    try:
        with wash.mutation_lock():
            for item in items:
                raw = str(item.get('path') or '') if isinstance(item, dict) else ''
                if raw in seen: continue
                seen.add(raw)
                before = {k: result[k] for k in count_keys}; error_start = len(result['errors'])
                try:
                    if not isinstance(item, dict): raise ValueError('清理条目无效')
                    tag = item.get('lib'); path, root, ancestors = _cleanup_target(raw, tag)
                    nas_inventory = wash._residue_inventory(path) if ancestors is not None else None
                    if nas_inventory and not nas_inventory['.']['safe']: raise ValueError('NAS 保留：' + nas_inventory['.']['reason'])
                    cloud_path = cloud_root = cloud_ancestors = cloud_inventory = None
                    if tag == 'local':
                        cloud_root = _cloud_root(); cloud_path = cloud_root / path.relative_to(root)
                        if item.get('cloud_path') != str(cloud_path): raise ValueError('115 映射与预览不一致，请重新扫描')
                        _, cloud_ancestors = _optional_directory(cloud_path, cloud_root)
                        if cloud_ancestors is not None:
                            cloud_inventory = wash._residue_inventory(cloud_path)
                            if not cloud_inventory['.']['safe']: raise ValueError('115 保留：' + cloud_inventory['.']['reason'])
                    elif item.get('cloud_path'): raise ValueError('分享库条目不能删除 115 本地源')
                    if ancestors is None and cloud_ancestors is None:
                        result['already_absent'] += 1; continue
                    if cloud_inventory is not None:
                        deletion = wash._remove_residue(cloud_path, cloud_root, cloud_inventory, cloud_ancestors)
                        result['cloud_files_removed'] += deletion['files_removed']; result['errors'].extend(deletion['errors'])
                        if not deletion['removed']: continue  # 失败不顺带移除 NAS，便于复查和重试。
                        result['cloud_count'] += 1
                    if nas_inventory is not None:
                        deletion = wash._remove_residue(path, root, nas_inventory, ancestors)
                        result['files_removed'] += deletion['files_removed']; result['errors'].extend(deletion['errors'])
                        if not deletion['removed']: continue
                        result['nas_count'] += 1; removed_nas.append(str(path))
                    result['count'] += 1
                    wash.log.info('目录联动清理完成: NAS=%s; 115=%s', path, cloud_path or '分享库不关联115')
                except (OSError, ValueError, TypeError) as error:
                    result['errors'].append(raw + ': ' + str(error))
                    wash.log.warning('目录联动清理跳过: %s: %s', raw, error)
                finally:
                    delta = {k: result[k] - before[k] for k in count_keys}
                    errors = result['errors'][error_start:]
                    changed = any(delta[k] for k in ('nas_count', 'cloud_count', 'files_removed', 'cloud_files_removed'))
                    status = 'partial' if errors and changed else 'skipped' if errors else 'absent' if delta['already_absent'] else 'success'
                    audit_items.append({'path': raw, 'lib': item.get('lib') if isinstance(item, dict) else '',
                                        'cloud_path': str(item.get('cloud_path') or '') if isinstance(item, dict) else '',
                                        'status': status, **delta, 'errors': errors[:50]})
            if removed_nas:
                try: wash._eng().notify_emby_deleted(removed_nas, background=True)
                except Exception as error: result['errors'].append('Emby 刷新失败: ' + str(error))
    except wash.MutationBusy as error:
        return {'status': 'busy', 'message': str(error)}
    except OSError as error:
        result['errors'].append(str(error))
    wash.log.info('目录联动清理结果: NAS %s目录/%s文件; 115 %s目录/%s文件; 未完成%s项',
                  result['nas_count'], result['files_removed'], result['cloud_count'],
                  result['cloud_files_removed'], len(result['errors']))
    result['failed_count'] = sum(x['status'] in ('partial', 'skipped') for x in audit_items)
    result['requested_count'] = len(audit_items)
    result['completed_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
    title = '清理完成' if not result['errors'] else '清理结束，有未完成项目'
    summary = 'NAS %s 个目录 / %s 个文件；115 %s 个目录 / %s 个文件；跳过/未完成 %s 项' % (
        result['nas_count'], result['files_removed'], result['cloud_count'], result['cloud_files_removed'], result['failed_count'])
    details = [summary]
    labels = {'success': '完成', 'partial': '部分完成', 'skipped': '跳过', 'absent': '已不存在'}
    for row in audit_items[:300]:
        details.append('%s · %s · NAS: %s · 115: %s' % (
            labels[row['status']], '分享库' if row['lib'] == 'share' else '本地库',
            row['path'], row['cloud_path'] or '不关联'))
    details.extend('未完成原因: ' + error for error in result['errors'][:100])
    try:
        result['audit_recorded'] = audit_logger.write('目录清理', title + ' · ' + summary, details,
            extra={'kind': 'directory_cleanup', 'summary': {k: result[k] for k in (*count_keys, 'failed_count', 'requested_count')},
                   'cleanup_items': audit_items[:300], 'items_truncated': len(audit_items) > 300})
        if not result['audit_recorded']: result['audit_error'] = '执行记录写入失败，请查看服务日志'
    except Exception as error:
        result['audit_recorded'] = False; result['audit_error'] = '执行记录写入失败: ' + str(error)
        wash.log.warning('%s', result['audit_error'])
    return result
