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


def _decode(raw):
    if not raw:
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict) or any(not isinstance(path, str) or not path or type(stamp) not in (int, float) or not math.isfinite(stamp) or stamp < 0 for path, stamp in data.items()):
        raise ValueError('内部删除登记格式损坏，已停止删除。')
    return data


def remember(store, paths):
    """Atomically persist all markers before removal; errors must stop removal."""
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
            data[path] = now
        if len(data) > KEEP:
            raise RuntimeError('内部删除登记已达容量上限，已停止删除以保护源文件。')
        db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',
                   (KEY, json.dumps(data, ensure_ascii=False)))
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return True


def recent(store, window=WINDOW):
    """Read live markers; a read/format error must fail closed, never return []."""
    now = time.time()
    row = store.db.execute('SELECT value FROM meta WHERE key=?', (KEY,)).fetchone()
    data = _decode(row[0] if row else '')
    return {str(path) for path, stamp in data.items()
            if now - stamp <= window}
