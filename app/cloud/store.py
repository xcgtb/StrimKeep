"""Cloud records in the existing StrimKeep SQLite database."""

import json
import time
import uuid
from contextlib import contextmanager
from app import storage
from app.cloud import states

SCHEMA = """
CREATE TABLE IF NOT EXISTS cloud_accounts (
 id TEXT PRIMARY KEY, provider TEXT NOT NULL, uid TEXT NOT NULL, name TEXT NOT NULL,
 sealed TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, updated REAL NOT NULL,
 UNIQUE(provider,uid));
CREATE TABLE IF NOT EXISTS cloud_files (
 account_id TEXT NOT NULL, file_id TEXT NOT NULL, parent_id TEXT NOT NULL,
 name TEXT NOT NULL, size INTEGER NOT NULL, sha1 TEXT NOT NULL, pickcode TEXT NOT NULL,
 is_dir INTEGER NOT NULL, observed REAL NOT NULL, PRIMARY KEY(account_id,file_id));
CREATE TABLE IF NOT EXISTS cloud_directory_settings (
 account_id TEXT PRIMARY KEY, payload TEXT NOT NULL, revision INTEGER NOT NULL,
 updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS cloud_media_probes (
 account_id TEXT NOT NULL, file_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
 payload TEXT NOT NULL, updated REAL NOT NULL, PRIMARY KEY(account_id,file_id));
CREATE TABLE IF NOT EXISTS cloud_jobs (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, state TEXT NOT NULL, payload TEXT NOT NULL,
 result TEXT NOT NULL DEFAULT '{}', message TEXT NOT NULL DEFAULT '',
 created REAL NOT NULL, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS cloud_steps (
 job_id TEXT NOT NULL, step_key TEXT NOT NULL, state TEXT NOT NULL,
 payload TEXT NOT NULL, result TEXT NOT NULL DEFAULT '{}', updated REAL NOT NULL,
 PRIMARY KEY(job_id,step_key));
CREATE INDEX IF NOT EXISTS cloud_steps_child ON cloud_steps(json_extract(result,'$.job_id'));
CREATE TABLE IF NOT EXISTS cloud_job_controls (
 job_id TEXT PRIMARY KEY, stop_requested INTEGER NOT NULL, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS cloud_job_due (
 job_id TEXT PRIMARY KEY, due REAL NOT NULL, origin_job TEXT NOT NULL,
 file_id TEXT NOT NULL, created REAL NOT NULL, UNIQUE(origin_job,file_id));
CREATE INDEX IF NOT EXISTS cloud_job_due_origin ON cloud_job_due(origin_job);
CREATE INDEX IF NOT EXISTS cloud_jobs_created ON cloud_jobs(created DESC);
CREATE TABLE IF NOT EXISTS cloud_job_links (
 job_id TEXT PRIMARY KEY, parent_id TEXT NOT NULL, created REAL NOT NULL);
CREATE INDEX IF NOT EXISTS cloud_job_links_parent ON cloud_job_links(parent_id);
CREATE TABLE IF NOT EXISTS cloud_recovery_requests (
 origin_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS cloud_upgrades (
 id TEXT PRIMARY KEY, job_id TEXT NOT NULL, new_asset TEXT NOT NULL,
 old_asset TEXT NOT NULL, state TEXT NOT NULL, payload TEXT NOT NULL,
 result TEXT NOT NULL, updated REAL NOT NULL, UNIQUE(job_id,old_asset));
CREATE TABLE IF NOT EXISTS cloud_plans (
 id TEXT PRIMARY KEY, account_id TEXT NOT NULL, revision INTEGER NOT NULL,
 state TEXT NOT NULL, payload TEXT NOT NULL, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS cloud_assets (
 id TEXT PRIMARY KEY, account_id TEXT NOT NULL, file_id TEXT NOT NULL,
 local_path TEXT NOT NULL UNIQUE, payload TEXT NOT NULL, nonce TEXT NOT NULL,
 state TEXT NOT NULL, updated REAL NOT NULL, UNIQUE(account_id,file_id,local_path));
CREATE INDEX IF NOT EXISTS cloud_assets_source ON cloud_assets(account_id,file_id);
CREATE TABLE IF NOT EXISTS cloud_share_sources (
 id TEXT PRIMARY KEY, account_id TEXT NOT NULL, code_hash TEXT NOT NULL,
 sealed TEXT NOT NULL, revision INTEGER NOT NULL, updated REAL NOT NULL,
 UNIQUE(account_id,code_hash));
CREATE TABLE IF NOT EXISTS cloud_rules (
 id TEXT PRIMARY KEY, payload TEXT NOT NULL, enabled INTEGER NOT NULL,
 next_run REAL NOT NULL, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS cloud_service_settings (
 account_id TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL,
 revision INTEGER NOT NULL, account_revision INTEGER NOT NULL, updated REAL NOT NULL,
 PRIMARY KEY(account_id,kind));
CREATE TABLE IF NOT EXISTS cloud_channels (
 id TEXT PRIMARY KEY, account_id TEXT NOT NULL, account_revision INTEGER NOT NULL,
 revision INTEGER NOT NULL, payload TEXT NOT NULL, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS cloud_inbox (
 id TEXT PRIMARY KEY, event_key TEXT NOT NULL UNIQUE, account_id TEXT NOT NULL,
 source TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL,
 job_id TEXT NOT NULL DEFAULT '', result TEXT NOT NULL DEFAULT '{}', created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS cloud_channel_cursors (
 id TEXT PRIMARY KEY, cursor INTEGER NOT NULL, initialized INTEGER NOT NULL,
 next_poll REAL NOT NULL, last_error TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cloud_ingestion (
 id TEXT PRIMARY KEY, asset_id TEXT NOT NULL, connection_id TEXT NOT NULL,
 revision INTEGER NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL,
 next_check REAL NOT NULL, attempts INTEGER NOT NULL, result TEXT NOT NULL,
 updated REAL NOT NULL, UNIQUE(asset_id,connection_id,revision,payload));
CREATE TABLE IF NOT EXISTS cloud_ingestion_notifications (
 id TEXT PRIMARY KEY, state TEXT NOT NULL, updated REAL NOT NULL);
CREATE INDEX IF NOT EXISTS cloud_ingestion_asset ON cloud_ingestion(asset_id,connection_id,revision);
CREATE INDEX IF NOT EXISTS cloud_ingestion_origin ON cloud_ingestion(json_extract(payload,'$.origin_job'));
CREATE TABLE IF NOT EXISTS cloud_transfer_requests (
 account_id TEXT NOT NULL, request_id TEXT NOT NULL, sealed TEXT NOT NULL,
 job_id TEXT NOT NULL, created REAL NOT NULL, PRIMARY KEY(account_id,request_id));
CREATE TABLE IF NOT EXISTS cloud_progress (
 job_id TEXT PRIMARY KEY, total INTEGER, processed INTEGER NOT NULL,
 phase TEXT NOT NULL, current TEXT NOT NULL, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS cloud_deletions (
 account_id TEXT NOT NULL, file_id TEXT NOT NULL, asset_id TEXT NOT NULL,
 state TEXT NOT NULL, payload TEXT NOT NULL, result TEXT NOT NULL,
 updated REAL NOT NULL, PRIMARY KEY(account_id,file_id));
"""


@contextmanager
def db():
    with storage._transaction() as con:
        if not con.in_transaction:
            con.execute("BEGIN IMMEDIATE")
        for statement in SCHEMA.split(";"):
            if statement.strip():
                con.execute(statement)
        yield con


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def ident():
    return uuid.uuid4().hex


def account(account_id):
    with db() as con:
        row = con.execute(
            "SELECT id,provider,uid,name,sealed,revision FROM cloud_accounts WHERE id=?",
            (account_id,),
        ).fetchone()
    return (
        dict(zip(("id", "provider", "uid", "name", "sealed", "revision"), row))
        if row
        else None
    )


def accounts():
    with db() as con:
        rows = con.execute(
            "SELECT id,provider,uid,name,revision FROM cloud_accounts ORDER BY updated DESC"
        ).fetchall()
    return [
        dict(zip(("id", "provider", "uid", "name", "revision"), row)) for row in rows
    ]


def save_file(account_id, row):
    with db() as con:
        con.execute(
            "INSERT OR REPLACE INTO cloud_files VALUES(?,?,?,?,?,?,?,?,?)",
            (
                account_id,
                row["id"],
                row["parent_id"],
                row["name"],
                row["size"],
                row["sha1"],
                row["pickcode"],
                int(row["is_dir"]),
                time.time(),
            ),
        )


def job(job_id):
    with db() as con:
        row = con.execute(
            "SELECT id,kind,state,result,message,created,updated FROM cloud_jobs WHERE id=?",
            (job_id,),
        ).fetchone()
    if not row:
        return None
    value = dict(
        zip(("id", "kind", "state", "result", "message", "created", "updated"), row)
    )
    value["result"] = json.loads(value["result"])
    with db() as con:
        due = con.execute("SELECT due,origin_job FROM cloud_job_due WHERE job_id=?", (job_id,)).fetchone()
        if due:
            value["due"] = due[0]
            value["origin_job"] = due[1]
        control = con.execute("SELECT stop_requested FROM cloud_job_controls WHERE job_id=?", (job_id,)).fetchone()
        value["stop_requested"] = bool(control and control[0])
        progress = con.execute(
            "SELECT total,processed,phase,current,updated FROM cloud_progress WHERE job_id=?",
            (job_id,),
        ).fetchone()
    if progress:
        value["progress"] = dict(
            zip(("total", "processed", "phase", "current", "updated"), progress)
        )
    return value


def jobs(limit=50):
    with db() as con:
        ids = [
            row[0]
            for row in con.execute(
                "SELECT id FROM cloud_jobs ORDER BY created DESC LIMIT ?", (limit,)
            )
        ]
    return [job(i) for i in ids]


def job_summaries(limit=50, query=''):
    """Poll bounded metadata, never deserialize whole file results for every job."""
    with db() as con:
        pattern = '%'+query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')+'%'
        rows = con.execute(
            "SELECT j.id,j.kind,j.state,j.message,j.created,j.updated,"
            "d.due,d.origin_job,c.stop_requested,p.total,p.processed,p.phase,p.current,p.updated "
            "FROM cloud_jobs j LEFT JOIN cloud_job_due d ON d.job_id=j.id "
            "LEFT JOIN cloud_job_controls c ON c.job_id=j.id "
            "LEFT JOIN cloud_progress p ON p.job_id=j.id "
            "WHERE (?='' OR j.id LIKE ? ESCAPE '\\' OR j.message LIKE ? ESCAPE '\\') "
            "ORDER BY j.created DESC,j.id LIMIT ?", (query, pattern, pattern, max(1, min(int(limit), 200)))
        ).fetchall()
        values = []
        for row in rows:
            value = dict(zip(('id', 'kind', 'state', 'message', 'created', 'updated'), row[:6]))
            if row[6] is not None:
                value.update(due=row[6], origin_job=row[7])
            value['stop_requested'] = bool(row[8])
            if row[10] is not None:
                value['progress'] = dict(zip(('total', 'processed', 'phase', 'current', 'updated'), row[9:]))
            values.append(value)
        _job_activity(con, values)
    return values


def attach_job_activity(value):
    with db() as con:
        _job_activity(con, [value])


def _job_activity(con, values):
    if not values:
        return
    ids = [v['id'] for v in values]
    marks = ','.join('?' for _ in ids)
    children = con.execute(
        f'SELECT d.job_id,d.origin_job,j.state FROM cloud_job_due d '
        f'JOIN cloud_jobs j ON j.id=d.job_id WHERE d.origin_job IN ({marks})', ids
    ).fetchall()
    owners = {child: parent for child, parent, _ in children}
    activity = {key: {'publication': {}, 'notification': {}, 'deferred': {}} for key in ids}
    for _, parent, state in children:
        group = activity[parent]['deferred']
        group[state] = group.get(state, 0) + 1
    # Read aggregate counters only: step payload/result can contain large file manifests.
    rows = con.execute(
        "SELECT job_id,CASE WHEN step_key LIKE 'publication:%' THEN 'publication' "
        "ELSE 'notification' END,state,COUNT(*) FROM cloud_steps "
        f"WHERE (job_id IN ({marks}) OR job_id IN "
        f"(SELECT job_id FROM cloud_job_due WHERE origin_job IN ({marks}))) "
        "AND (step_key LIKE 'publication:%' OR step_key LIKE 'poster:%' "
        "OR step_key='notification' OR step_key LIKE 'notification:%') GROUP BY 1,2,3",
        ids + ids
    ).fetchall()
    for job_id, kind, state, count in rows:
        for owner in {job_id, owners.get(job_id)} & activity.keys():
            group = activity[owner][kind]
            group[state] = group.get(state, 0) + count
    for value in values:
        a = activity[value['id']]
        p, n, d = a['publication'], a['notification'], a['deferred']
        waiting = sum(p.get(s, 0) + d.get(s, 0) for s in ('queued', 'running', 'dispatched'))
        sending = n.get('dispatched', 0)
        interrupted = d.get('cancelled', 0) + d.get('stopped', 0)
        attention = bool(interrupted) or any(group.get(s, 0) for group in (p, n, d) for s in ('unknown', 'uncertain', 'failed', 'needs_review'))
        a['active'] = value['state'] in ('queued', 'running') or bool(waiting or sending)
        a['needs_attention'] = value['state'] in ('failed', 'uncertain', 'needs_review') or attention
        if value.get('stop_requested') and value['state'] == 'running':
            a['label'] = '正在安全停止'
        elif value['state'] in ('failed', 'uncertain', 'needs_review'):
            a['label'] = {'failed': '执行失败', 'uncertain': '结果待核实', 'needs_review': '需要处理'}[value['state']]
        elif waiting and value['state'] != 'running':
            a['label'] = 'STRM 后续处理中'
        elif p.get('queued', 0) + p.get('dispatched', 0):
            a['label'] = '整理与 STRM 生成中'
        elif sending:
            a['label'] = '正在发送通知'
        elif interrupted:
            a['label'] = '后续生成已取消或停止'
        elif attention:
            a['label'] = '后续结果待核实'
        else:
            # 展示文案统一由 states 模块提供，避免状态词在多个文件里各写一遍。
            a['label'] = ('等待定时执行' if value.get('due') else '等待执行'
                          ) if value['state'] == 'queued' else states.JOB_STATE_LABELS.get(
                              value['state'], value['state'])
        value['activity'] = a


def account_has_active_jobs(account_id):
    """账号是否还有排队或执行中的任务 —— 断开账号前的安全检查。

    除了 payload.account_id，还要按 plan_id 反查：一个任务可能是用该账号创建的计划发起的。
    """
    with db() as con:
        row = con.execute(
            "SELECT 1 FROM cloud_jobs j WHERE state IN ('queued','running') AND "
            "(json_extract(payload,'$.account_id')=? OR EXISTS (SELECT 1 FROM cloud_plans p "
            "WHERE p.id=json_extract(j.payload,'$.plan_id') AND p.account_id=?)) LIMIT 1",
            (account_id, account_id),
        ).fetchone()
    return row is not None


def disconnect_account(account_id):
    """断开账号：删除账号及其目录设置与探测缓存，资产标记 disconnected，规则停用。

    调用方必须先确认没有未完成任务（见 account_has_active_jobs）。
    保留 cloud_jobs / cloud_steps 等历史记录，便于事后追溯。
    """
    with db() as con:
        con.execute("DELETE FROM cloud_accounts WHERE id=?", (account_id,))
        con.execute("DELETE FROM cloud_directory_settings WHERE account_id=?", (account_id,))
        con.execute("DELETE FROM cloud_media_probes WHERE account_id=?", (account_id,))
        con.execute("UPDATE cloud_assets SET state='disconnected' WHERE account_id=?", (account_id,))
        con.execute(
            "UPDATE cloud_rules SET enabled=0 WHERE json_extract(payload,'$.account_id')=?",
            (account_id,),
        )


def delete_channel(account_id, channel_id):
    """删除某个账号下的频道；account_id 参与条件，防止跨账号误删。"""
    with db() as con:
        affected = con.execute(
            "DELETE FROM cloud_channels WHERE id=? AND account_id=?",
            (channel_id, account_id),
        ).rowcount
    return affected


def asset(asset_id):
    with db() as con:
        row = con.execute(
            "SELECT id,account_id,file_id,local_path,payload,nonce,state FROM cloud_assets WHERE id=?",
            (asset_id,),
        ).fetchone()
    if not row:
        return None
    result = dict(
        zip(
            ("id", "account_id", "file_id", "local_path", "payload", "nonce", "state"),
            row,
        )
    )
    result["payload"] = json.loads(result["payload"])
    return result


def cancel_queued_job(job_id, now=None):
    """取消尚未开始的任务；返回受影响行数（0 表示已经开始，不能中断）。"""
    with db() as con:
        return con.execute(
            "UPDATE cloud_jobs SET state='cancelled',updated=? WHERE id=? AND state='queued'",
            (time.time() if now is None else now, job_id),
        ).rowcount


def list_rules():
    with db() as con:
        rows = con.execute(
            "SELECT id,payload,enabled,next_run FROM cloud_rules ORDER BY updated DESC"
        ).fetchall()
    return [{"id": r[0], **json.loads(r[1]), "enabled": bool(r[2]), "next_run": r[3]} for r in rows]


def append_job_result(job_id, updates):
    """把若干键合并进 cloud_jobs.result，返回合并后的完整结果。"""
    with db() as con:
        row = con.execute('SELECT result FROM cloud_jobs WHERE id=?', (job_id,)).fetchone()
        payload = json.loads(row[0]) if row and row[0] else {}
        payload.update(updates)
        con.execute('UPDATE cloud_jobs SET result=?,updated=? WHERE id=?',
                    (encode(payload), time.time(), job_id))
    return payload


def save_rule(rule_id, payload, enabled, due, now=None):
    with db() as con:
        con.execute("INSERT INTO cloud_rules VALUES(?,?,?,?,?)",
                    (rule_id, encode(payload), int(enabled), due,
                     time.time() if now is None else now))


def delete_rule(rule_id):
    with db() as con:
        return con.execute("DELETE FROM cloud_rules WHERE id=?", (rule_id,)).rowcount


def rule_payload(rule_id):
    with db() as con:
        row = con.execute("SELECT payload FROM cloud_rules WHERE id=?", (rule_id,)).fetchone()
    return json.loads(row[0]) if row else None


def account_revision(account_id):
    with db() as con:
        row = con.execute("SELECT revision FROM cloud_accounts WHERE id=?", (account_id,)).fetchone()
    return row[0] if row else None


def update_rule_enabled(rule_id, enabled, due, payload, now=None):
    with db() as con:
        con.execute(
            "UPDATE cloud_rules SET enabled=?,next_run=?,updated=?,payload=? WHERE id=?",
            (int(enabled), due, time.time() if now is None else now, encode(payload), rule_id))
