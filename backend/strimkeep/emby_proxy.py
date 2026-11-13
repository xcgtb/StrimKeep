"""Emby 反代：把 115 直链交给播放器（与「Emby 与飞牛反代」页面的行为一致）。

上游的快不来自 STRM 里写的地址，而来自反代：客户端连反代端口 → 反代带 API Key
转发 Emby 接口 → 在 PlaybackInfo 的返回体里补上 ``MediaSources[*].DirectStreamUrl``，
值是预先解析好的 115 CDN 直链。（2026-11-09 黑盒比对：同一部片直连上游 Emby 8096 与
经反代 8097，两份响应逐字段对照，结构差异只有这一个字段，Path / Protocol /
SupportsDirectPlay 一字未改。）客户端于是直接向 CDN 取流：视频不经过 Emby、不经过
反代、也不经过 frp 隧道。本模块只转发 API 流量（KB 级），视频字节一个都不代理。

形态对齐上游：**可以有多台反代服务器**，每台一条规则（名称 / 上游 / API Key / 端口 /
开关），各自独立监听、独立启停、独立播放记录；上游与 Key 留空即沿用「Emby 服务连接」。
"""
import asyncio
import json
import os
import re
import socket
import time
import uuid
from urllib.parse import urlsplit

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocketDisconnect, WebSocketState
from websockets.asyncio.client import connect as websocket_connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake

from .cloud115 import CloudError, playback_request
from .playaddr import ua_label

DEFAULT_PORT = 9001
STORAGE_KEY = 'emby_proxy_servers'
PLAYS_TABLE = 'emby_proxy_plays'
# 逐跳首部与由 httpx 重新生成的首部，转发时必须剔除。
_HOP_BY_HOP = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
               'te', 'trailer', 'transfer-encoding', 'upgrade', 'host',
               'content-length', 'accept-encoding'}
# 播放信息接口：客户端据此决定「直接连直链」还是「让服务端转发」。
_PLAYBACK = re.compile(r'/(?:Items|items)/[^/]+/PlaybackInfo/?$')
_SHARE = re.compile(r'^/play115/share/(?P<sid>[0-9A-Za-z]+)(?:/|$)')
# 视频/音频字节流：客户端走这些路径时，反代退回 CDN 直链，而不是替它搬字节。
# 不这么做的话，播放链路是 客户端 → VPS(frp) → NAS 反代 → Emby → 115，再原路返回，
# 实测只有 2.4 MB/s（68 Mbps 的片必然卡），直连 CDN 是 12-19 MB/s。
_STREAM = re.compile(r'^/emby/(?:videos|audio)/(?P<id>[0-9A-Za-z]{1,64})/'
                     r'(?:stream(?:\.[0-9A-Za-z]{1,8})?|original(?:\.[0-9A-Za-z]{1,8})?)$', re.I)
_STREAM_FILE = re.compile(r'^/emby/Items/(?P<id>[0-9A-Za-z]{1,64})/File(?:/[^/]*)?$', re.I)
STREAM_PATH_TTL = 600.0
# 转码/HLS：需要 Emby 现场编码，字节绕不开隧道，单独记账提示。
_TRANSCODE = re.compile(r'/(?:master\.m3u8|\.ts$|hls/|TranscodingUrl)', re.I)


def play_identity(path: str):
    """从播放地址里取 115 身份：``('pickcode', x)`` / ``('share', sid)`` / ``None``。

    只认本项目生成的两类播放地址；其余地址一律不碰，交给客户端照常走 Emby 转发。
    """
    try:
        parts = urlsplit(str(path or ''))
    except ValueError:
        return None
    if parts.scheme not in ('http', 'https') or not parts.hostname:
        return None
    segments = [s for s in parts.path.split('/') if s]
    if len(segments) < 2 or segments[0] != 'play115':
        return None
    if segments[1] == 'share':
        match = _SHARE.match(parts.path)
        return ('share', match.group('sid')) if match else None
    return ('pickcode', segments[1])


RESOLVE_RETRY_DELAY = 0.3
RESOLVE_TOTAL_TIMEOUT = 12.0


async def resolve_direct(identity, user_agent, cloud, store, retries=1):
    """解析身份 → ``(直链, 原因码)``；成功时原因码为空。

    失败就直接放弃的代价很大：播放器会退回走隧道（现场实测 4 MB/s 对 20 MB/s），
    表现为"公网播放又卡了"。115 的分享/文件链接偶尔瞬时失败（限流、连接抖动），
    所以这里重试一次，并把原因码带出去落库——不然只知道"没注入"，不知道为什么。
    """
    if not identity:
        return None, 'no_identity'
    kind, value = identity
    token = playback_request.set(True)   # 走与 /play115 相同的按 (账号, 文件, UA) 缓存
    reason = 'unknown'
    try:
        for attempt in range(retries + 1):
            try:
                async with asyncio.timeout(20):
                    if kind == 'pickcode':
                        url, _ = await cloud.url(value, user_agent)
                        return url, ''
                    row = store.db.execute('SELECT * FROM share_strms WHERE id=?',
                                           (value,)).fetchone()
                    if not row:
                        return None, 'share_missing'
                    if row['revoked']:
                        return None, 'share_revoked'
                    url, _ = await cloud.share_url_download(
                        row['file_id'], row['share_code'], row['receive_code'], user_agent)
                    return url, ''
            except TimeoutError:
                reason = 'timeout'
            except CloudError as exc:
                code = str(exc.code or 'CLOUD_UNAVAILABLE')
                reason = 'cloud:' + (code if re.fullmatch(r'[A-Z0-9_]{1,48}', code) else 'CLOUD_UNAVAILABLE')
                if code in ('LOGIN_REQUIRED', 'LOGIN_INVALID', 'RATE_LIMITED', 'SHARE_NOT_FOUND', 'FILE_NOT_FOUND'):
                    return None, reason
            except KeyError:
                reason = 'cloud:INVALID_SHARE_DATA'
            if attempt < retries:
                await asyncio.sleep(RESOLVE_RETRY_DELAY)
        return None, reason
    finally:
        playback_request.reset(token)


async def direct_link(identity, user_agent, cloud, store):
    """兼容入口：只要直链，失败返回 None。"""
    url, _ = await resolve_direct(identity, user_agent, cloud, store)
    return url


async def inject_direct_links(payload, user_agent, cloud, store, detail=False):
    """并发解析不同媒体源，仅补 DirectStreamUrl；超时保留原有响应。"""
    sources = payload.get('MediaSources') if isinstance(payload, dict) else None
    if not isinstance(sources, list) or not sources:
        return (0, ['no_sources']) if detail else 0
    gate = asyncio.Semaphore(4)
    pending = {}
    reasons = []
    for source in sources:
        if not isinstance(source, dict) or source.get('DirectStreamUrl'):
            continue
        identity = play_identity(source.get('Path'))
        if not identity:
            reasons.append('no_identity')
            continue
        pending.setdefault(identity, []).append(source)

    async def resolve(identity):
        async with gate:
            try:
                return await resolve_direct(identity, user_agent, cloud, store)
            except Exception as exc:
                return None, 'exception:%s' % type(exc).__name__

    tasks = {asyncio.create_task(resolve(identity)): values for identity, values in pending.items()}
    if not tasks:
        return (0, reasons) if detail else 0
    try:
        done, remaining = await asyncio.wait(tasks, timeout=RESOLVE_TOTAL_TIMEOUT)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    for task in remaining:
        task.cancel()
    if remaining:
        await asyncio.gather(*remaining, return_exceptions=True)
        reasons.extend('timeout' for _ in remaining)
    filled = 0
    for task in done:
        url, reason = task.result()
        if url:
            for source in tasks[task]:
                source['DirectStreamUrl'] = url
                filled += 1
        elif reason:
            reasons.append(reason)
    return (filled, reasons) if detail else filled


def _quieten(task):
    """后台监听任务结束后把异常取走：uvicorn 用 SystemExit 表达启动失败，不能让它冒到循环里。"""
    try:
        task.exception()
    except BaseException:
        pass


# 老版本只有一套单台配置；这些键现在只作为**迁移来源**读一次，迁移完即删。
LEGACY_KEYS = ('emby_proxy_enabled', 'emby_proxy_port', 'emby_proxy_upstream', 'emby_proxy_api_key')


class Config:
    """一条反代规则的生效配置。

    上游与 API Key 留空即回落到「Emby 服务连接」（`emby_url` / `emby_key`）；
    环境变量只作用于默认规则，便于容器化部署时不落库。

    注意：这里**不读** `emby_proxy_*` 那套老键。它们曾和规则字段并存，导致"设置里
    改了没效果"（页面写规则、旧键还在被读）。现在只在迁移时作为来源读一次。
    """

    def __init__(self, store, rule=None):
        rule = rule or {}
        default = not rule.get('id')

        def pick(field, env, fallback=''):
            value = rule.get(field)
            if value not in (None, ''):
                return value
            if default and os.environ.get(env):
                return os.environ[env]
            return store.get(fallback, '') if fallback else ''

        self.port = int(pick('port', 'STRIMKEEP_EMBY_PROXY_PORT', '') or DEFAULT_PORT)
        self.upstream = str(pick('upstream', 'STRIMKEEP_EMBY_UPSTREAM', 'emby_url') or '').rstrip('/')
        self.api_key = str(pick('api_key', 'STRIMKEEP_EMBY_KEY', 'emby_key') or '')
        enabled = rule.get('enabled')
        if enabled is None:
            flag = str(os.environ.get('STRIMKEEP_EMBY_PROXY', '')).strip().lower() if default else ''
            enabled = flag in ('1', 'true', 'yes', 'on') if flag else bool(self.upstream)
        self.enabled = bool(enabled)
        self.ready = self.enabled and bool(self.upstream)


class ProxyServer:
    """一台反代服务器：独立端口、独立连接池、独立开关。"""

    def __init__(self, rule, store, cloud, metrics=None, on_play=None):
        self.rule = rule
        self.store = store
        self.cloud = cloud
        self.metrics = metrics
        self.on_play = on_play
        self.server = None
        self.task = None
        self.client = None
        self.logged = 0.0
        # 剧集/影片 ID → 上游 Path：播放器每几秒就发一个 Range，不能每次都回源问
        self.paths = {}

    @property
    def id(self):
        return self.rule.get('id', '')

    def config(self):
        return Config(self.store, self.rule)

    def running(self):
        return bool(self.server and self.server.started)

    def event(self, message, throttle=60.0):
        """写操作记录；同类失败按节流合并，避免播放器重连刷屏。"""
        now = time.monotonic()
        if now - self.logged < throttle:
            return
        self.logged = now
        try:
            self.store.event('play', message)
        except Exception:
            pass

    def _headers(self, request, config, inject_key=False):
        headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP}
        headers['accept-encoding'] = 'identity'   # 注入要读 JSON，别让上游压缩
        # 公网请求只转发客户端自己的凭据，不用服务器管理员 Key 提权。
        return headers

    def _query(self, request, config, inject_key=False):
        return request.url.query

    def _local_strm(self, emby_path):
        """Emby 给的 /media/... 换成容器里的 /strm/...；越界或不像 STRM 一律拒绝。"""
        if not emby_path or not emby_path.lower().endswith('.strm'):
            return ''
        config = self.store.get('console_config', {}) or {}
        emby_root = str(config.get('emby_strm_path') or '/media').rstrip('/')
        local_root = str(os.environ.get('STRIMKEEP_STRM_DIR') or '/strm').rstrip('/')
        if not emby_root or not emby_path.startswith(emby_root + '/'):
            return ''
        relative = emby_path[len(emby_root) + 1:]
        if not relative or '..' in relative.split('/'):
            return ''
        return os.path.join(local_root, relative)

    def _read_strm(self, path):
        """读 STRM 内容（只读开头几 KB）：里面就是 play115 地址。"""
        try:
            if os.path.getsize(path) > 64 * 1024:
                return ''
            with open(path, 'rb') as handle:
                return handle.read(4096).decode('utf-8', 'replace').strip().splitlines()[0].strip()
        except (OSError, IndexError):
            return ''

    def _ensure_client(self):
        if self.client is None:
            self.client = httpx.AsyncClient(
                timeout=httpx.Timeout(connect=5.0, read=60.0, write=60.0, pool=30.0),
                limits=httpx.Limits(max_connections=200, max_keepalive_connections=50))
        return self.client

    async def _upstream_path(self, item, request, config):
        """问上游 Emby 这部片的 Path。

        用 PlaybackInfo 而不是 ``/emby/Items/{id}``：后者在这个 Emby 版本上直接 404
        （实测返回 "The file '/emby/Items/14176' could not be found."），而 PlaybackInfo
        是播放器本来就会调的接口，返回的 ``MediaSources[0].Path`` 就是 STRM 里那条 play115 地址。
        """
        cached = self.paths.get(item)
        now = time.monotonic()
        if cached and now - cached[1] < STREAM_PATH_TTL:
            return cached[0]
        headers = self._headers(request, config)
        headers['content-type'] = 'application/json'
        key = request.query_params.get('api_key') or config.api_key
        params = {'api_key': key} if key else {}
        body = json.dumps({'EnableDirectPlay': True, 'EnableDirectStream': True})
        path = ''
        try:
            response = await self._ensure_client().post(
                config.upstream + '/emby/Items/' + item + '/PlaybackInfo',
                params=params, headers=headers, content=body, timeout=8.0)
            payload = response.json()
            sources = payload.get('MediaSources') if isinstance(payload, dict) else None
            if isinstance(sources, list) and sources and isinstance(sources[0], dict):
                path = str(sources[0].get('Path') or '')
        except (httpx.HTTPError, ValueError):
            path = ''
        self.paths[item] = (path, now)
        return path

    async def _stream_redirect(self, request, config):
        """视频/音频流 → 302 到 115 直链；解析不出直链就返回 None（照旧转发）。"""
        match = _STREAM.match(request.url.path) or _STREAM_FILE.match(request.url.path)
        if not match:
            if _TRANSCODE.search(request.url.path):
                # 转码/HLS 分片只能由 Emby 现场产出，字节必然经过隧道。
                # 这不是 302 能解决的：客户端要么换直连，要么关掉转码。
                self.event('转码/HLS 请求（%s）：视频将占用隧道，请改为直连播放'
                           % request.url.path[:80], throttle=30.0)
            return None
        path = await self._upstream_path(match.group('id'), request, config)
        identity = play_identity(path) if path else None
        if not identity and path:
            # Emby 的 Path 是库里的 .strm 文件（/media/...），真正的 play115 地址在文件内容里。
            local = self._local_strm(path)
            if local:
                identity = play_identity(self._read_strm(local))
        ua = request.headers.get('user-agent', '')
        if not identity:
            # 不是我们的内容（本地/其它来源）：只能转发，但绝不占用视频通道的是我们的片。
            self.event('视频流不是 115 内容（Path=%s），走转发' % str(path)[:120], throttle=30.0)
            return None
        url, reason = await resolve_direct(identity, ua, self.cloud, self.store)
        if not url:
            # 根因收口：frp 只跑 API/海报。我们的片取不到直链时**绝不退回转发**，
            # 否则视频又会塞回隧道（实测 2.4 MB/s），表现就是"又卡了"。
            self.event('视频流直链解析失败（%s），拒绝经隧道转发' % reason, throttle=10.0)
            return JSONResponse(
                {'error': {'code': 'DIRECT_LINK_UNAVAILABLE',
                           'message': '取 115 直链失败（%s）。为避免视频经 frp 隧道转发，本次已拒绝。'
                                      '请检查 115 登录/分享状态后重试。' % reason}}, 503)
        self.event('视频流 302 直连 115（%s）' % ua_label(ua), throttle=10.0)
        return RedirectResponse(url, status_code=302)


    async def handle(self, request):
        config = request.app.state.proxy_config()
        if not config.ready:
            return JSONResponse({'error': {'code': 'PROXY_DISABLED',
                                           'message': '反代未启用：请先开启这台服务器。'}}, 503)
        if request.url.path in ('/', '/health'):
            # 手机能不能连上这个端口，用浏览器打开这里就能判断，不必翻日志。
            return JSONResponse({'service': 'strimkeep-emby-proxy', 'name': self.rule.get('name', ''),
                                 'enabled': True, 'upstream': urlsplit(config.upstream).netloc})
        if request.method in ('GET', 'HEAD'):
            # 视频流先尝试 302 到 115 直链：成功则字节完全不经过反代与 frp。
            redirect = await self._stream_redirect(request, config)
            if redirect is not None:
                return redirect
        rewrite = request.method in ('GET', 'POST') and bool(_PLAYBACK.search(request.url.path))
        # 上游 Emby 对分块（chunked）请求体解析不可靠：实测 AuthenticateByName 经反代返回
        # 400 `Value cannot be null. (Parameter 'name')`，直连却是正常的 401。所以 API 请求体
        # 一律读全带上 Content-Length 再转发；只有超过 16MB 的大上传才退回流式。
        size = request.headers.get('content-length', '')
        if request.method in ('GET', 'HEAD') or (size.isdigit() and int(size) > 16 * 1024 * 1024):
            body = request.stream()
        else:
            body = await request.body()
        url = config.upstream + request.url.path
        if request.url.query:
            url += '?' + self._query(request, config, rewrite)
        started = time.monotonic()
        client = self._ensure_client()
        try:
            upstream = await client.send(client.build_request(
                request.method, url, headers=self._headers(request, config, rewrite), content=body),
                stream=True)
        except httpx.HTTPError as exc:
            self.event('反代转发失败：%s（%s）' % (type(exc).__name__, self.rule.get('name', '')))
            return JSONResponse({'error': {'code': 'UPSTREAM_UNREACHABLE',
                                           'message': '反代无法连接 Emby，请检查 Emby 服务地址。'}}, 502)
        if rewrite and upstream.status_code == 200:
            return await self._rewrite(request, upstream, started)
        headers = {k: v for k, v in upstream.headers.items() if k.lower() not in _HOP_BY_HOP}

        async def relay():
            # 客户端连接由实例持有（见 start/stop），这里只关掉上游这条响应流。
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            finally:
                await upstream.aclose()

        return StreamingResponse(relay(), status_code=upstream.status_code, headers=headers)

    async def _rewrite(self, request, upstream, started):
        try:
            raw = await upstream.aread()
            content_type = upstream.headers.get('content-type', 'application/json')
        finally:
            await upstream.aclose()
        ua = request.headers.get('user-agent', '')
        try:
            payload = json.loads(raw)
        except ValueError:
            return Response(raw, status_code=200, media_type=content_type,
                            headers={'Cache-Control': 'private, no-store'})
        filled = 0
        outcome = 'error'
        reasons = []
        try:
            filled, reasons = await inject_direct_links(payload, ua, self.cloud, self.store,
                                                        detail=True)
            if filled:
                outcome = 'ok'
                self.event('反代注入直链：%d 条（%s）' % (filled, ua_label(ua)), throttle=5.0)
            elif not reasons:
                # 没有可注入的源（不是我们的路径，或上游已带 DirectStreamUrl）：
                # 这不是失败，之前一律记成 error，播放记录里全是假报警。
                outcome = 'empty'
        except Exception as exc:
            reasons = ['exception:%s' % type(exc).__name__]
            self.event('反代注入直链失败：%s（%s）' % (type(exc).__name__, ua_label(ua)))
        finally:
            if self.metrics is not None:
                try:
                    self.metrics.record_play(outcome, (time.monotonic() - started) * 1000)
                except Exception:
                    pass
            if self.on_play is not None:
                try:
                    self.on_play(self, request, filled, outcome, ','.join(reasons)[:120])
                except Exception:
                    pass
        # 内容已重新序列化，长度与压缩信息不能照抄上游。
        return Response(json.dumps(payload, ensure_ascii=False).encode(),
                        status_code=200, media_type='application/json',
                        headers={'Cache-Control': 'private, no-store'})


    async def handle_websocket(self, socket):
        """Forward Emby's websocket with the caller's credentials and subprotocols."""
        config = socket.app.state.proxy_config()
        if not config.ready or socket.url.path.rstrip('/').lower().split('/')[-1] not in ('socket', 'embywebsocket'):
            await socket.close(code=1008)
            return
        try:
            parts = urlsplit(config.upstream + socket.url.path)
            if parts.scheme not in ('http', 'https') or not parts.netloc:
                await socket.close(code=1008)
                return
            scheme = 'wss' if parts.scheme == 'https' else 'ws'
            upstream_url = '%s://%s%s' % (scheme, parts.netloc, parts.path)
            if socket.url.query:
                upstream_url += '?' + socket.url.query
            excluded = _HOP_BY_HOP | {'origin', 'user-agent', 'sec-websocket-key',
                                      'sec-websocket-version', 'sec-websocket-extensions',
                                      'sec-websocket-protocol'}
            headers = [(k.decode('latin-1'), v.decode('latin-1')) for k,v in socket.scope['headers']
                       if k.decode('latin-1').lower() not in excluded]
            async with websocket_connect(upstream_url, additional_headers=headers,
                                         origin=socket.headers.get('origin'),
                                         user_agent_header=socket.headers.get('user-agent'),
                                         subprotocols=socket.scope.get('subprotocols') or None,
                                         open_timeout=5, close_timeout=5, max_size=16*1024*1024,
                                         proxy=None) as upstream:
                await socket.accept(subprotocol=upstream.subprotocol)

                async def to_upstream():
                    try:
                        while True:
                            event = await socket.receive()
                            if event['type'] == 'websocket.disconnect':
                                return
                            if event.get('text') is not None:
                                await upstream.send(event['text'])
                            elif event.get('bytes') is not None:
                                await upstream.send(event['bytes'])
                    except (WebSocketDisconnect, ConnectionClosed):
                        return

                async def to_client():
                    try:
                        async for message in upstream:
                            if isinstance(message, bytes):
                                await socket.send_bytes(message)
                            else:
                                await socket.send_text(message)
                    except (WebSocketDisconnect, ConnectionClosed):
                        return

                tasks = [asyncio.create_task(to_upstream()), asyncio.create_task(to_client())]
                try:
                    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
        except (OSError, InvalidHandshake, TimeoutError, ValueError):
            # Never log the URL: Emby clients may put access tokens in the query.
            if socket.application_state == WebSocketState.CONNECTING:
                await socket.close(code=1013)
        finally:
            if socket.application_state == WebSocketState.CONNECTED:
                try:
                    await socket.close()
                except (RuntimeError, WebSocketDisconnect):
                    pass


    def build(self):
        app = Starlette(routes=[WebSocketRoute('/{path:path}', self.handle_websocket),
                                Route('/{path:path}', self.handle,
                                      methods=['GET', 'POST', 'PUT', 'DELETE', 'HEAD', 'OPTIONS'])])
        app.state.owner = self
        app.state.proxy_config = self.config
        return app

    async def start(self):
        config = self.config()
        if not config.ready:
            return None
        # 端口自己先绑：uvicorn 在后台任务里绑失败会 sys.exit(3)，SystemExit 会从 Task 抛出
        # 把整个事件循环带崩（实测：整个应用卡在 startup）。先绑一次就不会走到那条路径。
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(('0.0.0.0', config.port))
            listener.listen(2048)
        except OSError as exc:
            listener.close()
            self.event('反代没起来：端口 %d 绑定失败（%s）——%s'
                       % (config.port, type(exc).__name__, self.rule.get('name', '')), throttle=5.0)
            return None
        self.server = uvicorn.Server(uvicorn.Config(self.build(), host='0.0.0.0', port=config.port,
                                                    log_level='warning', access_log=False))
        # 连接池由实例持有：请求作用域里建 client 会在响应体还没发完时就被关掉。
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=60.0, write=60.0, pool=30.0),
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=50))
        self.task = asyncio.create_task(self.server.serve(sockets=[listener]))
        self.task.add_done_callback(_quieten)
        for _ in range(40):
            await asyncio.sleep(0.05)
            if self.server.started:
                break
        if not self.server.started:
            self.event('反代没起来：端口 %d 未能开始监听（%s）'
                       % (config.port, self.rule.get('name', '')), throttle=5.0)
            await self.stop()
            return None
        self.event('反代已监听：0.0.0.0:%d → %s（%s）'
                   % (config.port, config.upstream, self.rule.get('name', '')), throttle=1.0)
        return self.server

    async def stop(self):
        if self.server is not None:
            self.server.should_exit = True
        if self.task is not None:
            try:
                await asyncio.wait_for(self.task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self.task.cancel()
            except BaseException:      # uvicorn 用 SystemExit 表达启动失败，不能外泄
                pass
        if self.client is not None:
            await self.client.aclose()
            self.client = None
        self.server = None
        self.task = None

    async def restart(self):
        """保存设置后立刻生效：先停旧监听，再按新配置起（端口、开关都随规则走）。"""
        await self.stop()
        return await self.start()


class EmbyProxyManager:
    """多台反代服务器的管理者：规则落库在 settings，运行态在内存。"""

    def __init__(self, store, cloud, metrics=None):
        self.store = store
        self.cloud = cloud
        self.metrics = metrics
        self.servers = {}   # rule_id -> ProxyServer
        self.ensure_table()

    # ---------- 存储 ----------

    def ensure_table(self):
        """播放记录表；老库没有就补上，失败不影响反代本身工作。"""
        try:
            with self.store.db:
                self.store.db.execute(
                    'CREATE TABLE IF NOT EXISTS %s (id INTEGER PRIMARY KEY AUTOINCREMENT, '
                    'rule_id TEXT, name TEXT, port INTEGER, target TEXT, user_agent TEXT, '
                    'filled INTEGER, outcome TEXT, at REAL, reason TEXT)' % PLAYS_TABLE)
                columns = {row[1] for row in self.store.db.execute(
                    'PRAGMA table_info(%s)' % PLAYS_TABLE)}
                if 'reason' not in columns:      # 老库补列：0.9.91 之前没有失败原因
                    self.store.db.execute('ALTER TABLE %s ADD COLUMN reason TEXT' % PLAYS_TABLE)
        except Exception:
            pass

    def rules(self):
        """当前规则列表；老版本只有单台配置时迁移成一条默认规则，并把老键删掉。"""
        rules = self.store.get(STORAGE_KEY) or []
        if not isinstance(rules, list):
            rules = []
        if not rules:
            rules = [self._migrate_legacy()]
            self.save_rules(rules)
        else:
            # 规则已经在库里：老键没有任何读取方了，直接清掉（迁移只在"没有规则"时才有意义）。
            self._forget_legacy()
        changed = False
        for index, rule in enumerate(rules):
            rule.setdefault('id', uuid.uuid4().hex[:12])
            rule.setdefault('name', 'Emby-%d' % (index + 1))
            rule.setdefault('upstream', '')
            rule.setdefault('api_key', '')
            rule.setdefault('port', DEFAULT_PORT)
            rule.setdefault('enabled', False)
            changed = changed or 'id' not in rules[index]
        return rules

    def _forget_legacy(self):
        """删掉老的单台配置键；没有可删的就什么都不做（幂等）。"""
        forget = getattr(self.store, 'forget', None)
        if not callable(forget):
            return []
        removed = forget(LEGACY_KEYS)
        if removed:
            self.store.event('emby-proxy', '已删除旧版单台反代遗留设置项：%s。'
                             % '、'.join(removed))
        return removed

    def _migrate_legacy(self):
        """把老的「单台配置」迁成一条规则：端口/开关照抄，上游与 Key 只在**确有差异**时才带过来。

        老键（`emby_proxy_*`）从这之后就不再有读取方，直接删掉——留着只会让人以为改它们有效。
        """
        port = self.store.get('emby_proxy_port') or DEFAULT_PORT
        enabled = bool(self.store.get('emby_proxy_enabled'))
        shared_url = str(self.store.get('emby_url') or '').rstrip('/')
        shared_key = str(self.store.get('emby_key') or '')
        legacy_url = str(self.store.get('emby_proxy_upstream') or '').rstrip('/')
        legacy_key = str(self.store.get('emby_proxy_api_key') or '')
        rule = {'id': uuid.uuid4().hex[:12], 'name': 'Emby-1',
                'upstream': legacy_url if legacy_url and legacy_url != shared_url else '',
                'api_key': legacy_key if legacy_key and legacy_key != shared_key else '',
                'port': int(port), 'enabled': enabled}
        self._forget_legacy()
        return rule

    def save_rules(self, rules):
        self.store.set_many({STORAGE_KEY: rules})

    def find(self, rule_id):
        for rule in self.rules():
            if rule['id'] == rule_id:
                return rule
        return None

    def status(self):
        """给设置页用：规则 + 是否在监听 + 端口/上游是否已配置（不外泄 Key）。"""
        result = []
        for rule in self.rules():
            server = self.servers.get(rule['id'])
            config = Config(self.store, rule)
            result.append({'id': rule['id'], 'name': rule['name'], 'port': config.port,
                           'enabled': bool(rule.get('enabled')), 'running': bool(server and server.running()),
                           'upstream': config.upstream,
                           'api_key_configured': bool(config.api_key)})
        return result


    # ---------- 运行态 ----------

    def _server_for(self, rule):
        server = self.servers.get(rule['id'])
        if server is None:
            server = ProxyServer(rule, self.store, self.cloud, self.metrics, self.record_play)
            self.servers[rule['id']] = server
        server.rule = rule
        return server

    async def apply_rule(self, rule):
        """让某条规则的运行态与配置一致：开启则监听（换端口就重启），关闭则停止。"""
        server = self._server_for(rule)
        config = Config(self.store, rule)
        if not config.ready:
            await server.stop()
            return server
        await server.restart()
        return server

    async def set_enabled(self, rule_id, enabled):
        rules = self.rules()
        for rule in rules:
            if rule['id'] == rule_id:
                rule['enabled'] = bool(enabled)
                self.save_rules(rules)
                return await self.apply_rule(rule)
        return None

    async def apply_all(self):
        """启动时按规则把该听的都听上；关闭的服务器不占端口。"""
        for rule in self.rules():
            await self.apply_rule(rule)
        enabled = [rule for rule in self.rules() if rule.get('enabled')]
        if enabled:
            self.store.event('system', 'Emby 反代已按规则启动：%d 台（%s）。'
                             % (len(enabled), '、'.join('%s:%s' % (rule['name'], rule['port'])
                                                        for rule in enabled)))
        return self.status()

    async def restart_all(self):
        """改了 Emby 服务连接（上游地址 / Key）后，所有在跑的服务器都换到新上游。"""
        for rule in self.rules():
            await self.apply_rule(rule)
        return self.status()

    async def stop_all(self):
        for server in list(self.servers.values()):
            await server.stop()
        self.servers.clear()

    # ---------- 增删 / 测试 / 播放记录 ----------

    def add_rule(self):
        rules = self.rules()
        used = {int(rule.get('port') or DEFAULT_PORT) for rule in rules}
        port = DEFAULT_PORT
        while port in used and port < 65000:
            port += 1
        rule = {'id': uuid.uuid4().hex[:12], 'name': 'Emby-%d' % (len(rules) + 1),
                'upstream': '', 'api_key': '', 'port': port, 'enabled': False}
        rules.append(rule)
        self.save_rules(rules)
        return rule

    async def delete_rule(self, rule_id):
        rules = [rule for rule in self.rules() if rule['id'] != rule_id]
        server = self.servers.pop(rule_id, None)
        if server is not None:
            await server.stop()
        self.save_rules(rules)
        return rules

    async def test_rule(self, rule_id):
        """照上游的「测试」：带 API Key 打一次上游 Emby，回报版本与延迟。"""
        rule = self.find(rule_id)
        if rule is None:
            return None
        config = Config(self.store, rule)
        if not config.upstream:
            return {'ok': False, 'message': '还没有配置 Emby 服务连接：请先在上面填写地址与 API Key。'}
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(connect=5.0, read=10.0,
                                                               write=10.0, pool=5.0)) as client:
                response = await client.get(config.upstream + '/emby/System/Info',
                                            headers={'X-Emby-Token': config.api_key} if config.api_key else {})
            elapsed = (time.monotonic() - started) * 1000
            data = response.json() if response.headers.get('content-type', '').startswith('application/json') else {}
            if response.status_code != 200:
                return {'ok': False, 'message': 'Emby 返回 HTTP %d' % response.status_code,
                        'latency_ms': round(elapsed)}
            return {'ok': True, 'latency_ms': round(elapsed),
                    'message': '连接正常：%s %s' % (data.get('ServerName', ''), data.get('Version', ''))}
        except (httpx.HTTPError, ValueError) as exc:
            return {'ok': False, 'message': '连不上 Emby：%s' % type(exc).__name__}

    def record_play(self, server, request, filled, outcome, reason=''):
        """播放记录（对应上游的「播放记录」）：只留必要字段，路径截断。"""
        config = Config(self.store, server.rule)
        try:
            with self.store.db:
                self.store.db.execute(
                    'INSERT INTO %s (rule_id,name,port,target,user_agent,filled,outcome,at,' 
                    'reason) VALUES (?,?,?,?,?,?,?,?,?)' % PLAYS_TABLE,
                    (server.id, server.rule.get('name', ''), config.port,
                     request.url.path[:300], ua_label(request.headers.get('user-agent', '')),
                     int(filled), outcome, time.time(), (reason or '')[:120]))
                self.store.db.execute(
                    'DELETE FROM %s WHERE id NOT IN (SELECT id FROM %s ORDER BY id DESC LIMIT 2000)'
                    % (PLAYS_TABLE, PLAYS_TABLE))
        except Exception:
            pass

    def plays(self, rule_id, limit=50):
        try:
            rows = self.store.db.execute(
                'SELECT * FROM %s WHERE rule_id=? ORDER BY id DESC LIMIT ?' % PLAYS_TABLE,
                (rule_id, max(1, min(int(limit), 200)))).fetchall()
        except Exception:
            return []
        return [dict(row) for row in rows]

