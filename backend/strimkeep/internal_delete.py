"""Durable safety markers for program-initiated STRM removal.

A delayed Emby delete webhook cannot reliably distinguish a former file from a
new file reusing the same path. While a marker is live, prefer preserving data;
manual delete of a reused path can be processed after the marker expires.
"""
import json
import math
import time

KEY = 'internal_deletes'
WINDOW = 7 * 24 * 3600
KEEP = 100000
# 分享库与媒体库的路径互不重叠，但「程序删过」的登记必须按来源分域：
# 旧版没有分域的登记一律视为受管输出（继续保护网盘真删），分享侧不再被它豁免。
SHARE_PREFIX = '\x00share:'
KINDS = ('managed', 'share')


def _prefixed(kind, path):
    return (SHARE_PREFIX + path) if kind == 'share' else path


def _unprefix(key):
    if key.startswith(SHARE_PREFIX):
        return 'share', key[len(SHARE_PREFIX):]
    return 'managed', key


def _decode(raw):
    if not raw:
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict) or any(not isinstance(path, str) or not path or type(stamp) not in (int, float) or not math.isfinite(stamp) or stamp < 0 for path, stamp in data.items()):
        raise ValueError('内部删除登记格式损坏，已停止删除。')
    return data


def remember(store, paths, kind='managed'):
    """Atomically persist all markers before removal; errors must stop removal.

    kind='share' 只保护分享库的 STRM；默认 'managed' 保护受管输出。
    """
    if kind not in KINDS:
        raise ValueError('未知的内部删除登记类型')
    paths = {str(path) for path in paths if path}
    if not paths:
        return True
    db = store.db
    if db.in_transaction:
        raise RuntimeError('内部删除登记无法在未提交事务中确保落盘，已停止删除。')
    now = time.time()
    try:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT value FROM meta WHERE key=?', (KEY,)).fetchone()
        data = _decode(row[0] if row else '')
        data = {path: stamp for path, stamp in data.items()
                if now - stamp <= WINDOW}
        for path in paths:
            data[_prefixed(kind, path)] = now
        if len(data) > KEEP:
            raise RuntimeError('内部删除登记已达容量上限，已停止删除以保护源文件。')
        db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',
                   (KEY, json.dumps(data, ensure_ascii=False)))
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return True


def recent(store, window=WINDOW, kind=None):
    """Read live markers; a read/format error must fail closed, never return [].

    kind=None 返回两类并集（库存扫描用）；指定来源时只看该来源。
    旧版无前缀登记按 'managed' 处理 —— 这正是「分享侧被媒体库清理记录挡住」的修复。
    """
    if kind is not None and kind not in KINDS:
        raise ValueError('未知的内部删除登记类型')
    now = time.time()
    row = store.db.execute('SELECT value FROM meta WHERE key=?', (KEY,)).fetchone()
    data = _decode(row[0] if row else '')
    seen = set()
    for key, stamp in data.items():
        if now - stamp > window:
            continue
        item_kind, path = _unprefix(key)
        if kind is None or item_kind == kind:
            seen.add(path)
    return seen


def forget(store, paths, kind=None):
    """新文件发布到同一路径后清掉旧登记。

    否则「程序删旧版 → 新文件写回同路径」之后，用户手动深度清理仍会被
    7 天前的登记判成「程序自己删的」而跳过（分享侧不清理就是这样发生的）。
    """
    if kind is not None and kind not in KINDS:
        raise ValueError('未知的内部删除登记类型')
    paths = {str(path) for path in paths if path}
    if not paths:
        return 0
    db = store.db
    if db.in_transaction:
        raise RuntimeError('内部删除登记无法在未提交事务中确保落盘，已停止删除。')
    now = time.time()
    removed = 0
    try:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT value FROM meta WHERE key=?', (KEY,)).fetchone()
        data = {key: stamp for key, stamp in _decode(row[0] if row else '').items()
                if now - stamp <= WINDOW}
        for key in list(data):
            item_kind, path = _unprefix(key)
            if path in paths and (kind is None or item_kind == kind):
                del data[key]
                removed += 1
        db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',
                   (KEY, json.dumps(data, ensure_ascii=False)))
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return removed
