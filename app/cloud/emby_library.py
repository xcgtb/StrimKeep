"""Independent Emby connections and durable, path-verified ingestion receipts."""
import hashlib
import html
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import PurePosixPath
from urllib.parse import urlsplit
import httpx
from app.cloud import credentials, services, settings, store, events
from app.cloud.errors import CloudError

DEFAULT = {"connections": []}
_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cloud-emby-confirm")
_future = None


def get(account_id):
    value = services.get(account_id, "emby_library", DEFAULT)
    return {"revision": value["revision"], "items": [{k: v for k, v in c.items() if k != "sealed_key"} | {"key_saved": bool(c["sealed_key"])} for c in value["config"]["connections"]]}


def save(account_id, body):
    expected = body.get("expected_revision")
    entry = body.get("connection", {})
    if not isinstance(entry, dict):
        raise CloudError("Emby 连接格式无效")
    cid = entry.get("id") or store.ident()
    if not re.fullmatch(r"[a-f0-9]{32}", str(cid)):
        raise CloudError("Emby 连接 ID 无效")
    def validate(cfg, old):
        items = cfg["connections"]
        previous = next((c for c in items if c["id"] == cid), None)
        if body.get("delete") is True:
            cfg["connections"] = [c for c in items if c["id"] != cid]
            return cfg
        if set(entry)-{"id", "name", "url", "key", "enabled", "notify", "personal_root", "share_root"}:
            raise CloudError("Emby 连接字段无效")
        data = {"id": cid, "name": "", "url": "", "enabled": False, "notify": True, "personal_root": "", "share_root": "", **(previous or {}), **entry}
        key = data.pop("key", "")
        if not isinstance(key, str) or len(key) > 512:
            raise CloudError("Emby API Key 无效")
        data["sealed_key"] = credentials.seal({"key": key}) if key else (previous or {}).get("sealed_key", "")
        if not isinstance(data["name"], str) or not 1 <= len(data["name"]) <= 80 or any(type(data[k]) is not bool for k in ("enabled", "notify")):
            raise CloudError("请填写连接名称与有效开关")
        if not isinstance(data["url"], str):
            raise CloudError("Emby 地址无效")
        parsed = urlsplit(data["url"])
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise CloudError("Emby 地址仅支持不含账号、参数的 HTTP/HTTPS 地址")
        data["url"] = data["url"].rstrip("/")
        for k in ("personal_root", "share_root"):
            root = data[k]
            if not isinstance(root, str) or len(root) > 1000 or (root and (not root.startswith("/") or ".." in PurePosixPath(root).parts or any(ord(c)<32 for c in root))):
                raise CloudError("Emby 映射目录需为绝对路径，留空表示此连接不扫描该库")
            data[k] = root.rstrip("/") or ("/" if root else "")
        if data["enabled"] and (not data["sealed_key"] or not (data["personal_root"] or data["share_root"])):
            raise CloudError("启用前请填写 API Key 与至少一个库路径映射")
        cfg["connections"] = [c for c in items if c["id"] != cid]+[data]
        if len(cfg["connections"]) > 10:
            raise CloudError("最多保存10个 Emby 连接")
        return cfg
    services.save(account_id, "emby_library", {}, expected, DEFAULT, validate)
    return get(account_id)


def request(connection, path, method="GET", params=None, body=None):
    key = credentials.unseal(connection["sealed_key"])["key"]
    try:
        with httpx.Client(timeout=12, follow_redirects=False) as client:
            response = client.request(method, connection["url"]+path, headers={"X-Emby-Token": key}, params=params, json=body)
            if response.status_code not in (200, 204):
                raise ValueError()
            return response.json() if response.content else {}
    except Exception:
        raise CloudError("Emby 请求失败，请检查连接地址、API Key 或网络", 502) from None


def test(account_id, cid):
    cfg = services.get(account_id, "emby_library", DEFAULT)
    connection = next((c for c in cfg["config"]["connections"] if c["id"] == cid), None)
    if not connection:
        raise CloudError("Emby 连接不存在", 404)
    result = request(connection, "/System/Info")
    if not result.get("Id"):
        raise CloudError("Emby 服务身份验证失败", 502)
    return {"name": result.get("ServerName", "Emby"), "version": result.get("Version", "")}


def mapped_path(asset, connection):
    payload = asset["payload"]
    root = connection["share_root" if payload.get("source_kind") == "share" else "personal_root"]
    if not root:
        return None
    try:
        relative = PurePosixPath(asset["local_path"]).relative_to(PurePosixPath(payload["output_root"]))
    except ValueError:
        raise CloudError("STRM 路径不在登记的输出根目录", 409) from None
    if ".." in relative.parts:
        raise CloudError("STRM 路径无效", 409)
    return str(PurePosixPath(root)/relative)


def watch(asset):
    """Record once for each published content/path and connection configuration."""
    if not settings.get()["emby_refresh_enabled"]:
        return
    cfg = services.get(asset["account_id"], "emby_library", DEFAULT)
    for c in cfg["config"]["connections"]:
        if not c["enabled"]:
            continue
        path = mapped_path(asset, c)
        if not path:
            continue
        from app.cloud import notifications
        origin=events._JOB.get() or ""
        with store.db() as con:
            due=con.execute("SELECT origin_job FROM cloud_job_due WHERE job_id=?",(origin,)).fetchone()
        if due:origin=due[0]
        payload = {"account_id":asset["account_id"], "path":path, "digest":asset["payload"]["digest"], "local_path":asset["local_path"],"origin_job":origin,"notification_group":notifications.group_key(asset,c["id"],cfg["revision"],origin)}
        key = hashlib.sha256(store.encode([asset["id"],c["id"],cfg["revision"],payload]).encode()).hexdigest()
        with store.db() as con:
            changed = con.execute("INSERT OR IGNORE INTO cloud_ingestion VALUES(?,?,?,?,?,'pending',?,0,'{}',?)", (key,asset["id"],c["id"],cfg["revision"],store.encode(payload),time.time(),time.time())).rowcount
            if changed:
                events.emit('emby','等待 Emby 入库确认：'+asset['payload']['media']['title']+' · '+c['name'],con=con)


def confirmed_item(connection, asset, path):
    """Require exact mapped path, TMDB identity and TV season/episode identity."""
    media = asset["payload"]["media"]
    params = {"Path":path,"Recursive":"true","Fields":"Path,ProviderIds,MediaSources","Limit":100,"IncludeItemTypes":"Episode" if media["kind"] == "tv" else "Movie"}
    result = request(connection, "/Items", params=params)
    if not isinstance(result.get("Items"), list) or len(result["Items"]) > 100:
        raise CloudError("Emby 返回的入库数据无效", 502)
    for item in result["Items"]:
        paths = [item.get("Path"), *[v.get("Path") for v in item.get("MediaSources", [])]]
        if path not in paths or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", str(item.get("Id", ""))):
            continue
        if media["kind"] == "tv":
            if item.get("Type") != "Episode" or item.get("ParentIndexNumber") != media.get("season") or item.get("IndexNumber") != media.get("episode"):
                continue
            sid = str(item.get("SeriesId", ""))
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", sid):
                continue
            identity = request(connection, "/Items/"+sid)
        else:
            if item.get("Type") != "Movie":
                continue
            identity = item
        providers = {k.lower(): str(v) for k,v in (identity.get("ProviderIds") or {}).items()}
        if providers.get("tmdb") == str(media["tmdb_id"]):
            return item
    return None


def refresh_pending(connection, account_id, revision, now):
    """Submit up to 100 valid due paths together, retaining each ingestion receipt."""
    with store.db() as con:
        rows = con.execute(
            "SELECT id,asset_id,payload,attempts FROM cloud_ingestion WHERE connection_id=? AND revision=? AND state='pending' AND next_check<=? AND attempts%10=0 ORDER BY next_check LIMIT 100",
            (connection["id"], revision, now),
        ).fetchall()
    selected, paths = [], []
    for iid, asset_id, raw, attempts in rows:
        data = json.loads(raw)
        if data.get("account_id") != account_id or data.get("refresh_attempt") == attempts:
            continue
        asset = store.asset(asset_id)
        if not asset or asset["state"] != "active" or asset["local_path"] != data["local_path"] or asset["payload"]["digest"] != data["digest"] or mapped_path(asset, connection) != data["path"]:
            continue
        selected.append((iid, data, attempts))
        if data["path"] not in paths:
            paths.append(data["path"])
    if not paths:
        return
    cfg = services.get(account_id, "emby_library", DEFAULT)
    if cfg["revision"] != revision or not settings.get()["emby_refresh_enabled"]:
        return
    request(connection, "/Library/Media/Updated", method="POST", body={"Updates": [{"Path": path, "UpdateType": "Created"} for path in paths]})
    with store.db() as con:
        for iid, data, attempts in selected:
            data["refresh_attempt"] = attempts
            con.execute("UPDATE cloud_ingestion SET payload=? WHERE id=? AND state='pending' AND attempts=?", (store.encode(data), iid, attempts))
    events.emit('emby', '已提交 Emby 路径刷新：'+connection['name']+' · '+str(len(paths))+' 个 STRM；等待实际入库确认')


def poll_one():
    now = time.time()
    with store.db() as con:
        row = con.execute("SELECT id,asset_id,connection_id,revision,payload,attempts FROM cloud_ingestion WHERE state='pending' AND next_check<=? ORDER BY next_check LIMIT 1", (now,)).fetchone()
    if not row:
        return
    iid, asset_id, cid, revision, raw, attempts = row
    payload = json.loads(raw)
    state, result, item, connection = "pending", {}, None, None
    asset = store.asset(asset_id)
    try:
        cfg = services.get(payload["account_id"], "emby_library", DEFAULT)
        connection = next((c for c in cfg["config"]["connections"] if c["id"] == cid and c["enabled"]), None)
        if not asset or asset["state"] != "active" or asset["payload"]["digest"] != payload["digest"] or asset["local_path"] != payload["local_path"] or cfg["revision"] != revision or not connection or mapped_path(asset,connection) != payload["path"]:
            state, result = "stale", {"message":"连接、映射或资产已变化，请重新登记入库检查"}
        elif not settings.get()["emby_refresh_enabled"]:
            state, result = "paused", {"message":"Emby 联动已关闭"}
        else:
            if attempts % 10 == 0:
                refresh_pending(connection, payload["account_id"], revision, now)
            item = confirmed_item(connection, asset, payload["path"])
            state = "confirmed" if item else "pending"
            result = {"message":"Emby 已确认入库" if item else "等待 Emby 扫描与刮削", "emby_item_id":item["Id"] if item else ""}
    except CloudError as error:
        result = {"message":str(error)}
    if state == "pending" and attempts >= 119:
        state = "timeout"
        result["message"] = "入库确认超时（约2小时）；请核对路径与刮削结果后重新检查"
    # A configuration change during HTTP reads invalidates the evidence as well.
    with store.db() as con:
        current = con.execute("SELECT revision FROM cloud_service_settings WHERE account_id=? AND kind='emby_library'", (payload["account_id"],)).fetchone()
        bound = con.execute("SELECT state,local_path,payload FROM cloud_assets WHERE id=?", (asset_id,)).fetchone()
        if not current or current[0] != revision or not bound or bound[0] != "active" or bound[1] != payload["local_path"] or json.loads(bound[2]).get("digest") != payload["digest"]:
            state, item, result = "stale", None, {"message":"读取期间连接或资产变化，请重新检查"}
    # Commit confirmed receipt before sending: ambiguous Telegram sends are never replayed.
    with store.db() as con:
        changed = con.execute("UPDATE cloud_ingestion SET state=?,next_check=?,attempts=?,result=?,updated=? WHERE id=? AND state='pending'", (state,now+60,attempts+1,store.encode(result),now,iid)).rowcount
    if not changed:
        return
    if state != 'pending' or attempts in (0,9,29,59):
        events.emit('emby',result.get('message','')+' · '+str(asset_id)[:12],level='WARNING' if state in ('timeout','stale') else 'INFO')
    if state != 'pending' and connection:
        from app.cloud import notifications
        notifications.send_confirmed(payload.get('notification_group'),connection)


def records(account_id):
    with store.db() as con:
        rows = con.execute("SELECT i.id,i.asset_id,i.connection_id,i.state,i.result,i.attempts,i.updated,json_extract(i.payload,'$.path'),json_extract(a.payload,'$.media.title') FROM cloud_ingestion i LEFT JOIN cloud_assets a ON a.id=i.asset_id WHERE json_extract(i.payload,'$.account_id')=? ORDER BY i.updated DESC LIMIT 100", (account_id,)).fetchall()
    return [{"id":r[0],"asset_id":r[1],"connection_id":r[2],"state":r[3],"result":json.loads(r[4]),"attempts":r[5],"updated":r[6],"path":r[7] or "","title":r[8] or ""} for r in rows]


def retry(account_id, iid):
    with store.db() as con:
        row = con.execute("SELECT asset_id FROM cloud_ingestion WHERE id=? AND json_extract(payload,'$.account_id')=?", (iid,account_id)).fetchone()
    if not row:
        raise CloudError("入库记录不存在",404)
    asset = store.asset(row[0])
    if not asset or asset["state"] != "active":
        raise CloudError("STRM 资产不可用",409)
    watch(asset)
    with store.db() as con:
        con.execute("UPDATE cloud_ingestion SET state='pending',attempts=0,next_check=?,payload=json_remove(payload,'$.refresh_attempt') WHERE id=? AND state IN ('timeout','paused')", (time.time(),iid))


def _flush_confirmed():
    from app.cloud import notifications
    with store.db() as con:
        groups=con.execute("SELECT DISTINCT connection_id,revision,payload FROM cloud_ingestion i WHERE state='confirmed' AND json_extract(payload,'$.notification_group') NOT IN (SELECT id FROM cloud_ingestion_notifications) AND NOT EXISTS (SELECT 1 FROM cloud_ingestion p WHERE p.state='pending' AND json_extract(p.payload,'$.notification_group')=json_extract(i.payload,'$.notification_group')) LIMIT 20").fetchall()
    for cid,revision,raw in groups:
        p=json.loads(raw);cfg=services.get(p['account_id'],'emby_library',DEFAULT)
        connection=next((c for c in cfg['config']['connections'] if c['id']==cid and c['enabled']),None)
        if connection and cfg['revision']==revision:notifications.send_confirmed(p.get('notification_group'),connection)
        elif p.get('notification_group'):
            with store.db() as con:con.execute("INSERT OR IGNORE INTO cloud_ingestion_notifications VALUES(?,'stale',?)",(p['notification_group'],time.time()))
    poll_one()


def schedule():
    global _future
    if _future is None or _future.done():
        with store.db() as con:
            exists=con.execute("SELECT 1 FROM cloud_ingestion WHERE (state='pending' AND next_check<=?) OR (state='confirmed' AND json_extract(payload,'$.notification_group') NOT IN (SELECT id FROM cloud_ingestion_notifications)) LIMIT 1",(time.time(),)).fetchone()
        if exists:_future=_POOL.submit(_flush_confirmed)
