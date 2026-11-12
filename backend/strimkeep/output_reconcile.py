"""三表对账：``output_registry`` / ``owned_outputs`` / ``share_strms``。

三张表描述两套归属，写法分散在 ``outputs.py``、``sync.py``、``share_service.py`` 里，
一旦某处少写一笔就长期不一致，而**症状很难认**：

- ``owned_outputs`` 是 ``output_registry.files`` 的展开（每个本地文件一行）。同步清理按它判断
  "这个文件是不是我的、能不能删"：归属行在、文件没了 → 以后同名文件落地会被误删；
  归属行没了、文件还在 → 变成没人管的野文件。
- ``share_strms`` 是另一套归属（分享生成的 STRM 不写 ``owned_outputs``）。同一路径若被两套
  认领，同步清理和分享撤销会互相拆台。

只做**能证明的**修复，判断不了的只报告：

- ``ghost_ownership``   归属行在、文件不存在 → 删归属行（先备份）
- ``orphan_ownership``  归属行的受管文件已不在台账 → 删归属行（先备份）
- ``missing_owned``     台账列了、归属行没有 → 文件在则按台账指纹补建
- ``stale_in_registry`` 台账列的文件已不在磁盘 → 从台账 ``files`` 摘掉（保留其余字段）
- ``path_conflict``     同一路径被两套归属同时认领 → 只报告，交人工判断
- ``share_missing``     分享 STRM 文件不存在 → 置 ``revoked=1``（不删行，留痕）
"""
import asyncio
import json
import os
import time

GRACE = 3600.0
CLEANUP_DIR = 'cleanup'
KINDS = ('ghost_ownership', 'orphan_ownership', 'missing_owned',
         'stale_in_registry', 'path_conflict', 'share_missing')


def _exists(root, relative):
    return bool(relative) and os.path.isfile(os.path.join(root, str(relative)))


def _load(files):
    try:
        value = json.loads(files or '{}')
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _analyze(store, root, grace=GRACE):
    """完整核算（不截断）：返回 ``(计数, 明细字典)``。"""
    now = time.time()
    db = store.db
    registry = db.execute('SELECT account_id, file_id, relative_path, files, updated '
                          'FROM output_registry').fetchall()
    owned = db.execute('SELECT relative_path, account_id, file_id, digest '
                       'FROM owned_outputs').fetchall()
    shares = db.execute('SELECT id, strm_path, revoked, created FROM share_strms').fetchall()

    known = {(row['account_id'], row['file_id']) for row in registry}
    owned_by_path = {row['relative_path']: row for row in owned}
    in_registry_files, detail = set(), {kind: [] for kind in KINDS}

    for row in registry:
        fresh = now - float(row['updated'] or 0) <= grace
        for relative, digest in _load(row['files']).items():
            in_registry_files.add(relative)
            if _exists(root, relative):
                if relative not in owned_by_path and not fresh:
                    detail['missing_owned'].append(
                        {'relative_path': relative, 'account_id': row['account_id'],
                         'file_id': row['file_id'], 'digest': digest})
            elif not fresh:
                detail['stale_in_registry'].append(
                    {'relative_path': relative, 'account_id': row['account_id'],
                     'file_id': row['file_id']})

    for row in owned:
        if not _exists(root, row['relative_path']):
            detail['ghost_ownership'].append(dict(row))
        elif (row['account_id'], row['file_id']) not in known:
            detail['orphan_ownership'].append(dict(row))

    share_paths = {}
    for row in shares:
        if row['strm_path']:
            share_paths[row['strm_path']] = row
        if (not row['revoked'] and row['strm_path'] and not _exists(root, row['strm_path'])
                and now - float(row['created'] or 0) > grace):
            detail['share_missing'].append({'id': row['id'], 'strm_path': row['strm_path']})

    for relative in sorted(in_registry_files & set(share_paths)):
        detail['path_conflict'].append({'relative_path': relative,
                                        'share_id': share_paths[relative]['id']})

    counts = {kind: len(items) for kind, items in detail.items()}
    counts.update(registry=len(registry), owned=len(owned), share=len(shares))
    return counts, detail


def scan(store, root, grace=GRACE):
    """只统计；给接口/人看的是计数 + 每类前 5 条样例。"""
    counts, detail = _analyze(store, root, grace)
    issues = [{'kind': kind, 'count': len(items), 'samples': items[:5]}
              for kind, items in detail.items() if items]
    return {'counts': counts, 'healthy': not issues, 'issues': issues}


def fix(store, root, dry_run=False, grace=GRACE):
    """执行可证明的修复；``dry_run`` 只算不动。返回各类处理量与备份路径。"""
    counts, detail = _analyze(store, root, grace)
    result = {'ok': True, 'dry_run': bool(dry_run), 'backup': '',
              'registry': counts['registry'], 'owned': counts['owned'],
              'share': counts['share']}
    result.update({kind: len(detail[kind]) for kind in KINDS})
    if dry_run:
        return result
    total = sum(result[kind] for kind in KINDS)
    if not total:
        return result

    directory = os.path.join(store.root, CLEANUP_DIR)
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, 'reconcile-%s.json' % time.strftime('%Y%m%d-%H%M%S'))
    with open(path, 'w', encoding='utf-8') as handle:
        json.dump({'owned_outputs_deleted': detail['ghost_ownership'] + detail['orphan_ownership'],
                   'owned_outputs_added': detail['missing_owned'],
                   'output_registry_files_pruned': detail['stale_in_registry'],
                   'share_strms_revoked': detail['share_missing'],
                   'path_conflict_unhandled': detail['path_conflict']},
                  handle, ensure_ascii=False, indent=1)

    now = time.time()
    with store.db:
        for item in detail['ghost_ownership'] + detail['orphan_ownership']:
            store.db.execute('DELETE FROM owned_outputs WHERE relative_path=? '
                             'AND account_id=? AND file_id=?',
                             (item['relative_path'], item['account_id'], item['file_id']))
        for item in detail['missing_owned']:
            store.db.execute('INSERT OR REPLACE INTO owned_outputs VALUES(?,?,?,?)',
                             (item['relative_path'], item['account_id'],
                              item['file_id'], item.get('digest') or ''))
        for item in detail['stale_in_registry']:
            row = store.db.execute('SELECT files FROM output_registry '
                                   'WHERE account_id=? AND file_id=?',
                                   (item['account_id'], item['file_id'])).fetchone()
            files = _load(row['files'] if row else '{}')
            files.pop(item['relative_path'], None)
            store.db.execute('UPDATE output_registry SET files=?, updated=? '
                             'WHERE account_id=? AND file_id=?',
                             (json.dumps(files, ensure_ascii=False, separators=(',', ':')),
                              now, item['account_id'], item['file_id']))
        for item in detail['share_missing']:
            store.db.execute('UPDATE share_strms SET revoked=1 WHERE id=?', (item['id'],))
    result['backup'] = path
    return result


def install(app, store, admin):
    """手动入口：看对账结果，或立刻修一次。"""
    from fastapi import Depends, Query

    @app.get('/api/v1/strm/reconcile', dependencies=[Depends(admin)])
    async def reconcile_status():
        return await asyncio.to_thread(scan, store, str(app.state.strms))

    @app.post('/api/v1/strm/reconcile', dependencies=[Depends(admin)])
    async def reconcile_fix(dry_run: int = Query(0, ge=0, le=1)):
        result = await asyncio.to_thread(fix, store, str(app.state.strms), bool(dry_run))
        if not dry_run and any(result.get(kind) for kind in
                               ('ghost_ownership', 'orphan_ownership', 'missing_owned',
                                'stale_in_registry', 'share_missing')):
            store.event('strm', '三表对账：删归属 %d、补归属 %d、摘台账 %d、撤销分享 %d（备份 %s）。'
                        % (result['ghost_ownership'] + result['orphan_ownership'],
                           result['missing_owned'], result['stale_in_registry'],
                           result['share_missing'], os.path.basename(result.get('backup') or '')))
        return result
