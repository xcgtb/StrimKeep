"""Reuse saved intake directories as explicit, deduplicated organizer schedules."""
import hashlib
import json
import time
from app.cloud import directories, receiving, schedules, store, workflow
from app.cloud.errors import CloudError
from app.cloud.provider115 import numeric


def sources(account_id):
    value = directories.get(account_id)
    result = {}
    labels = {"source":"整理来源", "transfer":"分享转存", "magnet":"磁力云下载", "ed2k":"ed2k云下载", "instant":"秒传", "channel":"频道默认"}
    for role, label in labels.items():
        folder = value["directories"].get(role)
        if folder:
            entry = result.setdefault(folder["id"], {"id":folder["id"],"name":folder["name"],"roles":[]})
            entry["roles"].append(label)
    for channel in receiving.channels(account_id):
        if channel["enabled"]:
            entry = result.setdefault(channel["target_cid"], {"id":channel["target_cid"],"name":"频道保存目录","roles":[]})
            entry["roles"].append("频道："+channel["name"])
    return {"items":list(result.values()),"revision":value["revision"],"target":value["directories"]["target"]}


def _outside(provider, source, target):
    # Avoid either nesting direction. An archive containing an intake root would
    # make repeated scans pick up already archived material.
    for start, forbidden in ((target, source), (source, target)):
        cursor, seen = start, set()
        while True:
            if cursor == forbidden:
                raise CloudError("归档、来源和接收目录不得相同或相互包含",409)
            if cursor == "0":
                break
            if cursor in seen or len(seen) >= 64:
                raise CloudError("目录层级异常，请重新选择",409)
            seen.add(cursor)
            row = provider.info(cursor)
            if not row["is_dir"]:
                raise CloudError("配置路径不是目录",409)
            cursor = row["parent_id"]


def save(account_id, config, selected, expected):
    initial = sources(account_id)
    if initial["revision"] != expected:
        raise CloudError("接收目录配置已变化，请刷新",409)
    if not isinstance(selected,list) or not 1 <= len(selected) <= 20 or len(set(selected)) != len(selected) or set(selected)-{r["id"] for r in initial["items"]}:
        raise CloudError("请勾选已保存的接收目录")
    dirs = directories.get(account_id)["directories"]
    if not dirs["target"]:
        raise CloudError("请先选择归档目录",409)
    target = dirs["target"]["id"]
    unknown = dirs["unknown"]["id"] if dirs["unknown"] else None
    account = store.account(account_id)
    provider = workflow.verify_account({"account_id":account_id,"account_revision":account["revision"]})
    due = schedules.next_run(config,time.time())
    for source in selected:
        workflow._directory(provider,source)
        _outside(provider,source,target)
        if unknown:
            _outside(provider,source,unknown)
        for other in selected:
            if other != source:
                _outside(provider,source,other)
    if sources(account_id) != initial:
        raise CloudError("接收目录或频道配置变化，请刷新后重试",409)
    ids = []
    with store.db() as con:
        current = con.execute("SELECT revision FROM cloud_accounts WHERE id=?",(account_id,)).fetchone()
        directory = con.execute("SELECT revision FROM cloud_directory_settings WHERE account_id=?",(account_id,)).fetchone()
        if not directory or directory[0] != expected:
            raise CloudError("保存期间接收目录变化，请刷新重试",409)
        if not current or current[0] != account["revision"]:
            raise CloudError("账号授权变化，请重新保存",409)
        # Only this dedicated group is updated. Existing manually-created rules stay intact.
        con.execute("UPDATE cloud_rules SET enabled=0 WHERE json_extract(payload,'$.account_id')=? AND json_extract(payload,'$.intake_rule')=1",(account_id,))
        for source in selected:
            rid = hashlib.sha256(("intake:"+account_id+":"+source).encode()).hexdigest()[:32]
            name = next(r["name"] for r in initial["items"] if r["id"] == source)
            payload = {**config,"kind":"workflow","account_id":account_id,"account_revision":account["revision"],"source_cid":source,"target_cid":target,"unknown_cid":unknown,"overrides":{},"name":"接收整理 · "+name,"intake_rule":True}
            con.execute("INSERT INTO cloud_rules VALUES(?,?,1,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,enabled=1,next_run=excluded.next_run,updated=excluded.updated",(rid,store.encode(payload),due,time.time()))
            ids.append(rid)
    return {"ids":ids,"next_run":due}


def ready(provider, source):
    """Keep intake roots untouched while 115 reports unfinished/unknown downloads."""
    for page in range(1,21):
        response = provider.offline_tasks(page)
        for task in response["items"]:
            if str(task.get("status")) != "2" and str(task.get("wp_path_id") or source) == source:
                raise CloudError("此接收目录仍有未完成或失败的115云下载，本轮保留原位；下次定时扫描再检查",409)
        count = response.get("page_count")
        if count is not None:
            if isinstance(count, str) and count.isdigit():
                count = int(count)
            if type(count) is not int or count < 0:
                raise CloudError("115云下载分页状态不明，暂停本轮整理",502)
            if page >= count:
                return
        elif len(response["items"]) < 30:
            return
    raise CloudError("115云下载任务过多，无法完整核实，暂停本轮整理",409)


def run_now(account_id, expected_revision, source_cid=None):
    """Manual run uses saved directories; no need to build a preview in the browser."""
    from app.cloud import jobs
    cfg = directories.get(account_id)
    if cfg['revision'] != expected_revision:
        raise CloudError('目录配置变化，请刷新后重试',409)
    roots = cfg['directories']
    if not roots['target'] or (source_cid is None and not roots['source']):
        raise CloudError('请先保存扫描来源（或指定已保存的接收目录）和归档目录',409)
    source = roots['source']['id'] if source_cid is None else numeric(source_cid)
    if source_cid is not None and source not in {row['id'] for row in sources(account_id)['items']}:
        raise CloudError('只能选择当前账号已保存的资源接收目录',409)
    account = store.account(account_id)
    if not account:
        raise CloudError('账号不存在',404)
    payload = {'account_id':account_id,'account_revision':account['revision'],
               'source_cid':source,'target_cid':roots['target']['id'],
               'unknown_cid':roots['unknown']['id'] if roots['unknown'] else None,'overrides':{}}
    provider = workflow.verify_account(payload)
    _outside(provider,payload['source_cid'],payload['target_cid'])
    if payload['unknown_cid']:
        _outside(provider,payload['source_cid'],payload['unknown_cid'])
    with store.db() as con:
        current = con.execute('SELECT revision FROM cloud_directory_settings WHERE account_id=?',(account_id,)).fetchone()
        owner = con.execute('SELECT revision FROM cloud_accounts WHERE id=?',(account_id,)).fetchone()
        if not current or current[0] != expected_revision or not owner or owner[0] != account['revision']:
            raise CloudError('保存目录或账号变化，请重新提交',409)
        existing = con.execute("SELECT id FROM cloud_jobs WHERE kind='workflow' AND state IN ('queued','running') AND json_extract(payload,'$.account_id')=? AND json_extract(payload,'$.source_cid')=? ORDER BY created LIMIT 1",(account_id,payload['source_cid'])).fetchone()
        return existing[0] if existing else jobs.enqueue('workflow',payload,con)


def stop_all(account_id):
    from app.cloud import jobs
    if not store.account(account_id):
        raise CloudError('账号不存在',404)
    with store.db() as con:
        con.execute("UPDATE cloud_rules SET enabled=0,updated=? WHERE json_extract(payload,'$.account_id')=? AND json_extract(payload,'$.kind')='workflow'",(time.time(),account_id))
        running = con.execute("SELECT id FROM cloud_jobs WHERE kind IN ('organize','workflow') AND state IN ('queued','running') AND json_extract(payload,'$.account_id')=?",(account_id,)).fetchall()
        # Manual plan jobs carry the account on their plan rather than in the job payload.
        running += con.execute("SELECT j.id FROM cloud_jobs j JOIN cloud_plans p ON p.id=json_extract(j.payload,'$.plan_id') WHERE j.kind='organize' AND j.state IN ('queued','running') AND p.account_id=?",(account_id,)).fetchall()
    ids = list(dict.fromkeys(r[0] for r in running))
    for jid in ids:
        jobs.request_stop(jid)
    return {'stopped':ids,'message':'定时整理已暂停；'+str(len(ids))+' 个任务将在当前文件核实后停止。'}
