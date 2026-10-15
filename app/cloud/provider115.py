"""p115client adapter. No console login and no implicit retry of remote writes."""

import json
import re
from urllib.parse import urlsplit, parse_qs
from app.cloud.errors import CloudError


def numeric(value):
    value = str(value)
    if not re.fullmatch(r"[0-9]{1,25}", value):
        raise CloudError("文件或目录 ID 无效")
    return value


def sdk():
    from p115client import P115Client

    return P115Client


def checked(response):
    if not isinstance(response, dict) or response.get("state") not in (True, 1):
        raise CloudError("115 拒绝请求，请检查账号状态与操作条件", 502)
    return response


def normalize(row, parent="0"):
    if not isinstance(row, dict):
        raise CloudError("115 文件信息不完整", 502)
    if "fc" in row:
        is_dir = str(row["fc"]) == "0"
    elif "file_category" in row:
        is_dir = str(row["file_category"]) == "0"
    elif "is_directory" in row:
        is_dir = row["is_directory"] in (True, 1, "1")
    elif "sha" in row:
        is_dir = not row["sha"]
    elif "sha1" in row:
        is_dir = not row["sha1"]
    elif "file_sha1" in row:
        is_dir = not row["file_sha1"]
    else:
        is_dir = not bool(row.get("fid") or row.get("file_id"))
    raw_id = (
        (row.get("cid") if is_dir else None)
        or row.get("fid")
        or row.get("file_id")
        or row.get("category_id")
        or row.get("id")
        or ""
    )
    fid = numeric(raw_id)
    name = str(
        row.get("fn")
        or row.get("n")
        or row.get("file_name")
        or row.get("category_name")
        or row.get("name")
        or ""
    )
    if not name or len(name.encode()) > 1024:
        raise CloudError("115 返回异常文件名", 502)
    parent_id = next(
        (
            v
            for v in (
                row.get("pid"),
                row.get("parent_id"),
                row.get("cid") if not is_dir else None,
                row.get("category_id") if not is_dir else None,
            )
            if v not in (None, "")
        ),
        parent,
    )
    try:
        size = int(
            row.get("fs")
            or row.get("s")
            or row.get("file_size")
            or row.get("size")
            or 0
        )
    except (ValueError, TypeError):
        raise CloudError("115 返回异常文件大小", 502) from None
    if size < 0:
        raise CloudError("115 返回异常文件大小", 502)
    return {
        "id": fid,
        "parent_id": numeric(parent_id),
        "name": name,
        "is_dir": is_dir,
        "size": size,
        "sha1": str(
            row.get("sha") or row.get("sha1") or row.get("file_sha1") or ""
        ).upper(),
        "pickcode": str(
            row.get("pc") or row.get("pick_code") or row.get("pickcode") or ""
        ),
    }


def parse_share(link, password=""):
    text = str(link).strip()
    if "://" in text:
        parsed = urlsplit(text)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in ("115.com", "www.115.com", "115cdn.com")
            or parsed.username
            or parsed.password
        ):
            raise CloudError("请输入有效的 115 HTTPS 分享链接")
        match = re.fullmatch(
            r"/s/([A-Za-z0-9]{6,40})(?:-([A-Za-z0-9]{1,12}))?/?", parsed.path
        )
        if not match:
            raise CloudError("分享链接格式无效")
        code = match[1]
        password = (
            password
            or (parse_qs(parsed.query).get("password") or [""])[0]
            or match[2]
            or ""
        )
    else:
        code = text
    if not re.fullmatch(r"[A-Za-z0-9]{6,40}", code) or not re.fullmatch(
        r"[A-Za-z0-9]{0,12}", password
    ):
        raise CloudError("分享码或提取码无效")
    return code, password


class Provider115:
    def require(self, operation):
        if operation not in ("read", "organize", "transfer", "play", "official_delete"):
            raise CloudError("此操作暂不受支持", 409)

    def __init__(self, credentials, expected_uid=None):
        cookie = credentials.get("cookie")
        if not cookie:
            raise CloudError("请先扫码或填写 Cookie 登录 115", 409)
        self.client = sdk()(cookie, console_qrcode=False)
        self.uid = numeric(self.client.user_id)
        if expected_uid and self.uid != str(expected_uid):
            raise CloudError("账号身份已变化，操作已停止", 409)

    def call(self, method, *args, write=False, **kwargs):
        try:
            result = getattr(self.client, method)(*args, timeout=15, **kwargs)
            return checked(result) if isinstance(result, dict) else result
        except CloudError:
            raise
        except Exception:
            raise CloudError(
                (
                    "115 写入结果待核实，请查看任务记录"
                    if write
                    else "115 请求失败，请检查账号或稍后重试"
                ),
                502,
                uncertain=write,
            ) from None

    def profile(self):
        response = self.call("user_my")
        data = response.get("data") or response
        uid = str(data.get("user_id") or data.get("uid") or self.uid)
        if uid != self.uid:
            raise CloudError("115 返回账号身份不一致", 409)
        return {
            "uid": self.uid,
            "name": str(
                data.get("user_name")
                or data.get("userName")
                or data.get("name")
                or self.uid
            )[:100],
        }

    def page(self, parent="0", offset=0):
        parent = numeric(parent)
        payload = self.call(
            "fs_files",
            {
                "cid": parent,
                "limit": 100,
                "offset": offset,
                "show_dir": 1,
                "cur": 1,
                "o": "file_name",
                "asc": 1,
            },
        )
        envelope = payload["data"] if isinstance(payload.get("data"), dict) else payload
        rows = envelope.get("data", envelope.get("list"))
        if not isinstance(rows, list):
            raise CloudError("115 文件列表响应不完整", 502)
        try:
            count = int(envelope.get("count", payload.get("count")))
        except (ValueError, TypeError):
            raise CloudError("115 文件列表总数无效", 502) from None
        return {
            "items": [normalize(row, parent) for row in rows],
            "total": count,
            "offset": offset,
            "has_more": offset + len(rows) < count,
        }

    def list(self, parent="0"):
        parent = numeric(parent)
        result = []
        seen = set()
        offset = 0
        total = None
        while True:
            payload = self.call(
                "fs_files",
                {
                    "cid": parent,
                    "limit": 100,
                    "offset": offset,
                    "show_dir": 1,
                    "cur": 1,
                    "o": "file_name",
                    "asc": 1,
                },
            )
            envelope = (
                payload["data"] if isinstance(payload.get("data"), dict) else payload
            )
            rows = envelope.get("data", envelope.get("list"))
            if not isinstance(rows, list):
                raise CloudError("115 文件列表响应不完整", 502)
            try:
                count = int(envelope.get("count", payload.get("count")))
            except (TypeError, ValueError):
                raise CloudError("115 未返回完整目录总数", 502) from None
            if count < 0 or (total is not None and total != count):
                raise CloudError("115 目录在读取中发生变化，请重新扫描", 409)
            total = count
            for row in rows:
                item = normalize(row, parent)
                if item["id"] in seen or item["parent_id"] != parent:
                    raise CloudError("115 目录返回重复或其他目录的文件", 502)
                seen.add(item["id"])
                result.append(item)
            offset += len(rows)
            if offset == total:
                return result
            if not rows or offset > total or offset > 100000:
                raise CloudError("115 文件列表被截断，已停止扫描", 502)

    def info(self, file_id):
        response = self.call("fs_file", numeric(file_id))
        data = response.get("data")
        if isinstance(data, list) and len(data) == 1:
            data = data[0]
        item = normalize(data)
        if item["id"] != str(file_id):
            raise CloudError("115 返回了其他文件，操作已停止", 409)
        return item

    def mkdir(self, parent, name):
        response = self.call(
            "fs_mkdir", {"pid": numeric(parent), "cname": name}, write=True
        )
        data = response.get("data") or response
        return numeric(data.get("cid") or data.get("file_id") or data.get("id"))

    def move(self, file_id, parent):
        fid = numeric(file_id)
        return self.call(
            "fs_move",
            {
                "fid[0]": fid,
                "pid": numeric(parent),
                "conflict_policy": json.dumps({fid: {"action": "keep_both"}}),
            },
            write=True,
        )

    def rename(self, file_id, name):
        return self.call("fs_rename", [(numeric(file_id), name)], write=True)

    def share_page(self, code, password, parent="0", offset=0):
        response = self.call(
            "share_snap",
            {
                "share_code": code,
                "receive_code": password,
                "cid": numeric(parent),
                "limit": 100,
                "offset": offset,
            },
        )
        data = response.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("list"), list):
            raise CloudError("115 分享列表响应不完整", 502)
        rows = [normalize(row, parent) for row in data["list"]]
        try:
            total = int(data["count"])
        except (ValueError, TypeError, KeyError):
            raise CloudError("115 分享总数缺失", 502) from None
        return {
            "items": rows,
            "total": total,
            "offset": offset,
            "has_more": offset + len(rows) < total,
        }

    def receive(self, code, password, ids, target):
        return self.call(
            "share_receive",
            {
                "share_code": code,
                "receive_code": password,
                "file_id": ",".join(numeric(i) for i in ids),
                "cid": numeric(target),
            },
            write=True,
        )

    def share_download(self, code, password, file_id, ua):
        """Pinned SDK's share URL API; never call personal download/receive."""
        code, password = parse_share(code, password)
        url = self.call("share_download_url", {
            "share_code": code, "receive_code": password, "file_id": numeric(file_id),
        }, headers={"User-Agent": ua})
        try:
            if str(url.get("id")) != str(file_id) or url.get("is_dir"):
                raise CloudError("分享下载地址与文件身份不符", 409)
            return {"url": str(url), "headers": {"User-Agent": ua, **dict(url.get("headers") or {})},
                    "size": int(url.get("size") or 0), "sha1": str(url.get("sha1") or "").upper()}
        except (AttributeError, TypeError, ValueError):
            raise CloudError("分享下载地址响应不完整", 502) from None

    def download(self, file_id, pickcode, ua):
        url = self.call("download_url", pickcode, user_agent=ua)
        if str(url.get("id")) != str(file_id):
            raise CloudError("下载地址与源文件身份不符", 409)
        return {
            "url": str(url),
            "headers": dict(url.get("headers") or {}),
            "size": int(url.get("size") or 0),
        }

    def delete(self, file_id):
        return self.call("fs_delete", {"fid": numeric(file_id)}, write=True)

    def offline_add(self, url, target):
        result = self.call("clouddownload_task_add_url", {"url": url, "wp_path_id": numeric(target)}, write=True)
        data = result.get("data") if isinstance(result.get("data"), dict) else result
        return {"accepted": True, "info_hash": str(data.get("info_hash") or ""), "target_cid": str(target), "completed": False}

    def offline_tasks(self, page=1):
        result = self.call("clouddownload_task_list", {"page": page})
        rows = result.get("tasks")
        if not isinstance(rows, list):
            raise CloudError("115 云下载列表响应不完整", 502)
        return {"items": [{k: r.get(k) for k in ("name", "info_hash", "status", "percentDone", "file_id", "wp_path_id")} for r in rows], "page": page, "page_count": result.get("page_count")}

    def instant_receive(self, item, target):
        def challenge(value):
            if value == "0-131071" and item.get("preid"):
                return item["preid"]
            raise CloudError("115 要求其他分段校验，秒传链接缺少对应数据；未上传文件", 409)
        result = self.call("upload_file_init", filename=item["name"], filesha1=item["sha1"], filesize=item["size"], pid=numeric(target), read_range_bytes_or_hash=challenge, write=True)
        if result.get("reuse") is not True or result.get("status") != 2:
            raise CloudError("115 未命中秒传，未执行普通上传", 409)
        matches = [r for r in self.list(target) if not r["is_dir"] and r["name"] == item["name"] and r["size"] == item["size"] and r["sha1"] == item["sha1"]]
        if len(matches) != 1:
            raise CloudError("秒传结果待核实，不重复提交", 409, uncertain=True)
        return {"file_id": matches[0]["id"], "name": matches[0]["name"], "completed": True}

    def sign_in(self):
        self.call("user_points_sign_post", write=True)
        return {"accepted": True}

    def recycle_items(self):
        rows, seen, total, offset = [], set(), None, 0
        while True:
            result = self.call("recyclebin_list", {"offset": offset, "limit": 100})
            envelope=result["data"] if isinstance(result.get("data"),dict) else result
            data = envelope.get("list",envelope.get("data"))
            if not isinstance(data, list):
                raise CloudError("回收站响应不完整，未清理", 502)
            try:
                count = int(envelope.get("count",result.get("count")))
            except (ValueError, TypeError, KeyError):
                raise CloudError("回收站总数缺失，未清理", 502) from None
            if count > 5000 or count < 0 or (total is not None and total != count):
                raise CloudError("回收站内容过多或读取中变化，未清理", 409)
            total = count
            for r in data:
                rid = numeric(r.get("rid") or r.get("id") or "")
                if rid in seen:
                    raise CloudError("回收站返回重复记录，未清理", 502)
                seen.add(rid)
                try:
                    deleted = float(r.get("dtime") or r.get("delete_time") or 0)
                except (TypeError, ValueError):
                    deleted = 0
                rows.append({"id": rid, "name": str(r.get("file_name") or r.get("name") or ""), "deleted_at": deleted})
            offset += len(data)
            if offset == count:
                return rows
            if not data or offset > count:
                raise CloudError("回收站分页被截断，未清理", 502)

    def recycle_delete(self, ids, password=""):
        if not ids or len(ids) > 100:
            raise CloudError("请选择明确的回收站条目")
        self.call("recyclebin_clean", {"tid": ",".join(numeric(x) for x in ids)}, password=password, write=True)
        return {"accepted": True}

    def remove_empty_directory(self, snapshot):
        fid = numeric(snapshot["id"])
        current = self.info(fid)
        if fid == "0" or not current["is_dir"] or any(current[k] != snapshot[k] for k in ("id", "parent_id", "name")):
            raise CloudError("空目录身份变化，保留目录", 409)
        if self.list(fid):
            return {"kept": True, "reason": "目录新增内容，已保留"}
        self.delete(fid)
        try:
            receipt = self.removed_receipt(fid, snapshot, acknowledged=True)
        except CloudError:
            receipt = None
        if not receipt:
            raise CloudError("空目录删除结果待核实，不重复删除", 409, uncertain=True)
        return {"receipt": receipt}

    def removed_receipt(self, file_id, source, acknowledged=False):
        from app.cloud.deletion_receipts import removed
        return removed(self, numeric(file_id), source, acknowledged)

    def receipt(self, file_id, source):
        from app.cloud.deletion_receipts import receipt
        return receipt(self, numeric(file_id), source)


def for_account(account_id):
    from app.cloud import credentials

    row, saved = credentials.load(account_id)
    return Provider115(saved, row["uid"])
