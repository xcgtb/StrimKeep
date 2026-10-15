"""Opt-in daily check-in and retention-based explicit recycle IDs, with receipts."""
import hashlib
import json
import re
import time
from datetime import datetime
from app.cloud import services, credentials, store, jobs, schedules, workflow
from app.cloud.errors import CloudError

DEFAULT = {"signin_enabled":False,"signin_cron":"0 8 * * *","recycle_enabled":False,"recycle_cron":"0 4 * * *","timezone":"Asia/Shanghai","retention_days":7,"sealed_password":"","next_signin":0,"next_recycle":0,"last_error":""}


def raw(account_id):
    return services.get(account_id,"maintenance",DEFAULT)


def get(account_id):
    value=raw(account_id)
    value["config"]["password_saved"]=bool(value["config"].pop("sealed_password",""))
    return value


def save(account_id,patch,expected,confirmed=False):
    if set(patch)-{"signin_enabled","signin_cron","recycle_enabled","recycle_cron","timezone","retention_days","password"}:
        raise CloudError("维护配置字段无效")
    def validate(cfg,old):
        if any(type(cfg[k]) is not bool for k in ("signin_enabled","recycle_enabled")) or type(cfg["retention_days"]) is not int or not 0<=cfg["retention_days"]<=3650:
            raise CloudError("维护开关或回收站保留天数无效")
        schedules.zone(cfg["timezone"])
        for action in ("signin","recycle"):
            cfg["next_"+action]=schedules.cron_next(cfg[action+"_cron"],cfg["timezone"],time.time())
        if cfg["recycle_enabled"] and (not old["config"]["recycle_enabled"] or any(cfg[k]!=old["config"][k] for k in ("retention_days","recycle_cron","timezone"))) and confirmed is not True:
            raise CloudError("请确认按保留天数永久清理回收站",409)
        password=cfg.pop("password","")
        if password:
            if not isinstance(password,str) or not re.fullmatch(r"\d{6}",password):raise CloudError("回收站安全密钥应为6位数字")
            cfg["sealed_password"]=credentials.seal({"password":password})
        cfg["last_error"]=""
        return cfg
    services.save(account_id,"maintenance",patch,expected,DEFAULT,validate)
    return get(account_id)


def blocked(account_id):
    with store.db() as con:
        pending=con.execute("SELECT 1 FROM cloud_deletions WHERE account_id=? AND state IN ('dispatched','acknowledged','uncertain') LIMIT 1",(account_id,)).fetchone()
        uncertain=con.execute("SELECT 1 FROM cloud_jobs WHERE kind='maintenance' AND json_extract(payload,'$.account_id')=? AND json_extract(payload,'$.action')='recycle' AND state IN ('running','uncertain','needs_review') LIMIT 1",(account_id,)).fetchone()
    return bool(pending or uncertain)


def enqueue(account_id,action,confirmed=False):
    if action not in ("signin","recycle") or confirmed is not True:raise CloudError("请确认维护操作")
    cfg=raw(account_id)
    if action=="recycle" and blocked(account_id):raise CloudError("存在待核实的来源删除或回收站任务，先核实回执再清理",409)
    day=datetime.now(schedules.zone(cfg["config"]["timezone"])).date().isoformat()
    with store.db() as con:
        if action=="signin":
            old=con.execute("SELECT id FROM cloud_jobs WHERE kind='maintenance' AND json_extract(payload,'$.account_id')=? AND json_extract(payload,'$.action')='signin' AND json_extract(payload,'$.day')=? AND state IN ('queued','running','success','uncertain')",(account_id,day)).fetchone()
            if old:return old[0]
        return jobs.enqueue("maintenance",{"account_id":account_id,"account_revision":cfg["account_revision"],"maintenance_revision":cfg["revision"],"action":action,"day":day},con)


def schedule():
    now=time.time()
    with store.db() as con:
        rows=con.execute("SELECT account_id,payload,revision,account_revision FROM cloud_service_settings WHERE kind='maintenance'").fetchall()
        for aid,record,rev,arev in rows:
            cfg={**DEFAULT,**json.loads(record)}
            account=con.execute("SELECT revision FROM cloud_accounts WHERE id=?",(aid,)).fetchone()
            if not account or account[0]!=arev:
                cfg.update(signin_enabled=False,recycle_enabled=False,last_error="账号凭据变化，维护已暂停，请重新保存")
            else:
                for action in ("signin","recycle"):
                    if not cfg[action+"_enabled"] or cfg["next_"+action]>now:continue
                    day=datetime.fromtimestamp(now,schedules.zone(cfg["timezone"])).date().isoformat()
                    active=con.execute("SELECT 1 FROM cloud_jobs WHERE kind='maintenance' AND json_extract(payload,'$.account_id')=? AND json_extract(payload,'$.action')=? AND state IN ('queued','running','uncertain','needs_review') AND (?!='signin' OR json_extract(payload,'$.day')=?)",(aid,action,action,day)).fetchone()
                    if not active:
                        same_day=con.execute("SELECT 1 FROM cloud_jobs WHERE kind='maintenance' AND json_extract(payload,'$.account_id')=? AND json_extract(payload,'$.action')='signin' AND json_extract(payload,'$.day')=? AND state='success'",(aid,day)).fetchone()
                        if action!="signin" or not same_day:jobs.enqueue("maintenance",{"account_id":aid,"account_revision":arev,"maintenance_revision":rev,"action":action,"day":day},con)
                    cfg["next_"+action]=schedules.cron_next(cfg[action+"_cron"],cfg["timezone"],now)
            con.execute("UPDATE cloud_service_settings SET payload=? WHERE account_id=? AND kind='maintenance'",(store.encode(cfg),aid))


def run(job_id,payload):
    provider=workflow.verify_account(payload)
    cfg=raw(payload["account_id"])
    if cfg["revision"]!=payload["maintenance_revision"]:raise CloudError("维护配置已变化，请重新确认",409)
    if payload["action"]=="signin":
        return jobs.step(job_id,"signin",{},provider.sign_in)
    # Exclude this running task from the uncertainty gate.
    with store.db() as con:
        pending=con.execute("SELECT 1 FROM cloud_deletions WHERE account_id=? AND state IN ('dispatched','acknowledged','uncertain') LIMIT 1",(payload["account_id"],)).fetchone()
        older=con.execute("SELECT 1 FROM cloud_jobs WHERE id!=? AND kind='maintenance' AND json_extract(payload,'$.account_id')=? AND json_extract(payload,'$.action')='recycle' AND state IN ('running','uncertain','needs_review')",(job_id,payload["account_id"])).fetchone()
    if pending or older:raise CloudError("存在未核实删除，保留回收站条目",409)
    config=cfg["config"]
    cutoff=time.time()-config["retention_days"]*86400
    snapshot=provider.recycle_items()
    selected=[r for r in snapshot if 0<r["deleted_at"]<=cutoff]
    password=credentials.unseal(config["sealed_password"])["password"] if config["sealed_password"] else ""
    deleted=[]
    for offset in range(0,len(selected),100):
        chunk=selected[offset:offset+100]
        current={r["id"]:r for r in provider.recycle_items()}
        if any(current.get(r["id"])!=r for r in chunk):raise CloudError("回收站条目在核验中变化，未继续清理",409)
        ids=[r["id"] for r in chunk]
        def remove():
            account=store.account(payload["account_id"])
            if not account or account["revision"]!=payload["account_revision"] or raw(payload["account_id"])["revision"]!=payload["maintenance_revision"]:
                raise CloudError("账号或维护配置变化，未继续清理",409)
            with store.db() as con:
                if con.execute("SELECT 1 FROM cloud_deletions WHERE account_id=? AND state IN ('dispatched','acknowledged','uncertain') LIMIT 1",(payload["account_id"],)).fetchone():
                    raise CloudError("新增待核实源删除，保留回收站",409)
            provider.recycle_delete(ids,password)
            remaining={r["id"] for r in provider.recycle_items()}
            if set(ids)&remaining:raise CloudError("回收站清理结果待核实，不重复请求",409,uncertain=True)
            return {"deleted_ids":ids}
        jobs.step(job_id,"recycle:"+hashlib.sha256(store.encode(ids).encode()).hexdigest(),{"ids":ids},remove)
        deleted.extend(ids)
        jobs.progress(job_id,len(selected),len(deleted),"回收站已核实永久清理")
    return {"deleted_count":len(deleted),"retained_count":len(snapshot)-len(selected),"retention_days":config["retention_days"]}


def verify(job_id):
    with store.db() as con:
        record=con.execute("SELECT kind,state,payload FROM cloud_jobs WHERE id=?",(job_id,)).fetchone()
        steps=con.execute("SELECT step_key,payload FROM cloud_steps WHERE job_id=? AND step_key LIKE 'recycle:%' AND state='dispatched'",(job_id,)).fetchall()
    if not record or record[0]!="maintenance" or json.loads(record[2]).get("action")!="recycle" or record[1] not in ("uncertain","needs_review") or not steps:
        raise CloudError("此任务没有待核实的回收站清理",409)
    provider=workflow.verify_account(json.loads(record[2]))
    existing={r["id"] for r in provider.recycle_items()}
    for key,p in steps:
        ids=json.loads(p)["ids"]
        if existing.intersection(ids):raise CloudError("仍有原条目可见，保留待核实状态；未重发删除",409)
    with store.db() as con:
        for key,p in steps:
            con.execute("UPDATE cloud_steps SET state='verified',result=? WHERE job_id=? AND step_key=?",(store.encode({"deleted_ids":json.loads(p)["ids"],"verified_only":True}),job_id,key))
        con.execute("UPDATE cloud_jobs SET state='success',message='已只读核实已发送清理，未重发；剩余条目留待下次任务',updated=? WHERE id=?",(time.time(),job_id))
    return {"verified":True,"resent":False}
