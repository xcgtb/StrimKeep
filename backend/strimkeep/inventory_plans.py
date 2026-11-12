"""Persistent single-claim lifecycle for future inventory deletion plans.

This module does not inspect or delete media. Callers must construct a complete,
authoritative snapshot (including all protected files, roots and rule settings)
and revalidate it immediately before claim. No execution route uses this yet.
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


def claim_plan(db, plan_id, current_snapshot, *, now=None):
    """Atomically claim once if the complete current snapshot still matches."""
    digest = snapshot_hash(current_snapshot)
    now = time.time() if now is None else now
    _begin(db)
    try:
        value = _load(db, plan_id)
        if value.get('state') != 'planned':
            raise PlanError('计划已领取或结束，不能重发')
        created, expires = value.get('created'), value.get('expires')
        if (type(created) not in (int, float) or type(expires) not in (int, float)
                or not math.isfinite(created) or not math.isfinite(expires) or not math.isfinite(now)
                or now >= expires or now < created):
            raise PlanError('计划已过期或时间异常')
        expected = value.get('digest')
        if (not isinstance(expected, str) or snapshot_hash(value.get('snapshot')) != expected
                or not hmac.compare_digest(expected, digest)):
            raise PlanError('计划或库存快照已变化，请重新预览')
        claim = secrets.token_hex(32)
        value.update(state='executing', claim_hash=hashlib.sha256(claim.encode()).hexdigest(), claimed=now)
        _write(db, plan_id, value)
        db.commit()
        return claim
    except BaseException:
        db.rollback()
        raise


def finish_plan(db, plan_id, claim, state, *, now=None, message=''):
    """Record a terminal result; interrupted executions are never retried here."""
    if state not in ('completed', 'failed') or not isinstance(claim, str):
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
        _write(db, plan_id, value)
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='inventory_plan_progress'").fetchone():
            db.execute('DELETE FROM inventory_plan_progress WHERE plan_id=?', (plan_id,))
        db.commit()
    except BaseException:
        db.rollback()
        raise


def record_progress(db, plan_id, claim, action):
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
        db.commit()
    except BaseException:
        db.rollback()
        raise
