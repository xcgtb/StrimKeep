"""Read-only 115 shares -> classified share-library references. No remote writes."""

import hashlib
import json
import re
import time
from pathlib import PurePosixPath
from app.cloud import credentials, store, settings, strm, jobs, workflow, recognition, file_filters
from app.cloud.errors import CloudError
from app.cloud import provider115

LIMIT = 2000


def save_source(account_id, code, password):
    code, password = provider115.parse_share(code, password)
    hashed = hashlib.sha256(code.encode()).hexdigest()
    with store.db() as con:
        old = con.execute("SELECT id,sealed,revision FROM cloud_share_sources WHERE account_id=? AND code_hash=?", (account_id, hashed)).fetchone()
        secret = {"code": code, "password": password}
        if old and unseal(old[1]) == secret:
            return old[0], old[2]
        sid, revision = (old[0], old[2]+1) if old else (store.ident(), 1)
        sealed = credentials.seal(secret)
        con.execute("INSERT INTO cloud_share_sources VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET sealed=excluded.sealed,revision=excluded.revision,updated=excluded.updated", (sid, account_id, hashed, sealed, revision, time.time()))
    return sid, revision


def unseal(value):
    return credentials.unseal(value)


def secret(sid, account_id, revision):
    with store.db() as con:
        row = con.execute("SELECT sealed,revision FROM cloud_share_sources WHERE id=? AND account_id=?", (sid, account_id)).fetchone()
    if not row or row[1] != revision:
        raise CloudError("分享凭据已变化，请重新预览生成", 409)
    return unseal(row[0])


def page_all(provider, access, parent):
    result, seen, count, offset = [], set(), None, 0
    while True:
        page = provider.share_page(access["code"], access["password"], parent, offset)
        total = page["total"]
        if type(total) is not int or total < 0 or total > LIMIT or (count is not None and count != total):
            raise CloudError("分享目录过大或读取中发生变化，请选择更具体的目录", 409)
        count = total
        for row in page["items"]:
            if row["id"] in seen:
                raise CloudError("分享列表重复，已停止读取", 502)
            seen.add(row["id"])
            result.append({**row, "pickcode": "", "share_parent": parent})
        offset = len(result)
        if offset == count:
            return result
        if not page["items"] or offset > count:
            raise CloudError("分享列表被截断，已停止读取", 502)


def same(left, right):
    return all(left.get(k) == right.get(k) for k in ("id", "parent_id", "name", "size", "sha1", "is_dir"))


class View:
    """ffprobe/subtitle adapter never exposes personal info/download methods."""
    def __init__(self, provider, access, rows):
        self.provider, self.access = provider, access
        self.rows = {r["id"]: r for r in rows}

    def info(self, fid):
        expected = self.rows.get(fid)
        if not expected:
            raise CloudError("文件不属于已选分享", 409)
        rows = page_all(self.provider, self.access, expected["share_parent"])
        found = next((r for r in rows if r["id"] == fid), None)
        if not found or not same(expected, found):
            raise CloudError("分享失效、文件移出或指纹变化，请重新预览", 409)
        return found

    def download(self, fid, pickcode, ua):
        value = self.provider.share_download(self.access["code"], self.access["password"], fid, ua)
        if value["size"] != self.rows[fid]["size"] or (value.get("sha1") and value["sha1"].upper() != self.rows[fid]["sha1"].upper()):
            raise CloudError("分享下载大小与登记来源不符", 409)
        return value


def current(payload):
    provider = workflow.verify_account(payload)
    access = secret(payload["share_id"], payload["account_id"], payload["share_revision"])
    return provider, access


def snapshot(config):
    return {"share_layout_version": 2, **workflow.naming_snapshot(config), **{k: config[k] for k in ("public_url", "share_output_root", "nfo_enabled", "artwork_enabled")}}


def preview(preview_id, ids, overrides=None):
    if not ids or len(ids) > 100 or len(set(ids)) != len(ids):
        raise CloudError("请选择当前分享中的内容")
    with store.db() as con:
        record = con.execute("SELECT payload,state,expires FROM cloud_plans WHERE id=?", (preview_id,)).fetchone()
    if not record or record[1] != "share_pending" or record[2] < time.time():
        raise CloudError("分享预览过期或已使用，请重新预览", 409)
    data = json.loads(record[0])
    chosen = [r for r in data["items"] if r["id"] in ids]
    if len(chosen) != len(ids):
        raise CloudError("选择不属于当前分享预览", 409)
    provider = workflow.verify_account(data)
    access = workflow.share_access(data)
    fresh = {r["id"]: r for r in page_all(provider, access, data["share_parent"])}
    if any(r["id"] not in fresh or not same(r, fresh[r["id"]]) for r in chosen):
        raise CloudError("已选分享内容变化，请重新预览", 409)
    queue = [(fresh[r["id"]], 0) for r in chosen]
    rows, seen = [], set()
    while queue:
        row, depth = queue.pop(0)
        if row["id"] in seen or depth > 20 or len(seen) >= LIMIT:
            raise CloudError("分享目录重复、层级过深或内容过多，请分目录生成", 409)
        seen.add(row["id"])
        if row["is_dir"]:
            children = page_all(provider, access, row["id"])
            if len(seen)+len(queue)+len(children) > LIMIT:
                raise CloudError("分享内容过多，请分目录生成", 413)
            queue.extend((r, depth+1) for r in children)
        else:
            rows.append(row)
    cfg = settings.get()
    settings.validate(cfg)
    base = strm.root("share")
    if not cfg["public_url"]:
        raise CloudError("请先保存客户端可访问的播放地址", 409)
    sid, revision = save_source(data["account_id"], access["code"], access["password"])
    selected, filtered = file_filters.select(rows, cfg)
    view = View(provider, access, rows)
    items, skipped, paths = [], [], set()
    for row in selected:
        try:
            if not re.fullmatch(r"[A-Fa-f0-9]{40}", row["sha1"]) or row["size"] <= 0:
                raise CloudError("分享未提供完整大小与 SHA1，暂不生成", 409)
            def probe():
                from app.cloud import media_probe
                try:
                    return media_probe.extract("share:"+data["account_id"]+":"+sid, view, row)
                except CloudError as error:
                    if cfg["ffprobe_failure"] == "skip" or error.status == 409:
                        raise
                    return {"status": "fallback", "fields": {}, "warning": "分享媒体提取失败，已按设置回退文件名解析"}
            media = recognition.recognize(row["name"], (overrides or {}).get(row["id"]), cfg, probe if cfg["ffprobe_enabled"] else None)
            relative = media["directory"]+"/"+str(PurePosixPath(media["name"]).with_suffix(".strm"))
            destination = strm.target(base, relative)
            if destination.exists():
                with store.db() as con:
                    bound = con.execute("SELECT id FROM cloud_assets WHERE account_id=? AND file_id=? AND local_path=?", (data["account_id"], "share:"+sid+":"+row["id"], str(destination))).fetchone()
                asset = store.asset(bound[0]) if bound else None
                if not asset or not destination.is_file() or strm.digest(destination.read_bytes()) != asset["payload"].get("digest"):
                    raise CloudError("同名 STRM 已存在，保留现有版本；请先在片库核对去留，不另建分享来源目录", 409)
            if relative in paths:
                raise CloudError("本批命名输出相同，请修正模板或分次选择版本", 409)
            paths.add(relative)
            items.append({"source": row, "media": media, "followers": workflow.related_companions(rows, row, cfg)})
        except CloudError as error:
            skipped.append({"file_id": row["id"], "name": row["name"], "reason": str(error)})
    payload = {"account_id": data["account_id"], "account_revision": data["account_revision"],
               "share_id": sid, "share_revision": revision, "items": items, "skipped": skipped,
               "filtered": filtered, "config": snapshot(cfg), "root_identity": [base.stat().st_dev, base.stat().st_ino]}
    current(payload)
    pid = store.ident()
    with store.db() as con:
        con.execute("INSERT INTO cloud_plans VALUES(?,?,?,?,?,?)", (pid, data["account_id"], data["account_revision"], "share_strm_pending", store.encode(payload), time.time()+600))
    return {"id": pid, "items": items, "skipped": skipped, "filtered": filtered, "output_root": str(base), "effect": "仅生成 NAS 分享库 STRM；不转存、不改名、不移动、不删除 115 文件"}


def apply(plan_id, confirmed=False):
    if confirmed is not True:
        raise CloudError("请确认生成分享 STRM")
    with store.db() as con:
        record = con.execute("SELECT payload,state,expires FROM cloud_plans WHERE id=?", (plan_id,)).fetchone()
        if not record or record[1] != "share_strm_pending" or record[2] < time.time():
            raise CloudError("分享 STRM 预览过期或已使用", 409)
        payload = json.loads(record[0])
        if not payload["items"]:
            raise CloudError("当前没有可生成的已识别视频", 409)
        con.execute("UPDATE cloud_plans SET state='queued' WHERE id=?", (plan_id,))
        return jobs.enqueue("share_strm", {**payload, "plan_id": plan_id}, con=con)


def prune_legacy_directory(base, previous, share_id, identity):
    """Only remove now-empty parents inside this source's old wrapper, after publish."""
    if not previous:
        return
    from pathlib import Path
    old = Path(previous)
    if not old.is_relative_to(base):
        return
    parts = old.relative_to(base).parts
    wrapper = "分享-"+share_id[:12]
    if len(parts) < 2 or parts[0] != wrapper or old.exists():
        return
    directory = old.parent
    while directory != base:
        try:
            current_root = strm.root("share")
            if current_root != base or [base.stat().st_dev, base.stat().st_ino] != identity:
                return
            strm.target(base, str(directory.relative_to(base)))
            directory.rmdir()
        except (OSError, CloudError):
            return
        directory = directory.parent


def run(job_id, payload):
    result = {"generated": [], "skipped": payload["skipped"], "filtered": payload["filtered"], "errors": [], "publication": {}}
    provider, access = current(payload)
    if snapshot(settings.get()) != payload["config"]:
        raise CloudError("命名或输出配置已变化，请重新预览分享 STRM", 409)
    base = strm.root("share")
    if [base.stat().st_dev, base.stat().st_ino] != payload["root_identity"]:
        raise CloudError("分享库挂载身份变化，请重新预览", 409)
    view = View(provider, access, [r for i in payload["items"] for r in [i["source"], *i["followers"]]])
    from app.cloud.notifications import BatchPosters
    posters = BatchPosters(job_id, payload["items"], "share")
    for index, item in enumerate(payload["items"]):
        if jobs.stopping(job_id):
            posters.flush()
        jobs.checkpoint(job_id, result)
        counts = result['publication'].setdefault(item['media']['directory'], {'published':0,'failed':0})
        try:
            current(payload)
            if snapshot(settings.get()) != payload["config"]:
                raise CloudError("配置变化，已停止生成，请重新预览", 409)
            view.info(item["source"]["id"])
            for follower in item["followers"]:
                view.info(follower["id"])
            base = strm.root("share")
            if [base.stat().st_dev, base.stat().st_ino] != payload["root_identity"]:
                raise CloudError("分享库挂载变化，请重新预览", 409)
            with store.db() as con:
                previous = con.execute("SELECT local_path FROM cloud_assets WHERE account_id=? AND file_id=? AND json_extract(payload,'$.output_root')=?", (payload["account_id"], "share:"+payload["share_id"]+":"+item["source"]["id"], str(base))).fetchone()
            asset = strm.generate(payload["account_id"], item["source"], item["media"], companions=item["followers"], provider=view, source_context={"source_kind": "share", "share_id": payload["share_id"], "share_revision": payload["share_revision"]})
            if previous and previous[0] != asset["local_path"]:
                prune_legacy_directory(base, previous[0], payload["share_id"], payload["root_identity"])
            result["generated"].append({"asset_id": asset["id"], "path": asset["local_path"]})
            result.setdefault("warnings", []).extend({"name": item["source"]["name"], "reason": w} for w in asset["payload"].get("warnings", []))
            counts['published'] += 1
            posters.verified(index, item["source"], item["media"])
        except (CloudError, OSError):
            counts['failed'] += 1
            result["errors"].append({"name": item["source"]["name"], "reason": "分享读取或输出核验失败，保留现有引用；请检查分享、挂载与配置后重新预览"})
            break
        jobs.progress(job_id, len(payload["items"]), index+1, "生成分享 STRM", item["source"]["name"])
    posters.flush()
    if result["generated"]:
        result["refresh_pending"] = not workflow.refresh_library(payload["account_id"])
    return result


def playback(asset, account, provider, ua):
    payload = asset["payload"]
    access = secret(payload["share_id"], account["id"], payload["share_revision"])
    view = View(provider, access, [payload["source"]])
    view.info(payload["source"]["id"])
    return view.download(payload["source"]["id"], "", ua)
