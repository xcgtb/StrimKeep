# -*- coding: utf-8 -*-
"""Web 层安全工具（纯函数，不依赖 FastAPI，可直接单测）。

从 main.py 拆出：
  - 会话 Cookie 的签名 / 校验
  - CSRF：对「带会话 Cookie 的写请求」做 Origin / Referer 同源校验
  - 通用安全响应头
"""
import hashlib
import hmac
import time
from urllib.parse import urlsplit

SAFE_METHODS = frozenset({'GET', 'HEAD', 'OPTIONS', 'TRACE'})

# 只收紧「不会破坏现有功能」的头：
#  - frame-ancestors / X-Frame-Options：禁止被别的站点 iframe 嵌入（点击劫持）
#  - object-src / base-uri / form-action：堵住常见注入落点
# 前端含大量内联脚本，因此刻意不设置 script-src / default-src。
SECURITY_HEADERS = {
    'X-Content-Type-Options': 'nosniff',
    'X-Frame-Options': 'DENY',
    'Referrer-Policy': 'same-origin',
    'Content-Security-Policy': "frame-ancestors 'none'; object-src 'none'; base-uri 'self'; form-action 'self'",
}


# ───────────── 会话签名 ─────────────
def sign_session(key: bytes, user: str, exp: int) -> str:
    msg = ('%s|%d' % (user, exp)).encode()
    sig = hmac.new(key, msg, hashlib.sha256).hexdigest()
    return '%s|%d|%s' % (user, exp, sig)


def verify_session(key: bytes, web_user: str, token: str, now=None) -> bool:
    try:
        user, exp, sig = token.split('|')
        if int(exp) < (time.time() if now is None else now):
            return False
        good = hmac.new(key, ('%s|%s' % (user, exp)).encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, good) and hmac.compare_digest(user, web_user)
    except Exception:
        return False


# ───────────── CSRF ─────────────
def _host_of(value: str) -> str:
    """从 Origin / Referer 里取 host[:port]（小写）；解析失败返回空串。"""
    try:
        return (urlsplit(value).netloc or '').lower()
    except ValueError:
        return ''


def parse_allowed_origins(raw: str) -> frozenset:
    """ALLOWED_ORIGINS 环境变量：逗号分隔，可写 `https://nas.example.com` 或 `nas.example.com`。"""
    out = set()
    for item in (raw or '').split(','):
        item = item.strip().lower()
        if not item:
            continue
        out.add(_host_of(item) if '://' in item else item)
    return frozenset(out)


def csrf_blocked(method: str, headers, has_session_cookie: bool, extra_hosts=frozenset()) -> bool:
    """True 表示应拒绝该请求（疑似跨站请求）。

    规则（只管「浏览器会自动带上的凭据」——会话 Cookie）：
      - 读请求、或没带会话 Cookie 的请求：放行（Basic 头不会被浏览器自动附带，脚本 / curl 不受影响）
      - 带 Cookie 的写请求：Origin（没有则看 Referer）的 host 必须等于 Host / X-Forwarded-Host，
        或在 ALLOWED_ORIGINS 里；两者都缺失 → 拒绝（现代浏览器的跨站 POST 一定带 Origin）
    `headers` 需支持 `.get(name)`（Starlette 的 Headers 大小写不敏感；普通 dict 请用小写键）。
    """
    if method.upper() in SAFE_METHODS or not has_session_cookie:
        return False
    if headers.get('authorization'):
        return False
    origin = headers.get('origin')
    if origin is None:
        referer = headers.get('referer')
        if not referer:
            return True
        src = _host_of(referer)
    else:
        if origin == 'null':
            return True
        src = _host_of(origin)
    if not src:
        return True
    allowed = {h.strip().lower() for h in (headers.get('x-forwarded-host') or '').split(',') if h.strip()}
    host = (headers.get('host') or '').strip().lower()
    if host:
        allowed.add(host)
    allowed |= set(extra_hosts)
    return src not in allowed
