"""Transfer, remote organization and STRM share one durable file identity."""

import json
import time
from pathlib import PurePosixPath
from app.cloud import store, jobs, recognition, settings, directories, categories, naming, file_filters, quality8
from app.cloud.provider115 import for_account, numeric, parse_share
from app.cloud.errors import CloudError, UnrecognizedMedia


def verify_account(payload):
    account = store.account(payload["account_id"])
    if not account or account["revision"] != payload["account_revision"]:
        raise CloudError("账号授权已变化，请重新预览", 409)
    provider = for_account(account["id"])
    provider.profile()
    return provider


def walk(provider, cid, limit=20000, exclude=None, job_id=None):
    queue = [numeric(cid)]
    seen = set()
    result = []
    while queue:
        jobs.checkpoint(job_id)
        parent = queue.pop(0)
        if parent in seen:
            raise CloudError("目录树重复或存在循环，已停止扫描", 409)
        seen.add(parent)
        if len(seen) > 20000:
            raise CloudError("目录过大，请选择更具体的目录", 413)
        rows = provider.list(parent)
        for row in rows:
            if row["is_dir"]:
                if row["id"] not in (exclude or set()):
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
        if media.get("category_override"):
            choice["category"] = media["category_override"]
        if choice not in choices:
            choices.append(choice)
    if len(choices) > 1:
        raise CloudError("已有媒体身份记录不一致，请人工核对", 409)
    return choices[0] if choices else None


def related_companions(rows, video, config=None):
    video_extensions = set(config["video_extensions"]) | recognition.VIDEO if config else recognition.VIDEO
    metadata_extensions = config["metadata_extensions"] if config else recognition.SIDECAR
    stem = PurePosixPath(video["name"]).stem
    result = []
    for row in rows:
        path = PurePosixPath(row["name"])
        if (
            row["parent_id"] != video["parent_id"]
            or path.suffix.lower() not in metadata_extensions
        ):
            continue
        if path.stem != stem and not path.stem.startswith(stem + "."):
            continue
        owners = [
            r
            for r in rows
            if r["parent_id"] == row["parent_id"]
            and PurePosixPath(r["name"]).suffix.lower() in video_extensions
            and (
                path.stem == PurePosixPath(r["name"]).stem
                or path.stem.startswith(PurePosixPath(r["name"]).stem + ".")
            )
        ]
        if len(owners) == 1:
            result.append(row)
    return result


NAMING_KEYS = tuple(naming.DEFAULT_TEMPLATES) + file_filters.KEYS + ("naming_style", "rename_enabled", "movie_folder", "tv_folder", "classification_enabled", "enabled_categories", "custom_categories", "region_classification", "year_classification", "unknown_enabled", "region_mode", "region_groups", "animation_movie_folder", "ffprobe_enabled", "ffprobe_failure", "quality8_enabled", "quality8_rules", "quality8_coexist_dv")


def naming_snapshot(config):
    return {k: config[k] for k in NAMING_KEYS}


def recognize_source(account_id, provider, row, overrides, config):
    def probe():
        from app.cloud import media_probe
        try:
            return media_probe.extract(account_id, provider, row)
        except CloudError as error:
            if config["ffprobe_failure"] == "skip" or error.status == 409:
                raise
            return {"status": "fallback", "fields": {}, "warning": str(error).replace("，保留源文件", "")+"；已按设置回退文件名解析"}
    media = recognition.recognize(row["name"], media_choice(account_id, row, overrides), config, probe if config["ffprobe_enabled"] else None)
    with store.db() as con:
        prior = con.execute("SELECT payload FROM cloud_assets WHERE account_id=? AND file_id=? ORDER BY updated DESC LIMIT 1", (account_id, row["id"])).fetchone()
    original = row["name"]
    if prior:
        payload = json.loads(prior[0])
        if all(payload["source"][k] == row[k] for k in ("size", "sha1")):
            previous = payload["media"]
            original = previous.get("quality_source_name") or previous.get("naming_fields", {}).get("original_name") or original
    media["quality_source_name"] = original
    return media


def plan(account_id, source_cid, target_cid, overrides=None, unknown_cid=None, job_id=None):
    account = store.account(account_id)
    if not account:
        raise CloudError("请先扫码登录", 409)
    provider = for_account(account_id)
    _directory(provider, source_cid)
    _directory(provider, target_cid)
    if str(source_cid) == str(target_cid):
        raise CloudError("来源和归档不能是同一目录", 409)
    config = settings.get()
    if config["unknown_enabled"] and unknown_cid is None:
        selected = directories.get(account_id)["directories"]["unknown"]
        unknown_cid = selected["id"] if selected else None
    if config["unknown_enabled"]:
        if not unknown_cid:
            raise CloudError("请先在文件管理中选择未识别目录", 409)
        _directory(provider, unknown_cid)
        if str(unknown_cid) == str(target_cid):
            raise CloudError("未识别目录不能与归档目录相同", 409)
    else:
        unknown_cid = None
    files = walk(provider, source_cid, exclude={str(target_cid), str(unknown_cid)} - {str(source_cid)}, job_id=job_id)
    videos, filtered = file_filters.select(files, config)
    items = []
    skipped = []
    unknown_items = []
    paths = set()
    quality_candidates = []
    for index, row in enumerate(videos):
        jobs.checkpoint(job_id)
        if job_id:
            jobs.progress(job_id, len(videos), index, "识别预览", row["name"])
        try:
            media = recognize_source(account_id, provider, row, overrides, config)
            quality_candidates.append({"source": row, "media": media})
            path = (media["directory"] + "/" + media["name"]).casefold()
            if path in paths:
                raise CloudError("两个文件将生成相同名称，请选择版本或修正作品", 409)
            paths.add(path)
            related = related_companions(files, row, config)
            items.append({"source": row, "media": media, "followers": related})
        except CloudError as error:
            entry = {"file_id": row["id"], "name": row["name"], "reason": str(error)}
            if isinstance(error, UnrecognizedMedia) and unknown_cid and str(unknown_cid) != str(source_cid):
                with store.db() as con:
                    bound = con.execute("SELECT 1 FROM cloud_assets WHERE account_id=? AND file_id=? AND state IN ('active','publishing','needs_review')", (account_id, row["id"])).fetchone()
                if not bound:
                    folder = "来源-" + row["parent_id"]
                    related = related_companions(files, row, config)
                    unknown_items.append({"source": row, "followers": related, "unknown": True, "directory": folder, "reason": str(error)})
                    entry.update({"action": "unknown", "target": folder+"/"+row["name"]})
            skipped.append(entry)
    comparisons = quality_preview(provider, account, str(target_cid), quality_candidates, config, job_id) if config["quality8_enabled"] else {"comparisons": [], "preview_only": True}
    jobs.checkpoint(job_id)
    pid = store.ident()
    payload = {
        "account_id": account_id,
        "account_revision": account["revision"],
        "source_cid": numeric(source_cid),
        "target_cid": numeric(target_cid),
        "items": items,
        "skipped": skipped,
        "unknown_items": unknown_items,
        "unknown_cid": unknown_cid,
        "naming_snapshot": naming_snapshot(config),
        "filtered": filtered,
        "quality8": comparisons,
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
    return {"id": pid, "items": items, "skipped": skipped, "unknown_items": unknown_items, "unknown_cid": unknown_cid, "filtered": filtered, "quality8": comparisons, "expires_in": 900}


def quality_preview(provider, account, target_cid, candidates, config, job_id=None):
    identities = {quality8.identity(c["media"]) for c in candidates}
    with store.db() as con:
        assets = con.execute("SELECT payload FROM cloud_assets WHERE account_id=? AND state='active' AND json_extract(payload,'$.scope_cid')=?", (account["id"], target_cid)).fetchall()
    existing, seen, parents = [], set(), {}
    for raw in assets:
        data = json.loads(raw[0])
        media, old = data["media"], data["source"]
        if data.get("scope_cid") != target_cid or data.get("uid") != account["uid"] or quality8.identity(media) not in identities or old["id"] in seen:
            continue
        jobs.checkpoint(job_id)
        seen.add(old["id"])
        try:
            current = provider.info(old["id"])
            if not _snapshot_equal(old, current):
                raise CloudError("已有版本身份或位置变化", 409)
            parent, visited = current["parent_id"], set()
            while parent != target_cid:
                jobs.checkpoint(job_id)
                if parent == "0" or parent in visited or len(visited) >= 64:
                    raise CloudError("已有版本不在当前归档范围", 409)
                visited.add(parent)
                if parent not in parents:
                    parents[parent] = provider.info(parent)
                if not parents[parent]["is_dir"]:
                    raise CloudError("已有版本目录身份异常", 409)
                parent = parents[parent]["parent_id"]
            existing.append({"source": current, "media": media, "origin": "registered"})
        except jobs.JobStopped:
            raise
        except CloudError:
            existing.append({"source": old, "media": media, "origin": "registered", "blocked": "已有版本来源无法核实，不能作洗版依据"})
    comparisons = []
    for index, candidate in enumerate(candidates):
        jobs.checkpoint(job_id)
        for previous in existing + [{**c, "origin": "incoming"} for c in candidates[:index]]:
            if quality8.identity(previous["media"]) != quality8.identity(candidate["media"]) or previous["source"]["id"] == candidate["source"]["id"]:
                continue
            if len(comparisons) >= 500:
                return {"comparisons": comparisons, "preview_only": True, "truncated": True, "notice": "对比超过 500 项，请分目录预览"}
            explanation = {"suggestion": previous["blocked"], "blocked": True, "preview_only": True, "dimensions": []} if previous.get("blocked") else quality8.compare(quality8.facts(candidate["source"], candidate["media"]), quality8.facts(previous["source"], previous["media"]), config["quality8_rules"], config["quality8_coexist_dv"])
            comparisons.append({"new_id": candidate["source"]["id"], "new_name": candidate["source"]["name"], "existing_id": previous["source"]["id"], "existing_name": previous["source"]["name"], "existing_origin": previous["origin"], **explanation})
    return {"comparisons": comparisons, "preview_only": True, "truncated": False, "notice": "仅对比本批候选及当前归档范围已登记的115源版本；建议不会自动替换或删除"}


def apply(plan_id, confirmed=False, generate=True):
    if confirmed is not True:
        raise CloudError("请确认整理计划")
    with store.db() as con:
        row = con.execute(
            "SELECT payload,expires,state FROM cloud_plans WHERE id=?", (plan_id,)
        ).fetchone()
        if not row or row[1] < time.time() or row[2] != "pending":
            raise CloudError("计划已过期或已提交，请重新预览", 409)
        data = json.loads(row[0])
        if data.get("naming_snapshot") != naming_snapshot(settings.get()):
            if "naming_snapshot" in data:
                raise CloudError("命名或分类配置已变化，请重新预览", 409)
        if not data["items"] and not data.get("unknown_items"):
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
    if "naming_snapshot" in data and data["naming_snapshot"] != naming_snapshot(settings.get()):
        raise CloudError("命名或分类配置已变化，请重新预览", 409)
    provider = verify_account(data)
    if hasattr(provider, "require"):
        provider.require("organize")
    result = {"files": [], "skipped": list(data["skipped"]), "errors": [], "generated": [], "unknown": [], "warnings": [], "media_info": [], "filtered": data.get("filtered", []), "quality8": data.get("quality8", {})}
    all_items = [*data["items"], *data.get("unknown_items", [])]
    jobs.progress(job_id, len(all_items), 0, "整理")
    for index, item in enumerate(all_items):
        if jobs.stopping(job_id):
            result.update(stopped=True, remaining=[{"file_id": i["source"]["id"], "name": i["source"]["name"]} for i in all_items[index:]])
            break
        jobs.progress(job_id, len(all_items), index, "整理", item["source"]["name"])
        try:
            for source in [item["source"], *item["followers"]]:
                if not _snapshot_equal(source, provider.info(source["id"])):
                    raise CloudError("源文件在预览后变化，请重新预览", 409)
            target = ensure_directory(
                provider, job_id, data["unknown_cid"] if item.get("unknown") else data["target_cid"], item["directory"] if item.get("unknown") else item["media"]["directory"]
            )
            row = relocate(
                provider, job_id, item["source"], target, item["source"]["name"] if item.get("unknown") else item["media"]["name"]
            )
            store.save_file(data["account_id"], row)
            result["unknown" if item.get("unknown") else "files"].append(row)
            if not item.get("unknown"):
                information = item["media"].get("media_info", {"status": "filename"})
                result["media_info"].append({"file_id": row["id"], "name": row["name"], **information})
                if information.get("warning"):
                    result["warnings"].append({"name": row["name"], "reason": information["warning"]})
            old_stem = PurePosixPath(item["source"]["name"]).stem
            new_stem = PurePosixPath(row["name"]).stem
            companions = []
            for follower in item["followers"]:
                new_name = new_stem + follower["name"][len(old_stem) :]
                related = relocate(provider, job_id, follower, target, new_name)
                store.save_file(data["account_id"], related)
                companions.append(related)
            if item.get("unknown"):
                result["skipped"] = [x for x in result["skipped"] if x["file_id"] != row["id"]]
            elif payload.get("generate", True):
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
                error.partial_result = result
                raise
            result["errors"].append(
                {"file_id": item["source"]["id"], "reason": str(error)}
            )
        jobs.progress(
            job_id, len(all_items), index + 1, "整理", item["source"]["name"]
        )
    with store.db() as con:
        con.execute(
            "UPDATE cloud_plans SET state=? WHERE id=?",
            ("stopped" if result.get("stopped") else "done" if not result["errors"] else "needs_review", payload["plan_id"]),
        )
    if jobs.stopping(job_id):
        result.update(stopped=True)
        result.setdefault("remaining", [])
        jobs.progress(job_id, len(all_items), len(all_items)-len(result["remaining"]), "已安全停止")
    if result["generated"]:
        result["refresh_pending"] = not refresh_emby()
    return result


def create_categories(job_id, payload):
    provider = verify_account(payload)
    _directory(provider, payload["target_cid"])
    result = {"directories": []}
    for index, name in enumerate(payload["categories"]):
        jobs.progress(job_id, len(payload["categories"]), index, "创建分类", name)
        cid = ensure_directory(provider, job_id, payload["target_cid"], name)
        result["directories"].append({"id": cid, "name": name})
    jobs.progress(job_id, len(payload["categories"]), len(payload["categories"]), "创建分类")
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
    jobs.checkpoint(job_id)
    if hasattr(provider, "require"):
        provider.require("transfer")
    jobs.progress(job_id, len(payload["source_ids"]), 0, "准备转存目录")
    target = _directory(provider, payload["target_cid"])
    stage = ensure_directory(provider, job_id, target, "转存待整理-" + job_id[:12])
    if provider.list(stage):
        raise CloudError("转存目录已存在内容，禁止重复接收", 409, uncertain=True)
    jobs.checkpoint(job_id, {"stage_id": stage, "files": []})

    def receive():
        jobs.progress(
            job_id, len(payload["source_ids"]), 0, "转存中；结果确认前不会重复提交"
        )
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
    jobs.progress(
        job_id, len(payload["source_ids"]), len(payload["source_ids"]), "转存已核实"
    )
    if payload.get("organize_target"):
        jobs.checkpoint(job_id, result)
        try:
            planned = plan(payload["account_id"], stage, payload["organize_target"], job_id=job_id)
        except jobs.JobStopped:
            raise jobs.JobStopped(result)
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
    result = {"generated": [], "skipped": [], "errors": [], "warnings": []}
    config = settings.get()
    videos, result["filtered"] = file_filters.select(rows, config)
    jobs.progress(job_id, len(videos), 0, "生成 STRM")
    for index, row in enumerate(videos):
        jobs.progress(job_id, len(videos), index, "生成 STRM", row["name"])
        try:
            media = recognize_source(payload["account_id"], provider, row, payload.get("overrides"), config)
            if media.get("media_info", {}).get("warning"):
                result["warnings"].append({"name": row["name"], "reason": media["media_info"]["warning"]})
            from app.cloud import strm

            companions = related_companions(rows, row, config)
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
        jobs.progress(job_id, len(videos), index + 1, "生成 STRM", row["name"])
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
    if kind == "categories":
        return create_categories(job_id, payload)
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
            payload.get("unknown_cid"),
            job_id,
        )
        with store.db() as con:
            con.execute(
                "UPDATE cloud_plans SET state='queued' WHERE id=?", (planned["id"],)
            )
        return organize(job_id, {"plan_id": planned["id"], "generate": True})
    raise CloudError("任务类型无效")
