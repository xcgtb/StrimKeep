import asyncio
import hmac
import json
import os
import re
import secrets
import shutil
import sys
import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware
from pydantic import BaseModel, Field, ConfigDict

from . import __version__
from . import commands
from .cloud115 import Cloud115, CloudError, playback_request
from .login_clients import LOGIN_CLIENTS, CLIENT_LABELS
from .playaddr import play115_candidates, play_url, ua_label
from . import deep_delete
from .probe import probe
from .store import Store, digest
from .quality import (UpgradePolicy, public_policy, KEYS as RULE_KEYS, LABELS as RULE_LABELS,
                      CHOICES as RULE_CHOICES, OPTIONS as RULE_OPTIONS, SUGGESTED_ORDER)
from . import recognition_aliases
from .organizer import dumps
from .web_organizer import WebOrganizer
from .media import NeedsReview, PROBE_UA, classification, episode_label, naming
from .organize_options import OrganizeOptions, library_root, title_options
from .strm_options import effective as effective_strm
from .strm_options import share_effective_settings
from .phase5_api import install_phase5
from .metrics import Metrics, install_metrics, is_streaming
from .db_worker import DbWorker
from .emby_proxy import EmbyProxyManager
from .emby_proxy_api import install_emby_proxy
from .library_covers import install_library_covers
from .media_info_api import inject_loop, install_media_info
from .housekeeping import housekeeping_loop, install as install_housekeeping


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Login(Input):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=512)


class CloudThrottle(Input):
    """115 请求节奏：读接口慢一点只是慢，写接口太密会进风控。"""
    read_ms: int = Field(default=350, ge=0, le=5000)
    write_ms: int = Field(default=3000, ge=0, le=15000)
    burst: int = Field(default=15, ge=1, le=200)
    pause_ms: int = Field(default=30000, ge=0, le=600000)


class Settings(Input):
    cookie: str | None = Field(default=None, max_length=16000)
    tmdb_key: str | None = Field(default=None, max_length=1000)
    emby_url: str | None = Field(default=None, max_length=1000)
    emby_key: str | None = Field(default=None, max_length=1000)
    playback_base: str | None = Field(default=None, max_length=1000)
    cloud_throttle: CloudThrottle | None = None


class QRLogin(Input):
    app: str = Field(min_length=1, max_length=40)


class Source(Input):
    file_id: str = Field(pattern=r'^\d{1,24}$')


class OrganizeConfig(OrganizeOptions, Input):
    model_config = ConfigDict(extra="forbid")
    source_cids: list[str] = Field(default_factory=list, max_length=20)
    archive_cid: str = Field(default='', max_length=24)
    pending_cid: str = Field(default='', max_length=24)
    directory_labels: dict[str, str] = Field(default_factory=dict, max_length=30)
    retries: int = Field(default=1, ge=0, le=3)
    retry_interval: int = Field(default=2, ge=0, le=60)
    strm_delay: float = Field(default=1.5, ge=0, le=60)
    notify_emby: bool = True
    ffprobe_enabled: bool = True
    classify_enabled: bool = True
    wash_enabled: bool = True


UNRECOGNIZED_ITEM = (
    "state IN ('pending','review','skipped') "
    "AND step NOT IN ('人工忽略未识别','已人工忽略','已转手动匹配') "
    "AND (step LIKE '%未识别%' OR message LIKE '%未识别%')"
)


class DismissUnrecognized(Input):
    """人工确认未识别项：只改任务记录，不碰云端文件。"""
    item_ids: list[int] = Field(default_factory=list, max_length=200)


class ManualMatch(Input):
    """手动指定 TMDB 作品；整理仍走同一条归档/STRM 链路。"""
    tmdb_id: int = Field(ge=1, le=2_000_000_000)
    media_type: str = Field(pattern=r'^(movie|tv)$')
    season: int | None = Field(default=None, ge=0, le=1000)
    episode: int | None = Field(default=None, ge=1, le=100000)
    remember_alias: bool = False


def base_url(value: str, allow_path=False) -> str:
    p = urlsplit(value)
    try:
        p.port  # 访问即校验，端口非法会抛 ValueError
    except ValueError:
        raise HTTPException(422, '端口格式不正确。') from None
    if (p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password
            or p.query or p.fragment or any(c.isspace() for c in value)
            or (not allow_path and p.path not in ('', '/'))):
        raise HTTPException(422, '请填写有效的 HTTP/HTTPS 地址，播放基础地址只填写域名或 IP 和端口。')
    return value.rstrip('/')


def safe_name(name: str) -> str:
    name = re.sub(r'[\x00-\x1f\x7f/\\:*?"<>|]', '_', name)
    return name.strip(' .')[:100] or 'video'


def create_app(data_root=None, strm_root=None, cloud_factory=Cloud115):
    root = Path(data_root or os.environ.get('STRIMKEEP_DATA_DIR', '/data'))
    strms = Path(strm_root or os.environ.get('STRIMKEEP_STRM_DIR', '/strm'))
    store = Store(root, os.environ.get('ADMIN_USERNAME', ''), os.environ.get('ADMIN_PASSWORD', ''))
    strms.mkdir(parents=True, exist_ok=True)
    app = FastAPI(title='StrimKeep', version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store
    # 进程内计数（播放取链、HTTP 请求），/metrics 与总览共用。
    app.state.metrics = Metrics(store)
    app.state.db_worker = DbWorker()
    app.state.strms = strms.resolve()
    app.state.cloud = None
    app.state.probe_gate = asyncio.Semaphore(1)
    app.state.failed_logins = defaultdict(deque)
    app.state.play_limits = defaultdict(deque)
    app.state.play_logged = {}

    @asynccontextmanager
    async def lifespan(_):
        await app.state.db_worker.start()
        import fcntl
        instance_file = None
        if os.environ.get('STRIMKEEP_INLINE_WORKER') == '1':
            instance_file = (root / 'data/instance.lock').open('a')
            try:
                fcntl.flock(instance_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                instance_file.close()
                raise RuntimeError('只允许运行一个 StrimKeep 实例或 Worker。') from None
        if not shutil.which('ffprobe'):
            if instance_file is not None:
                instance_file.close()
            raise RuntimeError('FFprobe 未安装。')
        store.remove_retired_modules()
        # 深度删除已改为无状态：清掉旧版本遗留的内部删除登记，避免继续挡住清理/入库。
        with store.db:
            store.db.execute("DELETE FROM meta WHERE key='internal_deletes'")
            # 旧版「撤销」是软删除：现在只有保留/删除两种状态，历史 revoked 行直接清掉。
            store.db.execute("DELETE FROM share_strms WHERE revoked=1")
        from .library_name import migrate
        migrate(store,app.state.strms)
        # 0.8.30：库根一致性自检（配置 / 台账 / 磁盘三方对照，只报警不改文件）。
        from .library_roots import startup_check
        startup_check(store, app.state.strms)
        # ADMIN_PASSWORD only seeds the first database; changing it later in
        # Compose silently did nothing. Say so once instead of leaving people
        # believing the password was updated.
        if (os.environ.get('ADMIN_PASSWORD') and not store.meta('admin_env_password_notice')
                and not store.authenticate(os.environ.get('ADMIN_USERNAME', ''), os.environ['ADMIN_PASSWORD'])):
            store.event('system', '数据库已有管理账号：ADMIN_USERNAME / ADMIN_PASSWORD 只在首次创建时生效，本次启动未修改密码。')
            with store.db:
                store.db.execute("INSERT OR REPLACE INTO meta VALUES('admin_env_password_notice','1')")
        app.state.cloud = cloud_factory(store)
        from .network import ServiceHTTP
        app.state.http = ServiceHTTP(store)
        app.state.organizer = WebOrganizer(store, app.state.cloud, app.state.http, app.state.strms)
        # Upgrade only verified legacy share URLs while holding the instance
        # lock and before workers can publish or wash the same files.
        from .share_service import align_existing_share_urls
        align_existing_share_urls(store, app.state.strms)
        from .share_service import migrate_share_play_paths
        migrate_share_play_paths(store, app.state.strms)
        try:
            paths = json.loads(store.meta('share_url_refresh_pending') or '[]')
            connection = app.state.organizer.emby.connection()
            if paths and connection:
                from pathlib import PurePosixPath
                from .network import emby_request
                from .emby_connection import strm_mount_path
                mapping = strm_mount_path(store)
                updates = [{'Path': str(PurePosixPath(mapping) / p), 'UpdateType': 'Modified'} for p in paths]
                for start in range(0, len(updates), 200):
                    response = await emby_request(app.state.http, 'POST', connection[0] + '/Library/Media/Updated',
                                                  headers={'X-Emby-Token': connection[1]}, json={'Updates': updates[start:start+200]}, timeout=15)
                    response.raise_for_status()
                with store.db:
                    store.db.execute("DELETE FROM meta WHERE key='share_url_refresh_pending'")
                store.event('share', 'Emby 已接受 %d 条分享 STRM 地址更新，实际刷新结果以 Emby 为准。' % len(paths))
        except Exception as exc:
            store.event('share', '分享 STRM 地址已保留，Emby 刷新将在下次启动时重试（%s）。' % type(exc).__name__)
        app.state.organizer.start()
        # Emby 反代（「Emby 与飞牛反代」页）：按规则起多台服务器，只转发 API 流量，
        # 并在 PlaybackInfo 里补 115 直链——视频由客户端直接向 CDN 取，不经过本机。
        app.state.emby_proxy = EmbyProxyManager(store, app.state.cloud, app.state.metrics)
        try:
            await app.state.emby_proxy.apply_all()
        except OSError as exc:
            store.event('system', 'Emby 反代启动失败：%s（端口可能被占用）。' % type(exc).__name__)
        # 启动自检（0.8.19）：历史版本在 pause / cancel / 异常退出会把条目留在
        # state='discovered'，它们所属的任务早已结束，却仍然参与任务中心的
        # 「未处理」统计。这里一次性归一并留痕；幂等，健康库上不产生事件。
        settled = store.reconcile_terminal_items()
        if settled:
            store.event('system', '启动自检：%d 条遗留条目已归一到「跳过」（所属任务已结束）。' % settled)
        # Production Web runs no background loops; Worker owns inventory scans,
        # coverage, media info and housekeeping. Tests inline everything.
        if getattr(app.state.organizer, 'web_only', False) is False:
            app.state.inventory_scan_task = asyncio.create_task(app.state.inventory_scan_loop())
            app.state.coverage_task = asyncio.create_task(app.state.organizer.emby.coverage_loop())
            app.state.media_info_task = asyncio.create_task(inject_loop(app, store))
            app.state.housekeep_task = asyncio.create_task(housekeeping_loop(app, store))
        store.event('system', f'StrimKeep {__version__} 整理服务启动。')
        try:
            yield
        finally:
            for name in ('coverage_task', 'media_info_task',
                         'housekeep_task', 'inventory_scan_task'):
                task = getattr(app.state, name, None)
                if task and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            await app.state.organizer.close()
            if getattr(app.state, 'emby_proxy', None) is not None:
                await app.state.emby_proxy.stop_all()
            await app.state.cloud.close()
            await app.state.http.aclose()
            await app.state.db_worker.shutdown()
            if instance_file is not None:
                instance_file.close()
            store.close()

    app.router.lifespan_context = lifespan

    @app.middleware('http')
    async def headers_and_origin(request: Request, call_next):
        # 每个请求一个短 id，便于串起一次请求的多条日志。
        request_id = request.headers.get('x-request-id') or secrets.token_hex(4)
        started = time.monotonic()
        if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
            origin = request.headers.get('origin')
            allowed = {x.strip().rstrip('/') for x in os.environ.get('ALLOWED_ORIGINS','').split(',') if x.strip()}
            if origin and origin.rstrip('/') not in allowed and urlsplit(origin).netloc.lower() != request.headers.get('host', '').lower():
                return JSONResponse({'error': {'code': 'ORIGIN_REJECTED', 'message': '请求来源不匹配。'}},
                                    403, headers={'X-Request-Id': request_id})
            size = request.headers.get('content-length', '')
            limit = 65536
            if size.isdigit() and int(size) > limit:
                return JSONResponse({'error': {'code': 'BODY_TOO_LARGE', 'message': '请求内容过大。'}},
                                    413, headers={'X-Request-Id': request_id})
        response = await call_next(request)
        elapsed = time.monotonic() - started
        response.headers['X-Request-Id'] = request_id
        # 请求计数：只用路由模板做 label（避免 /play115/<pickcode> 制造无限基数），
        # 这是「错误率」的分母，配合 /metrics 才能算 SLO。
        metrics = getattr(app.state, 'metrics', None)
        if metrics is not None:
            route = getattr(request.scope.get('route'), 'path', '')
            metrics.record_request(request.method, route or 'unmatched', response.status_code)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['X-Frame-Options'] = 'DENY'
        # 管理界面经 frp 暴露到公网端口，明确禁止搜索引擎收录与传感器访问。
        response.headers['X-Robots-Tag'] = 'noindex, nofollow'
        response.headers['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=()'
        # Hashed Vite assets never change content, so they can be cached for a
        # year; HTML and every API response stay uncached. Without this the whole
        # bundle was downloaded again on each visit.
        hashed = request.url.path.startswith('/assets/') and response.status_code in (200, 304)
        response.headers['Cache-Control'] = ('public, max-age=31536000, immutable'
                                             if hashed else 'no-store')
        response.headers['Content-Security-Policy'] = "default-src 'self'; img-src 'self' data: https: http:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        return response

    @app.exception_handler(HTTPException)
    async def http_error(_, exc):
        return JSONResponse({'error': {'code': f'HTTP_{exc.status_code}', 'message': exc.detail}}, exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def invalid(_, exc):
        # Pydantic validation input can contain passwords or cookies; do not echo it.
        return JSONResponse({'error': {'code': 'INVALID_INPUT', 'message': '输入格式不正确，请检查字段。'}}, 422)

    @app.exception_handler(CloudError)
    async def cloud_error(_, exc):
        return JSONResponse({'error': {'code': exc.code, 'message': str(exc)}}, exc.status)

    @app.exception_handler(Exception)
    async def unexpected(request, exc):
        route = request.scope.get('route')
        endpoint = getattr(route, 'path', None) or '未匹配接口'
        store.event('error', '内部操作失败；接口：' + endpoint + '；错误类别：' + type(exc).__name__)
        return JSONResponse({'error': {'code': 'INTERNAL_ERROR', 'message': '内部操作失败，请检查运行状态和数据目录权限。'}}, 500)

    async def admin(request: Request):
        session = store.session(request.cookies.get('strimkeep_session', ''))
        if not session:
            raise HTTPException(401, '请先登录管理界面。')
        if request.method not in ('GET', 'HEAD'):
            if not hmac.compare_digest(request.headers.get('x-csrf-token', ''), session['csrf']):
                raise HTTPException(403, '会话校验失败，请刷新页面后重试。')
        return session

    def managed_path(relative: str) -> Path:
        path = (app.state.strms / relative).resolve()
        if not path.is_relative_to(app.state.strms):
            raise HTTPException(409, 'STRM 路径越界，已停止操作。')
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def write_strm(row, token):
        base = store.get('playback_base')
        if not base:
            raise HTTPException(409, '请先设置播放基础地址。')
        path = managed_path(row['relative_path'])
        temporary = path.with_suffix('.tmp-' + secrets.token_hex(5))
        # 公共播放地址构造（B02）：可读段只用于显示，身份由 row['id'] 决定。
        url = play_url(base, row['id'], row['name'] if 'name' in row.keys() else path.stem, token)
        try:
            with temporary.open('x', encoding='utf-8') as f:
                f.write(url + '\n')
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return url

    def row_for(item_id):
        row = store.db.execute('SELECT * FROM playbacks WHERE id=?', (item_id,)).fetchone()
        if not row:
            raise HTTPException(404, '验证样例不存在。')
        return row

    def supported(file):
        if Path(file['name']).suffix.lower() not in ('.mkv', '.mp4', '.m4v', '.mov', '.avi', '.ts', '.m2ts', '.webm'):
            raise HTTPException(422, '本阶段仅验证常规视频，ISO 等格式暂不处理。')

    @app.get('/health/live')
    async def live():
        return {'status': 'alive'}

    @app.get('/health/ready')
    async def ready():
        store.db.execute('SELECT 1').fetchone()
        if getattr(app.state.organizer, 'web_only', False):
            raw = store.meta('worker_heartbeat')
            try:
                heartbeat = json.loads(raw) if raw else None
            except (TypeError, ValueError):
                heartbeat = None
            if isinstance(heartbeat, dict):
                try:
                    fresh = time.time() - float(heartbeat.get('at') or 0) < 30
                except (TypeError, ValueError):
                    fresh = False
                faults = heartbeat.get('lane_faults') or {}
                if fresh and (heartbeat.get('closing') or any(faults.values())):
                    raise HTTPException(503, '后台任务车道异常，请检查 Worker 日志。')
            else:
                try:
                    fresh = heartbeat is not None and time.time() - float(heartbeat) < 30
                except (TypeError, ValueError):
                    fresh = False
            if not fresh:
                raise HTTPException(503, '后台 Worker 未就绪，请检查容器日志。')
        else:
            worker = app.state.organizer.worker
            if worker and worker.done() and not app.state.organizer.closing:
                raise HTTPException(503, '整理 Worker 已停止，请检查操作记录并重启服务。')
            if not app.state.organizer.closing and (any(task.done() for task in app.state.organizer.extra_workers) or any(app.state.organizer.lane_faults.values())):
                raise HTTPException(503, '任务车道异常，请检查操作记录。')
            if app.state.organizer.scheduler_fault or (app.state.organizer.clock and app.state.organizer.clock.done() and not app.state.organizer.closing):
                raise HTTPException(503, '调度器异常，请检查操作记录。')
        return {'status': 'ready', 'version': __version__, 'stage': 5}

    @app.post('/api/v1/auth/login')
    async def login(body: Login, request: Request):
        stamp = time.monotonic()
        for key, attempts in list(app.state.failed_logins.items()):
            while attempts and attempts[0] < stamp - 900: attempts.popleft()
            if not attempts: app.state.failed_logins.pop(key, None)
        peer = request.client.host if request.client else 'unknown'
        failures = app.state.failed_logins[peer]
        now = time.monotonic()
        while failures and failures[0] < now - 900:
            failures.popleft()
        # Verify credentials before honouring the failure budget. All visitors of
        # a tunnelled deployment share one peer address, so a limit checked first
        # would let a remote stranger lock the administrator out for 15 minutes.
        # Wrong passwords are still counted and throttled after the check.
        if not store.authenticate(body.username, body.password):
            failures.append(now)
            if len(failures) >= 8:
                raise HTTPException(429, '登录失败次数过多，请稍后再试。')
            raise HTTPException(401, '账号或密码不正确。')
        failures.clear()
        token, csrf = store.new_session()
        response = JSONResponse({'username': store.meta('username'), 'csrf': csrf})
        from .store import SESSION_SECONDS
        response.set_cookie('strimkeep_session', token, httponly=True, samesite='strict',
                            secure=os.environ.get('SESSION_COOKIE_SECURE', 'false').lower() == 'true', max_age=SESSION_SECONDS)
        return response

    @app.get('/api/v1/auth/me')
    async def me(session=Depends(admin)):
        return {'username': store.meta('username'), 'csrf': session['csrf']}

    @app.post('/api/v1/auth/logout', dependencies=[Depends(admin)])
    async def logout(request: Request):
        with store.db:
            store.db.execute('DELETE FROM sessions WHERE hash=?', (digest(request.cookies.get('strimkeep_session', '')),))
        response = JSONResponse({'ok': True})
        response.delete_cookie('strimkeep_session')
        return response

    @app.get('/api/v1/status', dependencies=[Depends(admin)])
    async def status():
        return {'version': __version__, 'stage': '媒体自动化工作台',
                'capabilities': {'login': True, 'cloud_browser': True, 'tmdb': True,
                                 'ffprobe': True, 'sample_strm': True, 'redirect_302': True,
                                 'organize': True, 'archive_index': True, 'upgrade': True, 'scheduled_sync': True, 'strm_sync': True, 'task_queue': True, 'notifications': True},
                'samples': store.db.execute('SELECT count(*) FROM playbacks').fetchone()[0],
                'organized': store.db.execute('SELECT count(*) FROM organized_files').fetchone()[0],
                'strm_root': str(app.state.strms)}

    @app.get('/api/v1/settings', dependencies=[Depends(admin)])
    async def settings_get():
        return {**{k + '_configured': bool(store.get(k)) for k in ('cookie', 'tmdb_key', 'emby_key')},
                'emby_url': store.get('emby_url'), 'playback_base': store.get('playback_base'),
                'cloud_throttle': {**Cloud115.THROTTLE_DEFAULTS, **(store.get('cloud_throttle') or {})}}

    @app.put('/api/v1/settings', dependencies=[Depends(admin)])
    async def settings_put(body: Settings):
        values = body.model_dump(exclude_none=True)
        for k in ('emby_url', 'playback_base'):
            if k in values and values[k]:
                values[k] = base_url(values[k], allow_path=k == 'emby_url')
        if 'cookie' in values:
            cookie = values['cookie']
            if not all(re.search(r'(?:^|;\s*)' + k + r'=[^;\s]+', cookie) for k in ('UID', 'CID', 'SEID')) or '\n' in cookie or '\r' in cookie:
                raise HTTPException(422, 'Cookie 需包含 UID、CID、SEID，且不能包含换行。')
        store.set_many(values)
        if 'cloud_throttle' in values:
            app.state.cloud._throttle_cache = None
        if ('emby_url' in values or 'emby_key' in values) and getattr(app.state, 'emby_proxy', None):
            await app.state.emby_proxy.refresh_default_connection()
        store.event('settings', '连接配置已保存。')
        return await settings_get()

    @app.post('/api/v1/connections/115/test', dependencies=[Depends(admin)])
    async def test_cloud():
        try:
            result = await app.state.cloud.status()
        except CloudError as exc:
            if exc.code not in ('LOGIN_REQUIRED', 'LOGIN_INVALID'):
                raise
            # 115 掉登录不能把管理端一起踢出去：401 会被前端当成管理员会话失效。
            # 这里只回报状态，由页面引导去扫码或填 Cookie。
            store.event('115', '115 登录检查未通过：' + str(exc)[:120])
            return {'connected': False, 'message': str(exc)}
        app.state.organizer.login_ready()
        store.event('115', '115 登录状态检查通过。')
        return {**result, 'connected': True}

    @app.get('/api/v1/connections/115/clients', dependencies=[Depends(admin)])
    async def qr_clients():
        selected = store.get('115_qr_client')
        return {'items': LOGIN_CLIENTS, 'selected': selected if selected in CLIENT_LABELS else ''}

    @app.post('/api/v1/connections/115/qr', dependencies=[Depends(admin)])
    async def qr_start(body: QRLogin):
        if body.app not in CLIENT_LABELS:
            raise HTTPException(422, '请选择支持的登录客户端。')
        return await app.state.cloud.qr_start(body.app)

    @app.delete('/api/v1/connections/115/qr/{key}', dependencies=[Depends(admin)])
    async def qr_cancel(key: str):
        return app.state.cloud.qr_cancel(key)

    @app.post('/api/v1/connections/115/qr/{key}/poll', dependencies=[Depends(admin)])
    async def qr_poll(key: str):
        result = await app.state.cloud.qr_poll(key)
        if result.get('status') == 'connected':
            app.state.organizer.login_ready()
        return result

    def checked_config(body: OrganizeConfig):
        config = body.model_dump()
        ids = config['source_cids'] + [config['archive_cid']] + ([config['pending_cid']] if config['pending_cid'] else [])
        if (not config['source_cids'] or any(not re.fullmatch(r'[1-9]\d{0,23}', x) for x in ids)
                or len(set(config['source_cids'])) != len(config['source_cids'])
                or config['archive_cid'] == config['pending_cid']
                or any(x in config['source_cids'] for x in (config['archive_cid'], config['pending_cid']))):
            raise HTTPException(422, '请选择至少一个待整理目录和归档目录；未识别目录可留空，所选目录不能重复。')
        if any(len(k) > 24 or len(v) > 500 for k, v in config['directory_labels'].items()):
            raise HTTPException(422, '目录标签过长。')
        return config

    @app.get('/api/v1/organize/config', dependencies=[Depends(admin)])
    async def organize_config():
        return {'config': {**OrganizeConfig().model_dump(), **store.get('organize_config', {})},
                'strm_root': str(app.state.strms),
                'library_prefix': library_root(store.get('organize_config', {}) or {}),
                'scope': '扫描待整理目录，识别、洗版并生成 STRM；直接读取当前作品目录，无需先建立归档索引。NFO/图片交给 Emby。'}

    @app.get('/api/v1/upgrade/config', dependencies=[Depends(admin)])
    async def upgrade_config():
        policy = public_policy(store.get('upgrade_policy'))
        return {'config': policy,
                'labels': {r['key']: RULE_LABELS[r['key']] for r in policy['rules']}, 'choices': RULE_CHOICES,
                'options': RULE_OPTIONS, 'suggested_order': SUGGESTED_ORDER}

    @app.put('/api/v1/upgrade/config', dependencies=[Depends(admin)])
    async def upgrade_config_save(body: UpgradePolicy):
        # 共存维度只剩「杜比 / 非杜比」：旧的分辨率 / 帧率 / 三档动态范围字段在模型层已归零，这里只校验规则完整性。
        if (({r.key for r in body.rules} not in (set(RULE_KEYS), set(RULE_KEYS) | {'revision'}) if body.coexist_version == 1 else {r.key for r in body.rules} != set(RULE_KEYS)) or body.season_layout != 'standard'):
            raise CloudError('请使用当前版本的多版本设置；九项比较规则须完整。', 'INVALID_UPGRADE_POLICY', 422)
        policy = public_policy(body.model_dump())
        store.set_many({'upgrade_policy': policy})
        store.event('upgrade', '洗版规则已保存；仅后续新建整理任务使用这份配置。')
        return await upgrade_config()

    @app.post('/api/v1/upgrade/recycle/check', dependencies=[Depends(admin)])
    async def recycle_check():
        await app.state.cloud.status()
        rows = await app.state.cloud.recycle_files()
        return {'connected': True, 'count': len(rows), 'message': '回收站分页读取通过；本次没有修改任何文件。'}

    @app.put('/api/v1/organize/config', dependencies=[Depends(admin)])
    async def save_organize_config(body: OrganizeConfig):
        config = checked_config(body)
        try:
            await app.state.organizer.validate(config)
        except NeedsReview as exc:
            raise HTTPException(422, str(exc)) from None
        try:effective_strm(store,organize_value=config)
        except ValueError as exc:raise HTTPException(422,str(exc)) from None
        store.set_many({'organize_config': config})
        store.event('organize', '扫描整理目录与参数已核实并保存。')
        return await organize_config()

    def job_maps(job_ids):
        """P01：任务列表批量统计，消除逐任务 N+1 查询。"""
        counts_map, emby_map = {}, {}
        job_ids = list(job_ids)
        if job_ids:
            marks = ','.join('?' * len(job_ids))
            for jid, state, n in store.db.execute(
                    f'SELECT job_id,state,count(*) FROM organize_items WHERE job_id IN ({marks}) GROUP BY job_id,state', job_ids):
                counts_map.setdefault(jid, {})[state] = n
            for row in store.db.execute(f'SELECT * FROM emby_deliveries WHERE job_id IN ({marks})', job_ids):
                emby_map[row['job_id']] = row
        return counts_map, emby_map

    def job_item_stats(job_ids):
        """P02：把「未识别 / 被规则过滤」两类计数也改成批量查询。

        以前 `job_public` 对每条任务各跑 2 条 LIKE 统计：任务列表一次 40 条、
        `/tasks/events` 每 2 秒重建 30 条 → 每轮 60~80 次全表扫描（`organize_items`
        现场 4582 行）。这里用两条 GROUP BY 一次算完，查询数与任务数无关。
        口径与逐条统计完全一致（同样的 LIKE 条件），只是合并成一次查询。
        """
        unidentified, filtered = {}, {}
        job_ids = list(job_ids)
        if job_ids:
            marks = ','.join('?' * len(job_ids))
            for jid, n in store.db.execute(
                    f"SELECT job_id,count(*) FROM organize_items WHERE job_id IN ({marks}) "
                    f"AND ({UNRECOGNIZED_ITEM}) GROUP BY job_id", job_ids):
                unidentified[jid] = n
            for jid, n in store.db.execute(
                    f"SELECT job_id,count(*) FROM organize_items WHERE job_id IN ({marks}) AND ("
                    " step LIKE '%保留已有%' OR step LIKE '%未列入%' OR step LIKE '%低于最小%'"
                    " OR step LIKE '%命中忽略关键词%') GROUP BY job_id", job_ids):
                filtered[jid] = n
        return {'unidentified': unidentified, 'filtered': filtered}

    def job_public(row, counts_map=None, emby_map=None, stats=None):
        result = {key: row[key] for key in ('id','state','created','updated','stage','message','manifest','notify_state')}
        job_config = json.loads(row['config'])
        delivery = (emby_map or {}).get(row['id']) if emby_map is not None else app.state.organizer.emby.row(row['id'])
        # 归档的历史投递（超过 7 天仍未确认）不再当成当前状态展示，否则任务中心会一直挂红点。
        if delivery and delivery['state'] != 'archived':
            # 0.8.20：优先读 emby_deliveries 的摘列（total/confirmed）。只有历史行
            # 尚未回填（两列都为 0）时才回退解析 payload —— 正常路径不再解析 JSON。
            keys = delivery.keys()
            total = delivery['total'] if 'total' in keys else 0
            confirmed = delivery['confirmed'] if 'confirmed' in keys else 0
            if not total and not confirmed:
                try:
                    fallback = json.loads(delivery['payload']).get('targets') or []
                except (TypeError, ValueError):
                    fallback = []
                total = len(fallback)
                confirmed = sum(1 for t in fallback if isinstance(t, dict) and t.get('confirmed'))
            result['emby'] = {'state': delivery['state'], 'attempts': delivery['attempts'],
                              'total': total, 'confirmed': confirmed, 'message': delivery['message']}
        result['sync_mode'] = job_config.get('sync_mode', '')
        result['scan_filter'] = job_config.get('scan_filter', {})
        if job_config.get('job_kind') == 'strm_sync':
            result['sync_metrics'] = job_config.get('sync_metrics')
            result['sync_trigger'] = job_config.get('sync_trigger', '')
            result['emby_target_name'] = job_config.get('emby_target_name') or (
                '已关闭' if job_config.get('emby_target') == 'off' else '')
        if counts_map is not None:
            counts = counts_map.get(row['id'], {})
        else:
            counts = dict(store.db.execute('SELECT state,count(*) FROM organize_items WHERE job_id=? GROUP BY state', (row['id'],)).fetchall())
        result['counts'] = counts
        result['total'] = sum(counts.values())
        # 「未识别」= 停在未识别转移的文件。以前前端看 counts.pending，而 pending 只是
        # 中间态、跑完就变 done/skipped，所以那一类永远显示 0。
        # 「过滤」= 被规则挡在库外的条目：洗版/已有更好版本（保留已有）、扩展名不匹配
        # （未列入）、体积不足（低于最小）。只统计 scan_filter.excluded 抓不到现场最多
        # 的那种——「已有同集或同名条目，保留已有」记在条目 step 上，config 里是 0。
        # stats 由 job_item_stats() 批量算好；没有传（单条查询场景/老调用）时退回逐条查询。
        if stats is not None:
            unidentified = stats['unidentified'].get(row['id'], 0)
            filtered_items = stats['filtered'].get(row['id'], 0)
        else:
            unidentified = store.db.execute(
                f"SELECT count(*) FROM organize_items WHERE job_id=? AND ({UNRECOGNIZED_ITEM})",
                (row['id'],)).fetchone()[0]
            filtered_items = store.db.execute(
                "SELECT count(*) FROM organize_items WHERE job_id=? AND ("
                " step LIKE '%保留已有%' OR step LIKE '%未列入%' OR step LIKE '%低于最小%'"
                " OR step LIKE '%命中忽略关键词%')", (row['id'],)).fetchone()[0]
        result['unidentified'] = unidentified
        result['filtered_items'] = filtered_items
        result['filtered'] = filtered_items + int(
            (result.get('scan_filter') or {}).get('excluded') or 0)
        result['config'] = {k: v for k, v in job_config.items() if k in ('job_kind','source_cids','archive_cid','pending_cid','directory_labels','scheduled','notify_emby','upgrade_policy','retries','retry_interval','strm_delay','target_cid','target_label','origin','transfer_parent','organize_options') or k in OrganizeOptions.model_fields}
        states_ok = all(state in ('done', 'pending', 'skipped', 'recycled') for state in counts)
        result['can_retry_cleanup'] = bool(app.state.organizer.cleanup.retryable(row) and row['manifest'] and states_ok)
        # 已结束的任务也能重查目录残留（cleanup 会重新列目录，并核实扫描时记录的未匹配字幕）。
        cleanup_state = job_config.get('empty_cleanup')
        result['can_recheck_cleanup'] = bool(
            row['state'] in ('completed', 'partial') and row['manifest'] and states_ok
            and isinstance(cleanup_state, dict) and cleanup_state.get('entries'))
        result['kind'] = result['config'].get('job_kind', 'organize')
        result['finished'] = sum(counts.get(s,0) for s in ('done','pending','skipped','recycled','review'))
        result['progress'] = round(result['finished'] * 100 / result['total']) if result['total'] else (100 if row['state'] in ('completed','partial') else 0)
        # 预计剩余：只在运行中给，历史任务不编 ETA。
        if row['state'] == 'running' and result['total']:
            elapsed = max(1.0, time.time() - row['created'])
            rate = result['finished'] / elapsed if result['finished'] else 0
            result['elapsed_seconds'] = int(elapsed)
            result['eta_seconds'] = int((result['total'] - result['finished']) / rate) if rate else None
        if result['kind'] == 'archive_index':
            latest = store.db.execute('SELECT * FROM archive_snapshots WHERE job_id=? ORDER BY id DESC LIMIT 1', (row['id'],)).fetchone()
            result['counts'] = {'indexed': latest['resolved'], 'unresolved': latest['unresolved'], 'unsupported': latest['unsupported']} if latest else {}
            result['total'] = latest['total'] if latest else 0
            result['finished'] = sum(result['counts'].values())
            result['progress'] = round(result['finished'] * 100 / result['total']) if result['total'] else (100 if row['state'] in ('completed','partial') else 0)
        return result

    app.state.job_maps = job_maps
    app.state.job_item_stats = job_item_stats
    app.state.job_public = job_public

    install_phase5(app, store, admin, job_public)
    install_emby_proxy(app, store, admin, lambda: app.state.emby_proxy)
    install_media_info(app, store, admin)
    install_housekeeping(app, store, admin)
    install_metrics(app, store)
    from .console_api import install_console
    install_console(app, store, admin)
    from .health_api import install_health
    install_health(app, store, admin)
    from .transfer_api import install_transfer
    install_transfer(app, store, admin, job_public)
    from .offline_api import install_offline
    install_offline(app, store, admin)
    from .explore_api import install_explore
    install_explore(app, store, admin)
    from .inventory_api import install_inventory
    install_inventory(app, store, admin)
    from .inventory_operations_api import install_inventory_operations
    install_inventory_operations(app, store, admin)
    from .resource_discovery import install_resource_discovery
    install_resource_discovery(app, store, admin)
    from .share_api import install_share
    install_share(app, store, admin)
    from .channels_api import install_channels
    install_channels(app, store, admin)
    install_library_covers(app, store, admin)

    def archive_scope():
        config = store.get('organize_config', {})
        root_id = config.get('archive_cid', '')
        match = re.search(r'(?:^|;\s*)UID=(\d+)', store.get('cookie'))
        if not root_id:
            raise HTTPException(409, '请先在扫描整理保存归档根目录。')
        if not match:
            raise CloudError('请先登录 115 并检查连接。', 'LOGIN_REQUIRED', 409)
        return config, root_id, match[1]

    @app.get('/api/v1/archive/index')
    async def archive_summary(_=Depends(admin)):
        config, root_id, account_id = archive_scope()
        summary = app.state.organizer.index.summary(account_id, root_id)
        return {**summary, 'root_id': root_id, 'root_label': config.get('directory_labels', {}).get(root_id, root_id)}

    @app.get('/api/v1/archive/entries', dependencies=[Depends(admin)])
    async def archive_entries(offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100),
                              state: str = Query('all', pattern=r'^(all|resolved|unresolved|unsupported)$'),
                              q: str = Query('', max_length=200)):
        _, root_id, account_id = archive_scope()
        snapshot = app.state.organizer.index.latest(account_id, root_id)
        if not snapshot:
            return {'items': [], 'total': 0, 'has_more': False, 'offset': offset}
        where, params = 'snapshot_id=?', [snapshot['id']]
        if state != 'all':
            where += ' AND state=?'; params.append(state)
        if q:
            where += ' AND (name LIKE ? OR relative_path LIKE ? OR CAST(tmdb_id AS TEXT)=?)'
            params.extend(('%' + q + '%', '%' + q + '%', q))
        total = store.db.execute('SELECT count(*) FROM archive_entries WHERE ' + where, params).fetchone()[0]
        fields = 'file_id,name,relative_path,size,state,message,media_type,tmdb_id,season,episode,end_episode'
        rows = store.db.execute('SELECT ' + fields + ' FROM archive_entries WHERE ' + where + ' ORDER BY file_id LIMIT ? OFFSET ?', (*params, limit, offset)).fetchall()
        return {'items': [dict(row) for row in rows], 'total': total, 'has_more': offset + len(rows) < total, 'offset': offset}

    @app.post('/api/v1/archive/jobs', dependencies=[Depends(admin)])
    async def start_archive_index():
        config, root_id, account_id = archive_scope()
        if not store.get('tmdb_key'):
            raise HTTPException(409, '归档身份核查需要先配置 TMDB。')
        snapshot_config = {'job_kind': 'archive_index', 'archive_cid': root_id, 'account_id': account_id,
                           'directory_labels': {root_id: config.get('directory_labels', {}).get(root_id, root_id)}}
        job_id = store.jobs.create(snapshot_config)
        store.event('archive', f'归档索引任务 {job_id} 已入队，只读取既有文件。')
        return job_public(app.state.organizer.job(job_id))

    @app.get('/api/v1/organize/jobs', dependencies=[Depends(admin)])
    async def organize_jobs(before: int = Query(2**62, ge=1)):
        rows = store.db.execute('SELECT * FROM organize_jobs WHERE id<? ORDER BY id DESC LIMIT 30', (before,)).fetchall()
        counts_map, emby_map = job_maps([row['id'] for row in rows])
        return {'items': [job_public(row, counts_map, emby_map) for row in rows]}

    @app.post('/api/v1/organize/jobs', dependencies=[Depends(admin)])
    async def start_organize():
        config = store.get('organize_config', None)
        if config is None:
            raise HTTPException(409, '请先保存扫描整理目录。')
        if not store.get('tmdb_key') or not store.get('playback_base') or not store.get('cookie'):
            raise HTTPException(409, '请先配置 115 登录、TMDB 和 StrimKeep 播放基础地址。')
        match = re.search(r'(?:^|;\s*)UID=(\d+)', store.get('cookie'))
        if not match:
            raise HTTPException(409, '无法核实 115 Cookie 中的账号 UID，请重新登录并检查。')
        config = app.state.organizer.tasks.config('organize')
        job_id, merged = app.state.organizer.tasks.enqueue(config)
        result = job_public(app.state.organizer.job(job_id))
        result['merged'] = merged
        return result

    @app.get('/api/v1/organize/jobs/{job_id}', dependencies=[Depends(admin)])
    async def organize_job(job_id: int, after: int = Query(0, ge=0)):
        row = app.state.organizer.job(job_id)
        if not row:
            raise HTTPException(404, '任务不存在。')
        is_organize = json.loads(row['config']).get('job_kind', 'organize') == 'organize'
        items = []
        for item in store.db.execute(
                f'SELECT *, ({UNRECOGNIZED_ITEM}) AS unidentified FROM organize_items '
                'WHERE job_id=? AND id>? ORDER BY id LIMIT 100', (job_id, after)):
            original, plan = json.loads(item['original']), json.loads(item['plan'])
            alias = recognition_aliases.candidate(original) if is_organize and item['unidentified'] else None
            items.append({k: item[k] for k in ('id','file_id','state','step','message')} |
                         {'unidentified': bool(item['unidentified']), 'original_name': original.get('name') or item['file_id'], 'source_cid': original.get('parent_id') or '',
                          'target_name': plan.get('target_name', ''), 'cloud_parts': plan.get('parts', []),
                          'relative_path': plan.get('relative_path', ''), 'media': plan.get('media'),
                          'tmdb_id': plan.get('details', {}).get('id'), 'warnings': plan.get('warnings', []),
                          'alias_candidate': alias, 'recognition_alias': (plan.get('hint') or {}).get('recognition_alias'),
                          'upgrade': {key: plan.get('wash', {}).get(key) for key in ('decision','reason','group','rule_version','comparisons')},
                          'recycle': [{'file_id': fid, 'state': action['state'], 'rid': action.get('rid', '')}
                                      for fid, action in plan.get('wash', {}).get('actions', {}).items()]})
        return {'job': job_public(row), 'items': items, 'has_more': len(items) == 100}

    @app.get('/api/v1/organize/recognition-aliases', dependencies=[Depends(admin)])
    async def list_recognition_aliases():
        return {'items': [dict(rule) for rule in store.db.execute(
            f'SELECT {recognition_aliases.PUBLIC_COLUMNS} FROM recognition_aliases ORDER BY id DESC')]}

    @app.delete('/api/v1/organize/recognition-aliases/{rule_id}', dependencies=[Depends(admin)])
    async def remove_recognition_alias(rule_id: int):
        with store.transaction(immediate=True):
            removed = store.db.execute('DELETE FROM recognition_aliases WHERE id=?', (rule_id,)).rowcount
        if removed:
            store.event('organize', f'已移除识别纠正规则 #{rule_id}。')
        return {'removed': bool(removed)}

    @app.post('/api/v1/organize/jobs/{job_id}/items/{item_id}/manual-match', dependencies=[Depends(admin)])
    async def manual_match(job_id: int, item_id: int, body: ManualMatch):
        row = app.state.organizer.job(job_id)
        if not row:
            raise HTTPException(404, '任务不存在。')
        item = store.db.execute(
            f'SELECT *, ({UNRECOGNIZED_ITEM}) AS unidentified FROM organize_items '
            'WHERE id=? AND job_id=?', (item_id, job_id)).fetchone()
        if not item:
            raise HTTPException(404, '任务条目不存在。')
        if row['state'] in ('queued', 'running', 'paused', 'waiting_login'):
            raise HTTPException(409, '任务仍在进行，请先等待结束或取消后再手动匹配。')
        source_config = json.loads(row['config'] or '{}')
        if source_config.get('job_kind', 'organize') != 'organize':
            raise HTTPException(409, '只有扫描整理任务的未识别项支持手动匹配。')
        if not item['unidentified']:
            raise HTTPException(409, '只有未识别项可以手动匹配。')
        if body.media_type == 'tv' and (body.season is None or body.episode is None):
            raise HTTPException(422, '剧集手动匹配需要填写季号和集号。')
        original = json.loads(item['original'] or '{}')
        alias = recognition_aliases.candidate(original) if body.remember_alias else None
        if body.remember_alias and (not alias or alias['media_type'] != body.media_type
                                    or (body.media_type == 'tv' and
                                        (alias['season'], alias['episode']) != (body.season, body.episode))):
            raise HTTPException(422, '此文件的原片名、类型或季集不足以保存自动纠正规则。可取消勾选后单独手动匹配。')
        try:
            current = await app.state.organizer.cloud.file(original['id'])
        except CloudError as exc:
            raise HTTPException(409, f'源文件不可访问：{exc}') from exc
        # 未识别文件可能已被移入未识别目录；以云端当前名称/父目录为准。
        current = {**original, **{key: value for key, value in current.items() if value is not None}}
        if original.get('subtitles'):
            subtitles = []
            for sub in original['subtitles']:
                try:
                    latest = await app.state.organizer.cloud.file(sub['id'])
                except CloudError:
                    continue
                subtitles.append({**sub, **{key: value for key, value in latest.items() if value is not None}})
            current['subtitles'] = subtitles
        try:
            details = await app.state.organizer.tmdb(f'{body.media_type}/{body.tmdb_id}')
        except CloudError as exc:
            raise HTTPException(409, f'TMDB 查询失败：{exc}') from exc
        if int(details.get('id', 0)) != body.tmdb_id:
            raise HTTPException(409, 'TMDB 返回的作品 ID 与填写值不一致。')
        title = details.get('title') or details.get('name') or ''
        year = (details.get('release_date') or details.get('first_air_date') or '')[:4]
        if not title or not re.fullmatch(r'\d{4}', year):
            raise HTTPException(409, 'TMDB 作品缺少名称或首映年份，不能生成规范目录。')
        if alias and body.media_type == 'tv':
            try:
                episode = await app.state.organizer.tmdb_episode(body.tmdb_id, body.season, body.episode)
            except CloudError as exc:
                raise HTTPException(409, f'TMDB 季集查询失败：{exc}') from exc
            if not episode or (episode.get('season_number'), episode.get('episode_number')) != (body.season, body.episode):
                raise HTTPException(409, 'TMDB 尚未核实该季集。可取消勾选后单独手动匹配。')
        hint = {
            'query': title, 'year': int(year), 'media_type': body.media_type,
            'season': body.season if body.media_type == 'tv' else None,
            'episode': body.episode if body.media_type == 'tv' else None,
            'end_episode': body.episode if body.media_type == 'tv' else None,
            'tmdb_id': body.tmdb_id, 'identity_source': 'manual',
        }
        config = dict(source_config)
        for key in ('repair_only', 'cleanup_only', 'empty_cleanup'):
            config.pop(key, None)
        config.update({
            'only_file_ids': [str(original['id'])],
            'manual_hint': hint,
            'manual_source_job': job_id,
            'manual_source_item': item_id,
            'origin': 'manual_import',
            'scheduled': False,
            # 手动匹配失败时原位保留，避免再次把文件往未识别目录搬。
            'pending_cid': '',
        })
        with store.transaction(immediate=True):
            # Another request or the Worker may have dealt with the source
            # while we were waiting for 115/TMDB. Claim it before publishing.
            eligible = store.db.execute(
                f'SELECT 1 FROM organize_items WHERE id=? AND job_id=? AND original=? '
                f'AND ({UNRECOGNIZED_ITEM}) AND EXISTS (SELECT 1 FROM organize_jobs '
                "WHERE id=? AND state NOT IN ('queued','running','paused','waiting_login'))",
                (item_id, job_id, item['original'], job_id)).fetchone()
            if not eligible:
                raise HTTPException(409, '该条目已变更，请刷新任务详情。')
            if alias:
                try:
                    recognition_aliases.remember(store, alias, hint)
                except ValueError as exc:
                    raise HTTPException(409, str(exc)) from None
            new_job_id = store.jobs.create(config)
            store.jobs.insert_item(new_job_id, original['id'], current, state='discovered')
            store.jobs.set_manifest(new_job_id, 1)
            store.jobs.update_item(
                item_id, step='已转手动匹配',
                message=f'已创建手动匹配任务 #{new_job_id}：TMDB {body.tmdb_id}。原任务记录保留。')
        store.event('organize', f'任务 {job_id} 条目 {item_id}：已创建手动匹配任务 #{new_job_id}。')
        return {'job': job_public(app.state.organizer.job(new_job_id)),
                'source_job': job_id, 'source_item': item_id, 'saved_alias': bool(alias)}

    @app.post('/api/v1/organize/jobs/{job_id}/dismiss-unrecognized', dependencies=[Depends(admin)])
    async def dismiss_unrecognized(job_id: int, body: DismissUnrecognized):
        row = app.state.organizer.job(job_id)
        if not row:
            raise HTTPException(404, '任务不存在。')
        if row['state'] in ('queued', 'running', 'paused', 'waiting_login'):
            raise HTTPException(409, '任务仍在进行，请先等待结束或取消后再人工处理未识别项。')
        with store.transaction(immediate=True):
            row = store.jobs.get(job_id)
            if row['state'] in ('queued', 'running', 'paused', 'waiting_login'):
                raise HTTPException(409, '任务状态已变更，请刷新任务详情。')
            sql = f'SELECT id FROM organize_items WHERE job_id=? AND ({UNRECOGNIZED_ITEM})'
            params: list = [job_id]
            if body.item_ids:
                marks = ','.join('?' for _ in body.item_ids)
                sql += f' AND id IN ({marks})'
                params.extend(body.item_ids)
            items = store.db.execute(sql + ' ORDER BY id', tuple(params)).fetchall()
            if not items:
                raise HTTPException(409, '没有可忽略的未识别项。')
            for item in items:
                store.jobs.update_item(item['id'], state='skipped', step='已人工忽略',
                                       message='已确认忽略，原文件保留。')
            closed = False
            if row['state'] in ('partial', 'needs_review'):
                remaining = store.db.execute(
                    'SELECT 1 FROM organize_items WHERE job_id=? AND ('
                    "state IN ('review','pending','planned','discovered') "
                    "OR (state='skipped' AND step NOT IN ('人工忽略未识别','已人工忽略') "
                    "AND json_extract(plan,'$.blocks_job')=1) "
                    f'OR ({UNRECOGNIZED_ITEM})) LIMIT 1', (job_id,)).fetchone()
                if not remaining:
                    store.jobs.close(job_id, '人工忽略未识别项后结项')
                    closed = True
        store.event('organize', f'任务 {job_id}：人工忽略 {len(items)} 个未识别项'
                    + ('并结项。' if closed else '。'))
        return {'dismissed': len(items), 'closed': closed,
                'job': job_public(app.state.organizer.job(job_id))}

    @app.post('/api/v1/organize/jobs/{job_id}/{action}', dependencies=[Depends(admin)])
    async def organize_control(job_id: int, action: str):
        row = app.state.organizer.job(job_id)
        if not row:
            raise HTTPException(404, '任务不存在。')
        active = ('queued','running','paused','waiting_login','needs_review')
        if action == 'retry-cleanup':
            if not app.state.organizer.cleanup.retryable(row) or not row['manifest']:
                raise HTTPException(409, '当前任务没有可重试的空目录清理。')
            if store.db.execute("SELECT 1 FROM organize_items WHERE job_id=? AND state NOT IN ('done','pending','skipped','recycled')", (job_id,)).fetchone():
                raise HTTPException(409, '该任务的视频处理尚未结束。')
            config = json.loads(row['config'])
            config['cleanup_only'] = True
            message = '仅核查并重试本任务已记录的空目录清理。'
            app.state.organizer.update_job(job_id, config=dumps(config), state='queued', message=message)
            store.event('organize', f'任务 {job_id}：{message}')
            return job_public(app.state.organizer.job(job_id))
        if action == 'recheck-cleanup':
            # 已结束的任务也能重查残留：只把 retained/missing 的条目放回待检。
            # cleanup 会重新列目录，只有能核实的字幕残留和「空」或「只剩海报/NFO」才回收，
            # 没有扫描快照的历史字幕仍会保留。
            if row['state'] not in ('completed', 'partial') or not row['manifest']:
                raise HTTPException(409, '只有已结束的整理任务可以重新核查目录。')
            if store.db.execute("SELECT 1 FROM organize_items WHERE job_id=? AND state NOT IN ('done','pending','skipped','recycled')", (job_id,)).fetchone():
                raise HTTPException(409, '该任务的视频处理尚未结束。')
            config = json.loads(row['config'])
            state = config.get('empty_cleanup')
            if not isinstance(state, dict) or not state.get('entries'):
                raise HTTPException(409, '该任务没有目录清理记录。')
            for entry in state['entries']:
                if entry.get('state') in ('retained', 'missing'):
                    entry['state'] = 'pending'
            config['empty_cleanup'] = state
            config['cleanup_only'] = True
            message = '重新核查本任务留下的目录与海报/NFO 残留。'
            app.state.organizer.update_job(job_id, config=dumps(config), state='queued', message=message)
            store.event('organize', f'任务 {job_id}：{message}')
            return job_public(app.state.organizer.job(job_id))
        if action == 'retry-output' and row['state'] in ('completed','partial','cancelled'):
            eligible = store.db.execute("SELECT id,plan FROM organize_items WHERE job_id=? AND state IN ('done','archived')", (job_id,)).fetchall()
            if not eligible:
                raise HTTPException(409, '该任务没有已归档的文件可补齐。')
            with store.db:
                for item in eligible:
                    plan = json.loads(item['plan'])
                    plan['warnings'] = []
                    store.jobs.update_item(item['id'], state='archived', step='等待补齐本地输出',
                                           message='', plan=plan)
                config = json.loads(row['config'])
                config.pop('cleanup_only', None)
                config['repair_only'] = True
                store.jobs.set_config(job_id, config)
            app.state.organizer.update_job(job_id, state='queued', message='先核实已归档文件，再补齐本地输出；不会再次通知 Emby，请完成后手动扫描。')
            return job_public(app.state.organizer.job(job_id))
        if action == 'close' and row['state'] in ('partial', 'needs_review', 'cancelled'):
            # 人工结项：任务不再计入「部分未完成」，原始失败原因保留在 message 里可追溯。
            # 只改任务状态，不动条目的 step/message —— 点进详情仍能看到当时为什么失败。
            # 条目也要落终态：只改任务状态的话，任务中心的「未识别 N 个」还挂着，
            # 用户会觉得「手动完结了还在提示未完成」。
            store.jobs.skip_job_items(job_id, '已人工结项')
            note = '（已人工结项，不再重试）'
            message = (row['message'] or '').strip()
            if note not in message:
                message = (message.rstrip('。') + '。' if message else '') + note
            app.state.organizer.update_job(job_id, state='completed', stage='已人工结项', message=message,
                                            notify_state='not_needed')
            store.event('organize', f'任务 {job_id}：已人工结项，不再计入未完成。')
            return job_public(app.state.organizer.job(job_id))
        if row['state'] not in active:
            raise HTTPException(409, '任务已结束。')
        if action == 'pause' and row['state'] in ('queued','running','waiting_login'):
            state, message = 'paused', '已请求暂停；在途操作核实后停止，保留已完成步骤。'
        elif action == 'resume' and row['state'] in ('paused','waiting_login','needs_review'):
            state, message = 'queued', '等待核查实际状态后继续。'
        elif action == 'cancel':
            state, message = 'cancelled', '已请求取消；不会回滚已完成操作，在途操作仍会核查。'
        else:
            raise HTTPException(409, '当前状态不支持此操作。')
        app.state.organizer.update_job(job_id, state=state, message=message)
        store.event('organize', f'任务 {job_id}：{message}')
        return job_public(app.state.organizer.job(job_id))

    @app.get('/api/v1/cloud/files', dependencies=[Depends(admin)])
    async def files(cid: str = Query('0', pattern=r'^\d{1,24}$'), offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=200)):
        return await app.state.cloud.list_files(int(cid), offset, limit)

    async def tmdb(path: str, params=None):
        key = store.get('tmdb_key')
        if not key:
            raise HTTPException(409, '请先保存 TMDB API Key。')
        try:
            response = await app.state.http.get('https://api.themoviedb.org/3/' + path,
                                               params={'api_key': key, 'language': 'zh-CN', **(params or {})})
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError):
            raise CloudError('TMDB 请求失败，请检查 API Key 和网络。', 'TMDB_UNAVAILABLE') from None

    @app.post('/api/v1/connections/tmdb/test', dependencies=[Depends(admin)])
    async def test_tmdb():
        await tmdb('configuration')
        store.event('tmdb', 'TMDB 连接检查通过。')
        return {'connected': True}

    @app.get('/api/v1/tmdb/search', dependencies=[Depends(admin)])
    async def search(q: str = Query(min_length=1, max_length=200), media_type: str = Query('movie', pattern='^(movie|tv)$')):
        data = await tmdb('search/' + media_type, {'query': q, 'include_adult': 'false'})
        return {'items': [{'id': r['id'], 'title': r.get('title') or r.get('name'),
                           'year': (r.get('release_date') or r.get('first_air_date') or '')[:4],
                           'overview': r.get('overview', '')[:400], 'media_type': media_type}
                          for r in data.get('results', [])[:20]]}

    async def emby(path: str, method='GET'):
        connection = app.state.organizer.emby.connection()
        url, key = connection or ('','')
        if not url or not key:
            raise HTTPException(409, '请先保存 Emby 地址和 API Key。')
        from .network import emby_request
        try:
            response = await emby_request(app.state.http,method,url + path,headers={'X-Emby-Token': key})
            response.raise_for_status()
            return response.json() if response.content else {}
        except (httpx.HTTPError, ValueError):
            raise CloudError('Emby 请求失败或结果不明；本次不会自动重试。', 'EMBY_UNAVAILABLE') from None

    class EmbyTest(Input):
        emby_url:str=Field(default='',max_length=1000)
        emby_key:str=Field(default='',max_length=500)

    @app.post('/api/v1/connections/emby/test', dependencies=[Depends(admin)])
    async def test_emby(body:EmbyTest=None):
        from .emby_connection import probe
        try:
            result=await probe(app.state.http,store,base_url(body.emby_url,True) if body and body.emby_url else None,body.emby_key if body else None)
            return {k:v for k,v in result.items() if k!='info'}
        except ValueError as exc:raise HTTPException(502,str(exc)) from None

    @app.post('/api/v1/emby/scan', dependencies=[Depends(admin)])
    async def scan_emby():
        store.event('emby', '手动扫描通知开始，本次仅尝试一次。')
        await emby('/Library/Refresh', 'POST')
        store.event('emby', 'Emby 已接受扫描通知；入库结果需在 Emby 确认。')
        return {'accepted': True, 'message': 'Emby 已接受扫描通知，请到 Emby 查看入库结果。'}

    @app.post('/api/v1/probe', dependencies=[Depends(admin)])
    async def probe_source(body: Source):
        if app.state.probe_gate.locked():
            raise HTTPException(409, '已有探测在执行，请等待完成。')
        async with app.state.probe_gate:
            file = await app.state.cloud.file(body.file_id)
            supported(file)
            ua = PROBE_UA
            url, headers = await app.state.cloud.url(file['pickcode'], ua)
            headers = {**headers, 'User-Agent': ua}
            started = time.monotonic()
            media = await probe(url, headers)
            store.event('probe', '远程媒体探测完成。')
            return {'file': file, 'media': media, 'elapsed_seconds': round(time.monotonic() - started, 2)}

    class Preview(Input):
        name: str = Field(min_length=1, max_length=400)
        folder: str | None = Field(default=None, max_length=800)
        tmdb_id: int | None = None
        use_tmdb: bool = True
        library: Literal['cloud', 'share'] = 'cloud'

    @app.post('/api/v1/strm/rebuild', dependencies=[Depends(admin)])
    async def rebuild_outputs(dry_run: int = Query(0, ge=0, le=1)):
        """按当前规则重写所有受管 STRM（重新整理本地输出，不动云端文件）。

        用途：命名/地址规则升级后，把历史 STRM 刷成当前规范形式（例如补上扩展名、
        换库根）。只改本地文件与台账，云端一个字节都不动。

        0.8.20 起：
        - `?dry_run=1` 只预览「会重写多少条、缺多少条」，不写任何文件；
        - 真正执行前把将被覆盖的旧内容快照进 `strm_rebuild_backups`，
          可用 `POST /api/v1/strm/rebuild/rollback` 一键还原；
        - 文件读写走线程池（见 rebuild.Rebuild.run），不再阻塞事件循环：原来
          892 行的同步循环会把播放取链与页面请求一起卡住。
        """
        if getattr(app.state.organizer, 'web_only', False):
            if not commands.worker_ready(store):
                raise HTTPException(503, '后台 Worker 未就绪，请稍后再试。')
            command_id = commands.submit(store, 'strm_rebuild', {'dry_run': bool(dry_run)})
            result = await commands.wait(store, command_id, timeout=600.0)
            if result is None:
                raise HTTPException(504, '重写仍在后台执行，请稍后查看状态。')
            if isinstance(result, dict) and result.get('failed') and 'message' in result:
                raise HTTPException(409, str(result.get('message') or '重写失败。'))
        else:
            try:
                result = await app.state.organizer.rebuild.run(dry_run=bool(dry_run))
            except RuntimeError as exc:
                raise HTTPException(409, str(exc)) from None
        store.event('strm', '重新整理本地输出（%s）：重写 %d 条，未变 %d 条，缺失 %d 条，失败 %d 条。'
                    % ('预览' if dry_run else '已执行', result['rewritten'], result['unchanged'],
                       result['missing'], result['failed']))
        return result

    @app.get('/api/v1/strm/rebuild/status', dependencies=[Depends(admin)])
    async def rebuild_status():
        """最近一次重建的状态（运行中/完成/已回滚）。页面可轮询它显示进度。"""
        return app.state.organizer.rebuild.status()

    @app.post('/api/v1/strm/rebuild/rollback', dependencies=[Depends(admin)])
    async def rebuild_rollback(backup_id: int = Query(0, ge=0)):
        """把最近一次（或指定）重建快照的本地字节写回，并同步台账指纹。"""
        if getattr(app.state.organizer, 'web_only', False):
            if not commands.worker_ready(store):
                raise HTTPException(503, '后台 Worker 未就绪，请稍后再试。')
            command_id = commands.submit(store, 'strm_rebuild_rollback',
                                         {'backup_id': int(backup_id or 0)})
            result = await commands.wait(store, command_id, timeout=300.0)
            if result is None:
                raise HTTPException(504, '回滚仍在后台执行，请稍后查看状态。')
            if isinstance(result, dict) and result.get('failed') and 'message' in result:
                message = str(result.get('message') or '回滚失败。')
                if '不存在' in message:
                    raise HTTPException(404, message)
                raise HTTPException(409, message)
            return result
        try:
            return await app.state.organizer.rebuild.rollback(backup_id or None)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from None

    @app.post('/api/v1/organize/preview', dependencies=[Depends(admin)])
    async def preview_organize(body: Preview):
        """用所选库的当前规则预览作品识别和分类。"""
        from .media import media_from_filename, parse_name, require_single_episode, technical_name
        name = body.name.strip()
        try:
            hint = parse_name(name, body.folder)
            if body.tmdb_id:
                hint['tmdb_id'] = int(body.tmdb_id)
            require_single_episode(hint, name)
            recognition_aliases.apply_alias(store, hint)
        except NeedsReview as exc:
            return {'name': name, 'ok': False, 'reason': str(exc), 'stage': 'parse', 'library': body.library}
        media = media_from_filename(name)
        result = {'name': name, 'ok': True, 'stage': 'parsed', 'hint': hint, 'library': body.library,
                  'technical': technical_name(media, name), 'source': 'filename',
                  'media': {k: media.get(k) for k in ('width', 'height', 'codec', 'dynamic_range',
                                                      'bit_depth', 'fps', 'dolby_vision_profile')},
                  'audio': media.get('audio') or []}
        if not body.use_tmdb:
            return result
        try:
            organizer = app.state.organizer
            details = await organizer.identify(hint)
            kind = hint['media_type']
            episodes = []
            if kind == 'tv':
                season = hint['season']
                for number in range(hint['episode'], hint['end_episode'] + 1):
                    if body.library == 'share':
                        episode = await organizer.tmdb(f'tv/{details["id"]}/season/{season}/episode/{number}')
                    else:
                        episode = await organizer.tmdb_episode(details['id'], season, number)
                    if episode is not None:
                        if episode.get('season_number') != season or episode.get('episode_number') != number:
                            raise NeedsReview('TMDB 季集映射不匹配。')
                        episodes.append(episode)
            if body.library == 'share':
                settings = share_effective_settings(store)
                config, policy = settings['organize'], settings['upgrade_policy']
            else:
                config, policy = store.get('organize_config', {}), public_policy(store.get('upgrade_policy'))
            config = await title_options(organizer, hint, details, config)
            try:
                parts, target_name, category, region = naming(
                    hint, details, episodes, media, name, policy, config)
            except NeedsReview as exc:
                # 预览只解析文件名，拿不到音轨；音频规则开着会必然失败。
                # 这里临时关掉音频规则再算一次，并说明「真实整理以 ffprobe 为准」。
                if '音轨' not in str(exc):
                    raise
                relaxed = {**policy, 'rules': [{**r, 'enabled': False} if r['key'] == 'audio' else r
                                               for r in policy['rules']]}
                parts, target_name, category, region = naming(
                    hint, details, episodes, media, name, relaxed, config)
                result['note'] = '文件名没有音轨信息，预览已临时关闭音频规则；真实整理以 ffprobe 探测为准。'
        except NeedsReview as exc:
            result.update({'ok': False, 'stage': 'tmdb', 'reason': str(exc)})
            return result
        decision = classification(details, kind, config)
        label = episode_label(hint) if kind == 'tv' else ''
        result.update({
            'stage': 'named', 'target_name': target_name, 'parts': parts,
            'category': category, 'region': region,
            'classification': {'category_reason': decision.category_reason, 'region_reason': decision.region_reason,
                               'genres': [{'id': g['id'], 'name': g.get('name') or ''}
                                          for g in details.get('genres', []) if 'id' in g]},
            'episode_label': label,
            'work': {'id': details.get('id'), 'title': details.get('title') or details.get('name'),
                     'year': (details.get('release_date') or details.get('first_air_date') or '')[:4]},
            'episode_title': (episodes[0].get('name') if len(episodes) == 1 else '') or '',
            'target_episode': label,
        })
        return result

    @app.post('/api/v1/playbacks', dependencies=[Depends(admin)])
    async def create_playback(body: Source):
        file = await app.state.cloud.file(body.file_id)
        supported(file)
        item_id, token = secrets.token_hex(16), ''
        row = {'id': item_id, 'file_id': file['id'], 'pickcode': file['pickcode'], 'name': file['name'],
               'relative_path': 'StrimKeep验证样例/' + safe_name(Path(file['name']).stem) + '-' + item_id[:8] + '.strm'}
        with store.db:
            store.db.execute('INSERT INTO playbacks(id,file_id,pickcode,name,relative_path,token_hash,created) VALUES(?,?,?,?,?,?,?)',
                             (*[row[k] for k in ('id', 'file_id', 'pickcode', 'name', 'relative_path')], digest(token), time.time()))
            url = write_strm(row, token)
        store.event('strm', '已生成验证样例 STRM，未修改云端文件。')
        return {**row, 'url': url}

    @app.get('/api/v1/playbacks', dependencies=[Depends(admin)])
    async def playbacks():
        return {'items': [dict(r) for r in store.db.execute(
            'SELECT id,file_id,name,relative_path,revoked,created FROM playbacks ORDER BY created DESC')]}

    @app.get('/api/v1/playbacks/{item_id}/strm', dependencies=[Depends(admin)])
    async def download_sample(item_id: str):
        row = row_for(item_id)
        path = managed_path(row['relative_path'])
        if not path.is_file():
            raise HTTPException(404, 'STRM 文件缺失，请重新生成播放入口以修复。')
        return FileResponse(path, media_type='application/octet-stream', filename=path.name)

    @app.post('/api/v1/playbacks/cleanup', dependencies=[Depends(admin)])
    async def cleanup_playbacks(include_active: bool = Query(False)):
        """清理没被任何在册输出引用的播放身份。

        现在 STRM 写的是 /play115/{pickcode}/...，取链不查库，playbacks 这张表实际
        只剩历史记账。默认只删「已撤销且无引用」的记录；include_active=1 时连未撤销
        的孤儿一起删。正在用的身份（output_registry.active=1 引用）一个都不动。
        """
        clause = '' if include_active else ' AND p.revoked=1'
        def local_playback_ids():
            from .identity import parse_url
            root = app.state.organizer.root
            if not root.is_dir():
                raise OSError('STRM 目录不可用')
            found = set()
            def failed(exc):
                raise exc
            for parent, dirs, files in os.walk(root, onerror=failed, followlinks=False):
                if any((Path(parent) / name).is_symlink() for name in dirs):
                    raise OSError('STRM 目录包含无法核验的符号链接目录')
                for name in files:
                    if not name.lower().endswith('.strm'):
                        continue
                    path = Path(parent) / name
                    if path.is_symlink():
                        raise OSError('STRM 目录包含无法核验的符号链接文件')
                    with path.open('r', encoding='utf-8') as stream:
                        value = stream.read(65537)
                    if len(value) > 65536:
                        raise OSError('STRM 文件内容过长')
                    identity = parse_url(value)
                    if identity and identity.get('kind') == 'playback':
                        found.add(identity['playback_id'])
            return found
        async with app.state.organizer.publication_gate:
            try:
                local_ids = await asyncio.to_thread(local_playback_ids)
            except (OSError, UnicodeError, ValueError):
                raise HTTPException(409, '无法完整核验本地 STRM，播放身份清理未执行。') from None
            rows = store.db.execute(
                'SELECT p.id, p.file_id, p.name FROM playbacks p WHERE NOT EXISTS ('
                'SELECT 1 FROM output_registry o WHERE o.playback_id=p.id AND o.active=1) '
                'AND NOT EXISTS (SELECT 1 FROM organized_files f WHERE f.playback_id=p.id)' + clause).fetchall()
            rows = [row for row in rows if row['id'] not in local_ids]
            if rows:
                with store.db:
                    for row in rows:
                        store.db.execute('DELETE FROM playbacks WHERE id=?', (row['id'],))
        if not rows:
            return {'removed': 0, 'kept': 0, 'message': '没有可清理的播放身份。'}
        store.event('play', '清理未被引用的播放身份 %d 条%s。'
                    % (len(rows), '' if include_active else '（仅已撤销）'))
        return {'removed': len(rows), 'kept': 0,
                'message': '已清理 %d 条未被 STRM 引用的播放身份。' % len(rows)}

    @app.post('/api/v1/playbacks/{item_id}/revoke', dependencies=[Depends(admin)])
    async def revoke(item_id: str):
        async with app.state.organizer.publication_gate:
            row_for(item_id)
            with store.db:
                store.db.execute('UPDATE playbacks SET revoked=1 WHERE id=?', (item_id,))
            store.event('token', '已撤销一个验证样例的播放入口。')
            return {'revoked': True}

    @app.post('/api/v1/playbacks/{item_id}/regenerate', dependencies=[Depends(admin)])
    async def regenerate(item_id: str):
        async with app.state.organizer.publication_gate:
            row = row_for(item_id)
            active = store.db.execute("SELECT 1 FROM organize_items i JOIN organize_jobs j ON i.job_id=j.id WHERE json_extract(i.plan,'$.playback_id')=? AND j.state IN ('queued','running','paused','waiting_login','needs_review')", (item_id,)).fetchone()
            if active:
                raise HTTPException(409, '该播放映射所属整理任务尚未结束，请先完成或取消任务再更新 Token。')
            if store.db.execute('SELECT 1 FROM playbacks WHERE relative_path=? AND id<>? AND revoked=0', (row['relative_path'], item_id)).fetchone():
                raise HTTPException(409, '该 STRM 路径已由其他有效版本接管，不能重新发布旧入口。')
            current = await app.state.cloud.file(row['file_id'])
            supported(current)
            if current['id'] != row['file_id']:
                raise HTTPException(409, '播放源文件身份不一致，不能更新 Token。')
            token = ''
            with store.db:
                store.db.execute('UPDATE playbacks SET token_hash=?,revoked=0 WHERE id=?', (digest(token), item_id))
                url = write_strm(row, token)
                for item in store.db.execute("SELECT id,plan FROM organize_items WHERE json_extract(plan,'$.playback_id')=?", (item_id,)).fetchall():
                    plan = json.loads(item['plan'])
                    plan['token_cipher'] = store.crypto.encrypt(token.encode()).decode()
                    plan['playback_base'] = store.get('playback_base')
                    plan.setdefault('local_files', {})[row['relative_path']] = digest(url + '\n')
                    store.jobs.set_item_plan(item['id'], plan)
            with store.db:
                for registry in store.db.execute('SELECT * FROM output_registry WHERE playback_id=?',(item_id,)).fetchall():
                    files = json.loads(registry['files']); files[row['relative_path']] = digest(url + '\n')
                    store.db.execute('UPDATE output_registry SET token_cipher=?,files=? WHERE account_id=? AND file_id=?',(store.crypto.encrypt(token.encode()).decode(),dumps(files),registry['account_id'],registry['file_id']))
                    store.db.execute('UPDATE owned_outputs SET digest=? WHERE relative_path=? AND account_id=? AND file_id=?',(digest(url+'\n'),row['relative_path'],registry['account_id'],registry['file_id']))
            return {'url': url}

    @app.post('/api/v1/emby/webhook')
    async def emby_webhook(request: Request, token: str = Query('', max_length=400)):
        """深度删除联动：接收 Emby 原生 / deep.delete 的删除事件（令牌鉴权，默认关闭）。"""
        secret = store.webhook_token()
        # 令牌可以放在查询串（Emby 的 Webhook 插件只认 URL），也接受请求头；
        # 头部方式不会落进反向代理日志，配置方支持时优先用头部。
        supplied = token or str(request.headers.get('x-strimkeep-token') or '')
        if not secret or not supplied or not hmac.compare_digest(secret.encode(), supplied[:400].encode()):
            raise HTTPException(403, '深度删除联动未启用或令牌无效。')
        busy = store.db.execute(
            "SELECT count(*) FROM organize_jobs WHERE state IN ('queued','running')"
            " AND json_extract(config,'$.job_kind') IN ('organize','transfer','strm_sync','share_organize')").fetchone()[0]
        if busy:
            raise HTTPException(409, '整理或转存任务执行中，为避免竞态请稍后重发删除事件。')
        try:
            payload = await request.json()
        except Exception:
            raise HTTPException(422, '事件体不是合法 JSON。')
        event = deep_delete.parse_event(payload)
        # 验收脚本会发一条假事件来验证鉴权：带上这个头就照常校验，但不写事件/面板。
        quiet = request.headers.get('x-strimkeep-acceptance') == '1'
        if not event:
            # 收到了但不是我们认的删除事件：也要留痕，否则排查时只能看到"没反应"。
            if not quiet:
                store.event('deep_delete', '深度删除：收到无法识别的事件 %s（未执行任何删除）'
                            % json.dumps(payload, ensure_ascii=False)[:800])
            return {'accepted': False,
                    'reason': '事件类型或字段不支持（只处理 Movie/Episode/Season/Series 的删除）'}
        if not event.get('path'):
            if not quiet:
                store.event('deep_delete', '深度删除：事件缺少具体文件或作品路径，仅有 TMDB 编号不能确认删除范围；未执行。')
            return {'accepted': False, 'matched': 0, 'reason': '缺少可核对的文件或作品路径，未执行删除。'}
        targets = deep_delete.match_targets(store, event)
        purged = 0
        if event.get('type') in ('Season', 'Series'):
            purged = deep_delete.purge_history(store, targets.get('candidates') or [], True)
            if purged and not quiet:
                store.event('deep_delete', '深度删除 · 已清除该作品/季 %d 条历史失效记录（revoked/已停用）。' % purged)
        matched = len(targets['managed']) + len(targets['share'])
        if not matched:
            path = event.get('path') or ('tmdb:%s' % event.get('tmdb'))
            if purged:
                if not quiet:
                    store.event('deep_delete', '深度删除 · 已由库存清理完成：该作品/季已无待删除记录（清理 %d 条历史记录）。' % purged)
                    store.set_meta('deep_delete_last', {'at': time.time(), 'mode': '已清理',
                                                        'path': path, 'errors': 0, 'details': []})
                return {'accepted': True, 'matched': 0, 'cleaned': True,
                        'reason': '库存台账已清理，无需重复删除'}
            if not quiet:
                store.event('deep_delete', '深度删除 · 未匹配：收到「%s」事件 %s，没有找到这条文件的取链记录（未执行任何删除）'
                            % (event['type'], path))
                store.set_meta('deep_delete_last', {'at': time.time(), 'mode': '未匹配',
                                                    'path': path, 'errors': 0, 'details': []})
            return {'accepted': True, 'matched': 0,
                    'reason': '没有找到这条文件的取链记录，未执行任何删除'}
        dry_run = bool((store.get('sync_config') or {}).get('deep_delete_dry_run', True))
        mode = '预演' if dry_run else '执行'
        source = event.get('path') or ('tmdb:%s' % event.get('tmdb'))
        # 只入队，立刻返回：100 集的整季删除也不会让 Emby 等到超时。
        batch, queued = deep_delete.enqueue(store, targets, dry_run, source)
        store.event('deep_delete', '深度删除 · 命中：整理 %d 条、分享 %d 条（%s）'
                    % (len(targets['managed']), len(targets['share']), mode))
        store.event('deep_delete', '深度删除 · 排队：%d 条已交给后台按序执行，单条都会回读确认（%s）'
                    % (queued, mode))
        deep_delete.progress(store, batch)
        return {'accepted': True, 'matched': matched, 'queued': queued, 'batch': batch,
                'dry_run': dry_run,
                'reason': '已入队，后台按序执行；进度见面板「最近一次深度删除」'}

    @app.get('/api/v1/emby/deep-delete/last', dependencies=[Depends(admin)])
    async def deep_delete_last():
        """最近一次深度删除的逐条明细（面板展示用）。"""
        return store.meta_json('deep_delete_last', {'at': 0, 'details': []})

    @app.get('/api/v1/emby/deep-delete/history', dependencies=[Depends(admin)])
    async def deep_delete_history(limit: int = 20):
        """最近的若干批深度删除（含逐条明细），面板「历史」用。"""
        return deep_delete.history(store, limit)

    @app.post('/api/v1/emby/webhook/selftest', dependencies=[Depends(admin)])
    async def webhook_selftest():
        """链路自检：只读，用台账里的真实路径跑一遍匹配，永远不删任何东西。

        Emby 那头配没配、令牌换没换都不影响它——用来快速回答"我们这侧正常吗"。
        """
        config = store.get('sync_config') or {}
        report = deep_delete.selftest(store)
        report['dry_run'] = bool(config.get('deep_delete_dry_run', True))
        report['token_ready'] = bool(store.webhook_token())
        last = store.db.execute(
            "SELECT at, message FROM events WHERE message LIKE '深度删除%' ORDER BY id DESC LIMIT 1").fetchone()
        report['last_received'] = last['message'] if last else ''
        report['last_received_at'] = last['at'] if last else 0
        return report

    def log_play(message):
        """播放日志：写库失败绝不能影响播放本身。"""
        try:
            store.event('play', message)
        except Exception:
            pass

    @app.api_route('/play/{item_id}', methods=['GET', 'HEAD'])
    async def play(item_id: str, request: Request, token: str = Query('', max_length=200)):
        row = store.db.execute('SELECT * FROM playbacks WHERE id=?', (item_id,)).fetchone()
        if not row or row['revoked']:
            raise HTTPException(403, '播放入口不存在或已撤销。')
        # Bound live requests per playback identity before calling the 115 API.
        recent = app.state.play_limits[item_id]
        now = time.monotonic()
        while recent and recent[0] < now - 60:
            recent.popleft()
        if len(recent) >= 60:
            raise HTTPException(429, '播放请求过于频繁，请稍后再试。')
        recent.append(now)
        # Keep the per-item limiter bounded for long-running instances.
        if len(app.state.play_limits) > 512:
            for key, attempts in list(app.state.play_limits.items()):
                while attempts and attempts[0] < now - 60:
                    attempts.popleft()
                if not attempts:
                    app.state.play_limits.pop(key, None)
        ua = request.headers.get('user-agent', '')
        if '\r' in ua or '\n' in ua:
            raise HTTPException(422, 'UA 请求头无效。')
        play_token = playback_request.set(True)
        play_started = time.monotonic()
        outcome = 'error'   # 0.8.22：取链结果进 /metrics（ok / error / timeout）
        try:
            async with asyncio.timeout(35):
                for attempt in range(2):
                    try:
                        url, _ = await app.state.cloud.url(row['pickcode'], ua)
                        # 第四层状态：播放器实际取链成功。按入口节流记录，避免刷屏。
                        logged = app.state.play_logged.get(item_id, 0.0)
                        if time.monotonic() - logged > 60:
                            app.state.play_logged[item_id] = time.monotonic()
                            store.event('play', f'播放入口取链成功：{row["name"][:60]}（入口 {item_id[:8]}…）')
                        outcome = 'ok'
                        return RedirectResponse(url, status_code=302)
                    except CloudError as exc:
                        if exc.code in ('LOGIN_REQUIRED', 'LOGIN_INVALID') or attempt == 1:
                            raise
                        await asyncio.sleep(1)
        except TimeoutError:
            outcome = 'timeout'
            raise CloudError('播放取链超过时限，请重试。', 'PLAY_TIMEOUT', 504) from None
        finally:
            playback_request.reset(play_token)
            app.state.metrics.record_play(outcome, (time.monotonic() - play_started) * 1000)

    @app.api_route('/play/{item_id}/{slug:path}', methods=['GET', 'HEAD'])
    async def play_slugged(item_id: str, request: Request, slug: str = '', token: str = Query('', max_length=200)):
        # 可读别名段：STRM 里写成 /play/{id}/{文件名}，播放器路径栏能看出是什么片。
        # slug 只作展示，路由按 item_id 解析；分享库走 /play115/share/，不经过这里。
        return await play(item_id=item_id, request=request, token=token)

    @app.api_route('/play115/{pickcode}/{fid}/{sha1}/{size}/{name:path}',
                   methods=['GET', 'HEAD'])
    async def play115(pickcode: str, fid: str, sha1: str, size: str, name: str,
                      request: Request, token: str = Query('', max_length=200)):
        ua = request.headers.get('user-agent', '')
        if '\r' in ua or '\n' in ua:
            raise HTTPException(422, 'UA 请求头无效。')
        key = 'p115:' + pickcode
        recent = app.state.play_limits[key]
        now = time.monotonic()
        while recent and recent[0] < now - 60:
            recent.popleft()
        if len(recent) >= 60:
            raise HTTPException(429, '播放请求过于频繁，请稍后再试。')
        recent.append(now)
        if len(app.state.play_limits) > 512:
            for k, attempts in list(app.state.play_limits.items()):
                while attempts and attempts[0] < now - 60:
                    attempts.popleft()
                if not attempts:
                    app.state.play_limits.pop(k, None)
        play_token = playback_request.set(True)
        play_started = time.monotonic()
        outcome = 'error'   # 0.8.22：取链结果进 /metrics（ok / error / timeout）
        started = time.monotonic()
        try:
            async with asyncio.timeout(35):
                for attempt in range(2):
                    try:
                        url, _ = await app.state.cloud.url(pickcode, ua)
                        # /play115 是「身份写在 STRM 里」的通道，以前成功失败都不留痕，
                        # 出问题只能看现象。按入口节流记录，避免播放器重连刷屏。
                        if time.monotonic() - app.state.play_logged.get(key, 0.0) > 60:
                            app.state.play_logged[key] = time.monotonic()
                            log_play('播放取链成功：%s（pickcode %s…，%.2fs）'
                                     % (ua_label(ua), pickcode[:8], time.monotonic() - started))
                        outcome = 'ok'
                        return RedirectResponse(url, status_code=302)
                    except CloudError as exc:
                        if exc.code in ('LOGIN_REQUIRED', 'LOGIN_INVALID') or attempt == 1:
                            log_play('播放取链失败：%s（pickcode %s…，UA %s）'
                                     % (exc.code, pickcode[:8], ua_label(ua)))
                            raise
                        await asyncio.sleep(1)
        except TimeoutError:
            outcome = 'timeout'
            log_play('播放取链超时（pickcode %s…，UA %s）' % (pickcode[:8], ua_label(ua)))
            raise CloudError('播放取链超过时限，请重试。', 'PLAY_TIMEOUT', 504) from None
        finally:
            playback_request.reset(play_token)
            app.state.metrics.record_play(outcome, (time.monotonic() - play_started) * 1000)

    @app.get('/api/v1/events', dependencies=[Depends(admin)])
    async def events(before: int = Query(2**62, ge=1), limit: int = Query(50, ge=1, le=200)):
        return {'items': [dict(r) for r in store.db.execute(
            'SELECT * FROM events WHERE id<? ORDER BY id DESC LIMIT ?', (before, limit))]}

    static = Path(os.environ.get('STRIMKEEP_STATIC_DIR', str(Path(__file__).resolve().parents[2] / 'frontend/dist')))
    if static.is_dir():
        app.mount('/', GZipMiddleware(StaticFiles(directory=static, html=True),minimum_size=1024,compresslevel=4), name='frontend')
    return app
