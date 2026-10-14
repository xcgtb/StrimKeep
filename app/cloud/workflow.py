"""Transfer, remote organization and STRM share one durable file identity."""

import json
import time
from pathlib import PurePosixPath
from app.cloud import store, jobs, recognition
from app.cloud.provider115 import for_account, numeric, parse_share
from app.cloud.errors import CloudError


def verify_account(payload):
    account = store.account(payload["account_id"])
    if not account or account["revision"] != payload["account_revision"]:
        raise CloudError("账号授权已变化，请重新预览", 409)
    provider = for_account(account["id"])
    provider.profile()
    return provider


def walk(provider, cid, limit=20000):
    queue = [numeric(cid)]
    seen = set()
    result = []
    while queue:
        parent = queue.pop(0)
        if parent in seen:
            raise CloudError("目录树重复或存在循环，已停止扫描", 409)
        seen.add(parent)
        if len(seen) > 20000:
            raise CloudError("目录过大，请选择更具体的目录", 413)
        rows = provider.list(parent)
        for row in rows:
            if row["is_dir"]:
                queue.append(row["id"])
            else:
                result.append(row)
        if len(result) > limit:
            raise CloudError("文件过多，请分目录建立任务", 413)
    return result


def _directory(provider, cid):
    cid = numeric(cid)
    if cid != "0" and not provider.info(cid)["is_dir"]:
        raise CloudError("目标不是目录", 409)
    return cid


def media_choice(account_id, row, overrides):
    if (overrides or {}).get(row["id"]):
        return overrides[row["id"]]
    with store.db() as con:
        values = con.execute(
            "SELECT payload FROM cloud_assets WHERE account_id=? AND file_id=?",
            (account_id, row["id"]),
        ).fetchall()
    choices = []
    for value in values:
        payload = json.loads(value[0])
        source = payload["source"]
        media = payload["media"]
        if source["size"] != row["size"] or source["sha1"] != row["sha1"]:
            raise CloudError("已绑定源文件的内容指纹变化，请人工核对", 409)
        choice = {k: media.get(k) for k in ("kind", "tmdb_id", "season", "episode")}
        if choice not in choices:
            choices.append(choice)
    if len(choices) > 1:
        raise CloudError("已有媒体身份记录不一致，请人工核对", 409)
    return choices[0] if choices else None


def plan(account_id, source_cid, target_cid, overrides=None):
    account = store.account(account_id)
    if not account:
        raise CloudError("请先扫码登录", 409)
    provider = for_account(account_id)
    _directory(provider, source_cid)
    _directory(provider, target_cid)
    files = walk(provider, source_cid)
    items = []
    skipped = []
    paths = set()
    for row in files:
        if PurePosixPath(row["name"]).suffix.lower() not in recognition.VIDEO:
            continue
        try:
            media = recognition.recognize(
                row["name"], media_choice(account_id, row, overrides)
            )
            path = (media["directory"] + "/" + media["name"]).casefold()
            if path in paths:
                raise CloudError("两个文件将生成相同名称，请选择版本或修正作品", 409)
            paths.add(path)
            related = []
            stem = PurePosixPath(row["name"]).stem
            for follower in files:
                other = PurePosixPath(follower["name"])
                if (
                    follower["parent_id"] == row["parent_id"]
                    and other.suffix.lower() in recognition.SIDECAR
                    and (other.stem == stem or other.stem.startswith(stem + "."))
                ):
                    owners = [
                        r
                        for r in files
                        if r["parent_id"] == row["parent_id"]
                        and PurePosixPath(r["name"]).suffix.lower() in recognition.VIDEO
                        and (
                            other.stem == PurePosixPath(r["name"]).stem
                            or other.stem.startswith(
                                PurePosixPath(r["name"]).stem + "."
                            )
                        )
                    ]
                    if len(owners) == 1:
                        related.append(follower)
            items.append({"source": row, "media": media, "followers": related})
        except CloudError as error:
            skipped.append(
                {"file_id": row["id"], "name": row["name"], "reason": str(error)}
            )
    pid = store.ident()
    payload = {
        "account_id": account_id,
        "account_revision": account["revision"],
        "source_cid": numeric(source_cid),
        "target_cid": numeric(target_cid),
        "items": items,
        "skipped": skipped,
    }
    with store.db() as con:
        con.execute(
            "INSERT INTO cloud_plans VALUES(?,?,?,?,?,?)",
            (
                pid,
                account_id,
                account["revision"],
                "pending",
                store.encode(payload),
                time.time() + 900,
            ),
        )
    return {"id": pid, "items": items, "skipped": skipped, "expires_in": 900}


def apply(plan_id, confirmed=False, generate=True):
    if confirmed is not True:
        raise CloudError("请确认整理计划")
    with store.db() as con:
        row = con.execute(
            "SELECT payload,expires,state FROM cloud_plans WHERE id=?", (plan_id,)
        ).fetchone()
        if not row or row[1] < time.time() or row[2] != "pending":
            raise CloudError("计划已过期或已提交，请重新预览", 409)
        if not json.loads(row[0])["items"]:
            raise CloudError("计划没有可执行项目", 409)
        con.execute(
            "UPDATE cloud_plans SET state='queued' WHERE id=? AND state='pending'",
            (plan_id,),
        )
        return jobs.enqueue(
            "organize", {"plan_id": plan_id, "generate": bool(generate)}, con
        )


def _snapshot_equal(a, b):
    return all(
        a.get(k) == b.get(k)
        for k in ("id", "parent_id", "name", "size", "sha1", "is_dir")
    )


def ensure_directory(provider, job_id, parent, path):
    parent = _directory(provider, parent)
    for segment in PurePosixPath(path).parts:
        if segment in (".", "..", "/") or not segment:
            raise CloudError("整理目标路径无效")
        name = recognition.safe_name(segment)
        rows = [r for r in provider.list(parent) if r["name"] == name]
        if rows:
            if len(rows) != 1 or not rows[0]["is_dir"]:
                raise CloudError("目标目录存在同名冲突", 409)
            parent = rows[0]["id"]
            continue

        def create(parent=parent, name=name):
            try:
                fid = provider.mkdir(parent, name)
            except CloudError as error:
                rows = [
                    r
                    for r in provider.list(parent)
                    if r["name"] == name and r["is_dir"]
                ]
                if len(rows) != 1:
                    raise error
                fid = rows[0]["id"]
            result = provider.info(fid)
            if (
                not result["is_dir"]
                or result["parent_id"] != parent
                or result["name"] != name
            ):
                raise CloudError("新目录身份核验失败", 409, uncertain=True)
            return result

        parent = jobs.step(
            job_id,
            "mkdir:" + parent + ":" + name,
            {"parent": parent, "name": name},
            create,
        )["id"]
    return parent


def relocate(provider, job_id, source, target, name):
    current = provider.info(source["id"])
    if not _snapshot_equal(source, current):
        raise CloudError("源文件在预览后变化，请重新预览", 409)
    if current["parent_id"] == target and current["name"] == name:
        return current
    for parent, label in ((target, name), (current["parent_id"], name)):
        if any(
            r["id"] != current["id"] and r["name"] == label
            for r in provider.list(parent)
        ):
            raise CloudError("整理目标已有同名文件，已保留两个版本", 409)

    def rename():
        error = None
        try:
            provider.rename(current["id"], name)
        except CloudError as exc:
            error = exc
        found = provider.info(current["id"])
        if found["name"] != name or found["parent_id"] != current["parent_id"]:
            raise error or CloudError("重命名结果未确认", 409, uncertain=True)
        return found

    if current["name"] != name:
        current = jobs.step(
            job_id,
            "rename:" + source["id"],
            {"file_id": source["id"], "name": name},
            rename,
        )

    def move():
        # Recheck immediately before mutation, including concurrent outside clients.
        if any(
            r["id"] != current["id"] and r["name"] == name
            for r in provider.list(target)
        ):
            raise CloudError("目标出现同名文件，移动已停止", 409)
        error = None
        try:
            provider.move(current["id"], target)
        except CloudError as exc:
            error = exc
        found = provider.info(current["id"])
        if (
            found["parent_id"] != target
            or found["name"] != name
            or found["size"] != source["size"]
            or found["sha1"] != source["sha1"]
        ):
            raise error or CloudError("移动结果未确认", 409, uncertain=True)
        return found

    if current["parent_id"] != target:
        current = jobs.step(
            job_id,
            "move:" + source["id"],
            {"file_id": source["id"], "target": target},
            move,
        )
    return current


def organize(job_id, payload):
    with store.db() as con:
        row = con.execute(
            "SELECT payload,state FROM cloud_plans WHERE id=?", (payload["plan_id"],)
        ).fetchone()
    if not row or row[1] != "queued":
        raise CloudError("整理计划状态无效", 409)
    data = json.loads(row[0])
    provider = verify_account(data)
    result = {"files": [], "skipped": data["skipped"], "errors": [], "generated": []}
    for item in data["items"]:
        try:
            for source in [item["source"], *item["followers"]]:
                if not _snapshot_equal(source, provider.info(source["id"])):
                    raise CloudError("源文件在预览后变化，请重新预览", 409)
            target = ensure_directory(
                provider, job_id, data["target_cid"], item["media"]["directory"]
            )
            row = relocate(
                provider, job_id, item["source"], target, item["media"]["name"]
            )
            store.save_file(data["account_id"], row)
            result["files"].append(row)
            old_stem = PurePosixPath(item["source"]["name"]).stem
            new_stem = PurePosixPath(row["name"]).stem
            companions = []
            for follower in item["followers"]:
                new_name = new_stem + follower["name"][len(old_stem) :]
                related = relocate(provider, job_id, follower, target, new_name)
                store.save_file(data["account_id"], related)
                companions.append(related)
            if payload.get("generate", True):
                from app.cloud import strm

                asset = strm.generate(
                    data["account_id"],
                    row,
                    item["media"],
                    data["target_cid"],
                    companions,
                    provider,
                )
                result["generated"].append(
                    {"asset_id": asset["id"], "path": asset["local_path"]}
                )
                for warning in asset["payload"].get("warnings", []):
                    result["errors"].append({"file_id": row["id"], "reason": warning})
        except CloudError as error:
            if error.uncertain:
                raise
            result["errors"].append(
                {"file_id": item["source"]["id"], "reason": str(error)}
            )
    with store.db() as con:
        con.execute(
            "UPDATE cloud_plans SET state=? WHERE id=?",
            ("done" if not result["errors"] else "needs_review", payload["plan_id"]),
        )
    if result["generated"]:
        result["refresh_pending"] = not refresh_emby()
    return result


def preview_share(account_id, link, password="", cid="0", offset=0):
    account = store.account(account_id)
    if not account:
        raise CloudError("请先扫码登录", 409)
    code, password = parse_share(link, password)
    provider = for_account(account_id)
    page = provider.share_page(code, password, cid, offset)
    pid = store.ident()
    data = {
        "account_id": account_id,
        "account_revision": account["revision"],
        "code": code,
        "password": password,
        "share_parent": cid,
        "items": page["items"],
    }
    with store.db() as con:
        con.execute(
            "INSERT INTO cloud_plans VALUES(?,?,?,?,?,?)",
            (
                pid,
                account_id,
                account["revision"],
                "share_pending",
                store.encode(data),
                time.time() + 600,
            ),
        )
    return {"preview_id": pid, **page}


def transfer_submit(
    preview_id, ids, target_cid, confirmed=False, organize_target=None, generate=True
):
    if confirmed is not True:
        raise CloudError("请确认转存内容与目录")
    if not ids or len(set(ids)) != len(ids) or len(ids) > 100:
        raise CloudError("选择的文件无效")
    target_cid = numeric(target_cid)
    with store.db() as con:
        row = con.execute(
            "SELECT payload,state,expires FROM cloud_plans WHERE id=?", (preview_id,)
        ).fetchone()
        if not row or row[1] != "share_pending" or row[2] < time.time():
            raise CloudError("分享预览已过期或已使用", 409)
        data = json.loads(row[0])
        if not set(ids).issubset({r["id"] for r in data["items"]}):
            raise CloudError("选择不属于当前预览")
        con.execute(
            "UPDATE cloud_plans SET state='share_queued' WHERE id=? AND state='share_pending'",
            (preview_id,),
        )
        return jobs.enqueue(
            "transfer",
            {
                **data,
                "source_ids": ids,
                "target_cid": target_cid,
                "organize_target": organize_target,
                "generate": bool(generate),
            },
            con,
        )


def transfer(job_id, payload):
    provider = verify_account(payload)
    target = _directory(provider, payload["target_cid"])
    stage = ensure_directory(provider, job_id, target, "转存待整理-" + job_id[:12])
    if provider.list(stage):
        raise CloudError("转存目录已存在内容，禁止重复接收", 409, uncertain=True)

    def receive():
        provider.receive(
            payload["code"], payload["password"], payload["source_ids"], stage
        )
        rows = provider.list(stage)
        if not rows:
            raise CloudError(
                "115 已接受请求，目标内容尚未可见，等待核实", 409, uncertain=True
            )
        mapping = receipt_mapping(payload, rows)
        files = walk(provider, stage)
        if not files:
            raise CloudError("目标没有可核实文件，请检查任务目录", 409, uncertain=True)
        for file in files:
            store.save_file(payload["account_id"], file)
        return {
            "stage_id": stage,
            "files": files,
            "source_ids": payload["source_ids"],
            "mapping": mapping,
        }

    result = jobs.step(
        job_id,
        "receive",
        {"source_ids": payload["source_ids"], "stage_id": stage},
        receive,
    )
    if payload.get("organize_target"):
        planned = plan(payload["account_id"], stage, payload["organize_target"])
        with store.db() as con:
            con.execute(
                "UPDATE cloud_plans SET state='queued' WHERE id=?", (planned["id"],)
            )
        result["organization"] = organize(
            job_id,
            {"plan_id": planned["id"], "generate": payload.get("generate", True)},
        )
        result["errors"] = result["organization"]["errors"]
        result["skipped"] = result["organization"]["skipped"]
        result["refresh_pending"] = result["organization"].get("refresh_pending", False)
    return result


def receipt_mapping(payload, received):
    """Verify the selected top-level receipt, keeping share IDs distinct from account IDs."""
    expected = [x for x in payload["items"] if x["id"] in payload["source_ids"]]
    if len(received) != len(expected):
        raise CloudError("目标数量尚未与预览一致，请稍后核实", 409, uncertain=True)
    remaining = list(received)
    mapping = []
    for source in expected:
        matches = [
            x
            for x in remaining
            if x["name"] == source["name"]
            and x["is_dir"] == source["is_dir"]
            and (
                source["is_dir"]
                or (
                    x["size"] == source["size"]
                    and (not source["sha1"] or x["sha1"] == source["sha1"])
                )
            )
        ]
        if len(matches) != 1:
            raise CloudError(
                "目标内容不唯一或与分享不符，保留待核实状态", 409, uncertain=True
            )
        target = matches[0]
        remaining.remove(target)
        mapping.append({"source_id": source["id"], "target_id": target["id"]})
    return mapping


def sync(job_id, payload):
    provider = verify_account(payload)
    _directory(provider, payload["source_cid"])
    rows = walk(provider, payload["source_cid"])
    result = {"generated": [], "skipped": [], "errors": []}
    for row in rows:
        if PurePosixPath(row["name"]).suffix.lower() not in recognition.VIDEO:
            continue
        try:
            media = recognition.recognize(
                row["name"],
                media_choice(payload["account_id"], row, payload.get("overrides")),
            )
            from app.cloud import strm

            stem = PurePosixPath(row["name"]).stem
            companions = [
                r
                for r in rows
                if r["parent_id"] == row["parent_id"]
                and (
                    PurePosixPath(r["name"]).stem == stem
                    or PurePosixPath(r["name"]).stem.startswith(stem + ".")
                )
                and PurePosixPath(r["name"]).suffix.lower() not in recognition.VIDEO
            ]
            asset = strm.generate(
                payload["account_id"],
                row,
                media,
                payload["source_cid"],
                companions,
                provider,
            )
            result["generated"].append(asset["local_path"])
            for warning in asset["payload"].get("warnings", []):
                result["errors"].append({"file_id": row["id"], "reason": warning})
            store.save_file(payload["account_id"], row)
        except CloudError as error:
            result["skipped"].append(
                {"file_id": row["id"], "name": row["name"], "reason": str(error)}
            )
    if not result["errors"]:
        from app.cloud import strm

        result["reconciliation"] = strm.reconcile(
            payload["account_id"], payload["source_cid"], {r["id"] for r in rows}
        )
    if result["generated"]:
        result["refresh_pending"] = not refresh_emby()
    return result


def refresh_emby():
    from app import engine

    try:
        engine.emby_request("/Library/Refresh", method="POST", timeout=10, retries=0)
        return True
    except Exception:
        return False


def run(job_id, kind, payload):
    if kind == "transfer":
        return transfer(job_id, payload)
    if kind == "organize":
        return organize(job_id, payload)
    if kind == "sync":
        return sync(job_id, payload)
    if kind == "workflow":
        provider = verify_account(payload)
        _directory(provider, payload["source_cid"])
        planned = plan(
            payload["account_id"],
            payload["source_cid"],
            payload["target_cid"],
            payload.get("overrides"),
        )
        with store.db() as con:
            con.execute(
                "UPDATE cloud_plans SET state='queued' WHERE id=?", (planned["id"],)
            )
        return organize(job_id, {"plan_id": planned["id"], "generate": True})
    raise CloudError("任务类型无效")
