"""Durable queue backed by the existing task manager; never replay uncertain writes."""

import json
import logging
import threading
import time
from app import tasks
from app.cloud import store
from app.cloud.errors import CloudError

log = logging.getLogger("strimkeep")
_STOP = threading.Event()
_WAKE = threading.Event()
_THREAD = None
_LOCK = threading.RLock()


def enqueue(kind, payload, con=None):
    if kind not in ("transfer", "organize", "sync", "workflow", "categories"):
        raise CloudError("任务类型不受支持")
    job_id = store.ident()
    now = time.time()

    def insert(db):
        db.execute(
            "INSERT INTO cloud_jobs(id,kind,state,payload,created,updated) VALUES(?,?,?,?,?,?)",
            (job_id, kind, "queued", store.encode(payload), now, now),
        )

    if con is None:
        with store.db() as db:
            insert(db)
    else:
        insert(con)
    _WAKE.set()
    return job_id


def step(job_id, key, payload, fn):
    with store.db() as con:
        row = con.execute(
            "SELECT state,result FROM cloud_steps WHERE job_id=? AND step_key=?",
            (job_id, key),
        ).fetchone()
        if row:
            if row[0] == "verified":
                return json.loads(row[1])
            raise CloudError("此前操作结果尚未核实，禁止重复执行", 409, uncertain=True)
        con.execute(
            "INSERT INTO cloud_steps VALUES(?,?,?,?,?,?)",
            (job_id, key, "dispatched", store.encode(payload), "{}", time.time()),
        )
    result = fn()
    with store.db() as con:
        con.execute(
            "UPDATE cloud_steps SET state=?,result=?,updated=? WHERE job_id=? AND step_key=?",
            ("verified", store.encode(result), time.time(), job_id, key),
        )
    return result


def progress(job_id, total, processed, phase, current=""):
    if processed < 0 or (total is not None and not processed <= total <= 100000):
        raise CloudError("任务进度无效")
    with store.db() as con:
        con.execute(
            "INSERT INTO cloud_progress VALUES(?,?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET "
            "total=excluded.total,processed=excluded.processed,phase=excluded.phase,current=excluded.current,updated=excluded.updated",
            (
                job_id,
                total,
                processed,
                str(phase)[:80],
                str(current)[:512],
                time.time(),
            ),
        )


def execute(job_id):
    from app.cloud import workflow

    with store.db() as con:
        changed = con.execute(
            "UPDATE cloud_jobs SET state='running',updated=? WHERE id=? AND state='queued'",
            (time.time(), job_id),
        ).rowcount
        if not changed:
            return {"status": "error", "message": "任务已被领取或结束"}
        row = con.execute(
            "SELECT kind,payload FROM cloud_jobs WHERE id=?", (job_id,)
        ).fetchone()
    try:
        progress(job_id, None, 0, "准备")
        result = workflow.run(job_id, row[0], json.loads(row[1]))
        state = (
            "needs_review"
            if result.get("errors")
            or result.get("skipped")
            or result.get("refresh_pending")
            else "success"
        )
        message = "部分项目需要处理" if state == "needs_review" else "任务完成"
    except CloudError as error:
        result = {}
        state = "uncertain" if error.uncertain else "failed"
        message = str(error)
    except Exception:
        result = {}
        state = "needs_review"
        message = "任务异常，已停止；请查看步骤记录核实结果"
        # Provider exceptions can contain credentials: never log raw exception text.
        log.error("网盘任务 %s 异常，结果需要核实", job_id)
    with store.db() as con:
        con.execute(
            "UPDATE cloud_jobs SET state=?,result=?,message=?,updated=? WHERE id=?",
            (state, store.encode(result), message, time.time(), job_id),
        )
    try:
        notify(job_id, row[0], state, message)
    except Exception:
        log.warning("网盘任务通知记录失败，任务结果已保存")
    return {
        "status": "success" if state == "success" else "error",
        "message": message,
        "job_id": job_id,
    }


def notify(job_id, kind, state, message):
    from app.cloud import settings

    if not settings.get()["notify_enabled"]:
        return
    from app.tg import notify_telegram
    import html

    names = {
        "transfer": "转存",
        "organize": "整理",
        "sync": "STRM 更新",
        "workflow": "自动整理",
        "categories": "创建分类",
    }
    try:
        sent = bool(notify_telegram("115 " + names[kind] + "：" + html.escape(message)))
    except Exception:
        sent = False
    with store.db() as con:
        con.execute(
            "INSERT OR REPLACE INTO cloud_steps VALUES(?,?,?,?,?,?)",
            (
                job_id,
                "notification",
                "verified" if sent else "failed",
                "{}",
                store.encode({"sent": sent}),
                time.time(),
            ),
        )


def details(job_id):
    result = store.job(job_id)
    if not result:
        raise CloudError("任务不存在", 404)
    with store.db() as con:
        rows = con.execute(
            "SELECT step_key,state,result FROM cloud_steps WHERE job_id=? ORDER BY updated",
            (job_id,),
        ).fetchall()
    result["steps"] = [
        {"step": r[0], "state": r[1], "result": json.loads(r[2])} for r in rows
    ]
    return result


def continue_transfer(job_id, confirmed=False):
    """Continue a reconciled receipt once, without resending its share_receive."""
    from app.cloud import workflow
    from app.cloud.provider115 import for_account

    if confirmed is not True:
        raise CloudError("请确认继续整理")
    with store.db() as con:
        row = con.execute(
            "SELECT kind,state,payload,result FROM cloud_jobs WHERE id=?", (job_id,)
        ).fetchone()
        existing = con.execute(
            "SELECT result FROM cloud_steps WHERE job_id=? AND step_key='continue'",
            (job_id,),
        ).fetchone()
    if existing:
        return json.loads(existing[0])
    if not row or row[0] != "transfer" or row[1] != "success":
        raise CloudError("请先核实转存结果", 409)
    data = json.loads(row[2])
    receipt = json.loads(row[3])
    if not receipt.get("continuation_required") or not data.get("organize_target"):
        raise CloudError("此任务没有待继续的整理", 409)
    provider = for_account(data["account_id"])
    provider.profile()
    stage = provider.info(receipt["stage_id"])
    if (
        not stage["is_dir"]
        or stage["parent_id"] != data["target_cid"]
        or stage["name"] != "转存待整理-" + job_id[:12]
    ):
        raise CloudError("任务目录身份已变化，请重新选择来源预览", 409)
    files = workflow.walk(provider, stage["id"])
    old = {x["id"]: x for x in receipt["files"]}
    if {x["id"] for x in files} != set(old) or any(
        not workflow._snapshot_equal(old[x["id"]], x) for x in files
    ):
        raise CloudError("转存内容在核实后变化，请重新预览", 409)
    planned = workflow.plan(data["account_id"], stage["id"], data["organize_target"])
    if not planned["items"]:
        raise CloudError("没有可以继续整理的项目，请在整理页修正作品", 409)
    with store.db() as con:
        existing = con.execute(
            "SELECT result FROM cloud_steps WHERE job_id=? AND step_key='continue'",
            (job_id,),
        ).fetchone()
        if existing:
            return json.loads(existing[0])
        con.execute(
            "UPDATE cloud_plans SET state='queued' WHERE id=? AND state='pending'",
            (planned["id"],),
        )
        next_id = enqueue(
            "organize",
            {"plan_id": planned["id"], "generate": data.get("generate", True)},
            con,
        )
        result = {"job_id": next_id, "message": "继续整理已入队，转存请求没有重发"}
        con.execute(
            "INSERT INTO cloud_steps VALUES(?,?,?,?,?,?)",
            (job_id, "continue", "verified", "{}", store.encode(result), time.time()),
        )
    return result


def verify_transfer(job_id):
    """Read-only reconciliation. Never resends a timed-out receive request."""
    from app.cloud.provider115 import for_account
    from app.cloud import workflow

    with store.db() as con:
        row = con.execute(
            "SELECT kind,state,payload FROM cloud_jobs WHERE id=?", (job_id,)
        ).fetchone()
        step_row = con.execute(
            "SELECT payload FROM cloud_steps WHERE job_id=? AND step_key='receive'",
            (job_id,),
        ).fetchone()
    if (
        not row
        or row[0] != "transfer"
        or row[1] not in ("uncertain", "needs_review")
        or not step_row
    ):
        raise CloudError("该任务没有需要核实的转存步骤", 409)
    payload = json.loads(row[2])
    intent = json.loads(step_row[0])
    provider = for_account(payload["account_id"])
    provider.profile()
    stage = provider.info(intent["stage_id"])
    if (
        not stage["is_dir"]
        or stage["parent_id"] != payload["target_cid"]
        or stage["name"] != "转存待整理-" + job_id[:12]
    ):
        raise CloudError("转存目录身份已变化，保留待核实状态", 409)
    received = provider.list(stage["id"])
    mapping = workflow.receipt_mapping(payload, received)
    files = workflow.walk(provider, stage["id"])
    for file in files:
        store.save_file(payload["account_id"], file)
    result = {
        "stage_id": stage["id"],
        "files": files,
        "mapping": mapping,
        "continuation_required": bool(payload.get("organize_target")),
        "message": (
            "转存已核实，未重新发送请求；可继续整理已核实的文件，或在文件页手动预览"
            if payload.get("organize_target")
            else "转存已核实，未重新发送请求"
        ),
    }
    with store.db() as con:
        changed = con.execute(
            "UPDATE cloud_jobs SET state='success',result=?,message=?,updated=? WHERE id=? AND state IN ('uncertain','needs_review')",
            (store.encode(result), "转存结果已核实", time.time(), job_id),
        ).rowcount
        if not changed:
            raise CloudError("任务状态已变化，请刷新记录", 409)
        con.execute(
            "UPDATE cloud_steps SET state='verified',result=?,updated=? WHERE job_id=? AND step_key='receive'",
            (store.encode(result), time.time(), job_id),
        )
    return result


def schedule():
    now = time.time()
    with store.db() as con:
        rows = con.execute(
            "SELECT id,payload FROM cloud_rules WHERE enabled=1 AND next_run<=?", (now,)
        ).fetchall()
        for rid, raw in rows:
            # One pending run per rule; a failed run does not duplicate remote writes.
            data = json.loads(raw)
            active = con.execute(
                "SELECT 1 FROM cloud_jobs WHERE state IN ('queued','running') AND json_extract(payload,'$.rule_id')=?",
                (rid,),
            ).fetchone()
            if not active:
                enqueue("workflow", {**data, "rule_id": rid}, con)
            con.execute(
                "UPDATE cloud_rules SET next_run=?,updated=? WHERE id=?",
                (now + max(300, int(data.get("interval_seconds", 3600))), now, rid),
            )


def _loop():
    while not _STOP.is_set():
        try:
            schedule()
            with store.db() as con:
                row = con.execute(
                    "SELECT id FROM cloud_jobs WHERE state='queued' ORDER BY created LIMIT 1"
                ).fetchone()
            if row:
                try:
                    task = tasks.manager.spawn(
                        "cloud_workbench",
                        execute,
                        row[0],
                        exclusive=True,
                        source="cloud",
                    )
                except tasks.TaskBusy:
                    pass
                else:
                    while not task.done.wait(0.5):
                        if _STOP.is_set():
                            return
        except Exception:
            log.error("网盘任务调度失败，保留待处理记录")
        _WAKE.wait(2)
        _WAKE.clear()


def start():
    global _THREAD
    with _LOCK:
        if _THREAD and _THREAD.is_alive():
            return
        with store.db() as con:
            con.execute(
                "UPDATE cloud_jobs SET state='needs_review',message='服务重启，操作结果需核实',updated=? WHERE state='running'",
                (time.time(),),
            )
        _STOP.clear()
        _THREAD = threading.Thread(target=_loop, name="cloud-workbench", daemon=True)
        _THREAD.start()


def stop():
    global _THREAD
    _STOP.set()
    _WAKE.set()
    if _THREAD:
        _THREAD.join(3)
    _THREAD = None
