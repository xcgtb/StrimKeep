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
    started = time.monotonic()
    diagnostic = {'event':key.split(':')[0], 'file_id':payload.get('file_id'),
                  'source_name':payload.get('source_name'), 'target_name':payload.get('name'),
                  'parent_id':payload.get('parent'), 'target_id':payload.get('target'),
                  'sha1':payload.get('sha1'), 'size':payload.get('size')}
    if stage:
        events.emit(stage,label+'开始'+('：'+str(payload.get('name')) if payload.get('name') else ''),job_id,details={**diagnostic,'status':'started'})
    try:
        result = fn()
    except CloudError as error:
        with store.db() as con:
            con.execute("UPDATE cloud_steps SET result=?,updated=? WHERE job_id=? AND step_key=? AND state='dispatched'",
                        (store.encode({'reason': events.clean(str(error)), 'uncertain': error.uncertain}), time.time(), job_id, key))
        if stage:
            events.emit(stage,label+('结果待核实：' if error.uncertain else '未完成：')+str(error),job_id,level='WARNING',details={**diagnostic,'status':'uncertain' if error.uncertain else 'failed','elapsed_ms':int((time.monotonic()-started)*1000),'reason':str(error)})
        raise
    except Exception as error:
        # The exception type is useful; provider exception text can carry credentials.
        reason = '调用出现 '+type(error).__name__+'，该操作结果未核实；先查看文件当前状态，不要重复提交。'
        with store.db() as con:
            con.execute("UPDATE cloud_steps SET result=?,updated=? WHERE job_id=? AND step_key=? AND state='dispatched'",
                        (store.encode({'reason': reason, 'uncertain': True}), time.time(), job_id, key))
        if stage:
            events.emit(stage, label+'结果未核实：'+reason, job_id, level='WARNING', details={**diagnostic, 'status': 'uncertain', 'reason': reason})
        raise
    with store.db() as con:
        con.execute(
            "UPDATE cloud_steps SET state=?,result=?,updated=? WHERE job_id=? AND step_key=?",
            ("verified", store.encode(result), time.time(), job_id, key),
        )
    if stage:
        events.emit(stage,label+'结果已核实'+('：'+str(payload.get('name')) if payload.get('name') else ''),job_id,details={**diagnostic,'status':'verified','elapsed_ms':int((time.monotonic()-started)*1000)})
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
    from app.cloud import workflow, diagnostics

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
    started = time.monotonic()
    events.emit('queue','开始执行：'+row[0],job_id,details={'event':row[0],'status':'started'})
    try:
        progress(job_id, None, 0, "准备")
        checkpoint(job_id)
        result = workflow.run(job_id, row[0], json.loads(row[1]))
        if stopping(job_id):
            result["stopped"] = True
        state = (
            "needs_review"
            if diagnostics.requires_review(result)
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
    events.emit('queue',message+' · '+state,job_id,level='ERROR' if state=='failed' else 'WARNING' if state in ('uncertain','needs_review') else 'INFO',details={'event':row[0],'status':state,'elapsed_ms':int((time.monotonic()-started)*1000),'result':message})
    diagnostics.emit_result(job_id, result, state, message)
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
    from app.cloud import settings, notifications, diagnostics

    cfg = settings.get()
    category = {"intake":"transfer", "transfer":"transfer", "organize":"organize", "workflow":"organize", "categories":"organize", "sync":"strm", "publish":"strm", "share_strm":"strm", "channel":"channel", "maintenance":"maintenance"}.get(kind, "organize")
    job = store.job(job_id)
    result = (job or {}).get("result", {})
    operation = result.get('organization') or result
    notice_key = 'notification'
    if kind == 'publish':
        combined = notifications.deferred_result(job_id)
        if combined is None:
            events.emit('notify','等待本批延时 STRM 发布收尾，暂不发送零散通知',job_id)
            return
        job_id, operation = combined
        notice_key = 'notification:deferred'
        state = 'needs_review' if operation['errors'] else 'success'
        message = '延时 STRM 批量完成' if not operation['errors'] else '延时 STRM 部分项目需要处理'
    if operation.get('deferred') and not operation.get('generated'):
        events.emit('strm','已安排延时 STRM 发布；落盘后统一发送海报与结果',job_id,details={'event':'deferred','status':'queued','count':len(operation['deferred'])})
        return
    if kind in ('organize','workflow','sync','share_strm') and state == 'success' and not any(operation.get(k) for k in ('files','unknown','generated','deferred','errors','skipped','refresh_pending')) and not operation.get('reconciliation',{}).get('removed'):
        events.emit('strm','本次没有新增或更新内容，跳过 STRM 通知与 Emby 刷新',job_id,details={'event':'empty_batch','status':'skipped','count':0})
        return
    # A combined organize+STRM job follows the STRM switch for its result;
    # the earlier work poster independently follows notify_organize.
    if operation.get('generated') or operation.get('deferred'):
        category = 'strm'
    if not cfg["notify_enabled"] or not cfg["notify_"+category]:
        reason = '网盘任务通知总开关 notify_enabled 已关闭' if not cfg['notify_enabled'] else '分模块通知 notify_'+category+' 已关闭'
        events.emit('notify','此类任务通知已关闭，本次不发送：'+reason,job_id)
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
        text = "115 " + names.get(kind, kind) + "：" + html.escape(message)
        generated=operation.get('generated',[])
        rows=[]
        if kind in ('workflow','organize','sync','publish','share_strm') or result.get('organization'):
            problems = diagnostics.for_job(job) if job and kind != 'publish' else None
            text, rows = notifications.batch_result(operation,state,message,job_id=job_id,kind=kind,diagnostic_report=problems)
        sent=notifications.deliver(job_id, notice_key, lambda: notify_telegram(text))
        if sent and cfg['notify_strm_details'] and generated:
            for index, detail in enumerate(notifications.detail_batches(rows),1):
                if not notifications.deliver(job_id, notice_key+':details:'+str(index), lambda: notify_telegram(detail)):
                    sent=False
                    break
    except Exception:
        sent = False
        events.emit('notify','任务通知内容处理失败，结果记录已保存，请查看任务详情',job_id,level='WARNING')


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
    store.attach_job_activity(result)
    from app.cloud import diagnostics
    result['diagnostics'] = diagnostics.for_job(result)
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
    stage = workflow.transfer_container(provider, data, receipt, job_id)
    source_ids = [r["target_id"] for r in receipt["mapping"]] if receipt.get("receipt_mode") == "direct" else None
    files = workflow.walk(provider, stage["id"], source_ids=source_ids)
    old = {x["id"]: x for x in receipt["files"]}
    if {x["id"] for x in files} != set(old) or any(
        not workflow._snapshot_equal(old[x["id"]], x) for x in files
    ):
        raise CloudError("转存内容在核实后变化，请重新预览", 409)
    planned = workflow.plan(data["account_id"], stage["id"], data["organize_target"], source_ids=source_ids)
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
    result = {
        **workflow.transfer_receipt(provider, payload, intent, job_id),
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
    from app.cloud import offline_handoff
    offline_handoff.schedule()
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
                        # Scans must continue while organization/metadata work
                        # is running; this scheduler never submits 115 writes.
                        from app.cloud import emby_library, offline_handoff
                        emby_library.schedule()
                        offline_handoff.schedule()
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
