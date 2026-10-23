"""Transfer, remote organization and STRM share one durable file identity."""

import json
import re
import time
from pathlib import PurePosixPath
from app.core import parse_season_dir
from app.cloud import store, jobs, recognition, settings, directories, categories, naming, file_filters, quality8
from app.cloud.provider115 import for_account, numeric, parse_share
from app.cloud.errors import CloudError, UnrecognizedMedia, diagnostic as error_diagnostic
from app.domain.provider_errors import stops_batch


def verify_account(payload):
    account = store.account(payload["account_id"])
    if not account or account["revision"] != payload["account_revision"]:
        raise CloudError("账号授权已变化，请重新预览", 409)
    provider = for_account(account["id"])
    provider.profile()
    return provider


def walk(provider, cid, limit=20000, exclude=None, job_id=None, directory_rows=None, source_ids=None, initial_rows=None):
    from app.cloud import events
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
        started = time.monotonic()
        events.emit('organize','开始读取目录：'+parent,job_id,details={'event':'directory_read','status':'started','parent_id':parent})
        rows = initial_rows if parent == str(cid) and initial_rows is not None else provider.list(parent)
        if parent == str(cid) and source_ids is not None:
            selected = set(source_ids)
            rows = [r for r in rows if r["id"] in selected]
            if {r["id"] for r in rows} != selected:
                raise CloudError("本次转存内容已移动或缺失，请重新预览", 409)
        folders = sum(r['is_dir'] for r in rows)
        events.emit('organize','目录读取完成：'+parent+' · 子目录 '+str(folders)+' 个，文件 '+str(len(rows)-folders)+' 个',job_id,details={'event':'directory_read','status':'verified','parent_id':parent,'directory_count':folders,'file_count':len(rows)-folders,'elapsed_ms':int((time.monotonic()-started)*1000)})
        for row in rows:
            if row["is_dir"]:
                if row["id"] not in (exclude or set()):
                    queue.append(row["id"])
                    if directory_rows is not None:
                        directory_rows.append(row)
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


NAMING_KEYS = tuple(naming.DEFAULT_TEMPLATES) + file_filters.KEYS + ("naming_style", "rename_enabled", "movie_folder", "tv_folder", "classification_enabled", "enabled_categories", "custom_categories", "region_classification", "year_classification", "unknown_enabled", "region_mode", "region_groups", "animation_movie_folder", "ffprobe_enabled", "ffprobe_failure", "quality8_enabled", "quality8_rules", "quality8_coexist_dv", "quality8_mode", "cleanup_empty_directories", "strm_delay_seconds", "operation_delay_seconds", "generate_strm_enabled")


def naming_snapshot(config):
    return {k: config[k] for k in NAMING_KEYS}


def parent_context(provider, row, cache):
    """Read the nearest explicit work ID and season from real parent directories."""
    parent, visited, context = row["parent_id"], set(), {"parents": []}
    while parent != "0":
        if parent in visited or len(visited) >= 64:
            raise CloudError("父目录存在循环或层级过深，请重新选择来源", 409)
        visited.add(parent)
        if parent not in cache:
            cache[parent] = provider.info(parent)
        folder = cache[parent]
        if not folder["is_dir"]:
            raise CloudError("来源父目录身份异常", 409)
        context["parents"].append({k: folder[k] for k in ("id", "parent_id", "name", "is_dir")})
        name = folder["name"]
        season = parse_season_dir(name)
        if season is not None and "season" not in context:
            context["season"] = season
        identities = set(re.findall(r"(?i)[\[{]tmdb-(\d+)[\]}]", name))
        if len(identities) > 1:
            raise UnrecognizedMedia("父目录包含多个 TMDB 身份，请人工核对")
        if identities and "tmdb_id" not in context:
            context.update(tmdb_id=next(iter(identities)), directory=name)
        # Classification is only a type hint; the explicit nearest ID stays authoritative.
        if "kind" not in context:
            if name == "电影" or name.endswith("电影"):
                context["kind"] = "movie"
            elif name in ("动漫", "剧集", "综艺", "儿童节目") or name.endswith(("动漫", "剧集", "综艺")):
                context["kind"] = "tv"
        parent = folder["parent_id"]
    return context


def recognize_source(account_id, provider, row, overrides, config, directory_cache=None):
    from app.cloud import events
    started = time.monotonic()
    events.emit('recognize','识别开始：'+row['name'])
    def probe():
        from app.cloud import media_probe
        try:
            return media_probe.extract(account_id, provider, row)
        except CloudError as error:
            if config["ffprobe_failure"] == "skip" or error.status == 409:
                raise
            return {"status": "fallback", "fields": {}, "warning": str(error).replace("，保留源文件", "")+"；已按设置回退文件名解析"}
    choice = media_choice(account_id, row, overrides)
    needs_parent = not choice or (choice.get('kind') == 'tv' and choice.get('season') is None)
    context = parent_context(provider, row, directory_cache if directory_cache is not None else {}) if needs_parent else {}
    from app.cloud import automatic_runs
    media = automatic_runs.recognize(account_id, row, [choice, context], config,
        lambda: recognition.recognize(row["name"], choice, config, probe if config["ffprobe_enabled"] else None, context=context))
    if context.get("parents"):
        media["recognition_context"] = context
    if not choice and context.get("tmdb_id") and not re.search(r"(?i)[\[{]tmdb-\d+[\]}]", row["name"]):
        events.emit('recognize','父目录作品身份：'+context['directory']+' · TMDB '+context['tmdb_id'])
    with store.db() as con:
        prior = con.execute("SELECT payload FROM cloud_assets WHERE account_id=? AND file_id=? ORDER BY updated DESC LIMIT 1", (account_id, row["id"])).fetchone()
    original, previous = row["name"], {}
    if prior:
        payload = json.loads(prior[0])
        if all(payload["source"][k] == row[k] for k in ("size", "sha1")):
            previous = payload["media"]
            original = previous.get("quality_source_name") or previous.get("naming_fields", {}).get("original_name") or original
    diagnostic = {'file_id':row['id'],'source_name':row['name'],'sha1':row['sha1'],'size':row['size'],
                  'kind':media['kind'],'tmdb_id':media['tmdb_id'],'season':media.get('season'),'episode':media.get('episode')}
    events.emit('recognize','识别完成：'+media['title']+' · TMDB '+str(media['tmdb_id']),details={**diagnostic,'status':'verified','elapsed_ms':int((time.monotonic()-started)*1000)})
    events.emit('naming','命名预览：'+row['name']+' → '+media['directory']+'/'+media['name'],details={**diagnostic,'status':'preview','target_name':media['name'],'path':media['directory']})
    media["quality_source_name"] = original
    from app.cloud import replacement
    return replacement.decorate(row, media, config, previous)


def plan(account_id, source_cid, target_cid, overrides=None, unknown_cid=None, job_id=None, source_ids=None, expected_revision=None):
    started = time.monotonic()
    account = store.account(account_id)
    if not account:
        raise CloudError("请先扫码登录", 409)
    if expected_revision is not None and account['revision'] != expected_revision:
        raise CloudError('账号授权已变化，请重新预览', 409)
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
            raise CloudError("请先选择未识别目录", 409)
        _directory(provider, unknown_cid)
        if str(unknown_cid) == str(target_cid):
            raise CloudError("未识别目录不能与归档目录相同", 409)
    else:
        unknown_cid = None
    from app.cloud import events
    events.emit('organize','开始扫描整理来源目录 ID '+str(source_cid),job_id)
    cleanup_candidates = []
    files = walk(provider, source_cid, exclude={str(target_cid), str(unknown_cid)} - {str(source_cid)}, job_id=job_id, directory_rows=cleanup_candidates if config["cleanup_empty_directories"] else None, source_ids=source_ids)
    videos, filtered = file_filters.select(files, config)
    events.emit('organize','来源预分析：文件 '+str(len(files))+' 个，可处理视频 '+str(len(videos))+' 个，过滤 '+str(len(filtered))+' 项',job_id,details={'event':'preanalysis','status':'finished','file_count':len(files),'video_count':len(videos),'filtered_count':len(filtered)})
    items = []
    skipped = []
    unknown_items = []
    paths = set()
    quality_candidates = []
    directory_cache = {}
    from app.cloud import replacement
    for index, row in enumerate(videos):
        jobs.checkpoint(job_id)
        if job_id:
            jobs.progress(job_id, len(videos), index, "识别预览", row["name"])
        try:
            media = recognize_source(account_id, provider, row, overrides, config, directory_cache)
            quality_candidates.append({"source": row, "media": media})
            path = (media["directory"] + "/" + media["name"]).casefold()
            if path in paths and not replacement.enabled(config):
                raise CloudError("两个文件将生成相同名称，请选择版本或修正作品", 409)
            paths.add(path)
            related = related_companions(files, row, config)
            items.append({"source": row, "media": media, "followers": related,
                          "override": (overrides or {}).get(row['id'])})
        except CloudError as error:
            events.emit('recognize','跳过：'+row['name']+' · '+str(error),job_id,level='WARNING')
            entry = {"file_id": row["id"], "name": row["name"], **error_diagnostic(error, 'recognize'),
                     "sha1": row.get('sha1', ''), "size": row.get('size', 0),
                     "override": (overrides or {}).get(row['id']), "recognition": getattr(error, 'recognition', None)}
            if error.uncertain or stops_batch(error.category):
                error.partial_result = {'skipped': skipped, 'errors': [entry],
                    'remaining': [{'file_id': r['id'], 'name': r['name']} for r in videos[index+1:]]}
                raise
            if isinstance(error, UnrecognizedMedia) and unknown_cid and str(unknown_cid) != str(source_cid):
                with store.db() as con:
                    bound = con.execute("SELECT 1 FROM cloud_assets WHERE account_id=? AND file_id=? AND state IN ('active','publishing','needs_review')", (account_id, row["id"])).fetchone()
                if not bound:
                    folder = "来源-" + row["parent_id"]
                    related = related_companions(files, row, config)
                    unknown_items.append({"source": row, "followers": related, "unknown": True, "directory": folder, "reason": str(error)})
                    entry.update({"action": "unknown", "target": folder+"/"+row["name"]})
            skipped.append(entry)
    quality_held = []
    if replacement.enabled(config):
        items, quality_held = replacement.select(provider, account, str(target_cid), items, config, job_id)
    comparisons = quality_preview(provider, account, str(target_cid), quality_candidates, config, job_id) if config["quality8_enabled"] else {"comparisons": [], "preview_only": True}
    if replacement.enabled(config):
        comparisons.update(preview_only=False, notice='执行模式：新版本归档并发布后核实删除已登记旧源；平局和较差来片保留原位')
    jobs.checkpoint(job_id)
    events.emit('organize','动作规划完成：归档视频 '+str(len(items))+' 个，目标目录 '+str(len({i['media']['directory'] for i in items}))+' 个，未识别待处理 '+str(len(unknown_items))+' 个，跳过 '+str(len(skipped))+' 项',job_id,details={'event':'action_plan','status':'finished','video_count':len(items),'directory_count':len({i['media']['directory'] for i in items}),'renamed_count':sum(i['source']['name']!=i['media']['name'] for i in items),'skipped_count':len(skipped),'elapsed_ms':int((time.monotonic()-started)*1000)})
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
        "cleanup_candidates": cleanup_candidates,
        "quality_held": quality_held,
    }
    with store.db() as con:
        current = con.execute('SELECT revision FROM cloud_accounts WHERE id=?', (account_id,)).fetchone()
        if not current or current[0] != account['revision']:
            raise CloudError('扫描期间账号授权变化，请重新扫描预览', 409)
        jobs.checkpoint(job_id)
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
    return {"id": pid, "items": items, "skipped": skipped, "unknown_items": unknown_items, "unknown_cid": unknown_cid, "filtered": filtered, "quality8": comparisons, "cleanup_candidates": cleanup_candidates, "quality_held": quality_held, "expires_in": 900}


def quality_preview(provider, account, target_cid, candidates, config, job_id=None):
    identities = {quality8.identity(c["media"]) for c in candidates}
    with store.db() as con:
        assets = con.execute("SELECT payload FROM cloud_assets WHERE account_id=? AND state='active' AND coalesce(json_extract(payload,'$.source_kind'),'personal')='personal' AND json_extract(payload,'$.scope_cid')=?", (account["id"], target_cid)).fetchall()
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
    from app.cloud import pipeline
    pipeline.prepare(generate)
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
        if not generate and any(i.get('upgrades') for i in data['items']):
            raise CloudError('执行洗版必须先生成并核实新 STRM，不能关闭生成', 409)
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


def _operation_pause():
    import time
    seconds = settings.get()["operation_delay_seconds"]
    if seconds:
        time.sleep(seconds)


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
            _operation_pause()
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
            {"file_id": source["id"], "name": name, "source_name":current['name'], "parent":current['parent_id'], "sha1":source['sha1'], "size":source['size']},
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
            _operation_pause()
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
            {"file_id": source["id"], "target": target, "source_name":current['name'], "parent":current['parent_id'], "sha1":source['sha1'], "size":source['size']},
            move,
        )
    return current


def organize(job_id, payload):
    if payload.get('recovery_output_snapshot'):
        from app.cloud.automation import output_snapshot
        if payload['recovery_output_snapshot'] != output_snapshot():
            raise CloudError('恢复排队期间输出配置变化，请重新核对', 409)
    with store.db() as con:
        row = con.execute(
            "SELECT payload,state FROM cloud_plans WHERE id=?", (payload["plan_id"],)
        ).fetchone()
    if not row or row[1] != "queued":
        raise CloudError("整理计划状态无效", 409)
    data = json.loads(row[0])
    with store.db() as con:
        con.execute("UPDATE cloud_jobs SET payload=json_set(payload,'$.recovery_plan_id',?,'$.recovery_generate',?) WHERE id=?", (payload['plan_id'], int(payload.get('generate', True)), job_id))
    if "naming_snapshot" in data and data["naming_snapshot"] != naming_snapshot(settings.get()):
        raise CloudError("命名或分类配置已变化，请重新预览", 409)
    from app.cloud import pipeline
    pipeline.prepare(payload.get("generate", True))
    provider = verify_account(data)
    if hasattr(provider, "require"):
        provider.require("organize")
    result = {"files": [], "skipped": list(data["skipped"]), "errors": [], "generated": [], "unknown": [], "warnings": [], "media_info": [], "filtered": data.get("filtered", []), "quality8": data.get("quality8", {})}
    result['quality_held'] = data.get('quality_held', [])
    result['skipped'].extend(i for i in result['quality_held'] if i.get('blocked'))
    config = settings.get()
    moved_parents = set()
    validated_context = set()
    all_items = [*data["items"], *data.get("unknown_items", [])]
    from app.cloud.notifications import BatchPosters
    posters = BatchPosters(job_id, all_items)
    publisher = None
    if payload.get("generate", True) and config["generate_strm_enabled"] and not config["strm_delay_seconds"] and data["items"]:
        from app.cloud.publication import IncrementalPublisher
        publisher = IncrementalPublisher(job_id, data, all_items, result)
    jobs.progress(job_id, len(all_items), 0, "整理")
    try:
        for index, item in enumerate(all_items):
            if jobs.stopping(job_id):
                result.update(stopped=True, remaining=[{"file_id": i["source"]["id"], "name": i["source"]["name"]} for i in all_items[index:]])
                break
            jobs.progress(job_id, len(all_items), index, "整理", item["source"]["name"])
            try:
                for parent in item.get("media", {}).get("recognition_context", {}).get("parents", []):
                    if parent["id"] in validated_context:
                        continue
                    current_parent = provider.info(parent["id"])
                    if any(current_parent[k] != parent[k] for k in ("id", "parent_id", "name", "is_dir")):
                        raise CloudError("作品来源父目录在预览后变化，请重新预览", 409)
                    validated_context.add(parent["id"])
                if item.get('upgrades'):
                    if not payload.get('generate', True) or not config['generate_strm_enabled']:
                        raise CloudError('洗版必须生成新 STRM，保留源文件', 409)
                    from app.cloud import replacement
                    replacement.validate_refs(item['upgrades'], provider)
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
                elif payload.get("generate", True) and config["generate_strm_enabled"]:
                    if config["strm_delay_seconds"]:
                        from app.cloud import automation
                        deferred = automation.defer(job_id, data["account_id"], data["account_revision"], data["target_cid"], row, item["media"], companions, config["strm_delay_seconds"], item.get('upgrades'))
                        result.setdefault("deferred", []).append(deferred)
                    else:
                        publisher.submit(index, row, item["media"], companions, item.get('upgrades'))
                elif not item.get("unknown"):
                    # Explicit organize-only mode has no STRM stage to wait for.
                    posters.verified(index, row, item["media"])
                if row["parent_id"] != item["source"]["parent_id"]:
                    moved_parents.add(item["source"]["parent_id"])
            except CloudError as error:
                from app.cloud import events
                events.emit('organize','整理未完成：'+item['source']['name']+' · '+str(error),job_id,level='WARNING')
                if hasattr(error, 'upgrades'):
                    result['upgrades'] = error.upgrades
                result["errors"].append(
                    {"file_id": item["source"]["id"], "name": item['source']['name'], **error_diagnostic(error, 'organize')}
                )
                if error.uncertain or stops_batch(error.category):
                    result['remaining'] = [{'file_id': i['source']['id'], 'name': i['source']['name']} for i in all_items[index+1:]]
                    error.partial_result = result
                    raise
            finally:
                if publisher:
                    publisher.skip(index)
                else:
                    posters.finish(index)
            jobs.progress(
                job_id, len(all_items), index + 1, "整理", item["source"]["name"]
            )
    finally:
        if publisher:
            publisher.close()
    posters.flush()
    if config["cleanup_empty_directories"] and not result.get("stopped") and not result["errors"] and not jobs.stopping(job_id):
        from app.cloud import automation
        try:
            result["cleanup"] = automation.cleanup(provider, job_id, data, moved_parents)
            if result["cleanup"].get("stopped"):
                result["stopped"] = True
        except CloudError as error:
            result["cleanup"] = getattr(error, "cleanup_result", {})
            error.partial_result = result
            raise
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
        result["refresh_pending"] = not refresh_library(data["account_id"])
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


def share_access(payload):
    from app.cloud import credentials
    if payload.get("sealed_share"):
        return credentials.unseal(payload["sealed_share"])
    # Existing queued transfer jobs keep their original shape until completion.
    return {"code": payload["code"], "password": payload["password"]}


def preview_share(account_id, link, password="", cid="0", offset=0):
    from app.cloud import credentials
    account = store.account(account_id)
    if not account:
        raise CloudError("请先扫码登录", 409)
    code, password = parse_share(link, password)
    provider = for_account(account_id)
    from app.cloud import events
    events.emit('transfer','正在读取分享目录 · 页起点 '+str(offset))
    try:
        page = provider.share_page(code, password, cid, offset)
    except CloudError:
        events.emit('transfer','分享目录读取失败，请核对链接、提取码、账号权限与网络',level='WARNING')
        raise
    events.emit('transfer','分享目录读取完成 · 当前页 '+str(len(page['items']))+' 项')
    pid = store.ident()
    data = {
        "account_id": account_id,
        "account_revision": account["revision"],
        "sealed_share": credentials.seal({"code": code, "password": password}),
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


def transfer_from_saved(preview_id, ids, target_cid, confirmed=False, organize_target=None, generate=True, directory_revision=None):
    """The receive page references the organizer's saved archive directory."""
    from app.cloud import directories
    with store.db() as con:
        row = con.execute('SELECT account_id FROM cloud_plans WHERE id=?', (preview_id,)).fetchone()
    if not row:
        raise CloudError('分享预览已过期或已使用', 409)
    cfg = directories.get(row[0])
    if organize_target is not None:
        archive = cfg['directories'].get('target')
        if not archive or str(organize_target) != archive['id'] or directory_revision is not None and directory_revision != cfg['revision']:
            raise CloudError('整理归档目录已变化，请到影视整理保存后重新读取接收配置', 409)
    return transfer_submit(preview_id, ids, target_cid, confirmed, organize_target, generate, cfg['revision'] if organize_target is not None else None)


def verify_archive(payload):
    if payload.get('archive_revision') is not None:
        from app.cloud import directories
        cfg = directories.get(payload['account_id'])
        if cfg['revision'] != payload['archive_revision'] or (cfg['directories'].get('target') or {}).get('id') != payload['organize_target']:
            raise CloudError('排队期间归档目录配置变化，已停止；请重新预览', 409)


def transfer_submit(
    preview_id, ids, target_cid, confirmed=False, organize_target=None, generate=True, directory_revision=None
):
    if confirmed is not True:
        raise CloudError("请确认转存内容与目录")
    if not ids or len(set(ids)) != len(ids) or len(ids) > 100:
        raise CloudError("选择的文件无效")
    target_cid = numeric(target_cid)
    if organize_target:
        from app.cloud import pipeline
        pipeline.prepare(generate)
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
                "archive_revision": directory_revision if organize_target is not None else None,
                "generate": bool(generate),
            },
            con,
        )


def transfer(job_id, payload):
    try:
        return _transfer(job_id, payload)
    except jobs.JobStopped:
        raise
    except CloudError as error:
        from app.cloud.errors import diagnostic
        item = diagnostic(error)
        result = getattr(error, 'partial_result', {})
        result.setdefault('errors', []).append(item)
        error.partial_result = result
        raise


def _transfer(job_id, payload):
    provider = verify_account(payload)
    verify_archive(payload)
    if payload.get("organize_target"):
        from app.cloud import pipeline
        pipeline.prepare(payload.get("generate", True))
    jobs.checkpoint(job_id)
    if hasattr(provider, "require"):
        provider.require("transfer")
    jobs.progress(job_id, len(payload["source_ids"]), 0, "核对转存目标")
    target = _directory(provider, payload["target_cid"])
    before = provider.list(target)
    expected = [r for r in payload["items"] if r["id"] in payload["source_ids"]]
    if any(r["name"] == item["name"] for r in before for item in expected):
        raise CloudError("转存目标已有同名文件或目录，请更换目录或核对已有内容", 409)
    intent = {
        "source_ids": payload["source_ids"], "stage_id": target,
        "receipt_mode": "direct", "before_ids": [r["id"] for r in before],
        "target_snapshot": transfer_target(provider, target),
    }
    jobs.checkpoint(job_id, {"stage_id": target, "files": []})

    def receive():
        jobs.progress(
            job_id, len(payload["source_ids"]), 0, "转存中；结果确认前不会重复提交"
        )
        access = share_access(payload)
        try:
            provider.receive(access["code"], access["password"], payload["source_ids"], target)
        except CloudError as error:
            error.stage = "submit_transfer"
            raise
        try:
            return await_transfer_receipt(provider, payload, intent, job_id)
        except CloudError as error:
            # Receive has already returned; a read failure cannot prove no write.
            raise CloudError(str(error), error.status, uncertain=True, code=error.code, category=error.category, stage="verify_transfer") from None

    result = jobs.step(
        job_id,
        "receive",
        intent,
        receive,
    )
    jobs.progress(
        job_id, len(payload["source_ids"]), len(payload["source_ids"]), "转存已核实"
    )
    if payload.get("organize_target"):
        jobs.checkpoint(job_id, result)
        try:
            verify_archive(payload)
        except CloudError as error:
            error.partial_result = result
            raise
        try:
            planned = plan(payload["account_id"], target, payload["organize_target"], job_id=job_id, source_ids=[r["target_id"] for r in result["mapping"]])
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


def transfer_target(provider, target):
    # 115's virtual root is listable but has no fs_info file identity.
    if target == "0":
        return {"id": "0", "parent_id": "0", "name": "根目录", "is_dir": True}
    return provider.info(target)


def transfer_container(provider, payload, receipt, job_id):
    """Read both direct receipts and legacy per-job directory receipts."""
    current = transfer_target(provider, receipt["stage_id"])
    if receipt.get("receipt_mode") == "direct":
        expected = receipt["target_snapshot"]
        valid = current["id"] == payload["target_cid"] and all(
            current[k] == expected[k] for k in ("id", "parent_id", "name", "is_dir")
        )
    else:
        valid = current["parent_id"] == payload["target_cid"] and current["name"] == "转存待整理-" + job_id[:12]
    if not current["is_dir"] or not valid:
        raise CloudError("转存目标身份已变化，请重新选择来源预览", 409)
    return current


def transfer_receipt(provider, payload, intent, job_id):
    container = transfer_container(provider, payload, intent, job_id)
    rows = provider.list(container["id"])
    if intent.get("receipt_mode") == "direct":
        before = set(intent["before_ids"])
        names = {r["name"] for r in payload["items"] if r["id"] in payload["source_ids"]}
        rows = [r for r in rows if r["id"] not in before and r["name"] in names]
    mapping = receipt_mapping(payload, rows)
    # Use the listing that was actually matched, rather than immediately reading
    # the root a second time (which can return an older 115 directory snapshot).
    files = walk(provider, container["id"], source_ids=[r["id"] for r in rows], initial_rows=rows)
    if not files:
        raise CloudError("转存目录暂未显示文件，请稍后只读核实原任务", 409, uncertain=True, code="transfer_receipt_pending")
    for file in files:
        store.save_file(payload["account_id"], file)
    return {**intent, "files": files, "mapping": mapping}


# At most 60 seconds of backoff, in addition to the bounded provider read times.
# This retries only verification reads after a successful receive response.
TRANSFER_RECEIPT_DELAYS = (1, 2, 4, 8, 15, 30)


def _transfer_receipt_pause(job_id, seconds, error):
    until = time.monotonic() + seconds
    while True:
        # A write was sent but not verified: stopping must leave it reconcilable,
        # not turn it into a safely stopped or automatically replayable operation.
        if jobs.stopping(job_id):
            raise error
        remaining = until - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(1, remaining))


def await_transfer_receipt(provider, payload, intent, job_id):
    for attempt in range(len(TRANSFER_RECEIPT_DELAYS) + 1):
        try:
            return transfer_receipt(provider, payload, intent, job_id)
        except CloudError as error:
            if error.code != "transfer_receipt_pending" or attempt == len(TRANSFER_RECEIPT_DELAYS) or jobs.stopping(job_id):
                raise
            jobs.progress(job_id, len(payload["source_ids"]), 0,
                          "核验转存结果，等待115目录更新", str(error))
            _transfer_receipt_pause(job_id, TRANSFER_RECEIPT_DELAYS[attempt], error)


def receipt_mapping(payload, received):
    """Verify the selected top-level receipt, keeping share IDs distinct from account IDs."""
    expected = [x for x in payload["items"] if x["id"] in payload["source_ids"]]
    remaining = list(received)
    mapping = []
    missing = []
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
        if not matches and not any(x["name"] == source["name"] for x in remaining):
            missing.append(source)
            continue
        if len(matches) != 1:
            raise CloudError(
                "目标内容不唯一或与分享不符，保留待核实状态", 409, uncertain=True
            )
        target = matches[0]
        remaining.remove(target)
        mapping.append({"source_id": source["id"], "target_id": target["id"]})
    if remaining:
        raise CloudError("目标内容不唯一或与分享不符，保留待核实状态", 409, uncertain=True)
    if missing:
        raise CloudError(
            "目标数量尚未与预览一致（已核实 %d/%d 项），请稍后只读核实原任务；不要重复转存"
            % (len(mapping), len(expected)),
            409, uncertain=True, code="transfer_receipt_pending",
        )
    return mapping


def sync(job_id, payload):
    from app.cloud import pipeline, publication
    pipeline.output_ready()
    provider = verify_account(payload)
    _directory(provider, payload["source_cid"])
    rows = walk(provider, payload["source_cid"], job_id=job_id)
    result = {"generated": [], "skipped": [], "errors": [], "warnings": [], "publication": {}}
    config = settings.get()
    output_snapshot = {k: config[k] for k in publication.OUTPUT_KEYS}
    videos, result["filtered"] = file_filters.select(rows, config)
    directory_cache = {}
    jobs.progress(job_id, len(videos), 0, "生成 STRM")
    for index, row in enumerate(videos):
        if jobs.stopping(job_id):
            result.update(stopped=True, remaining=[{"file_id": r["id"], "name": r["name"]} for r in videos[index:]])
            break
        jobs.progress(job_id, len(videos), index, "生成 STRM", row["name"])
        media = None
        try:
            media = recognize_source(payload["account_id"], provider, row, payload.get("overrides"), config, directory_cache)
            if media.get("media_info", {}).get("warning"):
                result["warnings"].append({"name": row["name"], "reason": media["media_info"]["warning"]})
            from app.cloud import strm

            counts = result['publication'].setdefault(media['directory'], {'published':0,'failed':0})
            companions = related_companions(rows, row, config)
            asset = strm.generate(
                payload["account_id"],
                row,
                media,
                payload["source_cid"],
                companions,
                provider,
                expected_output=output_snapshot,
            )
            result["generated"].append(asset["local_path"])
            counts['published'] += 1
            for warning in asset["payload"].get("warnings", []):
                result["errors"].append({"file_id": row["id"], "reason": warning})
            store.save_file(payload["account_id"], row)
        except CloudError as error:
            if media is not None:
                result['publication'].setdefault(media['directory'], {'published':0,'failed':0})['failed'] += 1
            from app.cloud import events
            events.emit('strm','生成跳过：'+row['name']+' · '+str(error),job_id,level='WARNING')
            if error.uncertain or stops_batch(error.category):
                result['errors'].append({'file_id': row['id'], 'name': row['name'], **error_diagnostic(error, 'organize')})
                result['remaining'] = [{'file_id': r['id'], 'name': r['name']} for r in videos[index+1:]]
                error.partial_result = result
                raise
            result["skipped"].append(
                {"file_id": row["id"], "name": row["name"], **error_diagnostic(error, 'recognize'),
                 'recognition': getattr(error, 'recognition', None)}
            )
        jobs.progress(job_id, len(videos), index + 1, "生成 STRM", row["name"])
    result['cleanup_enabled'] = payload.get('reconcile', config['strm_reconcile_enabled'])
    if result['cleanup_enabled'] and not result["errors"] and not result["skipped"] and not result.get("stopped") and not jobs.stopping(job_id):
        from app.cloud import strm

        result["reconciliation"] = strm.reconcile(
            payload["account_id"], payload["source_cid"], {r["id"] for r in rows}
        )
        from app.cloud import events
        events.emit('strm', 'STRM 补生成/更新 %d 项；失效引用清理结果已记录（仅 NAS 自建引用）' % len(result['generated']), job_id)
    if result["generated"]:
        result["refresh_pending"] = not refresh_library(payload["account_id"])
    return result


def refresh_library(account_id):
    from app.cloud import emby_library, services
    cfg = services.get(account_id,"emby_library",emby_library.DEFAULT)
    if any(c["enabled"] for c in cfg["config"]["connections"]):
        return True  # Durable watchers request scans; this is not ingestion confirmation.
    return refresh_emby()


def refresh_emby():
    from app import engine
    from app.cloud import events

    if not settings.get()["emby_refresh_enabled"]:
        return True
    try:
        events.emit('emby','正在请求 Emby 扫描媒体库')
        engine.emby_request("/Library/Refresh", method="POST", timeout=10, retries=0)
        events.emit('emby','Emby 已接受扫描请求；实际入库需查看确认记录')
        return True
    except Exception:
        events.emit('emby','Emby 扫描请求失败，请检查连接配置',level='WARNING')
        return False


def run(job_id, kind, payload):
    if kind == 'organize_preview':
        from app.cloud import plan_preview
        return plan_preview.run(job_id, payload)
    if kind == 'inventory':
        from app.cloud import inventory
        return inventory.run(job_id, payload)
    if kind in ("intake", "channel", "maintenance"):
        from app.cloud import receiving, maintenance
        if kind == "maintenance":
            return maintenance.run(job_id, payload)
        return receiving.run(job_id, payload)
    if kind == "share_strm":
        from app.cloud import share_library
        return share_library.run(job_id, payload)
    if kind == "publish":
        from app.cloud import automation
        return automation.publish(job_id, payload)
    if kind == "categories":
        return create_categories(job_id, payload)
    if kind == "transfer":
        return transfer(job_id, payload)
    if kind == "organize":
        return organize(job_id, payload)
    if kind == "sync":
        return sync(job_id, payload)
    if kind == "workflow":
        from app.cloud import pipeline
        pipeline.prepare()
        if payload.get("intake_rule"):
            from app.cloud import intake_automation
            intake_automation.ready(verify_account(payload),payload["source_cid"])
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
        return organize(job_id, {"plan_id": planned["id"], "generate": settings.get()["generate_strm_enabled"]})
    raise CloudError("任务类型无效")


def transfer_link(account_id, link, password, organize, expected_revision, request_id, confirmed=False):
    """Explicit whole-link receive; retries of one intent return the original durable job."""
    from app.cloud import credentials
    if confirmed is not True:
        raise CloudError("请确认转存当前分享")
    intent = {"link":link,"password":password,"organize":organize,"revision":expected_revision}
    def prior(con):
        row=con.execute("SELECT sealed,job_id FROM cloud_transfer_requests WHERE account_id=? AND request_id=?",(account_id,request_id)).fetchone()
        if row:
            if credentials.unseal(row[0]) != intent:
                raise CloudError("此提交标识已用于其他内容，请重新发起",409)
            return row[1]
    with store.db() as con:
        existing=prior(con)
    if existing:
        return existing
    cfg=directories.get(account_id)
    if cfg["revision"] != expected_revision:
        raise CloudError("转存目录配置已变化，请刷新目录后重试",409)
    target=cfg["directories"]["transfer"]
    archive=cfg["directories"]["target"] if organize else None
    if not target or (organize and not archive):
        raise CloudError("请先在当前页选择转存目录；自动整理还需要归档目录",409)
    if organize:
        from app.cloud import pipeline
        pipeline.prepare()
    first=preview_share(account_id,link,password)
    if first["has_more"] or not first["items"]:
        raise CloudError("分享根目录为空或超过100项，请点击浏览分享分批选择",409)
    with store.db() as con:
        existing=prior(con)
        if existing:
            return existing
        saved=con.execute("SELECT payload,state,expires FROM cloud_plans WHERE id=?",(first["preview_id"],)).fetchone()
        data=json.loads(saved[0])
        revision=con.execute("SELECT revision FROM cloud_directory_settings WHERE account_id=?",(account_id,)).fetchone()
        account=con.execute("SELECT revision FROM cloud_accounts WHERE id=?",(account_id,)).fetchone()
        if not revision or revision[0] != expected_revision or not account or account[0] != data["account_revision"]:
            raise CloudError("账号或转存目录在读取期间变化，请重新发起",409)
        con.execute("UPDATE cloud_plans SET state='share_queued' WHERE id=?",(first["preview_id"],))
        jid=jobs.enqueue('transfer',{**data,"source_ids":[r["id"] for r in first["items"]],"target_cid":target["id"],"organize_target":archive["id"] if archive else None,"archive_revision":expected_revision if archive else None,"generate":True},con)
        con.execute("INSERT INTO cloud_transfer_requests VALUES(?,?,?,?,?)",(account_id,request_id,credentials.seal(intent),jid,time.time()))
    return jid
