"""Stable identities for automatic work and repeated failure notices; no IO."""
import hashlib
import json


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def execution_key(kind, payload):
    scheduling = {'rule_id', 'name', 'enabled', 'interval', 'interval_seconds', 'schedule',
                  'schedule_type', 'schedule_mode', 'cron_expression', 'cron', 'timezone', 'next_run', 'schedule_error'}
    return digest([kind, {k: v for k, v in payload.items() if k not in scheduling}])


def notice_scope(kind, payload):
    return digest([kind, {k: payload.get(k) for k in
                          ('account_id', 'account_revision', 'source_cid', 'target_cid', 'unknown_cid')}])


def failure_signature(operation):
    if any(operation.get(k) for k in ('files', 'unknown', 'generated', 'deferred', 'refresh_pending')):
        return None
    if operation.get('reconciliation', {}).get('removed'):
        return None
    problems = operation.get('skipped', []) + operation.get('errors', [])
    if not problems:
        return None
    return digest(sorted((json.dumps(p, sort_keys=True, ensure_ascii=False) for p in problems)))
