"""Browser-driven QR login; server-held, expiring, session-bound flows."""

import io
import base64
import hashlib
import secrets
import threading
import time
from app.cloud import credentials, store
from app.cloud.errors import CloudError
from app.cloud.provider115 import Provider115, sdk, checked, numeric

_LOCK = threading.RLock()
_FLOWS = {}
DEVICES = {"alipaymini", "wechatmini", "web", "android", "ios", "tv"}


def save_cookie(cookie):
    provider = Provider115({"cookie": cookie})
    profile = provider.profile()
    return save_verified(cookie, provider, profile)


def start(owner, device="alipaymini", mode="cookie", account_id=None):
    if device not in DEVICES:
        raise CloudError("登录设备不受支持")
    if mode not in ("cookie", "open"):
        raise CloudError("授权模式无效")
    target = store.account(account_id) if account_id else None
    if account_id and not target:
        raise CloudError("当前账号不存在", 404)
    with _LOCK:
        for key in list(_FLOWS):
            if _FLOWS[key]["expires"] < time.monotonic():
                _FLOWS.pop(key, None)
        if len(_FLOWS) >= 8:
            raise CloudError("授权会话较多，请稍后重试", 429)
    try:
        verifier = None
        app_id = None
        if mode == "open":
            from app.cloud import settings, open115

            app_id = settings.get()["open_app_id"]
            if not app_id:
                raise CloudError(
                    "管理员需先配置本应用的 OpenAPI AppID；Cookie 扫码可直接使用", 409
                )
            app_id = numeric(app_id)
            verifier = secrets.token_urlsafe(48)
            challenge = base64.b64encode(
                hashlib.sha256(verifier.encode()).digest()
            ).decode()
            response = open115.checked(
                sdk().login_qrcode_token_open(
                    {
                        "client_id": app_id,
                        "code_challenge": challenge,
                        "code_challenge_method": "sha256",
                    },
                    timeout=15,
                )
            )
        else:
            response = checked(sdk().login_qrcode_token(device, timeout=15))
        data = response["data"]
        if not isinstance(data, dict) or not all(
            data.get(k) for k in ("uid", "time", "sign", "qrcode")
        ):
            raise ValueError()
    except CloudError:
        raise
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
            "mode": mode,
            "verifier": verifier,
            "app_id": app_id,
            "target_uid": target["uid"] if target else None,
            "exchanged": False,
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
        if flow.get("mode") == "open":
            return _open_confirmation(flow_id, owner, flow)
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
    return save_authorization(provider.uid, profile or {}, {"cookie": cookie})


def save_authorization(uid, profile, patch):
    import sqlite3
    from app.cloud.provider115 import numeric

    uid = numeric(uid)
    with _LOCK:
        for _ in range(5):
            with store.db() as con:
                found = con.execute(
                    "SELECT id FROM cloud_accounts WHERE provider=? AND uid=?",
                    ("115", uid),
                ).fetchone()
            row, saved = credentials.load(found[0]) if found else (None, {})
            merged = {**saved, **patch}
            sealed = credentials.seal(merged)
            name = str(
                profile.get("name") or (row or {}).get("name") or "115 账号 " + uid
            )[:100]
            account_id = row["id"] if row else store.ident()
            try:
                with store.db() as con:
                    if row:
                        changed = con.execute(
                            "UPDATE cloud_accounts SET sealed=?,name=?,revision=revision+1,updated=? WHERE id=? AND sealed=? AND revision=?",
                            (
                                sealed,
                                name,
                                time.time(),
                                account_id,
                                row["sealed"],
                                row["revision"],
                            ),
                        ).rowcount
                    else:
                        con.execute(
                            "INSERT INTO cloud_accounts VALUES(?,?,?,?,?,1,?)",
                            (account_id, "115", uid, name, sealed, time.time()),
                        )
                        changed = 1
                if changed:
                    return account_id
            except sqlite3.IntegrityError:
                continue
    raise CloudError("账号授权同时变化，请刷新后重试", 409)


def _open_confirmation(flow_id, owner, flow):
    from app.cloud import open115

    with _LOCK:
        _flow(flow_id, owner)
        if flow["exchanged"]:
            raise CloudError("授权令牌交换结果待核实，请重新扫码", 409)
        flow["exchanged"] = True
    try:
        response = open115.checked(
            sdk().login_qrcode_access_token_open(
                {"uid": flow["data"]["uid"], "code_verifier": flow["verifier"]},
                timeout=15,
            )
        )
        value = open115.tokens(response["data"], flow["app_id"])
        provider = open115.OpenProvider(value, flow["target_uid"])
        profile = provider.profile()
        with _LOCK:
            _flow(flow_id, owner)
            account_id = save_authorization(provider.uid, profile, {"open": value})
            _FLOWS.pop(flow_id, None)
        return {"phase": "connected", "account_id": account_id, "mode": "open"}
    except Exception:
        with _LOCK:
            _FLOWS.pop(flow_id, None)
        raise CloudError(
            "OpenAPI 授权未完成或账号不一致，请重新获取二维码；没有重复交换令牌", 409
        ) from None


def describe():
    results = store.accounts()
    for item in results:
        try:
            _, saved = credentials.load(item["id"])
            value = saved.get("open") or {}
            item["authorization"] = {
                "cookie": bool(saved.get("cookie")),
                "open": bool(value),
                "open_state": value.get("state", "not_connected"),
                "expires_at": value.get("expires_at"),
                "app_id": value.get("app_id"),
                "transfer": bool(saved.get("cookie")),
                "official_delete": bool(value and value.get("state") == "ready"),
            }
        except CloudError as error:
            item["authorization"] = {"error": str(error)}
    return results
