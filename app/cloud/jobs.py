"""Durable queue backed by the existing task manager; never replay uncertain writes."""

import json
import logging
import threading
import time
from app import tasks
from app.cloud import store, events
from app.cloud.errors import CloudError

log = logging.getLogger("strimkeep")
_STOP = threading.Event()
_WAKE = threading.Event()
_THREAD = None
_LOCK = threading.RLock()


class JobStopped(CloudError):
    def __init__(self, result=None):
        super().__init__("已在安全边界停止", 409)
        self.result = {**(result or {}), "stopped": True}


def stopping(job_id):
    if not job_id:
        return False
    with store.db() as con:
        row = con.execute("SELECT stop_requested FROM cloud_job_controls WHERE job_id=?", (job_id,)).fetchone()
    return bool(row and row[0])


def checkpoint(job_id, result=None):
    if stopping(job_id):
        raise JobStopped(result)


def request_stop(job_id):
    with store.db() as con:
        row = con.execute("SELECT kind,state,payload FROM cloud_jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise CloudError("任务不存在", 404)
        if row[0] not in ("organize", "workflow", "transfer", "sync", "share_strm"):
            raise CloudError("此任务不支持停止整理", 409)
        if row[1] not in ("queued", "running", "stopped"):
            raise CloudError("任务已结束，请查看结果", 409)
        if row[1] == "stopped":
            return {"stop_requested": True, "rule_paused": False, "message": "任务已安全停止"}
        payload = json.loads(row[2])
        rule_id = payload.get("rule_id")
        if rule_id:
            con.execute("UPDATE cloud_rules SET enabled=0,updated=? WHERE id=?", (time.time(), rule_id))
        con.execute("INSERT OR REPLACE INTO cloud_job_controls VALUES(?,?,?)", (job_id, 1, time.time()))
        if row[1] == "queued":
            con.execute("UPDATE cloud_jobs SET state='stopped',message='执行前停止',updated=? WHERE id=? AND state='queued'", (time.time(), job_id))
            if payload.get("plan_id"):
                con.execute("UPDATE cloud_plans SET state='stopped' WHERE id=? AND state='queued'", (payload["plan_id"],))
    events.emit('queue','停止请求已保存，等待当前操作核实后停止',job_id)
    return {"stop_requested": True, "rule_paused": bool(rule_id), "message": "停止请求已保存；当前视频及关联文件完成后停止，已发出的转存请求先核实结果"}


def enqueue(kind, payload, con=None):
    if kind not in ("transfer", "organize", "sync", "workflow", "categories", "publish", "share_strm", "intake", "channel", "maintenance"):
        raise CloudError("任务类型不受支持")
    job_id = store.ident()
    now = time.time()

    def insert(db):
        db.execute(
            "INSERT INTO cloud_jobs(id,kind,state,payload,created,updated) VALUES(?,?,?,?,?,?)",
            (job_id, kind, "queued", store.encode(payload), now, now),
        )
        events.emit('queue', '任务已排队：'+kind, job_id, con=db)

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
    stage = 'naming' if key.startswith('rename:') else 'organize' if key.startswith(('move:','mkdir:')) else 'transfer' if key=='receive' else None
    label = '重命名' if key.startswith('rename:') else '移动归档' if key.startswith('move:') else '创建目录' if key.startswith('mkdir:') else '转存请求'
    if stage:
        events.emit(stage,label+'开始'+('：'+str(payload.get('name')) if payload.get('name') else ''),job_id)
    try:
        result = fn()
    except CloudError as error:
        if stage:
            events.emit(stage,label+('结果待核实：' if error.uncertain else '未完成：')+str(error),job_id,level='WARNING')
        raise
    with store.db() as con:
        con.execute(
            "UPDATE cloud_steps SET state=?,result=?,updated=? WHERE job_id=? AND step_key=?",
            ("verified", store.encode(result), time.time(), job_id, key),
        )
    if stage:
        events.emit(stage,label+'结果已核实'+('：'+str(payload.get('name')) if payload.get('name') else ''),job_id)
    return result


def progress(job_id, total, processed, phase, current=""):
    if processed < 0 or (total is not None and not processed <= total <= 100000):
        raise CloudError("任务进度无效")
    with store.db() as con:
        previous = con.execute("SELECT total,processed,phase,current FROM cloud_progress WHERE job_id=?",(job_id,)).fetchone()
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
        now = (total,processed,str(phase)[:80],str(current)[:512])
        if previous != now:
            stage = 'transfer' if '转存' in phase or '资源' in phase else 'recognize' if '识别' in phase else 'strm' if 'STRM' in phase else 'organize'
            events.emit(stage,str(phase)+' · '+str(processed)+'/'+str(total if total is not None else '?')+(' · '+str(current) if current else ''),job_id,con=con)


def execute(job_id):
    token=events._JOB.set(job_id)
    try:
        return _execute(job_id)
    finally:
        events._JOB.reset(token)


def _execute(job_id):
    from app.cloud import workflow

    with store.db() as con:
        changed = con.execute(
            "UPDATE cloud_jobs SET state='running',updated=? WHERE id=? AND state='queued' "
            "AND NOT EXISTS (SELECT 1 FROM cloud_job_due WHERE job_id=cloud_jobs.id AND due>?)",
            (time.time(), job_id, time.time()),
        ).rowcount
        if not changed:
            return {"status": "error", "message": "任务已被领取或结束"}
        row = con.execute(
            "SELECT kind,payload FROM cloud_jobs WHERE id=?", (job_id,)
        ).fetchone()
    events.emit('queue','开始执行：'+row[0],job_id)
    try:
        progress(job_id, None, 0, "准备")
        checkpoint(job_id)
        result = workflow.run(job_id, row[0], json.loads(row[1]))
        if stopping(job_id):
            result["stopped"] = True
        state = (
            "needs_review"
            if result.get("errors")
            or result.get("skipped")
            or result.get("refresh_pending")
            else "success"
        )
        if result.get("stopped"):
            state = "stopped"
        message = "已在安全边界停止" if state == "stopped" else "部分项目需要处理" if state == "needs_review" else "任务完成"
        if state == "success" and row[0] in ("intake", "channel"):
            message = "接收请求已提交，等待转存任务或115云下载完成" if result.get("accepted") else "规则未匹配，未接收资源"
    except JobStopped as error:
        result, state, message = error.result, "stopped", str(error)
    except CloudError as error:
        result = getattr(error, "partial_result", {})
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
    events.emit('queue',message+' · '+state,job_id,level='ERROR' if state=='failed' else 'WARNING' if state in ('uncertain','needs_review') else 'INFO')
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

    cfg = settings.get()
    category = {"intake":"transfer", "transfer":"transfer", "organize":"organize", "workflow":"organize", "categories":"organize", "sync":"strm", "publish":"strm", "share_strm":"strm", "channel":"channel", "maintenance":"maintenance"}.get(kind, "organize")
    if not cfg["notify_enabled"] or not cfg["notify_"+category]:
        events.emit('notify','此类任务通知已关闭，本次不发送',job_id)
        return
    from app.tg import notify_telegram
    import html

    names = {
        "transfer": "转存",
        "organize": "整理",
        "sync": "STRM 更新",
        "workflow": "自动整理",
        "categories": "创建分类",
        "publish": "延时 STRM",
        "share_strm": "分享免转存 STRM", "intake": "资源接收", "channel": "频道接收", "maintenance": "日常维护",
    }
    try:
        job = store.job(job_id)
        text = "115 " + names.get(kind, kind) + "：" + html.escape(message)
        result = (job or {}).get("result", {})
        operation=result.get('organization') or result
        generated=operation.get('generated',[])
        errors=operation.get('errors',[])
        if kind in ('workflow','organize','sync','publish','share_strm'):
            text+='\nSTRM 更新：'+str(len(generated))+' 个\n异常：'+str(len(errors))+' 个'
            if operation.get('deferred'):text+='\n等待联动生成：'+str(len(operation['deferred']))+' 项'
            if generated:text+='\n生成完成；Emby 实际入库以确认通知为准。'
        sent=bool(notify_telegram(text))
        if cfg['notify_strm_details'] and generated:
            paths=[]
            for item in generated:
                path=item.get('path','') if isinstance(item,dict) else item
                if path:paths.append(('🔗',str(path)))
                asset=store.asset(item.get('asset_id','')) if isinstance(item,dict) and item.get('asset_id') else None
                if asset:
                    paths.extend(('⬇️',str(v['path'])) for v in asset['payload'].get('owned',[]) if v.get('path'))
            paths=list(dict.fromkeys(paths))
            batches=[paths[i:i+20] for i in range(0,len(paths),20)]
            for index,chunk in enumerate(batches,1):
                detail='📄 STRM 文件明细 '+str(index)+'/'+str(len(batches))+'\n'
                detail+='\n'.join(mark+' '+html.escape(path) for mark,path in chunk)
                if not notify_telegram(detail):sent=False;break
    except Exception:
        sent = False
    events.emit('notify','任务通知已发送' if sent else '任务通知发送失败或结果未知，请核对机器人配置',job_id,level='INFO' if sent else 'WARNING')
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
    from app.cloud import replacement
    result['upgrades'] = replacement.summary(job_id)
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
    from app.cloud import schedules
    from app.cloud import maintenance, receiving
    maintenance.schedule()
    receiving.schedule()
    from app.cloud import channel_reader, emby_library
    channel_reader.schedule()
    emby_library.schedule()
    now = time.time()
    with store.db() as con:
        rows = con.execute(
            "SELECT id,payload FROM cloud_rules WHERE enabled=1 AND next_run<=?", (now,)
        ).fetchall()
        for rid, raw in rows:
            # One pending run per rule; a failed run does not duplicate remote writes.
            data = json.loads(raw)
            try:
                account = con.execute("SELECT revision FROM cloud_accounts WHERE id=?", (data["account_id"],)).fetchone()
                if not account or account[0] != data["account_revision"]:
                    raise CloudError("账号授权变化，规则已暂停，请重新创建")
                due = schedules.next_run(data, now)
            except CloudError as error:
                data["schedule_error"] = str(error)
                con.execute("UPDATE cloud_rules SET enabled=0,payload=?,updated=? WHERE id=?", (store.encode(data), now, rid))
                continue
            active = con.execute(
                "SELECT 1 FROM cloud_jobs WHERE state IN ('queued','running') AND json_extract(payload,'$.rule_id')=?",
                (rid,),
            ).fetchone()
            if not active:
                enqueue("sync" if data.get("kind") == "sync" else "workflow", {**data, "rule_id": rid}, con)
            con.execute(
                "UPDATE cloud_rules SET next_run=?,updated=? WHERE id=?",
                (due, now, rid),
            )


def _loop():
    while not _STOP.is_set():
        try:
            schedule()
            with store.db() as con:
                row = con.execute(
                    "SELECT j.id FROM cloud_jobs j LEFT JOIN cloud_job_due d ON d.job_id=j.id "
                    "WHERE j.state='queued' AND (d.due IS NULL OR d.due<=?) ORDER BY j.created LIMIT 1", (time.time(),)
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
