"""Read-only Emby discovery with explicit errors and a connection-bound API base."""
from urllib.parse import urlsplit,urlunsplit
from .network import emby_request,is_local_host
import httpx
from .store import digest

def fingerprint(url,key):return digest(url.rstrip('/')+'|'+key)

def saved_base(store):
    url,key=store.get('emby_url'),store.get('emby_key')
    if not url or not key:return None
    if store.get('emby_api_fingerprint')==fingerprint(url,key) and store.get('emby_api_base'):
        return store.get('emby_api_base'),key
    return url.rstrip('/') if url.rstrip('/').endswith('/emby') else url.rstrip('/')+'/emby',key

def error_message(exc):
    if isinstance(exc,httpx.HTTPStatusError):
        status=exc.response.status_code
        return f'Emby 返回 HTTP {status}：'+('API Key 无效或权限不足。' if status in (401,403) else '接口路径不存在，请检查服务地址或反向代理路径。' if status in (404,405) else '服务端请求失败。')
    if isinstance(exc,httpx.TimeoutException):return '访问 Emby 超时，请检查容器到 Emby 地址的网络及代理设置。'
    if isinstance(exc,httpx.ConnectError):return '无法连接 Emby，请检查服务地址、8096 端口、Docker 网络和 Emby 代理设置。'
    if isinstance(exc,httpx.HTTPError):return 'Emby 网络请求失败，请检查网络与代理。'
    return 'Emby 响应不是有效的服务信息，请检查是否填写了网页入口或反向代理错误页面。'

async def probe(http,store,url=None,key=None):
    url=(url if url is not None else store.get('emby_url')).strip().rstrip('/')
    key=key or store.get('emby_key')
    if not url or not key:raise ValueError('请填写 Emby 地址和 API Key。')
    if urlsplit(url).scheme not in ('http','https') or not urlsplit(url).hostname:raise ValueError('Emby 地址须为有效的 HTTP/HTTPS 服务地址。')
    bases=[url] if url.endswith('/emby') else [url+'/emby',url]
    configured_origin=urlsplit(url)
    loopbacks=[]
    if configured_origin.hostname not in ('localhost','127.0.0.1','::1') and is_local_host(configured_origin.hostname):
        local_url=urlunsplit((configured_origin.scheme,'127.0.0.1'+(':'+str(configured_origin.port) if configured_origin.port else ''),configured_origin.path,'',''))
        loopbacks=[local_url] if local_url.endswith('/emby') else [local_url+'/emby',local_url]
    bases+=loopbacks
    saved=saved_base(store)
    if saved and fingerprint(url,key)==fingerprint(store.get('emby_url'),store.get('emby_key')) and saved[0] in bases:bases.remove(saved[0]);bases.insert(0,saved[0])
    failure=None;unreachable=set()
    for base in bases:
        origin=urlsplit(base).netloc
        if origin in unreachable:continue
        if base in loopbacks and configured_origin.netloc not in unreachable and not (saved and saved[0]==base):continue
        try:
            response=await emby_request(http,'GET',base+'/System/Info',headers={'X-Emby-Token':key},timeout=httpx.Timeout(6,connect=2))
            response.raise_for_status();data=response.json()
            if not isinstance(data,dict) or not (data.get('ServerName') or data.get('Version') or data.get('Id')):raise ValueError('invalid response')
        except (httpx.HTTPError,ValueError) as exc:
            failure=exc
            if isinstance(exc,httpx.HTTPStatusError) and exc.response.status_code not in (404,405):break
            if isinstance(exc,(httpx.ConnectError,httpx.TimeoutException)):
                unreachable.add(origin)
                if loopbacks:continue
                break
            continue
        identity=fingerprint(url,key)
        if base in loopbacks and store.get('emby_server_fingerprint')==identity and store.get('emby_server_id') and data.get('Id')!=store.get('emby_server_id'):
            raise ValueError('本机回环地址返回了另一台 Emby，未切换服务器。')
        if identity==fingerprint(store.get('emby_url'),store.get('emby_key')):
            values={'emby_api_base':base,'emby_api_fingerprint':identity}
            if data.get('Id'):values.update(emby_server_id=data['Id'],emby_server_fingerprint=identity)
            store.set_many(values)
        return {'connected':True,'server':data.get('ServerName','Emby'),'version':data.get('Version',''),'api_base':base,'info':data}
    raise ValueError(error_message(failure)) from None
