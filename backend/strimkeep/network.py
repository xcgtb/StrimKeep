"""Proxy-aware external service client; playback keeps its independent pool."""
from contextlib import asynccontextmanager
from urllib.parse import urlsplit
import httpx
import socket

# 四段超时：连不上 5 秒内必须放弃（DNS/握手问题），读/写各 15 秒，
# 连接池排队 5 秒——把「永远不返回」变成可诊断的 TimeoutException。
TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=15.0, pool=5.0)

def local_addresses():
    addresses={'127.0.0.1','::1'}
    # Host-mode containers can reach a same-machine service on loopback even
    # when a global proxy or NAS interface binding breaks the LAN-IP route.
    try:
        import fcntl,struct
        with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sock:
            for _,name in socket.if_nameindex():
                try:addresses.add(socket.inet_ntoa(fcntl.ioctl(sock.fileno(),0x8915,struct.pack('256s',name.encode()[:15]))[20:24]))
                except OSError:pass
    except (ImportError,OSError):pass
    return addresses

def is_local_host(host):
    return host in ('localhost',*local_addresses())

async def emby_request(http,method,url,**kwargs):
    if hasattr(http,'emby_request'):return await http.emby_request(method,url,**kwargs)
    # HTTPX clients and test adapters keep their original method contracts.
    return await getattr(http,method.lower())(url,**kwargs)

class ServiceHTTP:
    def __init__(self,store):
        self.store=store
        self.direct=httpx.AsyncClient(timeout=TIMEOUT,follow_redirects=False,trust_env=False)
        self.emby_direct=httpx.AsyncClient(timeout=TIMEOUT,follow_redirects=False,trust_env=False)
        self.proxies={}
    def client(self,url):
        config=self.store.get('network_config',{})
        host=urlsplit(str(url)).hostname or ''
        external=host in ('api.telegram.org','api.themoviedb.org','image.tmdb.org')
        proxy=config.get('proxy_url') if config.get('enabled') and (external or (config.get('proxy_emby') and host==(urlsplit(self.store.get('emby_url') or '').hostname or ''))) else ''
        if not proxy:return self.direct
        if proxy not in self.proxies:self.proxies[proxy]=httpx.AsyncClient(proxy=proxy,timeout=TIMEOUT,follow_redirects=False,trust_env=False)
        return self.proxies[proxy]
    async def request(self,method,url,**kwargs):return await self.client(url).request(method,url,**kwargs)
    async def get(self,url,**kwargs):return await self.request('GET',url,**kwargs)
    async def post(self,url,**kwargs):return await self.request('POST',url,**kwargs)
    async def emby_request(self,method,url,**kwargs):
        host=urlsplit(str(url)).hostname or ''
        config=self.store.get('network_config',{})
        proxy=config.get('proxy_url') if config.get('enabled') and config.get('proxy_emby') and not is_local_host(host) else ''
        if proxy:
            if proxy not in self.proxies:self.proxies[proxy]=httpx.AsyncClient(proxy=proxy,timeout=TIMEOUT,follow_redirects=False,trust_env=False)
            client=self.proxies[proxy]
        else:client=self.emby_direct
        return await client.request(method,url,**kwargs)
    @asynccontextmanager
    async def stream(self,method,url,**kwargs):
        async with self.client(url).stream(method,url,**kwargs) as response:yield response
    async def aclose(self):
        await self.direct.aclose()
        await self.emby_direct.aclose()
        for client in self.proxies.values():await client.aclose()
