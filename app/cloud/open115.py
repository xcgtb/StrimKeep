"""Application-owned Open authorization, persisted rotating tokens and exact identity."""

from contextlib import contextmanager
import fcntl
import os
import secrets
import time
from app.cloud import credentials, store
from app.cloud.errors import CloudError
from app.cloud.provider115 import Provider115, numeric, normalize


def sdk():
    from p115client import P115OpenClient

    return P115OpenClient


def checked(response):
    if not isinstance(response, dict):
        raise CloudError("115 OpenAPI 响应不完整", 502)
    success = response.get("state") in (True, 1) or (
        "state" not in response and response.get("code") == 0
    )
    if not success or any(
        response.get(k, 0) not in (0, "0", None)
        for k in ("errno", "errNo", "errcode", "errCode", "code", "msg_code")
    ):
        raise CloudError("115 OpenAPI 拒绝请求，请检查本应用授权与接口权限", 502)
    return response


@contextmanager
def account_lock(account_id):
    if len(account_id) != 32 or any(c not in "0123456789abcdef" for c in account_id):
        raise CloudError("账号标识无效")
    file = credentials.storage_directory() / (".cloud-open-" + account_id + ".lock")
    fd = os.open(file, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        deadline = time.monotonic() + 20
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise CloudError("授权更新正在进行，请稍后重试", 409)
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)


def tokens(data, app_id):
    if not isinstance(data, dict):
        raise CloudError("115 未返回完整授权令牌", 502)
    access = data.get("access_token")
    refresh = data.get("refresh_token")
    try:
        ttl = int(data["expires_in"])
    except (KeyError, ValueError, TypeError):
        raise CloudError("115 授权有效期缺失", 502) from None
    if (
        not isinstance(access, str)
        or not 1 < len(access) < 8192
        or not isinstance(refresh, str)
        or not 1 < len(refresh) < 8192
        or not 60 <= ttl <= 86400 * 30
    ):
        raise CloudError("115 授权令牌或有效期无效", 502)
    return {
        "access_token": access,
        "refresh_token": refresh,
        "expires_at": time.time() + ttl,
        "app_id": numeric(app_id),
        "state": "ready",
        "token_revision": 1,
    }


def update(account_id, fn):
    """CAS merge protects a simultaneous Cookie login without holding SQL during HTTP."""
    for _ in range(5):
        row, saved = credentials.load(account_id)
        changed = fn(saved)
        if changed is None:
            return row, saved
        sealed = credentials.seal(changed)
        with store.db() as con:
            done = con.execute(
                "UPDATE cloud_accounts SET sealed=?,updated=? WHERE id=? AND sealed=? AND revision=?",
                (sealed, time.time(), account_id, row["sealed"], row["revision"]),
            ).rowcount
        if done:
            return store.account(account_id), changed
    raise CloudError("授权记录同时变化，请刷新后重试", 409)


def ensure_token(account_id):
    with account_lock(account_id):
        row, saved = credentials.load(account_id)
        value = saved.get("open")
        if not value:
            raise CloudError("此账号尚未绑定本应用的 OpenAPI 授权", 409)
        if value.get("state") != "ready":
            raise CloudError(
                "OpenAPI 授权结果待核实，请重新扫码授权；不重复使用旧刷新令牌", 409
            )
        if value.get("expires_at", 0) > time.time() + 120:
            return row, value
        intent = secrets.token_hex(16)

        def dispatch(current):
            previous = current.get("open", {})
            if previous != value:
                raise CloudError("授权已变化，请重新检查", 409)
            return {
                **current,
                "open": {**previous, "state": "refreshing", "refresh_intent": intent},
            }

        update(account_id, dispatch)
        try:
            response = checked(
                sdk().login_refresh_token_open(
                    {"refresh_token": value["refresh_token"]}, timeout=15
                )
            )
            replacement = tokens(response["data"], value["app_id"])
            replacement["token_revision"] = int(value.get("token_revision", 1)) + 1
        except Exception:

            def uncertain(current):
                previous = current.get("open", {})
                if previous.get("refresh_intent") != intent:
                    return None
                return {**current, "open": {**previous, "state": "needs_reauth"}}

            update(account_id, uncertain)
            raise CloudError(
                "OpenAPI 续期结果不确定，请重新扫码授权；旧刷新令牌不会重发", 409
            ) from None

        def commit(current):
            if current.get("open", {}).get("refresh_intent") != intent:
                return None
            return {**current, "open": replacement}

        row, saved = update(account_id, commit)
        current = saved.get("open", {})
        if current.get("state") != "ready":
            raise CloudError("OpenAPI 续期状态无法确认，请重新授权", 409)
        return row, current


class OpenProvider(Provider115):
    def require(self, operation):
        if operation not in ("read", "play", "official_delete"):
            raise CloudError(
                "分享转存与整理需要补充 Cookie 扫码授权；OpenAPI 可浏览、播放及官方删除",
                409,
            )

    def mkdir(self, *args):
        self.require("organize")

    def rename(self, *args):
        self.require("organize")

    def move(self, *args):
        self.require("organize")

    def share_page(self, *args):
        self.require("transfer")

    def receive(self, *args):
        self.require("transfer")

    def __init__(self, value, expected_uid=None):
        access = value.get("access_token")
        if not access:
            raise CloudError("请先完成 OpenAPI 扫码授权", 409)
        # No SDK implicit login or refresh: all rotation must pass the durable service.
        self.client = sdk()(
            access_token=access, refresh_token="", app_id=0, console_qrcode=False
        )
        self.client.headers["authorization"] = "Bearer " + access
        self.uid = numeric(expected_uid) if expected_uid is not None else None
        self._profile = None
        self.profile()

    def call(self, method, *args, write=False, **kwargs):
        try:
            result = getattr(self.client, method)(*args, timeout=15, **kwargs)
            if write and (
                not isinstance(result, dict)
                or not any(k in result for k in ("state", "code", "errno"))
            ):
                raise CloudError(
                    "OpenAPI 写入回执不完整，结果待核实", 502, uncertain=True
                )
            return checked(result) if isinstance(result, dict) or write else result
        except CloudError:
            raise
        except Exception:
            raise CloudError(
                (
                    "OpenAPI 写入结果待核实"
                    if write
                    else "OpenAPI 请求失败，请检查授权状态与权限"
                ),
                502,
                uncertain=write,
            ) from None

    def profile(self):
        if self._profile:
            return self._profile
        response = self.call("user_info")
        data = response.get("data")
        if not isinstance(data, dict):
            raise CloudError("OpenAPI 用户信息不完整", 502)
        uid = numeric(data.get("user_id") or data.get("uid") or "")
        if self.uid is not None and uid != self.uid:
            raise CloudError("OpenAPI 返回了其他账号身份，操作停止", 409)
        self.uid = uid
        self._profile = {
            "uid": uid,
            "name": str(
                data.get("user_name") or data.get("userName") or data.get("name") or uid
            )[:100],
        }
        return self._profile

    def info(self, file_id):
        response = self.call("fs_info", {"file_id": numeric(file_id)})
        data = response.get("data")
        if isinstance(data, list) and len(data) == 1:
            data = data[0]
        row = normalize(data)
        if row["id"] != str(file_id):
            raise CloudError("OpenAPI 返回了其他文件，操作停止", 409)
        return row

    def delete(self, file_id):
        return self.call("fs_delete", {"file_ids": numeric(file_id)}, write=True)

    def removed_receipt(self, file_id, source, acknowledged=False):
        """Explicit not-found + complete parent + acknowledged delete or exact RB ID."""
        from p115client import check_response
        from p115client.exception import P115FileNotFoundError

        fid = numeric(file_id)
        try:
            response = self.client.fs_info({"file_id": fid}, timeout=15)
        except Exception:
            raise CloudError("源文件状态查询失败，保留 STRM", 502) from None
        try:
            checked(response)
        except CloudError:
            if not isinstance(response, dict):
                raise
            code = next(
                (
                    response[k]
                    for k in (
                        "errno",
                        "errNo",
                        "errcode",
                        "errCode",
                        "code",
                        "msg_code",
                    )
                    if response.get(k)
                ),
                None,
            )
            try:
                code = int(code)
            except (ValueError, TypeError):
                raise CloudError("源文件状态无法明确核实，保留 STRM", 409) from None
            if code not in (20018, 31003, 50015, 70005, 70008, 90008, 430004, 231011):
                raise CloudError("源文件查询不是明确的不存在回执，保留 STRM", 409)
            try:
                check_response({**response, "state": False, "errno": code})
            except P115FileNotFoundError:
                pass
            except Exception:
                raise CloudError("源文件状态无法明确核实，保留 STRM", 409) from None
        else:
            return None
        if any(row["id"] == fid for row in self.list(source["parent_id"])):
            return None
        if acknowledged:
            return {
                "file_id": fid,
                "not_found_code": code,
                "parent_complete_absent": True,
                "acknowledged": True,
            }
        recycled = self.receipt(fid, source)
        return (
            {
                **recycled,
                "not_found_code": code,
                "parent_complete_absent": True,
                "acknowledged": False,
            }
            if recycled
            else None
        )

    def receipt(self, file_id, source):
        """A complete recycle-bin response must identify the exact removed file."""
        fid = numeric(file_id)
        offset = 0
        total = None
        seen = set()
        while offset <= 100000:
            response = self.call("recyclebin_list", {"limit": 100, "offset": offset})
            data = response.get("data")
            envelope = data if isinstance(data, dict) else response
            rows = envelope.get("list", envelope.get("data"))
            if not isinstance(rows, list):
                raise CloudError("OpenAPI 回收站回执不完整", 502)
            try:
                count = int(envelope.get("count", response.get("count")))
            except (ValueError, TypeError):
                raise CloudError("OpenAPI 回收站总数缺失", 502) from None
            if count < 0 or (total is not None and count != total):
                raise CloudError("回收站读取中变化，请稍后核实", 409)
            total = count
            for raw in rows:
                current = str(raw.get("file_id") or raw.get("fid") or "")
                rid = str(raw.get("id") or raw.get("rid") or current)
                if rid in seen:
                    raise CloudError("回收站返回重复内容，保留待核实状态", 409)
                seen.add(rid)
                if current != fid:
                    continue
                name = str(
                    raw.get("file_name") or raw.get("name") or raw.get("n") or ""
                )
                size = int(raw.get("file_size") or raw.get("size") or raw.get("s") or 0)
                sha = str(
                    raw.get("sha1") or raw.get("sha") or raw.get("file_sha1") or ""
                ).upper()
                if (
                    name != source["name"]
                    or size != source["size"]
                    or (sha and source["sha1"] and sha != source["sha1"])
                ):
                    raise CloudError("回收站记录与源文件指纹不一致，保留 STRM", 409)
                return {
                    "file_id": fid,
                    "recycle_id": rid,
                    "name": name,
                    "size": size,
                    "sha1": sha,
                }
            offset += len(rows)
            if offset == total:
                return None
            if not rows or offset > total:
                raise CloudError("回收站响应被截断，保留 STRM", 502)
        raise CloudError("回收站过大，请稍后核实目标文件", 413)


def for_account(account_id):
    row, value = ensure_token(account_id)
    return OpenProvider(value, row["uid"])
