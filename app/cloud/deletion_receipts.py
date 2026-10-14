"""Deletion evidence shared by the Cookie adapter: exact identity, never guesses."""

from app.cloud.errors import CloudError

NOT_FOUND = {20018, 31003, 50015, 70005, 70008, 90008, 430004, 231011}


def removed(provider, file_id, source, acknowledged=False):
    try:
        response = provider.client.fs_file(file_id, timeout=15)
    except Exception:
        raise CloudError("源文件状态查询失败，保留 STRM", 502) from None
    if not isinstance(response, dict):
        raise CloudError("源文件回执不完整，保留 STRM", 502)
    if response.get("state") in (True, 1):
        return None
    code = next((response[k] for k in ("errno", "errNo", "errcode", "errCode", "code", "msg_code") if response.get(k)), None)
    try:
        code = int(code)
    except (TypeError, ValueError):
        raise CloudError("源文件状态无法明确核实，保留 STRM", 409) from None
    if code not in NOT_FOUND:
        raise CloudError("源文件查询不是明确的不存在回执，保留 STRM", 409)
    if any(item["id"] == file_id for item in provider.list(source["parent_id"])):
        return None
    proof = {"file_id": file_id, "not_found_code": code, "parent_complete_absent": True, "acknowledged": bool(acknowledged)}
    if acknowledged:
        return proof
    recycled = receipt(provider, file_id, source)
    return {**recycled, **proof} if recycled else None


def receipt(provider, file_id, source):
    offset, total, seen = 0, None, set()
    while offset <= 100000:
        response = provider.call("recyclebin_list", {"limit": 100, "offset": offset})
        envelope = response["data"] if isinstance(response.get("data"), dict) else response
        rows = envelope.get("list", envelope.get("data"))
        if not isinstance(rows, list):
            raise CloudError("115 回收站回执不完整", 502)
        try:
            count = int(envelope.get("count", response.get("count")))
        except (ValueError, TypeError):
            raise CloudError("115 回收站总数缺失", 502) from None
        if count < 0 or (total is not None and count != total):
            raise CloudError("回收站读取中变化，请稍后核实", 409)
        total = count
        for raw in rows:
            if not isinstance(raw, dict):
                raise CloudError("回收站回执格式无效", 502)
            original_id = str(raw.get("file_id") or raw.get("fid") or "")
            rid = str(raw.get("rid") or raw.get("id") or original_id)
            if not rid or rid in seen:
                raise CloudError("回收站返回不完整或重复内容", 502)
            seen.add(rid)
            # A recycle record ID/name alone is not proof of a physical source ID.
            if original_id != file_id:
                continue
            name = str(raw.get("file_name") or raw.get("name") or raw.get("n") or "")
            try:
                size = int(raw.get("file_size") or raw.get("size") or raw.get("s") or 0)
            except (ValueError, TypeError):
                raise CloudError("回收站文件大小无效", 502) from None
            sha = str(raw.get("sha1") or raw.get("sha") or raw.get("file_sha1") or "").upper()
            if name != source["name"] or size != source["size"] or (sha and source["sha1"] and sha != source["sha1"]):
                raise CloudError("回收站记录与源文件指纹不一致，保留 STRM", 409)
            return {"file_id": file_id, "recycle_id": rid, "name": name, "size": size, "sha1": sha}
        offset += len(rows)
        if offset == total:
            return None
        if not rows or offset > total:
            raise CloudError("回收站响应被截断，保留 STRM", 502)
    raise CloudError("回收站过大，请稍后核实目标文件", 413)
