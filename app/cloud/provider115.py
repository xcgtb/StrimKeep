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
        if operation not in ("read", "organize", "transfer", "play"):
            raise CloudError("此操作需要本应用 OpenAPI 授权", 409)

    def __init__(self, credentials, expected_uid=None):
        cookie = credentials.get("cookie")
        if not cookie:
            raise CloudError("请先扫码登录 115", 409)
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

    def download(self, file_id, pickcode, ua):
        url = self.call("download_url", pickcode, user_agent=ua)
        if str(url.get("id")) != str(file_id):
            raise CloudError("下载地址与源文件身份不符", 409)
        return {
            "url": str(url),
            "headers": dict(url.get("headers") or {}),
            "size": int(url.get("size") or 0),
        }


def for_account(account_id):
    from app.cloud import credentials

    row, saved = credentials.load(account_id)
    if not saved.get("cookie") and saved.get("open"):
        from app.cloud import open115

        return open115.for_account(account_id)
    return Provider115(saved, row["uid"])
