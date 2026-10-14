"""Browser-driven QR login; server-held, expiring, session-bound flows."""

import io
import secrets
import threading
import time
from app.cloud import credentials, store
from app.cloud.errors import CloudError
from app.cloud.provider115 import Provider115, sdk, checked

_LOCK = threading.RLock()
_FLOWS = {}
DEVICES = {"alipaymini", "wechatmini", "web", "android", "ios", "tv"}


def save_cookie(cookie):
    provider = Provider115({"cookie": cookie})
    profile = provider.profile()
    sealed = credentials.seal({"cookie": cookie})
    with store.db() as con:
        row = con.execute(
            "SELECT id FROM cloud_accounts WHERE provider=? AND uid=?",
            ("115", profile["uid"]),
        ).fetchone()
        account_id = row[0] if row else store.ident()
        con.execute(
            "INSERT INTO cloud_accounts VALUES(?,?,?,?,?,1,?) "
            "ON CONFLICT(provider,uid) DO UPDATE SET sealed=excluded.sealed,name=excluded.name,"
            "revision=cloud_accounts.revision+1,updated=excluded.updated",
            (account_id, "115", profile["uid"], profile["name"], sealed, time.time()),
        )
    return store.account(account_id)["id"]


def start(owner, device="alipaymini"):
    if device not in DEVICES:
        raise CloudError("登录设备不受支持")
    with _LOCK:
        for key in list(_FLOWS):
            if _FLOWS[key]["expires"] < time.monotonic():
                _FLOWS.pop(key, None)
        if len(_FLOWS) >= 8:
            raise CloudError("授权会话较多，请稍后重试", 429)
    try:
        response = checked(sdk().login_qrcode_token(device, timeout=15))
        data = response["data"]
        if not isinstance(data, dict) or not all(
            data.get(k) for k in ("uid", "time", "sign", "qrcode")
        ):
            raise ValueError()
    except Exception:
        raise CloudError("115 二维码申请失败，请稍后重试", 502) from None
    flow_id = secrets.token_urlsafe(24)
    with _LOCK:
        _FLOWS[flow_id] = {
            "owner": owner,
            "device": device,
            "data": data,
            "expires": time.monotonic() + 300,
            "last_poll": 0,
            "busy": False,
        }
    return {"flow_id": flow_id, "expires_in": 300}


def _flow(flow_id, owner):
    value = _FLOWS.get(flow_id)
    if not value or value["owner"] != owner or value["expires"] < time.monotonic():
        raise CloudError("二维码已过期，请重新获取", 410)
    return value


def image(flow_id, owner):
    import qrcode

    with _LOCK:
        content = str(_flow(flow_id, owner)["data"]["qrcode"])
    if not 1 < len(content) < 4096:
        raise CloudError("二维码内容无效", 502)
    output = io.BytesIO()
    qrcode.make(content).save(output, format="PNG")
    return output.getvalue()


def cancel(flow_id, owner):
    with _LOCK:
        _flow(flow_id, owner)
        _FLOWS.pop(flow_id, None)


def poll(flow_id, owner):
    with _LOCK:
        flow = _flow(flow_id, owner)
        if flow["busy"] or time.monotonic() - flow["last_poll"] < 3:
            return {"phase": "waiting", "retry_after": 4}
        flow["busy"] = True
        flow["last_poll"] = time.monotonic()
        data = flow["data"]
    try:
        status = checked(
            sdk().login_qrcode_scan_status(
                {k: data[k] for k in ("uid", "time", "sign")}, timeout=12
            )
        )
        code = int(status["data"]["status"])
        if code in (-1, -2):
            cancel(flow_id, owner)
            return {"phase": "expired"}
        if code != 2:
            return {"phase": "scanned" if code == 1 else "waiting"}
        response = checked(
            sdk().login_qrcode_scan_result(data["uid"], app=flow["device"], timeout=15)
        )
        cookie = response["data"]["cookie"]
        if isinstance(cookie, dict):
            cookie = "; ".join(str(k) + "=" + str(v) for k, v in cookie.items())
        if not isinstance(cookie, str) or not all(
            k + "=" in cookie for k in ("UID", "CID", "SEID")
        ):
            raise CloudError("115 未返回完整登录凭据", 502)
        with _LOCK:
            _flow(flow_id, owner)
            # No network while the flow lock is held. Verify before claiming save.
        provider = Provider115({"cookie": cookie})
        profile = provider.profile()
        with _LOCK:
            _flow(flow_id, owner)
            account_id = save_verified(cookie, provider, profile)
            _FLOWS.pop(flow_id, None)
        return {"phase": "connected", "account_id": account_id}
    except CloudError:
        raise
    except Exception:
        return {"phase": "retry", "message": "115 状态查询暂时失败，正在重试"}
    finally:
        with _LOCK:
            if flow_id in _FLOWS:
                _FLOWS[flow_id]["busy"] = False


def save_verified(cookie, provider, profile=None):
    """Save only after caller has verified the remote session."""
    sealed = credentials.seal({"cookie": cookie})
    with store.db() as con:
        row = con.execute(
            "SELECT id FROM cloud_accounts WHERE provider=? AND uid=?",
            ("115", provider.uid),
        ).fetchone()
        account_id = row[0] if row else store.ident()
        con.execute(
            "INSERT INTO cloud_accounts VALUES(?,?,?,?,?,1,?) "
            "ON CONFLICT(provider,uid) DO UPDATE SET sealed=excluded.sealed,name=excluded.name,"
            "revision=cloud_accounts.revision+1,updated=excluded.updated",
            (
                account_id,
                "115",
                provider.uid,
                str((profile or {}).get("name") or "115 账号 " + provider.uid),
                sealed,
                time.time(),
            ),
        )
    return account_id
