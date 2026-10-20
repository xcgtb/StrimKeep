"""One explicit network route for Telegram preview and MTProto readers."""
from urllib.parse import quote, urlsplit, unquote
from urllib.request import getproxies, proxy_bypass
from functools import wraps
import threading

from app import network
from app.cloud.errors import CloudError

_PROBES = threading.BoundedSemaphore(2)


def probe(function):
    @wraps(function)
    def run(*args, **kwargs):
        if not _PROBES.acquire(blocking=False):
            raise CloudError('已有频道读取检查进行中，请等待当前检查结束后重试', 429)
        try:
            return function(*args, **kwargs)
        finally:
            _PROBES.release()
    return run


def route():
    try:
        cfg = network.proxy_config()
    except ValueError:
        raise CloudError('Telegram 代理配置无效，请检查服务连接中的代理地址与鉴权', 409) from None
    url, source = '', 'direct'
    if cfg['http_proxy_enabled'] == '1':
        url, source = cfg['http_proxy_url'], 'settings'
        if cfg.get('http_proxy_username'):
            auth = quote(cfg['http_proxy_username'], safe='') + ':' + quote(cfg.get('http_proxy_password') or '', safe='')
            url = 'http://' + auth + '@' + url[len('http://'):]
    elif not proxy_bypass('t.me'):
        proxies = getproxies()
        url = proxies.get('https') or proxies.get('all') or ''
        if url:
            source = 'environment'
    if url:
        try:
            p = urlsplit(url)
            if p.scheme not in ('http', 'https', 'socks5', 'socks5h', 'socks4') or not p.hostname:
                raise ValueError()
            port = p.port or (443 if p.scheme == 'https' else 80 if p.scheme == 'http' else 1080)
        except ValueError:
            raise CloudError('Telegram 代理地址无效，请检查服务连接与环境代理配置', 409) from None
    else:
        p, port = None, None
    return {'url': url, 'source': source, 'parsed': p, 'port': port}


def describe():
    value = route()
    return {'mode': value['source'], 'label': {'direct': '直接连接 Telegram', 'settings': '使用服务连接中的 HTTP 代理',
                                              'environment': '使用容器环境代理'}[value['source']],
            'remote_dns': bool(value['url'])}


def http_options():
    value = route()
    # A fixed t.me target is resolved by the configured proxy; a local DNS
    # preflight must not reject a valid remote-DNS proxy route.
    return {'proxy': value['url'] or None, 'trust_env': False}


def telegram_proxy():
    value = route()
    p = value['parsed']
    if p is None:
        return None
    if p.scheme == 'https':
        raise CloudError('Telegram 用户会话请使用 HTTP 或 SOCKS 代理端口；当前 HTTPS 代理不受支持', 409)
    return {'proxy_type': 'http' if p.scheme == 'http' else 'socks4' if p.scheme == 'socks4' else 'socks5',
            'addr': p.hostname, 'port': value['port'], 'username': unquote(p.username or '') or None,
            'password': unquote(p.password or '') or None, 'rdns': True}
