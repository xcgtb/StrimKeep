"""台账孤儿自动清理。

台账（``output_registry``）和分享登记（``share_strms``）只增不减，而 Emby 删除条目时会
把对应的 ``.strm`` 一起删掉（分享来源的更不会由同步重新生成），于是留下"记录还在、文件没了"
的孤儿行——它们会让「STRM 重建预览」一直显示 ``missing``。这里做自动跟进：

- 只删**文件确实不存在**的行，存在的行一条不碰；
- 新写入的行给 ``GRACE`` 秒宽限，避免和正在进行的写入抢；
- 删除前把整行导出到 ``<数据目录>/cleanup/orphans-auto-<时间>.json``，可原样插回；
- 触发时机：任一同步/整理任务跑完（``prune_loop``），或手动调接口。
"""
import json
import os
import time

GRACE = 3600.0      # 新行宽限 1 小时
CLEANUP_DIR = 'cleanup'


def _exists(root, relative):
    return os.path.isfile(os.path.join(root, str(relative or '')))


def scan(store, root, grace=GRACE):
    """只统计不删除；返回 ``(台账孤儿, 分享孤儿, 台账总数, 分享总数)``。"""
    now = time.time()
    registry = store.db.execute(
        'SELECT account_id, relative_path, updated FROM output_registry').fetchall()
    orphans = [dict(row) for row in registry
               if not _exists(root, row['relative_path'])
               and now - float(row['updated'] or 0) > grace]
    shares = store.db.execute('SELECT id, strm_path, created FROM share_strms').fetchall()
    share_orphans = [dict(row) for row in shares
                     if row['strm_path'] and not _exists(root, row['strm_path'])
                     and now - float(row['created'] or 0) > grace]
    return orphans, share_orphans, len(registry), len(shares)


def prune(store, root, dry_run=False, grace=GRACE):
    """清理孤儿行；``dry_run`` 只统计。返回统计与备份路径。"""
    orphans, share_orphans, total, total_share = scan(store, root, grace)
    result = {'ok': True, 'registry': len(orphans), 'share': len(share_orphans),
              'checked': total, 'checked_share': total_share, 'backup': ''}
    if dry_run or not (orphans or share_orphans):
        return result
    directory = os.path.join(store.root, CLEANUP_DIR)
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, 'orphans-auto-%s.json' % time.strftime('%Y%m%d-%H%M%S'))
    registry_doomed = {(row['relative_path'], row['account_id']) for row in orphans}
    share_doomed = {row['id'] for row in share_orphans}
    with open(path, 'w', encoding='utf-8') as handle:
        json.dump({
            'output_registry': [dict(row) for row in store.db.execute(
                'SELECT * FROM output_registry')
                if (row['relative_path'], row['account_id']) in registry_doomed],
            'share_strms': [dict(row) for row in store.db.execute('SELECT * FROM share_strms')
                            if row['id'] in share_doomed]},
            handle, ensure_ascii=False, indent=1)
    with store.db:
        for row in orphans:
            store.db.execute('DELETE FROM output_registry WHERE relative_path=? AND account_id=?',
                             (row['relative_path'], row['account_id']))
        for row in share_orphans:
            store.db.execute('DELETE FROM share_strms WHERE id=?', (row['id'],))
    result['backup'] = path
    return result


async def repair_missing_organized(app, store, grace=GRACE):
    """Repair missing local-only ledger rows after confirming the source is gone.

    A missing STRM alone is insufficient. Require the original account,
    missing output ownership, an idle organizer and an authoritative live 115
    check; unknown network/login state leaves the row untouched.
    """
    from .inventory_stale import _absent
    from .organize_options import library_root
    try:
        config = store.get('organize_config', {}) or {}
        library = library_root(config)
        account = str(app.state.organizer.index.account({}))
        root = str(app.state.strms)
        if not account or not library:
            return 0
        rows = store.db.execute("""SELECT o.file_id,o.playback_id,o.relative_path,j.config,j.updated
            FROM organized_files o JOIN organize_jobs j ON j.id=o.job_id
            JOIN playbacks p ON p.id=o.playback_id AND p.file_id=o.file_id
            WHERE NOT EXISTS(SELECT 1 FROM output_registry r WHERE r.file_id=o.file_id OR r.relative_path=o.relative_path)
              AND NOT EXISTS(SELECT 1 FROM owned_outputs w WHERE w.file_id=o.file_id OR w.relative_path=o.relative_path)
            ORDER BY j.updated""").fetchall()
    except Exception:
        return 0
    repaired = 0
    checked = 0
    for row in rows:
        try:
            job_config = json.loads(row['config'] or '{}')
            if (str(job_config.get('account_id') or '') != account
                    or time.time() - float(row['updated'] or 0) < grace
                    or not _absent(root, row['relative_path'], library)):
                continue
            if checked >= 20:
                break
            checked += 1
            if await app.state.cloud.live_file_exists(row['file_id']):
                continue
            async with app.state.organizer.publication_gate:
                if store.db.execute("SELECT 1 FROM organize_jobs WHERE state IN ('queued','running') LIMIT 1").fetchone():
                    continue
                if str(app.state.organizer.index.account({})) != account or not _absent(root, row['relative_path'], library):
                    continue
                with store.db:
                    removed = store.db.execute("""DELETE FROM organized_files
                        WHERE file_id=? AND playback_id=? AND relative_path=?
                        AND NOT EXISTS(SELECT 1 FROM output_registry r WHERE r.file_id=organized_files.file_id OR r.relative_path=organized_files.relative_path)
                        AND NOT EXISTS(SELECT 1 FROM owned_outputs w WHERE w.file_id=organized_files.file_id OR w.relative_path=organized_files.relative_path)""",
                        (row['file_id'], row['playback_id'], row['relative_path'])).rowcount
                repaired += removed
        except Exception:
            # Network, account, path or database uncertainty is not proof of deletion.
            continue
    return repaired


async def prune_loop(app, store, tick=60.0):
    """任务跑完就顺手清一次孤儿（不额外占一份定时）。"""
    import asyncio
    last_job = None
    while True:
        try:
            row = store.db.execute(
                "SELECT MAX(id) FROM organize_jobs WHERE state='completed'").fetchone()
            job_id = int(row[0] or 0) if row else 0
        except Exception:
            job_id = 0
        if last_job is None:
            last_job = job_id
        elif job_id > last_job:
            last_job = job_id
            try:
                result = await asyncio.to_thread(prune, store, str(app.state.strms))
                managed = await repair_missing_organized(app, store)
                if managed:
                    store.event('strm', '台账自愈：核实源文件已不存在，清理 %d 条失效整理记录。' % managed)
                if result.get('registry') or result.get('share'):
                    store.event('strm', '台账自动清理：删台账 %d 条、分享登记 %d 条（备份 %s）。'
                                % (result['registry'], result['share'],
                                   os.path.basename(result.get('backup') or '')))
            except Exception as exc:
                store.event('strm', '台账自动清理失败：%s' % type(exc).__name__)
        await asyncio.sleep(tick)


def install(app, store, admin):
    """手动入口：看一眼有多少孤儿、或立刻清一次。"""
    from fastapi import Depends, Query

    @app.get('/api/v1/strm/orphans', dependencies=[Depends(admin)])
    async def orphans_status():
        orphans, share_orphans, total, total_share = scan(store, str(app.state.strms))
        return {'registry': len(orphans), 'share': len(share_orphans),
                'checked': total, 'checked_share': total_share}

    @app.post('/api/v1/strm/orphans/prune', dependencies=[Depends(admin)])
    async def orphans_prune(dry_run: int = Query(0, ge=0, le=1)):
        result = prune(store, str(app.state.strms), dry_run=bool(dry_run))
        if not dry_run and (result.get('registry') or result.get('share')):
            store.event('strm', '手动清理台账孤儿：删台账 %d 条、分享登记 %d 条（备份 %s）。'
                        % (result['registry'], result['share'],
                           os.path.basename(result.get('backup') or '')))
        return result

