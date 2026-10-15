"""Registered STRM identities for governance; no path-based cloud guessing here."""

import hashlib
import hmac
import json
from pathlib import Path
import re
import time
from urllib.parse import urlsplit, parse_qs
from app import plan_guard
from app.cloud import store, settings, strm, credentials, provider115, events
from app.cloud.errors import CloudError


def lookup(path, root):
    path = Path(path)
    root = Path(root)
    before = plan_guard.fingerprint(path, (root,))
    if not before:
        raise CloudError("STRM 不可读、已变化或路径无效，保留来源", 409)
    try:
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != before["sha256"]:
            raise CloudError("STRM 在核验中变化，请重新预览", 409)
        line = content.decode("utf-8").strip()
    except (UnicodeDecodeError, OSError):
        raise CloudError("STRM 内容无法核验，保留来源", 409) from None
    if "/api/cloud/play/" not in line:
        raise CloudError("已登记 STRM 内容被替换，禁止按挂载路径兜底删除", 409)
    try:
        url = urlsplit(line)
        match = re.search(r"/api/cloud/play/([a-f0-9]{32})$", url.path)
        if not match:
            raise ValueError()
    except ValueError:
        raise CloudError("网盘播放入口格式变化，不能猜测来源", 409) from None
    asset = store.asset(match[1])
    if not asset or asset["local_path"] != str(path):
        raise CloudError(
            "网盘播放入口缺少此路径的已登记身份，禁止按挂载路径兜底删除", 409
        )
    if asset["state"] not in ("active", "revoked"):
        raise CloudError("来源身份尚未确认或已断开，保留 STRM", 409)
    payload = asset["payload"]
    if payload.get("source_kind") == "share":
        raise CloudError("分享免转存来源只允许在分享库清理 NAS 引用，不能删除 115 源文件", 409)
    base = Path(payload["output_root"])
    if strm.digest(content) != payload["digest"] or not hmac.compare_digest(
        (parse_qs(url.query).get("sig") or [""])[0],
        strm.signature(asset["id"], asset["nonce"]),
    ):
        raise CloudError("STRM 内容或签名已变化，保留来源与引用", 409)
    if (
        not base.is_dir()
        or base.is_symlink()
        or [base.stat().st_dev, base.stat().st_ino] != payload["root_identity"]
    ):
        raise CloudError("STRM 输出根目录身份变化，删除已停止", 409)
    try:
        strm.target(base, str(path.relative_to(base)))
    except ValueError:
        raise CloudError("STRM 不在登记的输出目录，保留来源", 409) from None
    account = store.account(asset["account_id"])
    if not account or account["provider"] != "115" or account["uid"] != payload["uid"]:
        raise CloudError("115 来源账号身份不一致，删除已停止", 409)
    sources = [payload["source"], *payload.get("companions", [])]
    if asset['file_id'] != str(sources[0].get('id')) or any(
        not isinstance(x, dict) or not re.fullmatch(r'[1-9][0-9]*', str(x.get('id','')))
        or not re.fullmatch(r'[0-9]+', str(x.get('parent_id','')))
        or type(x.get('size')) is not int or x['size'] < 0
        or x.get('is_dir') is not False
        for x in sources
    ):
        raise CloudError('登记来源的文件 ID 或指纹结构不一致，禁止删除', 409)
    if len({x["id"] for x in sources}) != len(sources):
        raise CloudError("来源包含重复文件身份，删除已停止", 409)
    return {
        "asset_id": asset["id"],
        "account_id": account["id"],
        "uid": account["uid"],
        "account_revision": account["revision"],
        "sources": sources,
        "media": payload["media"],
        "path": str(path),
        "fingerprint": before,
    }


def share_identity(path):
    # Share cleanup is NAS-only; even disconnected accounts do not require login.
    with store.db() as con:
        row = con.execute(
            "SELECT id FROM cloud_assets WHERE local_path=?", (str(path),)
        ).fetchone()
    return store.asset(row[0]) if row else None


def preview(path, root, local=True):
    if not local:
        asset = share_identity(path)
        payload = asset["payload"] if asset else {}
        return {
            "mode": "115_file_id",
            "asset_id": asset["id"] if asset else None,
            "file_id": asset["file_id"] if asset else None,
            "uid": payload.get("uid"),
            "effect": "仅清理 NAS 引用，保留 115 源文件",
            "enabled": True,
        }
    try:
        bound = lookup(path, root)
    except CloudError as error:
        return {"mode": "115_file_id", "blocked": str(error)}
    if bound is None:
        return None
    try:
        cookie = credentials.load(bound["account_id"])[1].get("cookie")
    except CloudError as error:
        return {"mode": "115_file_id", "blocked": str(error)}
    configured = bool(settings.get()["official_delete_enabled"])
    return {
        "mode": "115_file_id",
        "binding_version": 1,
        "identity_basis": "账号 UID + 文件 ID；SHA1 / 大小 / 父目录 / 文件名复核，不按 SHA1 搜索删除",
        "asset_id": bound["asset_id"],
        "account_id": bound["account_id"],
        "uid": bound["uid"],
        "account_revision": bound["account_revision"],
        "file_id": bound["sources"][0]["id"],
        "source_fingerprints": [
            {k: x[k] for k in ("id", "parent_id", "name", "size", "sha1", "is_dir")}
            for x in bound["sources"]
        ],
        "media_identity": {
            k: bound["media"].get(k) for k in ("kind", "tmdb_id", "season", "episode")
        },
        "effect": (
            "删除 115 已绑定源文件后清理 NAS 引用"
            if local
            else "仅清理 NAS 引用，保留 115 源文件"
        ),
        "delete_configured": configured,
        "authorization_state": "ready" if cookie else "not_connected",
        "authorization_mode": "cookie",
        "enabled": configured and bool(cookie),
    }


def record(account_id, file_id):
    with store.db() as con:
        row = con.execute(
            "SELECT state,payload,result FROM cloud_deletions WHERE account_id=? AND file_id=?",
            (account_id, file_id),
        ).fetchone()
    return (
        {"state": row[0], "source": json.loads(row[1]), "result": json.loads(row[2])}
        if row
        else None
    )


def _state(bound, source, state, result):
    with store.db() as con:
        con.execute(
            "UPDATE cloud_deletions SET state=?,result=?,updated=? WHERE account_id=? AND file_id=?",
            (
                state,
                store.encode(result),
                time.time(),
                bound["account_id"],
                source["id"],
            ),
        )


def _same(current, expected):
    return all(
        current.get(k) == expected.get(k)
        for k in ("id", "parent_id", "name", "size", "sha1", "is_dir")
    )


def _proof(provider, source, previous):
    result = previous["result"]
    trusted = previous["state"] == "verified" and result.get("parent_complete_absent")
    proof = provider.removed_receipt(
        source["id"], source, bool(result.get("acknowledged") or trusted)
    )
    if proof and trusted:
        # A persisted verified receipt survives recycle-bin housekeeping. The
        # current exact missing-file response and complete parent are still checked.
        proof = {
            **proof,
            "acknowledged": bool(result.get("acknowledged")),
            "previous_verified": True,
        }
    return proof


def _diagnostic(bound, source=None, **extra):
    source = source or bound['sources'][0]
    return {'asset_id':bound['asset_id'],'account_id':bound['account_id'],
            'file_id':source['id'],'parent_id':source['parent_id'],'source_name':source['name'],
            'sha1':source['sha1'],'size':source['size'],'path':bound['path'],
            'binding_version':1,**extra}


def delete_sources(bound, root, selected, dry_run=False):
    # Publication and deletion share a process gate. Persistent dispatch receipts
    # remain the authority across processes/restarts; no uncertain request replays.
    try:
        with strm._LOCK:
            return _delete_sources(bound, root, selected, dry_run)
    except CloudError as error:
        events.emit('governance','115 源删除停止，NAS 引用保留：'+str(error),level='WARNING',
                    details=_diagnostic(bound,status='uncertain' if error.uncertain else 'blocked',reason=str(error)))
        raise


def _delete_sources(bound, root, selected, dry_run=False):
    if not settings.get()["official_delete_enabled"]:
        raise CloudError(
            "此来源已绑定 115；请在我的网盘启用 Cookie 源文件删除", 409
        )
    refreshed = lookup(bound["path"], root)
    if refreshed != bound:
        raise CloudError("来源身份在确认后变化，请重新预览", 409)
    with store.db() as con:
        for source in bound["sources"]:
            refs = con.execute(
                "SELECT local_path,payload FROM cloud_assets WHERE account_id=? AND id!=? "
                "AND state IN ('active','publishing','needs_review') AND "
                "(file_id=? OR EXISTS (SELECT 1 FROM json_each(cloud_assets.payload,'$.companions') c "
                "WHERE CAST(json_extract(c.value,'$.id') AS TEXT)=?))",
                (bound['account_id'],bound['asset_id'],source['id'],source['id']),
            ).fetchall()
            if any(r[0] not in selected or json.loads(r[1]).get('source_kind') == 'share' for r in refs):
                raise CloudError(
                    "同一 115 源文件仍有未选中的播放引用，保留来源与 STRM", 409
                )
    provider = provider115.for_account(bound["account_id"])
    provider.require("official_delete")
    provider.profile()
    events.emit('governance','七维治理来源核验：'+bound['sources'][0]['name'],details=_diagnostic(bound,status='preflight',source_count=len(bound['sources'])))
    prepared = []
    # Verify the whole explicitly recorded group before deleting any source member.
    for source in bound["sources"]:
        if source["is_dir"] or not re.fullmatch(
            r"[0-9A-Fa-f]{40}", source["sha1"] or ""
        ):
            raise CloudError("源文件不是有完整指纹的视频或附属文件，禁止删除目录", 409)
        existing = record(bound["account_id"], source["id"])
        if existing:
            if not _same(existing["source"], source):
                raise CloudError("删除记录与来源身份不一致，保留 STRM", 409)
            if existing["state"] == "rejected":
                raise CloudError(
                    "此前删除被拒绝，请核实权限后在工作台重置被拒绝记录", 409
                )
            proof = _proof(provider, source, existing)
            if not proof:
                raise CloudError(
                    "此前写入结果未确认或源文件重新出现，不重复删除",
                    409,
                    uncertain=True,
                )
            if not dry_run:
                _state(bound, source, "verified", proof)
            prepared.append((source, proof))
            events.emit('governance','复用已核实删除回执，不重发请求：'+source['name'],details=_diagnostic(bound,source,status='verified',result='复用回执'))
            continue
        current = provider.info(source["id"])
        if not _same(current, source):
            raise CloudError("115 源文件在预览后变化，请重新同步身份", 409)
        prepared.append((source, None))
    if dry_run:
        events.emit('governance','来源预检完成，仅预览，未发送删除',details=_diagnostic(bound,status='preview',source_count=len(prepared)))
        return {
            "removed": len(prepared),
            "new_ids": [x["id"] for x, _ in prepared],
            "verified": [],
        }
    new_ids = []
    for source, proof in prepared:
        if proof:
            continue
        with store.db() as con:
            inserted = con.execute(
                "INSERT OR IGNORE INTO cloud_deletions VALUES(?,?,?,?,?,?,?)",
                (
                    bound["account_id"],
                    source["id"],
                    bound["asset_id"],
                    "dispatched",
                    store.encode(source),
                    "{}",
                    time.time(),
                ),
            ).rowcount
        if not inserted:
            raise CloudError(
                "此源文件已被另一个操作领取，结果待核实", 409, uncertain=True
            )
        # Recheck immediately before dispatch; no deletion retry is hidden in this layer.
        try:
            if not _same(provider.info(source["id"]), source):
                _state(bound, source, "rejected", {"acknowledged": False})
                raise CloudError("源文件在执行前变化，删除已停止", 409)
            events.emit('governance','按登记文件 ID 发送 115 删除：'+source['name'],details=_diagnostic(bound,source,status='dispatched'))
            provider.delete(source["id"])
            new_ids.append(source["id"])
        except CloudError as error:
            _state(
                bound,
                source,
                "uncertain" if error.uncertain else "rejected",
                {"acknowledged": False},
            )
            if not error.uncertain:
                raise
            proof = provider.removed_receipt(source["id"], source, False)
            if not proof:
                raise CloudError(
                    "115 删除结果待核实，STRM 保留，原请求不会重发", 409, uncertain=True
                )
            new_ids.append(source["id"])
        else:
            _state(bound, source, "acknowledged", {"acknowledged": True})
            proof = provider.removed_receipt(source["id"], source, True)
            if not proof:
                raise CloudError(
                    "115 已接受删除但来源状态尚未确认，STRM 保留", 409, uncertain=True
                )
        _state(bound, source, "verified", proof)
        events.emit('governance','115 源文件删除已核实：'+source['name'],details=_diagnostic(bound,source,status='verified',result='精确不存在回执 + 完整父目录检查'))
    return {
        "removed": len(new_ids),
        "new_ids": new_ids,
        "verified": [x["id"] for x, _ in prepared],
    }


def local_removed(bound):
    with store.db() as con:
        con.execute(
            "UPDATE cloud_assets SET state='revoked',updated=? WHERE id=?",
            (time.time(), bound["asset_id"]),
        )
    events.emit('governance','NAS STRM 已清理，播放引用已撤销',details={'asset_id':bound['asset_id'],'status':'revoked','path':bound.get('path','')})


def verify(asset_id):
    asset = store.asset(asset_id)
    if not asset:
        raise CloudError("播放引用不存在", 404)
    bound = lookup(asset["local_path"], Path(asset["payload"]["output_root"]))
    if not bound:
        raise CloudError("本地来源身份缺失", 409)
    provider = provider115.for_account(bound["account_id"])
    results = []
    for source in bound["sources"]:
        previous = record(bound["account_id"], source["id"])
        if not previous:
            continue
        if not _same(previous["source"], source):
            raise CloudError("删除记录与当前来源不同，保留 STRM", 409)
        proof = _proof(provider, source, previous)
        if not proof:
            raise CloudError("来源仍存在或回执尚不可核实，未重复发送删除", 409)
        _state(bound, source, "verified", proof)
        results.append(proof)
    if not results:
        raise CloudError("此引用没有待核实的删除记录", 409)
    return {
        "verified": results,
        "message": "只核实回执，未重发删除；重新预览治理可继续清理 NAS",
    }


def reset_rejected(asset_id):
    asset = store.asset(asset_id)
    if not asset:
        raise CloudError("播放引用不存在", 404)
    bound = lookup(asset["local_path"], Path(asset["payload"]["output_root"]))
    if not bound:
        raise CloudError("本地来源身份缺失", 409)
    provider = provider115.for_account(bound["account_id"])
    checked_ids = []
    for source in bound["sources"]:
        previous = record(bound["account_id"], source["id"])
        if not previous:
            continue
        if previous["state"] == "verified":
            if not _same(previous["source"], source) or not _proof(
                provider, source, previous
            ):
                raise CloudError("已核实的来源状态变化，不能重置其他成员", 409)
            continue
        if previous["state"] != "rejected":
            raise CloudError("仅明确拒绝的请求可重置；不确定结果必须核实", 409)
        if not _same(provider.info(source["id"]), source):
            raise CloudError("来源身份已变化，不能重置", 409)
        checked_ids.append(source["id"])
    if not checked_ids:
        raise CloudError("没有明确拒绝的记录可重置", 409)
    with store.db() as con:
        for fid in checked_ids:
            con.execute(
                "DELETE FROM cloud_deletions WHERE account_id=? AND file_id=? AND state='rejected'",
                (bound["account_id"], fid),
            )
    return {
        "reset": checked_ids,
        "message": "仅重置被拒绝的记录，未删除文件；请重新预览治理",
    }
