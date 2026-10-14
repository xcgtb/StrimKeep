"""Actual local HTTP contract checks; never a request to a real 115 account."""
import contextlib
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import pytest
from app.providers import p115_open as p


@contextlib.contextmanager
def server(reply,code=200,headers=None):
    calls=[]
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):self.reply()
        def do_POST(self):self.reply()
        def reply(self):
            calls.append({'method':self.command,'path':self.path,'auth':self.headers.get('Authorization'),
                          'ua':self.headers.get('User-Agent'),'cookie':self.headers.get('Cookie'),'body':self.rfile.read(int(self.headers.get('Content-Length',0)))})
            self.send_response(code)
            for key,value in (headers or {'Content-Type':'application/json'}).items():self.send_header(key,value)
            self.end_headers()
            self.wfile.write(reply if isinstance(reply,bytes) else json.dumps(reply).encode())
        def log_message(self,*args):pass
    svc=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=svc.serve_forever,daemon=True);thread.start()
    try:yield 'http://127.0.0.1:'+str(svc.server_port),calls
    finally:svc.shutdown();svc.server_close();thread.join()


def test_delete_posts_only_explicit_file_id_without_proxy_or_retries(monkeypatch):
    with server({'state':True}) as (url,calls):
        monkeypatch.setitem(p.ROUTES,'delete',('POST',url+'/delete'))
        monkeypatch.setenv('HTTP_PROXY','http://127.0.0.1:9')
        monkeypatch.setenv('NO_PROXY','')
        assert p.transport('delete',{'file_ids':'1234567890123456789'},'secret-access')['state']
        assert len(calls)==1 and calls[0]['method']=='POST'
        assert calls[0]['auth']=='Bearer secret-access'
        assert urllib.parse.parse_qs(calls[0]['body'].decode())=={'file_ids':['1234567890123456789']}


def test_redirect_never_forwards_account_token(monkeypatch):
    with server({'state':True}) as (other,received):
        with server(b'',302,{'Location':other+'/stolen'}) as (url,calls):
            monkeypatch.setitem(p.ROUTES,'user',('GET',url))
            with pytest.raises(p.ProviderError):p.transport('user',access_token='secret-access')
            assert len(calls)==1 and received==[]


@pytest.mark.parametrize('reply,code,headers',[
    (b'<html>secret-access</html>',200,{'Content-Type':'text/html'}),
    (b'not-json secret-access',200,None),
    ({'state':False,'message':'secret-access'},500,None),
    ({'state':False,'message':'secret-access','errno':999},200,None),
])
def test_unknown_http_or_response_never_means_missing_or_exposes_body(monkeypatch,reply,code,headers):
    with server(reply,code,headers) as (url,calls):
        monkeypatch.setitem(p.ROUTES,'stat',('GET',url))
        with pytest.raises(p.ProviderError) as caught:p.transport('stat',{'file_id':'10'})
        assert not caught.value.missing and 'secret-access' not in str(caught.value) and len(calls)==1


def test_oauth_zero_code_and_exact_ua_on_network(monkeypatch):
    with server({'code':0,'data':{}}) as (url,calls):
        monkeypatch.setitem(p.ROUTES,'device_start',('POST',url))
        assert p.transport('device_start',{'client_id':'789'})['code']==0
        monkeypatch.setitem(p.ROUTES,'download',('POST',url))
        # download success still requires the endpoint's explicit success flag.
        with pytest.raises(p.ProviderError):p.transport('download',{'pick_code':'abcd123456'},'secret',user_agent='Player/1')
        assert calls[-1]['ua']=='Player/1'
        with pytest.raises(p.ProviderError):p.transport('download',user_agent='bad\r\nHeader: value')
        assert len(calls)==2


def test_stat_only_explicit_not_found_code_is_absence(monkeypatch):
    with server({'state':False,'errno':20004}) as (url,calls):
        monkeypatch.setitem(p.ROUTES,'stat',('GET',url))
        with pytest.raises(p.ProviderError) as caught:p.transport('stat',{'file_id':'10'})
        assert caught.value.missing and len(calls)==1


@pytest.mark.parametrize('action,payload',[('move',{'file_ids':'1234567890123456789','to_cid':'2'}),('rename',{'file_id':'1234567890123456789','file_name':'剧名.S01E01.mkv'})])
def test_organize_transport_posts_exact_ids_and_fields(monkeypatch,action,payload):
    with server({'state':True}) as (url,calls):
        monkeypatch.setitem(p.ROUTES,action,('POST',url))
        assert p.transport(action,payload,'secret-access')['state']
        assert len(calls)==1 and calls[0]['method']=='POST'
        assert urllib.parse.parse_qs(calls[0]['body'].decode())=={k:[v] for k,v in payload.items()}


def test_web_receive_uses_only_auxiliary_cookie_and_selected_source_ids(monkeypatch):
    from app.providers import p115_web as web
    with server({'state':True}) as (url,calls):
        monkeypatch.setitem(web.ROUTES,'receive',('POST',url))
        assert web.transport('receive',{'share_code':'sharecodeabc','receive_code':'c1d2','file_id':'100,101','cid':'500'},web_cookie='UID=123; CID=fixture; SEID=fixture')['state']
        assert len(calls)==1 and calls[0]['auth'] is None
        assert calls[0]['cookie']=='UID=123; CID=fixture; SEID=fixture'
        assert urllib.parse.parse_qs(calls[0]['body'].decode())['file_id']==['100,101']


def test_web_redirect_never_forwards_auxiliary_cookie(monkeypatch):
    from app.providers import p115_web as web
    with server({'state':True}) as (other,received):
        with server(b'',302,{'Location':other+'/stolen'}) as (url,calls):
            monkeypatch.setitem(web.ROUTES,'user',('GET',url))
            with pytest.raises(p.ProviderError):web.transport('user',web_cookie='UID=123; CID=fixture; SEID=fixture')
            assert len(calls)==1 and not received


@pytest.mark.parametrize('reply,code',[(b'<html>secret-cookie</html>',200),({'state':False,'message':'secret-cookie'},403)])
def test_web_unknown_response_is_not_success_and_never_exposes_body(monkeypatch,reply,code):
    from app.providers import p115_web as web
    with server(reply,code) as (url,calls):
        monkeypatch.setitem(web.ROUTES,'receive',('POST',url))
        with pytest.raises(p.ProviderError) as caught:web.transport('receive',web_cookie='UID=123; CID=fixture; SEID=fixture')
        assert 'secret-cookie' not in str(caught.value) and len(calls)==1
