"""UA/credential-scoped direct URLs. Cookie-required links use bounded streaming."""

import asyncio
import hmac
import ipaddress
import socket
import threading
import time
from urllib.parse import urlsplit, parse_qs, urlunsplit
import httpx
from fastapi.responses import RedirectResponse, StreamingResponse
from app.cloud import store, strm
from app.cloud.provider115 import for_account
from app.cloud.errors import CloudError

_LOCK = threading.RLock()
_CACHE = {}
_GATES = [threading.Lock() for _ in range(64)]
ALLOWED = (".115.com", ".115cdn.com", ".115cdn.net", ".115vod.com")


def safe_url(value):
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme not in ("http", "https")
        or not host
        or parsed.username
        or parsed.password
    ):
        raise CloudError("115 返回的直链无效", 502)
    if not any(host.endswith(s) or host == s[1:] for s in ALLOWED):
        raise CloudError("115 返回了未认可的 CDN 主机，已停止播放解析", 502)
    try:
        port = parsed.port
    except ValueError:
        raise CloudError("CDN 端口无效", 502) from None
    if port not in (None, 80, 443):
        raise CloudError("CDN 端口无效", 502)
    return urlunsplit(("https", host, parsed.path, parsed.query, ""))


def resolve(asset_id, sig, ua):
    asset = store.asset(asset_id)
    if not asset or asset["state"] != "active":
        raise CloudError("播放引用不存在或已撤销", 404)
    if not hmac.compare_digest(str(sig), strm.signature(asset_id, asset["nonce"])):
        raise CloudError("播放签名无效", 403)
    account = store.account(asset["account_id"])
    if not account or account["uid"] != asset["payload"]["uid"]:
        raise CloudError("源账号身份不一致", 409)
    if len(ua) > 512 or any(ord(c) < 32 or ord(c) > 126 for c in ua):
        raise CloudError("播放器 UA 无效")
    ua = ua or "StrimKeep-Player"
    cache_key = (account["id"], account["revision"], asset["file_id"], ua)
    gate = _GATES[hash(cache_key) % len(_GATES)]
    with gate:
        with _LOCK:
            found = _CACHE.get(cache_key)
            if found and found["expires"] > time.time():
                return found
        provider = for_account(account["id"])
        current = provider.info(asset["file_id"])
        source = asset["payload"]["source"]
        if (
            current["is_dir"]
            or current["size"] != source["size"]
            or current["sha1"] != source["sha1"]
        ):
            raise CloudError("源文件身份变化，请同步工作台后再播放", 409)
        link = provider.download(current["id"], current["pickcode"], ua)
        link["url"] = safe_url(link["url"])
        link["headers"] = {str(k): str(v) for k, v in link["headers"].items()}
        params = parse_qs(urlsplit(link["url"]).query)
        try:
            expiry = min(
                time.time() + 120,
                float((params.get("t") or [time.time() + 120])[0]) - 20,
            )
        except (ValueError, TypeError):
            expiry = time.time() + 60
        link["expires"] = max(time.time(), expiry)
        link["requires_proxy"] = (
            any(k.lower() == "cookie" and v for k, v in link["headers"].items())
            or (params.get("f") or [""])[0] == "3"
        )
        with _LOCK:
            if len(_CACHE) > 256:
                _CACHE.clear()
            _CACHE[cache_key] = link
        return link


async def serve(request, asset_id, sig):
    value = await asyncio.to_thread(
        resolve, asset_id, sig, request.headers.get("user-agent", "")
    )
    if not value["requires_proxy"]:
        return RedirectResponse(
            value["url"],
            status_code=302,
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )
    # Only verified provider CDN URLs can receive its temporary download headers.
    parsed = urlsplit(value["url"])
    addresses = await asyncio.to_thread(
        socket.getaddrinfo, parsed.hostname, 443, type=socket.SOCK_STREAM
    )
    if not addresses or any(
        not ipaddress.ip_address(x[4][0]).is_global for x in addresses
    ):
        raise CloudError("CDN 地址解析异常，已停止代理", 502)
    headers = {
        k: v
        for k, v in value["headers"].items()
        if k.lower() in ("user-agent", "cookie", "referer")
    }
    headers["User-Agent"] = request.headers.get("user-agent") or "StrimKeep-Player"
    headers["Accept-Encoding"] = "identity"
    for key in ("range", "if-range"):
        if key in request.headers:
            headers[key] = request.headers[key]
    client = httpx.AsyncClient(
        follow_redirects=False, trust_env=False, timeout=httpx.Timeout(15, read=60)
    )
    try:
        response = await client.send(
            client.build_request(request.method, value["url"], headers=headers),
            stream=True,
        )
    except Exception:
        await client.aclose()
        raise CloudError("CDN 连接失败，请重新播放", 502) from None
    if response.status_code not in (200, 206, 416):
        await response.aclose()
        await client.aclose()
        raise CloudError("CDN 拒绝播放请求，请重新播放", 502)
    allowed = {
        "content-type",
        "content-length",
        "content-range",
        "accept-ranges",
        "etag",
        "last-modified",
    }
    output = {k: v for k, v in response.headers.items() if k.lower() in allowed}
    output["Cache-Control"] = "no-store"

    async def relay():
        try:
            async for block in response.aiter_raw():
                yield block
        finally:
            await response.aclose()
            await client.aclose()

    return StreamingResponse(relay(), status_code=response.status_code, headers=output)
