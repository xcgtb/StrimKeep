"""Authenticated Emby passthrough and exact, user-scoped source interception."""

import asyncio
import hashlib
import json
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit, parse_qs, urlunsplit
import httpx
from fastapi import FastAPI, Request, WebSocket, HTTPException
from fastapi.responses import Response, StreamingResponse, RedirectResponse
from app.cloud import settings, store, strm

proxy_app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
_MAP = {}
_LOCK = threading.RLock()
_SERVER = None
_THREAD = None
_ERROR = ""
REQUEST_HEADERS = {
    "authorization",
    "x-emby-token",
    "x-mediabrowser-token",
    "x-emby-authorization",
    "user-agent",
    "accept",
    "accept-language",
    "content-type",
    "range",
    "if-range",
    "if-none-match",
    "if-modified-since",
    "origin",
}
HOP = {
    "connection",
    "keep-alive",
    "transfer-encoding",
    "upgrade",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "host",
}


def upstream():
    from app import config

    value = str(config.load_config()["emby_host"]).rstrip("/")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise HTTPException(503, "Emby 地址未正确配置")
    return value


def owner(request, base):
    tokens = [
        request.headers.get(k, "")
        for k in (
            "authorization",
            "x-emby-token",
            "x-mediabrowser-token",
            "x-emby-authorization",
        )
    ]
    tokens.extend(
        v
        for k, v in request.query_params.multi_items()
        if k.lower() in ("api_key", "apikey", "access_token")
    )
    if not any(tokens):
        return None
    return hashlib.sha256((base + "\0" + "\0".join(tokens)).encode()).hexdigest()


def asset_for_path(path):
    parsed = urlsplit(path)
    cfg = settings.get()
    if parsed.scheme in ("http", "https"):
        public = urlsplit(cfg["public_url"])
        if parsed.netloc != public.netloc:
            return None
        match = re.fullmatch(
            re.escape(public.path.rstrip("/")) + r"/api/cloud/play/([a-f0-9]{32})",
            parsed.path,
        )
        if not match:
            return None
        asset = store.asset(match[1])
        sig = (parse_qs(parsed.query).get("sig") or [""])[0]
        if not asset or not __import__("hmac").compare_digest(
            sig, strm.signature(asset["id"], asset["nonce"])
        ):
            return None
    else:
        mapped = None
        base = cfg["emby_output_root"].rstrip("/")
        if base and (path == base or path.startswith(base + "/")):
            mapped = str(Path(cfg["output_root"]) / Path(path).relative_to(base))
        else:
            from app import engine

            converted = engine.emby_path_to_container(path)
            if converted is not None:
                mapped = str(converted)
        if not mapped:
            return None
        with store.db() as con:
            row = con.execute(
                "SELECT id FROM cloud_assets WHERE local_path=?", (mapped,)
            ).fetchone()
        asset = store.asset(row[0]) if row else None
    return asset if asset and asset["state"] == "active" else None


def cache_sources(identity, item_id, payload):
    if not identity or not isinstance(payload, dict):
        return
    sources = payload.get("MediaSources")
    if not isinstance(sources, list):
        return
    with _LOCK:
        for key in list(_MAP):
            if key[:2] == (identity, item_id) or _MAP[key][1] < time.time():
                _MAP.pop(key, None)
    for source in sources:
        if not isinstance(source, dict) or not source.get("Id"):
            continue
        asset = asset_for_path(str(source.get("Path") or ""))
        if asset:
            with _LOCK:
                _MAP[(identity, item_id, str(source["Id"]))] = (
                    asset["id"],
                    time.time() + 600,
                )
                if len(_MAP) > 1000:
                    _MAP.pop(next(iter(_MAP)))


def selected(request, identity, path):
    match = re.fullmatch(
        r"/(?:emby/)?Videos/([A-Za-z0-9_-]+)/stream(?:\.(\w+))?", path, re.I
    )
    query = {k.lower(): v for k, v in request.query_params.items()}
    if (
        not match
        or not identity
        or query.get("static", "").lower() != "true"
        or match[2] in ("m3u8", "ts")
    ):
        return None
    if query.get("subtitlemethod", "").lower() in ("encode", "burnin"):
        return None
    requested = query.get("mediasourceid")
    with _LOCK:
        found = [
            v[0]
            for k, v in _MAP.items()
            if k[:2] == (identity, match[1])
            and v[1] > time.time()
            and (not requested or k[2] == requested)
        ]
    return found[0] if len(found) == 1 else None


@proxy_app.api_route(
    "/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
)
async def proxy(request: Request, path: str):
    if not settings.get()["proxy_enabled"]:
        raise HTTPException(503, "Emby 反代未启用")
    base = upstream()
    raw = request.scope.get("raw_path", b"/").decode("ascii", "strict")
    if raw.startswith("//") or "\\" in raw:
        raise HTTPException(400, "请求路径无效")
    target = (
        base
        + "/"
        + raw.lstrip("/")
        + ("?" + request.url.query if request.url.query else "")
    )
    headers = {k: v for k, v in request.headers.items() if k.lower() in REQUEST_HEADERS}
    headers["Accept-Encoding"] = "identity"
    client = httpx.AsyncClient(
        follow_redirects=False, trust_env=False, timeout=httpx.Timeout(15, read=60)
    )
    try:
        response = await client.send(
            client.build_request(
                request.method,
                target,
                headers=headers,
                content=(
                    request.stream() if request.method not in ("GET", "HEAD") else None
                ),
            ),
            stream=True,
        )
    except Exception:
        await client.aclose()
        raise HTTPException(502, "Emby 连接失败") from None
    output = {
        k: v
        for k, v in response.headers.items()
        if k.lower() not in HOP and k.lower() != "set-cookie"
    }
    if output.get("location", "").startswith(base + "/"):
        output["location"] = output["location"][len(base) :]
    identity = owner(request, base)
    # Upstream validates this *current* stream request before any signed link is disclosed.
    asset_id = (
        selected(request, identity, raw) if response.status_code in (200, 206) else None
    )
    if asset_id:
        asset = store.asset(asset_id)
        if asset and asset["state"] == "active":
            await response.aclose()
            await client.aclose()
            return RedirectResponse(
                strm.link(asset_id, asset["nonce"]),
                status_code=302,
                headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
            )
    match = re.fullmatch(r"/(?:emby/)?Items/([A-Za-z0-9_-]+)/PlaybackInfo/?", raw, re.I)
    if (
        match
        and response.status_code == 200
        and "json" in response.headers.get("content-type", "")
    ):
        try:
            body = bytearray()
            async for block in response.aiter_raw():
                body.extend(block)
                if len(body) > 2 * 1024 * 1024:
                    raise HTTPException(502, "播放信息响应过大")
            try:
                cache_sources(identity, match[1], json.loads(body))
            except (ValueError, KeyError, TypeError):
                pass
            output["content-length"] = str(len(body))
            return Response(bytes(body), status_code=200, headers=output)
        finally:
            await response.aclose()
            await client.aclose()

    async def relay():
        try:
            async for block in response.aiter_raw():
                yield block
        finally:
            await response.aclose()
            await client.aclose()

    return StreamingResponse(relay(), status_code=response.status_code, headers=output)


@proxy_app.websocket("/{path:path}")
async def websocket_proxy(socket: WebSocket, path: str):
    from websockets.asyncio.client import connect

    if not settings.get()["proxy_enabled"]:
        await socket.close(code=1008)
        return
    base = upstream()
    parsed = urlsplit(base)
    target = urlunsplit(
        (
            "wss" if parsed.scheme == "https" else "ws",
            parsed.netloc,
            parsed.path.rstrip("/") + "/" + path.lstrip("/"),
            socket.url.query,
            "",
        )
    )
    headers = {k: v for k, v in socket.headers.items() if k.lower() in REQUEST_HEADERS}
    try:
        async with connect(
            target, additional_headers=headers, open_timeout=15, proxy=None
        ) as remote:
            await socket.accept()

            async def upload():
                while True:
                    event = await socket.receive()
                    if event["type"] == "websocket.disconnect":
                        return
                    await remote.send(
                        event.get("bytes")
                        if event.get("bytes") is not None
                        else event.get("text", "")
                    )

            async def download():
                async for value in remote:
                    if isinstance(value, bytes):
                        await socket.send_bytes(value)
                    else:
                        await socket.send_text(value)

            tasks = [asyncio.create_task(upload()), asyncio.create_task(download())]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    except Exception:
        try:
            await socket.close(code=1011)
        except Exception:
            pass


def status():
    return {
        "running": bool(_SERVER and _SERVER.started and _THREAD and _THREAD.is_alive()),
        "port": settings.get()["proxy_port"],
        "error": _ERROR,
    }


def start():
    global _SERVER, _THREAD, _ERROR
    if not settings.get()["proxy_enabled"]:
        return
    if _THREAD and _THREAD.is_alive():
        return
    import uvicorn

    _SERVER = uvicorn.Server(
        uvicorn.Config(
            proxy_app,
            host="0.0.0.0",
            port=settings.get()["proxy_port"],
            log_level="warning",
            access_log=False,
        )
    )

    def run():
        global _ERROR
        try:
            _SERVER.run()
        except BaseException:
            _ERROR = "反代启动失败，请检查端口占用"

    _ERROR = ""
    _THREAD = threading.Thread(target=run, name="cloud-emby-proxy", daemon=True)
    _THREAD.start()


def stop():
    global _SERVER, _THREAD
    if _SERVER:
        _SERVER.should_exit = True
    if _THREAD:
        _THREAD.join(3)
    _SERVER = None
    _THREAD = None
