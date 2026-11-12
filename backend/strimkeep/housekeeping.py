"""日常维护：`events` 表按时间与行数自动裁剪。

操作记录只增不减：现场 1.3 万行时还不明显，但它是「日志页」与总览统计的查询源，
几十万行之后翻页、导出、备份都会变慢。这里按两条线裁剪，都是**只删最老的**：

- 超过 ``events_keep_days`` 天（默认 90）；
- 剩余仍超过 ``events_keep_rows`` 行（默认 20000）时，再按 id 从旧到新删到上限。

两个阈值都可用设置项覆盖（``events_keep_days`` / ``events_keep_rows``），0 表示不按该条裁剪。
"""
import time

KEEP_DAYS = 90
KEEP_ROWS = 20000

# 已被取代、代码里不再有任何读取方的设置项。留着只会让人以为"改了有效果"：
# - media_info_extract：早期开关，现由 sync_config.emby_media_info 单一来源控制；
# - media_info_inject_minutes：早期的"任务完成后再等 N 分钟补一轮"，已改为入库即写 + cron。
DEAD_KEYS = ('media_info_extract', 'media_info_inject_minutes')


def purge_dead_config(store):
    """删掉无人读取的设置项，返回被删的键。"""
    forget = getattr(store, 'forget', None)
    if not callable(forget):
        return []
    removed = forget(DEAD_KEYS)
    if removed:
        store.event('system', '已清理不再使用的设置项：%s。' % '、'.join(removed))
    return removed


def limits(store):
    try:
        days = int(store.get('events_keep_days', KEEP_DAYS) or 0)
    except (TypeError, ValueError):
        days = KEEP_DAYS
    try:
        rows = int(store.get('events_keep_rows', KEEP_ROWS) or 0)
    except (TypeError, ValueError):
        rows = KEEP_ROWS
    return max(0, days), max(0, rows)


def stats(store):
    db = store.db
    total = db.execute('SELECT count(*) FROM events').fetchone()[0]
    oldest = db.execute('SELECT min(at) FROM events').fetchone()[0]
    days, rows = limits(store)
    return {'rows': total, 'oldest': float(oldest) if oldest else None,
            'keep_days': days, 'keep_rows': rows}


def prune(store, dry_run=False):
    """按时间 + 行数裁剪；返回本轮删除量。``dry_run`` 只统计。"""
    db = store.db
    days, rows = limits(store)
    result = {'ok': True, 'by_age': 0, 'by_count': 0, 'remaining': 0}
    cutoff = time.time() - days * 86400 if days else None
    if cutoff is not None:
        result['by_age'] = db.execute('SELECT count(*) FROM events WHERE at<?',
                                      (cutoff,)).fetchone()[0]
    total = db.execute('SELECT count(*) FROM events').fetchone()[0]
    remaining_after_age = total - result['by_age']
    if rows and remaining_after_age > rows:
        result['by_count'] = remaining_after_age - rows
    if dry_run:
        result['remaining'] = remaining_after_age - result['by_count']
        return result

    if result['by_age']:
        with store.db:
            store.db.execute('DELETE FROM events WHERE at<?', (cutoff,))
    if result['by_count']:
        # 按 id 删最老的若干条（id 与时间同向递增）
        with store.db:
            store.db.execute(
                'DELETE FROM events WHERE id IN (SELECT id FROM events ORDER BY id ASC LIMIT ?)',
                (result['by_count'],))
    result['remaining'] = store.db.execute('SELECT count(*) FROM events').fetchone()[0]
    if result['by_age'] or result['by_count']:
        try:
            store.event('system', '操作记录裁剪：按时间删 %d 条、按行数删 %d 条，剩余 %d 条。'
                        % (result['by_age'], result['by_count'], result['remaining']))
        except Exception:
            pass
    return result


async def housekeeping_loop(app, store, tick=6 * 3600):
    """启动后先裁一次，之后每 6 小时一次；顺带每 24 小时做一次三表对账。"""
    import asyncio
    from .output_reconcile import fix as reconcile

    ticked = 0
    while True:
        try:
            await asyncio.to_thread(purge_dead_config, store)
        except Exception:
            pass
        try:
            await asyncio.to_thread(prune, store)
        except Exception:
            pass
        ticked += 1
        if ticked % 4 == 1:          # 6h × 4 = 每天一次
            try:
                result = await asyncio.to_thread(reconcile, store, str(app.state.strms))
                changed = sum(result.get(kind, 0) for kind in
                              ('ghost_ownership', 'orphan_ownership', 'missing_owned',
                               'stale_in_registry', 'share_missing'))
                if changed:
                    store.event('strm', '三表自动对账：删归属 %d、补归属 %d、摘台账 %d、撤销分享 %d。'
                                % (result['ghost_ownership'] + result['orphan_ownership'],
                                   result['missing_owned'], result['stale_in_registry'],
                                   result['share_missing']))
            except Exception:
                pass
        await asyncio.sleep(tick)


def install(app, store, admin):
    from fastapi import Depends, Query

    @app.get('/api/v1/system/events/stats', dependencies=[Depends(admin)])
    async def events_stats():
        return stats(store)

    @app.post('/api/v1/system/events/prune', dependencies=[Depends(admin)])
    async def events_prune(dry_run: int = Query(0, ge=0, le=1)):
        return prune(store, dry_run=bool(dry_run))
