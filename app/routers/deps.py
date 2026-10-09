# -*- coding: utf-8 -*-
"""路由共享依赖：鉴权、会话、任务派发、公共常量。

所有 APIRouter 共用同一个 auth/spawn/会话签名实现，保证拆路由前后行为一致。
WEB_PASSWORD 的启动检查保留在本模块导入时执行（与旧 main.py 的导入时序一致：
engine 等模块先导入，密码检查后执行），子进程测试依赖这一行为。
"""
import os, secrets, logging
from pathlib import Path
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from app.version import __version__

try:
    from app import engine, bot, logger, scheduler, tasks  # noqa: F401
    from app import config as _cfg  # noqa: F401
    from app import security as _sec
except ImportError:
    import engine, bot, logger, scheduler, tasks  # noqa: F401
    import config as _cfg  # noqa: F401
    import security as _sec


WEB_USER = (os.environ.get('WEB_USER', '').strip() or 'admin')
WEB_PASSWORD = os.environ.get('WEB_PASSWORD', '').strip()
ALLOW_NO_AUTH = os.environ.get('ALLOW_NO_AUTH', '').strip() == '1'

if not WEB_PASSWORD:
    _msg = (
        '未设置 WEB_PASSWORD：Web 管理台不做任何鉴权，'
        '任何能访问到这个端口的人都可以直接登录并执行清理。'
    )
    if ALLOW_NO_AUTH:
        logging.getLogger('strimkeep').warning('%s 已通过 ALLOW_NO_AUTH=1 显式放行，继续启动。', _msg)
    else:
        logging.getLogger('strimkeep').error(
            '%s 拒绝启动。请设置 WEB_USER / WEB_PASSWORD，'
            '或明确知道风险后设置 ALLOW_NO_AUTH=1 跳过此检查。', _msg
        )
        raise SystemExit(1)

# 源码、本地构建默认显示发布版本；镜像构建可显式覆盖。
APP_VERSION = os.environ.get('APP_VERSION', '').strip() or __version__


_security = HTTPBasic(auto_error=False)

# ── Cookie 会话登录 ──
# 原来只有浏览器原生 Basic 弹框，手机 Safari 经常不记住，每次都要重输。
# 现在登录一次后写入 30 天有效的签名 Cookie；Basic 头依然兼容（脚本/curl 可继续用）。
SESSION_COOKIE = 'strimkeep_session'
SESSION_DAYS = int(os.environ.get('SESSION_DAYS', '30') or 30)
_SESSION_KEY = (os.environ.get('SESSION_SECRET', '').strip() or WEB_PASSWORD or 'noauth').encode()


def _sign_session(user: str, exp: int) -> str:
    return _sec.sign_session(_SESSION_KEY, user, exp)


def _verify_session(token: str) -> bool:
    return _sec.verify_session(_SESSION_KEY, WEB_USER, token)


# ── CSRF + 安全响应头（中间件在 main.py 注册，白名单在这里解析）──
# 反向代理改写了 Host 且没传 X-Forwarded-Host 时，用 ALLOWED_ORIGINS 补白名单（逗号分隔）。
_ALLOWED_HOSTS = _sec.parse_allowed_origins(os.environ.get('ALLOWED_ORIGINS', ''))


def auth(request: Request = None, credentials: HTTPBasicCredentials = Depends(_security)):
    # 保持对旧式单元测试/内部调用的兼容：auth(credentials)。
    if isinstance(request, HTTPBasicCredentials) and not isinstance(credentials, HTTPBasicCredentials):
        credentials, request = request, None
    if not isinstance(credentials, HTTPBasicCredentials):
        credentials = None
    if not WEB_PASSWORD:
        return True  # 仅当 ALLOW_NO_AUTH=1 放行启动时才会走到这里
    tok = request.cookies.get(SESSION_COOKIE) if request is not None else None
    if tok and _verify_session(tok):
        return True
    ok = bool(credentials) and \
        secrets.compare_digest(credentials.username, WEB_USER) and \
        secrets.compare_digest(credentials.password, WEB_PASSWORD)
    if not ok:
        # 故意不返回 WWW-Authenticate：否则浏览器会弹原生登录框，前端改用自带登录页
        raise HTTPException(status_code=401, detail='未登录或登录已过期')
    return True


# ═══════════════════ 任务系统 ═══════════════════
# 任务登记与互斥在 app/tasks.py，Web / Bot / 定时巡检共用同一个 manager
class Args:
    def __init__(self, kw='', plan='', dry_run=False, **kwargs):
        self.kw = kw; self.plan = plan; self.dry_run = dry_run
        for k, v in kwargs.items(): setattr(self, k, v)


def spawn(kind, fn, *fargs, **kwargs):
    try:
        return tasks.manager.spawn(kind, fn, *fargs, source='web', **kwargs)
    except tasks.TaskBusy as e:
        raise HTTPException(429, str(e))


def _current_task_dict():
    cur = tasks.manager.running()
    return cur.to_dict(with_logs=False) if cur else None


STATIC_DIR = Path(__file__).parent.parent.parent / 'static'
