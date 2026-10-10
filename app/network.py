# -*- coding: utf-8 -*-
"""Per-request external-service HTTP proxy; never install a global opener."""
import base64
import urllib.error
import urllib.parse
import urllib.request

try:
    from . import config as _cfg
except ImportError:
    import config as _cfg

PROXY_KEYS = ('http_proxy_enabled', 'http_proxy_url', 'http_proxy_username', 'http_proxy_password')


class _ExternalProxyHandler(urllib.request.ProxyHandler):
    def proxy_open(self, request, proxy, protocol):
        # Explicit Web choice applies only to external-service callers and takes
        # precedence over NO_PROXY; local Emby never uses this opener.
        parsed = urllib.parse.urlsplit(proxy)
        if parsed.username:
            credentials = urllib.parse.unquote(parsed.username) + ':' + urllib.parse.unquote(parsed.password or '')
            request.add_header('Proxy-Authorization', 'Basic ' + base64.b64encode(credentials.encode()).decode('ascii'))
        request.set_proxy(parsed.netloc.rsplit('@', 1)[-1], 'http')
        return None


def proxy_config(overrides=None, saved=None):
    cfg = dict(_cfg.load_config() if saved is None else saved)
    body = overrides or {}
    old_url = str(cfg.get('http_proxy_url') or '').rstrip('/')
    old_user = str(cfg.get('http_proxy_username') or '')
    for key in PROXY_KEYS:
        if key in body:
            value = str(body[key]).strip()
            if key == 'http_proxy_password' and _cfg.is_masked_value(value):
                continue
            cfg[key] = value
    enabled = str(cfg.get('http_proxy_enabled') or '0').lower()
    if enabled not in ('0', '1', 'false', 'true'):
        raise ValueError('代理开关必须为开启或关闭')
    cfg['http_proxy_enabled'] = '1' if enabled in ('1', 'true') else '0'
    url = str(cfg.get('http_proxy_url') or '').strip()
    if url:
        try:
            parsed = urllib.parse.urlsplit(url)
            port = 80 if parsed.port is None else parsed.port
            if (parsed.scheme != 'http' or not parsed.hostname or
                    parsed.username is not None or parsed.password is not None or
                    parsed.path not in ('', '/') or parsed.query or parsed.fragment or
                    any(ch.isspace() or ord(ch) < 32 for ch in url) or not 1 <= port <= 65535):
                raise ValueError()
        except ValueError:
            raise ValueError('代理地址格式错误，请填写 http://IP或域名:端口；鉴权请使用下方用户名和密码') from None
        cfg['http_proxy_url'] = url.rstrip('/')
    elif cfg['http_proxy_enabled'] == '1':
        raise ValueError('开启 HTTP 代理前请填写代理地址')
    if ':' in str(cfg.get('http_proxy_username') or ''):
        raise ValueError('代理用户名不能包含冒号')
    changed = old_url != str(cfg.get('http_proxy_url') or '') or old_user != str(cfg.get('http_proxy_username') or '')
    if changed and (saved or {}).get('http_proxy_password') and (
            'http_proxy_password' not in body or _cfg.is_masked_value(str(body.get('http_proxy_password') or ''))):
        raise ValueError('更换代理地址或用户名后请重新填写代理密码，无需鉴权时可清空密码')
    return cfg


def open_external(request, timeout=10, config=None):
    cfg = proxy_config(saved=config) if config is not None else proxy_config()
    if cfg['http_proxy_enabled'] != '1':
        # Preserve existing HTTP(S)_PROXY environment support when Web proxy is off.
        return urllib.request.urlopen(request, timeout=timeout)
    url = cfg['http_proxy_url']
    username = cfg.get('http_proxy_username') or ''
    password = cfg.get('http_proxy_password') or ''
    if username:
        auth = urllib.parse.quote(username, safe='') + ':' + urllib.parse.quote(password, safe='')
        url = 'http://' + auth + '@' + url[len('http://'):]
    opener = urllib.request.build_opener(_ExternalProxyHandler({'http': url, 'https': url}))
    try:
        return opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        reason = 'HTTP 代理认证失败，请检查用户名和密码' if error.code == 407 else '外部服务返回 HTTP %d' % error.code
        raise urllib.error.HTTPError(error.url, error.code, reason, error.headers, error.fp) from None
    except (TimeoutError, urllib.error.URLError, OSError) as error:
        if '407' in str(getattr(error, 'reason', error)):
            raise urllib.error.URLError('HTTP 代理认证失败，请检查用户名和密码') from None
        if isinstance(error, TimeoutError) or isinstance(getattr(error, 'reason', None), TimeoutError):
            raise TimeoutError('HTTP 代理连接超时，请检查地址、端口和网络') from None
        raise urllib.error.URLError('HTTP 代理连接失败，请检查地址、端口、鉴权和网络') from None
