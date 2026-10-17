# -*- coding: utf-8 -*-
"""登录 / 登出 / 首页"""
import asyncio, time
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse

try:
    from app.routers.deps import (
        WEB_USER, WEB_PASSWORD, SESSION_COOKIE, SESSION_DAYS,
        _sign_session, STATIC_DIR,
    )
except ImportError:
    from routers.deps import (
        WEB_USER, WEB_PASSWORD, SESSION_COOKIE, SESSION_DAYS,
        _sign_session, STATIC_DIR,
    )

import secrets

router = APIRouter()


@router.post('/api/login')
async def api_login(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    u = str(body.get('username', '')); p = str(body.get('password', ''))
    if not WEB_PASSWORD or (secrets.compare_digest(u, WEB_USER) and secrets.compare_digest(p, WEB_PASSWORD)):
        exp = int(time.time()) + SESSION_DAYS * 86400
        resp = JSONResponse({'status': 'success'})
        secure = (request.headers.get('x-forwarded-proto', request.url.scheme) == 'https')
        resp.set_cookie(SESSION_COOKIE, _sign_session(WEB_USER, exp), max_age=SESSION_DAYS * 86400,
                        httponly=True, samesite='lax', secure=secure, path='/')
        return resp
    await asyncio.sleep(1)  # 简单减缓暴力猜测（async 端点里不能 time.sleep，会卡住整个事件循环）
    return JSONResponse({'status': 'error', 'detail': '用户名或密码错误'}, status_code=401)


@router.post('/api/logout')
def api_logout():
    resp = JSONResponse({'status': 'success'})
    resp.delete_cookie(SESSION_COOKIE, path='/')
    return resp


@router.get('/')
def index():
    idx = STATIC_DIR / 'classic' / 'index.html'
    if not idx.exists():
        return JSONResponse({'error': f'前端文件不存在: {idx}'}, status_code=500)
    return FileResponse(idx, headers={
        'Cache-Control': 'no-cache, no-store, must-revalidate',
        'Pragma': 'no-cache',
        'Expires': '0',
    })


@router.get('/workspace/embed', include_in_schema=False)
def embedded_cloud_workspace():
    """Vue 3 cloud-only workspace within the classic first-party application.

    CSS injection is scoped to this endpoint, allowing both prebuilt source
    bundles and Docker-recompiled Vue assets to hide the second navigation.
    """
    idx = STATIC_DIR / 'index.html'
    if not idx.exists():
        return JSONResponse({'error': '115 工作台前端构建文件不存在'}, status_code=500)
    html = idx.read_text(encoding='utf-8')
    hide_shell = """<style id="classic-cloud-embed">
        .app-shell > .sidebar, .app-shell > .nav-scrim,
        .app-shell > .workspace > .topbar, .app-shell .mobile-dock,
        .app-shell > .workspace > .page-footer {display:none!important}
        .app-shell, .app-shell > .workspace {width:100%!important;max-width:none!important;
            min-height:0!important;margin:0!important;padding:0!important}
        .app-shell > .workspace > .page {max-width:none!important;padding:12px!important}
        </style>"""
    if '</head>' not in html:
        return JSONResponse({'error': '115 工作台前端入口无效'}, status_code=500)
    html = html.replace('</head>', hide_shell + '</head>', 1)
    return HTMLResponse(html, headers={'Cache-Control': 'no-store'})


@router.get('/favicon.ico', include_in_schema=False)
def favicon():
    """浏览器自动发现的收藏夹图标，登录前也可访问。"""
    return FileResponse(STATIC_DIR / 'icons/favicon.ico', media_type='image/vnd.microsoft.icon',
                        headers={'Cache-Control': 'public, max-age=86400'})
