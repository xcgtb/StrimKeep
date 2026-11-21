"""Durable command queue between the Web process and the Worker.

Web submits background operations here and the Worker writes their results back.
Commands interrupted by a Worker restart are requeued once, then failed.
"""
import asyncio
import json
import time

LIBRARY_COVER = 'library_cover'
RETIRED_COVER = 'cover_sweep'
COVER_OPERATIONS = (LIBRARY_COVER, RETIRED_COVER)
OPERATIONS = ('share_check', 'strm_rebuild', 'strm_rebuild_rollback')
KINDS = (*OPERATIONS, *COVER_OPERATIONS)
KEEP_SECONDS = 7 * 24 * 3600
MAX_ATTEMPTS = 1


def submit(store, kind, payload=None):
    if kind not in KINDS:
        raise ValueError('unknown command kind: %s' % kind)
    now = time.time()
    with store.transaction():
        cursor = store.db.execute(
            'INSERT INTO worker_commands(kind,payload,state,attempts,created,updated) '
            "VALUES(?,?, 'queued', 0, ?, ?)",
            (kind, json.dumps(payload or {}, ensure_ascii=False), now, now))
    return cursor.lastrowid


def get(store, command_id):
    row = store.db.execute(
        'SELECT * FROM worker_commands WHERE id=?', (command_id,)).fetchone()
    return dict(row) if row else None


def running(store, kind=None):
    sql = "SELECT * FROM worker_commands WHERE state IN ('queued','running')"
    params = ()
    if kind:
        sql += ' AND kind=?'
        params = (kind,)
    sql += ' ORDER BY id DESC LIMIT 1'
    row = store.db.execute(sql, params).fetchone()
    return dict(row) if row else None


def claim(store, kinds=KINDS):
    """Atomically take the oldest queued command; returns None when idle."""
    with store.db:
        placeholders = ','.join('?' for _ in kinds)
        row = store.db.execute(
            f"SELECT * FROM worker_commands WHERE state='queued' AND kind IN ({placeholders}) "
            "ORDER BY id LIMIT 1", kinds).fetchone()
        if row is None:
            return None
        now = time.time()
        changed = store.db.execute(
            "UPDATE worker_commands SET state='running',started=?,updated=? "
            "WHERE id=? AND state='queued'", (now, now, row['id'])).rowcount
        if changed != 1:
            return None
    claimed = dict(row)
    claimed['state'] = 'running'
    claimed['started'] = now
    claimed['updated'] = now
    return claimed


def finish(store, command_id, result=None, error=None):
    now = time.time()
    state = 'failed' if error else 'done'
    payload = ''
    if result is not None and not error:
        payload = json.dumps(result, ensure_ascii=False, default=str)
    with store.db:
        store.db.execute(
            "UPDATE worker_commands SET state=?,updated=?,finished=?,message=?,result=?, "
            "payload=CASE WHEN kind=? THEN json_remove(payload,'$.image') ELSE payload END "
            "WHERE id=?", (state, now, now, (error or '')[:500], payload, LIBRARY_COVER, command_id))


def recover_running(store):
    """Worker restart: requeue an interrupted command once, then fail it."""
    now = time.time()
    changed = 0
    with store.db:
        rows = store.db.execute(
            "SELECT id,attempts FROM worker_commands WHERE state='running'").fetchall()
        for row in rows:
            changed += 1
            if int(row['attempts'] or 0) < MAX_ATTEMPTS:
                store.db.execute(
                    "UPDATE worker_commands SET state='queued',attempts=attempts+1,"
                    "updated=?,message='' WHERE id=?", (now, row['id']))
            else:
                store.db.execute(
                    "UPDATE worker_commands SET state='failed',updated=?,finished=?,"
                    "message='后台重启，任务未完成',"
                    "payload=CASE WHEN kind=? THEN json_remove(payload,'$.image') ELSE payload END "
                    "WHERE id=?", (now, now, LIBRARY_COVER, row['id']))
    return changed


def prune(store, keep=KEEP_SECONDS):
    cutoff = time.time() - keep
    with store.db:
        return store.db.execute(
            "DELETE FROM worker_commands WHERE state IN ('done','failed') AND finished<?",
            (cutoff,)).rowcount


def worker_ready(store, timeout=30):
    """True when the Worker heartbeat is fresh and lanes are healthy."""
    raw = store.meta('worker_heartbeat')
    try:
        value = json.loads(raw) if raw else None
    except (TypeError, ValueError):
        return False
    if isinstance(value, dict):
        try:
            if time.time() - float(value.get('at') or 0) >= timeout:
                return False
        except (TypeError, ValueError):
            return False
        return not value.get('closing') and not any((value.get('lane_faults') or {}).values())
    try:
        return value is not None and time.time() - float(value) < timeout
    except (TypeError, ValueError):
        return False


async def wait(store, command_id, timeout=30.0, interval=0.25):
    """Poll a command; returns its result, an error dict, or None on timeout."""
    deadline = time.monotonic() + timeout
    while True:
        row = await asyncio.to_thread(get, store, command_id)
        if row is None:
            return None
        if row['state'] == 'done':
            try:
                return json.loads(row['result'] or 'null')
            except (TypeError, ValueError):
                return None
        if row['state'] == 'failed':
            return {'failed': True, 'message': row['message'] or '后台任务失败。'}
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(interval)
