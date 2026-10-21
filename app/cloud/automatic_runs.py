"""Automatic read-failure backoff and semantic notices, without replaying writes."""
import json
import time
from app.cloud import store, events
from app.cloud.errors import CloudError
from app.domain.automatic_runs import digest, execution_key, notice_scope, failure_signature


def equivalent_active(con, kind, payload):
    key = execution_key(kind, payload)
    rows = con.execute("SELECT payload FROM cloud_jobs WHERE kind=? AND state IN ('queued','running') "
                       "AND json_extract(payload,'$.account_id')=? AND json_extract(payload,'$.source_cid')=?",
                       (kind, payload.get('account_id'), payload.get('source_cid'))).fetchall()
    return any(execution_key(kind, json.loads(r[0])) == key for r in rows)


def job_payload(job_id):
    # Public task summaries intentionally omit private payloads.
    with store.db() as con:
        row = con.execute('SELECT payload FROM cloud_jobs WHERE id=?', (job_id,)).fetchone()
    return json.loads(row[0]) if row else {}


def recognize(account_id, row, context, config, callback):
    automatic = bool(job_payload(events._JOB.get()).get('rule_id')) if events._JOB.get() else False
    account = store.account(account_id)
    fingerprint = digest([row, context, config, (account or {}).get('revision'), 1])
    now = time.time()
    with store.db() as con:
        old = con.execute('SELECT payload,attempts,next_retry FROM cloud_scan_failures WHERE account_id=? AND file_id=? AND fingerprint=?',
                          (account_id, row['id'], fingerprint)).fetchone()
    if automatic and old and old[2] > now:
        error = json.loads(old[0])
        events.emit('recognize', '已知读取失败处于冷却期，保留源文件；可在整理预览中手动重试',
                    details={'file_id': row['id'], 'status': 'cooldown', 'code': error['code']})
        cached = CloudError(error['message'], error['status'], code=error['code'], stage=error['stage'], category=error.get('category', ''))
        if error.get('recognition'):
            cached.recognition = error['recognition']
        raise cached
    try:
        result = callback()
    except CloudError as error:
        # Cache only read-side diagnostic failures, never uncertain writes or
        # semantic unknown-media decisions that may have a different workflow.
        if not error.uncertain and error.stage in ('probe', 'tmdb_episode'):
            attempts = min((old[1] if old else 0) + 1, 8)
            delay = min(300 * 2 ** (attempts - 1), 3600)
            record = {'message': events.clean(str(error)), 'status': error.status,
                      'code': error.code, 'stage': error.stage, 'category': error.category,
                      'recognition': getattr(error, 'recognition', None)}
            with store.db() as con:
                con.execute('INSERT INTO cloud_scan_failures VALUES(?,?,?,?,?,?,?) ON CONFLICT(account_id,file_id) DO UPDATE SET '
                            'fingerprint=excluded.fingerprint,payload=excluded.payload,attempts=excluded.attempts,next_retry=excluded.next_retry,updated=excluded.updated',
                            (account_id, row['id'], fingerprint, store.encode(record), attempts, now+delay, now))
                con.execute('DELETE FROM cloud_scan_failures WHERE updated<?', (now-30*86400,))
        raise
    with store.db() as con:
        con.execute('DELETE FROM cloud_scan_failures WHERE account_id=? AND file_id=?', (account_id, row['id']))
    return result


def claim_notice(job, kind, state, operation):
    payload = job_payload(job['id']) if job else {}
    if not payload.get('rule_id'):
        return True
    scope = notice_scope(kind, payload)
    if state == 'failed' and not operation.get('errors'):
        operation = {**operation, 'errors': [{'reason': job.get('message') or '任务失败'}]}
    signature = failure_signature(operation) if state in ('needs_review', 'failed') else None
    with store.db() as con:
        if signature is None:
            con.execute('DELETE FROM cloud_notice_incidents WHERE scope=?', (scope,))
            return True
        old = con.execute('SELECT signature,job_id FROM cloud_notice_incidents WHERE scope=?', (scope,)).fetchone()
        if old and old[0] == signature:
            return old[1] == job['id']
        # Claim before transport: an unknown send is not automatically replayed.
        con.execute('INSERT INTO cloud_notice_incidents VALUES(?,?,?,?) ON CONFLICT(scope) DO UPDATE SET '
                    'signature=excluded.signature,job_id=excluded.job_id,updated=excluded.updated',
                    (scope, signature, job['id'], time.time()))
    return True
