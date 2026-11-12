"""媒体信息注入失败清单：哪一条没写进去、为什么。

以前注入失败只剩一个汇总数字（``failed: 7``），既不知道是哪几部，也不知道原因——
"某些片没时长/没音轨"只能靠人工逐条比对。这里按条目记一行，成功即清：

- 主键 ``item_id``：同一个条目只留最新状态，不会越积越多；
- ``attempts`` 累加、``first_at`` 保留首次时间、``last_at`` 每次刷新；
- 成功（写入/已有视频流）时删掉该行 —— 清单里永远只有"当前还没补上"的条目。
"""
import time

TABLE = 'media_info_failures'

REASONS = {
    'sidecar_missing': '没有媒体信息文件（还没提取，或文件被删）',
    'sidecar_unreadable': '媒体信息文件损坏或不是 JSON',
    'no_streams': '媒体信息文件里没有流信息',
    'write_failed': '写入 Emby 库失败（被占用或权限不足）',
    'emby_item_missing': 'Emby 里找不到这个条目',
}


def ensure_table(store):
    try:
        with store.db:
            store.db.execute(
                'CREATE TABLE IF NOT EXISTS %s (item_id TEXT PRIMARY KEY, name TEXT NOT NULL '
                "DEFAULT '', path TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '', "
                'attempts INTEGER NOT NULL DEFAULT 0, first_at REAL NOT NULL DEFAULT 0, '
                'last_at REAL NOT NULL DEFAULT 0)' % TABLE)
    except Exception:
        pass


def record(store, item_id, path='', reason='', name=''):
    """记一条失败；``reason`` 为空表示这次成功了 —— 清掉历史失败。"""
    ensure_table(store)
    item_id = str(item_id or '').strip()
    if not item_id:
        return
    with store.db:
        if not reason:
            store.db.execute('DELETE FROM %s WHERE item_id=?' % TABLE, (item_id,))
            return
        now = time.time()
        store.db.execute(
            'INSERT INTO %s (item_id,name,path,reason,attempts,first_at,last_at) '
            'VALUES (?,?,?,?,1,?,?) ON CONFLICT(item_id) DO UPDATE SET '
            'name=excluded.name, path=excluded.path, reason=excluded.reason, '
            'attempts=attempts+1, last_at=excluded.last_at' % TABLE,
            (item_id, name or _basename(path), str(path or ''), reason, now, now))


def _basename(path):
    text = str(path or '').replace('\\', '/')
    return text.rsplit('/', 1)[-1]


def listing(store, limit=20):
    ensure_table(store)
    rows = store.db.execute(
        'SELECT item_id,name,path,reason,attempts,first_at,last_at FROM %s '
        'ORDER BY last_at DESC LIMIT ?' % TABLE, (max(1, min(int(limit or 20), 200)),)).fetchall()
    return [{'item_id': row['item_id'], 'name': row['name'] or _basename(row['path']),
             'path': row['path'], 'reason': row['reason'],
             'reason_text': REASONS.get(row['reason'], row['reason']),
             'attempts': row['attempts'], 'first_at': row['first_at'],
             'last_at': row['last_at']} for row in rows]


def summary(store):
    ensure_table(store)
    row = store.db.execute('SELECT count(*) AS rows, MAX(last_at) AS latest FROM %s'
                           % TABLE).fetchone()
    return {'count': int(row['rows'] or 0), 'latest': row['latest'] or None}


def clear(store, item_id=None):
    ensure_table(store)
    with store.db:
        if item_id:
            cursor = store.db.execute('DELETE FROM %s WHERE item_id=?' % TABLE, (str(item_id),))
        else:
            cursor = store.db.execute('DELETE FROM %s' % TABLE)
    return cursor.rowcount or 0


def recorder(store):
    """给 ``Injector`` 的回调：单条注入结果 → 失败清单。"""
    def record_one(item_id, path, reason):
        record(store, item_id, path, reason)
    return record_one


def install(app, store, admin):
    from fastapi import Depends, HTTPException, Query

    @app.get('/api/v1/emby/media-info/failures', dependencies=[Depends(admin)])
    async def failures(limit: int = Query(20, ge=1, le=200)):
        return {**summary(store), 'items': listing(store, limit)}

    @app.post('/api/v1/emby/media-info/failures/clear', dependencies=[Depends(admin)])
    async def failures_clear(item_id: str = Query('', max_length=40)):
        removed = clear(store, item_id.strip() or None)
        return {'ok': True, 'removed': removed}
