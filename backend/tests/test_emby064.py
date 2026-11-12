"""Real TCP regression checks: proxy isolation, local fallback and shared route."""
import asyncio,json,threading
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from contextlib import contextmanager
import httpx,pytest
from test_organize import setup
from strimkeep.network import ServiceHTTP,emby_request
from strimkeep.emby_connection import probe,fingerprint

@contextmanager
def server():
    calls=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            calls.append((self.command,self.path,self.headers.get('X-Emby-Token')))
            if self.path=='/System/Info':data={'Id':'home-server','ServerName':'Home','Version':'4.9'}
            elif self.path.startswith('/Items/Counts'):data={'MovieCount':328,'SeriesCount':86}
            elif self.path.startswith('/Items'):data={'Items':[]}
            elif self.path.startswith('/Sessions'):data=[]
            else:self.send_response(404);self.end_headers();return
            raw=json.dumps(data).encode();self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
        def do_POST(self):
            calls.append((self.command,self.path,self.headers.get('X-Emby-Token')))
            self.rfile.read(int(self.headers.get('Content-Length','0')))
            self.send_response(204);self.end_headers()
    httpd=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=httpd.serve_forever,daemon=True);thread.start()
    try:yield httpd.server_port,calls
    finally:httpd.shutdown();httpd.server_close();thread.join(timeout=2)


def test_local_emby_remains_usable_with_broken_global_proxy(tmp_path):
    with server() as (port,calls):
        async def run():
            store,cloud,old,engine,_=setup(tmp_path)
            url=f'http://127.0.0.1:{port}'
            store.set_many({'emby_url':url,'network_config':{'enabled':True,'proxy_emby':True,'proxy_url':'http://127.0.0.1:1'}})
            async with ServiceHTTPContext(store) as client:
                # This is the shared route used before the fix: it is broken.
                with pytest.raises(httpx.ConnectError):await client.get(url+'/System/Info')
                result=await probe(client,store)
                assert result['connected'] and result['api_base']==url
                count=await emby_request(client,'GET',result['api_base']+'/Items/Counts')
                assert count.json()['MovieCount']==328
                response=await emby_request(client,'POST',engine.emby.connection()[0]+'/Library/Media/Updated',json={'Updates':[{'Path':'/strimkeep/影视媒体库/test.strm','UpdateType':'Created'}]})
                assert response.status_code==204
            assert any(c[1]=='/Library/Media/Updated' for c in calls)
            assert all(c[2]==store.get('emby_key') for c in calls if c[1]=='/System/Info')
            await old.aclose();store.close()
        asyncio.run(run())


class ServiceHTTPContext:
    def __init__(self,store):self.client=ServiceHTTP(store)
    async def __aenter__(self):return self.client
    async def __aexit__(self,*args):await self.client.aclose()


def test_same_machine_lan_binding_failure_recovers_on_loopback_and_keeps_route(tmp_path,monkeypatch):
    monkeypatch.setattr('strimkeep.network.local_addresses',lambda:{'127.0.0.1','127.0.0.2'})
    with server() as (port,calls):
        async def run():
            store,cloud,old,engine,_=setup(tmp_path)
            configured=f'http://127.0.0.2:{port}';effective=f'http://127.0.0.1:{port}'
            store.set_many({'emby_url':configured})
            async with ServiceHTTPContext(store) as client:
                result=await probe(client,store)
                assert result['api_base']==effective and store.get('emby_url')==configured
                assert engine.emby.connection()[0]==effective
                before=len(calls);assert (await probe(client,store))['connected'];assert len(calls)==before+1
                await emby_request(client,'POST',engine.emby.connection()[0]+'/Library/Media/Updated',json={'Updates':[]})
            assert calls[-1][1]=='/Library/Media/Updated'
            await old.aclose();store.close()
        asyncio.run(run())


def test_remote_server_failure_never_sends_credentials_to_loopback(tmp_path,monkeypatch):
    monkeypatch.setattr('strimkeep.emby_connection.is_local_host',lambda host:False)
    async def run():
        store,cloud,old,engine,_=setup(tmp_path);seen=[]
        def fail(req):seen.append(str(req.url));raise httpx.ConnectError('failed',request=req)
        async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
            with pytest.raises(ValueError):await probe(client,store)
        assert len(seen)==1 and all('127.0.0.1' not in url for url in seen)
        await old.aclose();store.close()
    asyncio.run(run())


def test_fallback_never_accepts_another_previously_known_server(tmp_path,monkeypatch):
    monkeypatch.setattr('strimkeep.network.local_addresses',lambda:{'127.0.0.1','127.0.0.2'})
    with server() as (port,calls):
        async def run():
            store,cloud,old,engine,_=setup(tmp_path);configured=f'http://127.0.0.2:{port}'
            store.set_many({'emby_url':configured,'emby_server_fingerprint':fingerprint(configured,store.get('emby_key')),'emby_server_id':'different-server'})
            async with ServiceHTTPContext(store) as client:
                with pytest.raises(ValueError,match='另一台 Emby'):await probe(client,store)
            assert not store.get('emby_api_base')
            await old.aclose();store.close()
        asyncio.run(run())


def test_web_connection_dashboard_and_manual_refresh_use_working_route(tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    from test_console060 import console_app,login
    monkeypatch.setattr('strimkeep.network.local_addresses',lambda:{'127.0.0.1','127.0.0.2'})
    with server() as (port,calls):
        app=console_app(tmp_path,monkeypatch)
        with TestClient(app) as client:
            login(client)
            app.state.store.set_many({'emby_url':f'http://127.0.0.2:{port}','emby_key':'emby-secret','network_config':{'enabled':True,'proxy_emby':True,'proxy_url':'http://127.0.0.1:1'}})
            checked=client.post('/api/v1/connections/emby/test')
            assert checked.status_code==200 and checked.json()['api_base']==f'http://127.0.0.1:{port}'
            dashboard=client.get('/api/v1/dashboard').json()
            assert dashboard['emby']['connected'] and dashboard['emby']['counts']['MovieCount']==328
            assert not dashboard['emby']['error']
            assert client.post('/api/v1/emby/scan').status_code==200
            assert calls[-1][1]=='/Library/Refresh' and calls[-1][2]=='emby-secret'
