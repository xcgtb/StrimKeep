"""Persistent claim and per-file checkpoints for inventory deletion plans.

This module does not inspect or delete media. Callers must construct a complete,
authoritative snapshot (including all protected files, roots and rule settings)
and revalidate it immediately before claim.
"""
import hashlib
import hmac
import json
import math
import secrets
import sqlite3
import time

PREFIX = 'inventory-plan:'
MAX_SNAPSHOT_BYTES = 32 * 1024 * 1024


class PlanError(ValueError):
    pass


def snapshot_hash(snapshot):
    if not isinstance(snapshot, dict) or not snapshot:
        raise PlanError('计划快照为空或格式无效')
    try:
        encoded = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                             allow_nan=False).encode('utf-8')
    except (TypeError, ValueError) as exc:
        raise PlanError('计划快照不可序列化') from exc
    if len(encoded) > MAX_SNAPSHOT_BYTES:
        raise PlanError('计划快照过大')
    return hashlib.sha256(encoded).hexdigest()


def _begin(db):
    if db.in_transaction:
        raise PlanError('计划事务已被占用')
    db.execute('BEGIN IMMEDIATE')


def _load(db, plan_id):
    if not isinstance(plan_id, str) or len(plan_id) != 32 or any(c not in '0123456789abcdef' for c in plan_id):
        raise PlanError('计划编号无效')
    row = db.execute('SELECT value FROM meta WHERE key=?', (PREFIX + plan_id,)).fetchone()
    try:
        value = json.loads(row[0]) if row else None
    except (TypeError, ValueError):
        value = None
    if not isinstance(value, dict):
        raise PlanError('计划不存在或已损坏')
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='inventory_plan_progress'").fetchone():
        records = db.execute('SELECT action FROM inventory_plan_progress WHERE plan_id=? ORDER BY seq', (plan_id,)).fetchall()
        if records:
            value['progress'] = list(value.get('progress') or []) + [json.loads(row[0]) for row in records]
    return value


def _write(db, plan_id, value):
    db.execute('UPDATE meta SET value=? WHERE key=?',
               (json.dumps(value, ensure_ascii=False, separators=(',', ':')), PREFIX + plan_id))


def queue_plan(db, plan_id):
    """Durably accept a confirmed plan before replying to the Web request."""
    _begin(db)
    try:
        value = _load(db, plan_id)
        if value.get('snapshot', {}).get('inventory', {}).get('policy', {}).get('guard_revision') != 4:
            raise PlanError('删除保护规则已更新，请重新扫描生成计划')
        now = time.time()
        if value.get('state') == 'planned':
            expires = value.get('expires')
            if type(expires) not in (int, float) or not math.isfinite(expires) or now >= expires:
                raise PlanError('计划已过期，请重新扫描生成计划')
            db.execute('INSERT INTO inventory_plan_dispatch(plan_id,queued_at) VALUES(?,?)', (plan_id, now))
        elif value.get('state') == 'needs_review' and value.get('claimed') and pending_action(db, plan_id)[0]:
            db.execute('UPDATE inventory_plan_dispatch SET queued_at=? WHERE plan_id=?', (now, plan_id))
            value.pop('finished', None)
            value['message'] = '正在重新核对清理进度'
        else:
            raise PlanError('计划已在执行或已经结束')
        value['state'] = 'starting'
        _write(db, plan_id, value)
        db.commit()
    except BaseException:
        db.rollback()
        raise


def next_queued_plan(db):
    row = db.execute("SELECT d.plan_id FROM inventory_plan_dispatch d JOIN meta m "
                     "ON m.key=? || d.plan_id WHERE json_extract(m.value,'$.state')='starting' "
                     "ORDER BY d.queued_at,d.plan_id LIMIT 1", (PREFIX,)).fetchone()
    return row[0] if row else None


def recover_dispatched_plans(db):
    """Only the Worker, under its instance lock, may requeue interrupted plans."""
    with db:
        db.execute("UPDATE meta SET value=json_set(value,'$.state','starting','$.message','正在恢复并核对清理进度') "
                   "WHERE key IN (SELECT ? || plan_id FROM inventory_plan_dispatch) "
                   "AND json_extract(value,'$.state')='executing'", (PREFIX,))


def fail_queued_plan(db, plan_id, message):
    _begin(db)
    try:
        value = _load(db, plan_id)
        if value.get('state') == 'starting':
            disputed = bool(value.get('claimed') and pending_action(db, plan_id)[0])
            value.update(state='needs_review' if disputed else 'failed',
                         message=str(message)[:180], finished=time.time())
            _write(db, plan_id, value)
            if not disputed:
                db.execute('DELETE FROM inventory_plan_dispatch WHERE plan_id=?', (plan_id,))
        db.commit()
    except BaseException:
        db.rollback()
        raise


def pending_action(db, plan_id):
    row = db.execute('SELECT current,confirmed FROM inventory_plan_dispatch WHERE plan_id=?',
                     (plan_id,)).fetchone()
    return (json.loads(row[0]) if row and row[0] else None, bool(row and row[1]))


def work_groups(db, plan_id, groups=None):
    if groups is None:
        row = db.execute('SELECT work_groups FROM inventory_plan_dispatch WHERE plan_id=?',
                         (plan_id,)).fetchone()
        return json.loads(row[0]) if row and row[0] else None
    with db:
        db.execute("UPDATE inventory_plan_dispatch SET work_groups=? WHERE plan_id=? AND work_groups=''",
                   (json.dumps(groups, ensure_ascii=False), plan_id))


def begin_action(db, plan_id, action):
    with db:
        updated = db.execute("UPDATE inventory_plan_dispatch SET current=?,confirmed=0 "
                             "WHERE plan_id=? AND current=''",
                             (json.dumps(action, ensure_ascii=False), plan_id)).rowcount
        if updated != 1:
            raise PlanError('清理进度不一致，请核对')


def confirm_action(db, plan_id):
    """Call in the transaction that clears the item's source registration."""
    updated = db.execute("UPDATE inventory_plan_dispatch SET confirmed=1 "
                         "WHERE plan_id=? AND current<>''", (plan_id,)).rowcount
    if updated != 1:
        raise PlanError('清理进度不一致，请核对')
    db.execute("INSERT OR REPLACE INTO meta(key,value) "
               "VALUES('inventory_cleanup_revision',lower(hex(randomblob(8))))")


def create_plan(db, snapshot, *, ttl=900, now=None):
    if not isinstance(ttl, (int, float)) or isinstance(ttl, bool) or not math.isfinite(ttl) or not 60 <= ttl <= 7200:
        raise PlanError('计划有效期无效')
    digest = snapshot_hash(snapshot)
    now = time.time() if now is None else now
    plan_id = secrets.token_hex(16)
    value = {'state': 'planned', 'snapshot': snapshot, 'digest': digest,
             'created': now, 'expires': now + ttl}
    _begin(db)
    try:
        db.execute('INSERT INTO meta(key,value) VALUES(?,?)',
                   (PREFIX + plan_id, json.dumps(value, ensure_ascii=False, separators=(',', ':'))))
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return plan_id


def claim_plan(db, plan_id, current_snapshot, *, now=None, dispatched=False):
    """Atomically claim once if the complete current snapshot still matches."""
    digest = snapshot_hash(current_snapshot) if current_snapshot is not None else None
    now = time.time() if now is None else now
    _begin(db)
    try:
        value = _load(db, plan_id)
        if value.get('state') != ('starting' if dispatched else 'planned'):
            raise PlanError('计划已领取或结束，不能重发')
        if dispatched and not db.execute('SELECT 1 FROM inventory_plan_dispatch WHERE plan_id=?',
                                         (plan_id,)).fetchone():
            raise PlanError('计划没有已确认的执行请求')
        resumed = dispatched and bool(value.get('claimed'))
        created, expires = value.get('created'), value.get('expires')
        if (type(created) not in (int, float) or type(expires) not in (int, float)
                or not math.isfinite(created) or not math.isfinite(expires) or not math.isfinite(now)
                or (not resumed and now >= expires) or now < created):
            raise PlanError('计划已过期或时间异常')
        expected = value.get('digest')
        if (not isinstance(expected, str) or snapshot_hash(value.get('snapshot')) != expected
                or (not resumed and (digest is None or not hmac.compare_digest(expected, digest)))):
            raise PlanError('计划或库存快照已变化，请重新预览')
        claim = secrets.token_hex(32)
        value.update(state='executing', claim_hash=hashlib.sha256(claim.encode()).hexdigest(), claimed=now)
        _write(db, plan_id, value)
        db.commit()
        return claim
    except BaseException:
        db.rollback()
        raise


def finish_plan(db, plan_id, claim, state, *, now=None, message='', dispatched=False):
    """Record a terminal result and keep a disputed action for inspection."""
    if state not in ('completed', 'failed', 'needs_review') or not isinstance(claim, str):
        raise PlanError('计划结束状态无效')
    _begin(db)
    try:
        value = _load(db, plan_id)
        digest = hashlib.sha256(claim.encode()).hexdigest()
        expected = value.get('claim_hash')
        if value.get('state') != 'executing' or not isinstance(expected, str) or not hmac.compare_digest(expected, digest):
            raise PlanError('计划领取身份或状态不符')
        value.update(state=state, finished=time.time() if now is None else now,
                     message=str(message)[:180])
        value.pop('claim_hash', None)
        # 执行完的逐条结果要留在计划里：进度表会被清空，不清空历史就没法回溯了。
        # 注意 _load 已经把进度表合并进 value['progress']，这里只做裁剪，别重复追加。
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='inventory_plan_progress'").fetchone():
            db.execute('DELETE FROM inventory_plan_progress WHERE plan_id=?', (plan_id,))
        value['progress'] = list(value.get('progress') or [])[:10000]
        if dispatched and state != 'needs_review':
            db.execute('DELETE FROM inventory_plan_dispatch WHERE plan_id=?', (plan_id,))
        _write(db, plan_id, value)
        db.commit()
    except BaseException:
        db.rollback()
        raise


def record_progress(db, plan_id, claim, action, *, dispatched=False):
    """Persist a verified completed action under the current claim."""
    if not isinstance(action, dict) or set(action) != {'side', 'id', 'path'}:
        raise PlanError('计划进度格式无效')
    _begin(db)
    try:
        db.execute('CREATE TABLE IF NOT EXISTS inventory_plan_progress (seq INTEGER PRIMARY KEY AUTOINCREMENT, plan_id TEXT NOT NULL, side TEXT NOT NULL, file_id TEXT NOT NULL, path TEXT NOT NULL, action TEXT NOT NULL, UNIQUE(plan_id, side, file_id, path))')
        state = db.execute("SELECT json_extract(value, '$.state'), json_extract(value, '$.claim_hash') FROM meta WHERE key=?", (PREFIX + plan_id,)).fetchone()
        actual = hashlib.sha256(claim.encode()).hexdigest()
        if not state or state[0] != 'executing' or not isinstance(state[1], str) or not hmac.compare_digest(state[1], actual):
            raise PlanError('计划领取身份或状态不符')
        try:
            db.execute('INSERT INTO inventory_plan_progress(plan_id, side, file_id, path, action) VALUES(?,?,?,?,?)',
                       (plan_id, action['side'], action['id'], action['path'], json.dumps(action, ensure_ascii=False, separators=(',', ':'))))
        except sqlite3.IntegrityError:
            raise PlanError('计划进度重复或损坏') from None
        if dispatched:
            updated = db.execute("UPDATE inventory_plan_dispatch SET current='',confirmed=0 "
                                 "WHERE plan_id=? AND current=? AND confirmed=1",
                                 (plan_id, json.dumps(action, ensure_ascii=False))).rowcount
            if updated != 1:
                raise PlanError('清理结果还未确认')
        db.commit()
    except BaseException:
        db.rollback()
        raise
