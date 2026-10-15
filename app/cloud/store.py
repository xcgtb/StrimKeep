"""Cloud records in the existing StrimKeep SQLite database."""

import json
import time
import uuid
from contextlib import contextmanager
from app import storage

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
CREATE TABLE IF NOT EXISTS cloud_job_controls (
 job_id TEXT PRIMARY KEY, stop_requested INTEGER NOT NULL, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS cloud_job_due (
 job_id TEXT PRIMARY KEY, due REAL NOT NULL, origin_job TEXT NOT NULL,
 file_id TEXT NOT NULL, created REAL NOT NULL, UNIQUE(origin_job,file_id));
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
