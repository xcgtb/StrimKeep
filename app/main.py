# -*- coding: utf-8 -*-
"""StrimKeep Web 层 —— FastAPI 应用装配。

约 40 个路由按页面域拆到 app/routers/（auth/system/governance/library/subscribe/settings），
本文件只保留：启动密码检查（deps）、CD2 看门狗、应用生命周期、静态挂载与路由装配。
main.app / main.auth / main.engine 等旧导入路径全部保留（测试和 CLI 在用）。
"""
import os, sys, logging, threading
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import engine, bot, logger, scheduler, tasks  # noqa: F401  (兼容旧测试/脚本：main.engine 等)
from app import config as _cfg  # noqa: F401
from app import security as _sec
from app import state_store as _state
from app.routers.deps import (  # noqa: F401
    auth, spawn, Args, STATIC_DIR,
    WEB_USER, WEB_PASSWORD, ALLOW_NO_AUTH, APP_VERSION,
    SESSION_COOKIE, SESSION_DAYS, _ALLOWED_HOSTS,
)
from app.routers import auth as _r_auth
from app.routers import system as _r_system
from app.routers import governance as _r_governance
from app.routers import library as _r_library
from app.routers import subscribe as _r_subscribe
from app.routers import settings as _r_settings


def _cd2_ready():
    """检查 CD2 是否就绪"""
    check_dirs = {'电影', '剧集', '儿童节目', '综艺', '动漫', '纪录片', '演唱会'}
    try:
        cd2 = engine.CLOUD_L_ROOT
        if not cd2.exists():
            return False
        dirs = {i.name for i in cd2.iterdir() if i.is_dir()}
        return bool(dirs & check_dirs)
    except OSError:
        return False


def _cd2_watchdog(max_wait=60, max_retries=5):
    """CD2 启动守门员：未就绪则退出容器让 Docker 重启"""
    import time as _t
    _logger = logging.getLogger('strimkeep')
    counter_file = engine.DATA_DIR / '.cd2_retry_count'

    retry_path = engine.STATE_DIR / 'cd2_watchdog.json'
    saved = _state.read(retry_path, {})
    if not saved.get('legacy_imported'):
        try:
            stamp, count = counter_file.read_text().strip().split(':')
            saved = {'ts': float(stamp), 'count': int(count)}
        except (OSError, ValueError):
            saved = {}
        saved['legacy_imported'] = True
        _state.save(retry_path, saved)
    count = int(saved.get('count') or 0) if _t.time() - float(saved.get('ts') or 0) < 300 else 0

    start = _t.time()
    while _t.time() - start < max_wait:
        if _cd2_ready():
            _logger.info('CD2 挂载就绪（等 %.1f 秒，重试次数 %d）', _t.time() - start, count)
            _state.save(retry_path, {'legacy_imported': True, 'count': 0})
            return
        _t.sleep(2)

    count += 1
    if count >= max_retries:
        _logger.error('CD2 挂载 %d 秒内未就绪，已重试 %d 次，放弃等待', max_wait, count)
        _state.save(retry_path, {'legacy_imported': True, 'count': 0})
        return

    _state.save(retry_path, {'legacy_imported': True, 'ts': _t.time(), 'count': count})

    _logger.warning('CD2 挂载未就绪，第 %d/%d 次重试，退出容器让 Docker 重启', count, max_retries)
    _t.sleep(2)
    os._exit(42)


def _startup():
    """进程级副作用统一在这里启动（以前散落在模块导入时，测试/工具一 import 就起线程）"""
    _log = logging.getLogger('strimkeep')
    try:
        _m = engine.db_migrate()
        engine.reload_config()
        _log.info('SQLite 单存储就绪：%s', _m)
    except Exception as error:
        _log.error('SQLite 迁移失败，后台任务未启动：%s', error)
        return
    # CD2 启动守门员：仅在显式开启时运行
    if os.environ.get('ENABLE_CD2_WATCHDOG', '0').strip().lower() in ('1', 'true', 'yes', 'on'):
        threading.Thread(target=_cd2_watchdog, daemon=True, name='cd2-watchdog').start()
    try:
        bot.start()
    except Exception as e:
        _log.warning('Bot 启动失败: %s', e)
    scheduler.start()


def _shutdown():
    scheduler.stop()
    bot.stop()


@asynccontextmanager
async def lifespan(_app):
    _startup()
    try:
        yield
    finally:
        _shutdown()


app = FastAPI(title='StrimKeep', version=APP_VERSION, lifespan=lifespan)

@app.middleware('http')
async def _security_mw(request: Request, call_next):
    """CSRF 同源校验 + 安全响应头。"""
    if request.url.path.startswith('/api/') and _sec.csrf_blocked(
            request.method, request.headers, bool(request.cookies.get(SESSION_COOKIE)), _ALLOWED_HOSTS):
        return JSONResponse({'status': 'error', 'detail': '跨站请求被拒绝（Origin 校验失败）。'
                             '如通过反向代理访问，请设置 ALLOWED_ORIGINS。'}, status_code=403)
    resp = await call_next(request)
    for k, v in _sec.SECURITY_HEADERS.items():
        resp.headers.setdefault(k, v)
    return resp


if STATIC_DIR.exists():
    app.mount('/static', StaticFiles(directory=str(STATIC_DIR)), name='static')

# 路由装配：顺序无关（路径互不冲突），分组见 app/routers/__init__.py
app.include_router(_r_auth.router)
app.include_router(_r_system.router)
app.include_router(_r_governance.router)
app.include_router(_r_library.router)
app.include_router(_r_subscribe.router)
app.include_router(_r_settings.router)


if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='0.0.0.0', port=8321)
