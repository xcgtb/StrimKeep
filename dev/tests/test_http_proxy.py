# -*- coding: utf-8 -*-
import base64
import io
import json
import os
import shutil
import ssl
import subprocess
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault('AGENT_DATA', tempfile.mkdtemp(prefix='strimkeep-proxy-tests-'))
os.environ.setdefault('WEB_PASSWORD', 'proxy-fixture-password')

from app import config, network, posters, tmdb, bot, tg
from app.routers import settings, library


def enabled(url='http://127.0.0.1:7890', **extra):
    return dict(config.DEFAULTS, http_proxy_enabled='1', http_proxy_url=url, **extra)


@pytest.mark.parametrize('url', ['socks5://localhost:1080', 'https://localhost:7890',
    'http://user:secret@localhost:7890', 'http://localhost/path', 'http://localhost?x=1',
    'http://localhost:0', 'http://localhost:65536', 'http://localhost:bad', 'http://',
    'http://local host:7890', 'http://localhost:7890\n@evil'])
def test_invalid_proxy_is_rejected_without_echoing_address(url):
    with pytest.raises(ValueError) as error:
        network.proxy_config(saved=enabled(url))
    assert str(error.value) == '代理地址格式错误，请填写 http://IP或域名:端口；鉴权请使用下方用户名和密码'


def test_defaults_are_off_and_enabled_requires_address():
    assert network.proxy_config(saved=config.DEFAULTS)['http_proxy_enabled'] == '0'
    with pytest.raises(ValueError):
        network.proxy_config(saved=enabled(''))


def test_masked_password_preserved_and_changed_proxy_needs_new_password():
    cfg = enabled(http_proxy_username='user', http_proxy_password='private-password')
    assert config.mask_config(cfg)['http_proxy_password'] == '********'
    assert network.proxy_config({'http_proxy_password':'********'}, saved=cfg)['http_proxy_password'] == 'private-password'
    with pytest.raises(ValueError):
        network.proxy_config({'http_proxy_url':'http://another-host:7890','http_proxy_password':'********'}, saved=cfg)
    assert network.proxy_config({'http_proxy_url':'http://another-host:7890','http_proxy_password':''}, saved=cfg)['http_proxy_password'] == ''


def test_save_persists_hot_config_and_failed_validation_does_not_write(monkeypatch):
    cfg = dict(config.DEFAULTS)
    monkeypatch.setattr(settings, 'load_config', lambda: dict(cfg))
    monkeypatch.setattr(settings, 'save_config', lambda value: cfg.update(value) or dict(cfg))
    monkeypatch.setattr(network._cfg, 'load_config', lambda: dict(cfg))
    reloads = []
    monkeypatch.setattr(settings.engine, 'reload_config', lambda: reloads.append(True))
    monkeypatch.setattr(settings.bot, 'restart', lambda: None)
    response = settings.api_set_config({'http_proxy_enabled':'1','http_proxy_url':'http://proxy.test:7890',
        'http_proxy_username':'user','http_proxy_password':'secret-password'})
    assert response['config']['http_proxy_password'] == '********'
    assert network.proxy_config()['http_proxy_url'] == 'http://proxy.test:7890'
    assert cfg['http_proxy_password'] == 'secret-password' and reloads == [True]
    with pytest.raises(settings.HTTPException) as error:
        settings.api_set_config({'http_proxy_url':'socks5://wrong:1080'})
    assert error.value.status_code == 400 and cfg['http_proxy_url'] == 'http://proxy.test:7890'
    settings.api_set_config({'http_proxy_enabled':'0','http_proxy_password':'********'})
    assert cfg['http_proxy_password'] == 'secret-password'


def test_disabled_uses_existing_transport_and_no_global_opener(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(network._cfg, 'load_config', lambda: dict(config.DEFAULTS))
    monkeypatch.setattr(urllib.request, 'urlopen', lambda request, timeout: sentinel)
    monkeypatch.setattr(urllib.request, 'install_opener', lambda *a: pytest.fail('global proxy changed'))
    assert network.open_external('https://api.themoviedb.org/3', timeout=3) is sentinel


def test_connection_errors_redact_credentials_and_keep_timeout_class(monkeypatch):
    class BrokenOpener:
        def open(self, *a, **kw):
            raise urllib.error.URLError('private-password http://user:private-password@proxy.test')
    monkeypatch.setattr(urllib.request, 'build_opener', lambda *a: BrokenOpener())
    with pytest.raises(urllib.error.URLError) as error:
        network.open_external('https://api.themoviedb.org/3', config=enabled(http_proxy_password='private-password'))
    assert 'private-password' not in str(error.value)
    assert tmdb._explore_failure(error.value)[0] == 'proxy'


@pytest.fixture
def tls_proxy(tmp_path, monkeypatch):
    if not shutil.which('openssl'):
        pytest.skip('openssl required to generate an ephemeral local TLS fixture')
    cert, key = tmp_path/'cert.pem', tmp_path/'key.pem'
    subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1',
        '-subj','/CN=api.themoviedb.org','-addext','subjectAltName=DNS:api.themoviedb.org',
        '-keyout',str(key),'-out',str(cert)], check=True, capture_output=True)
    server_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_tls.load_cert_chain(cert, key)
    client_tls = ssl.create_default_context(cafile=str(cert))
    original = urllib.request.build_opener
    monkeypatch.setattr(urllib.request, 'build_opener', lambda *handlers: original(*handlers, urllib.request.HTTPSHandler(context=client_tls)))
    records = []
    class Proxy(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_CONNECT(self):
            records.append((self.path,self.headers.get('Proxy-Authorization')))
            self.send_response(200); self.end_headers(); self.wfile.flush()
            with server_tls.wrap_socket(self.connection, server_side=True) as connection:
                request = b''
                while b'\r\n\r\n' not in request and len(request) < 16384:
                    request += connection.recv(4096)
                records.append(request.decode())
                data = b'{"ok":true}'
                connection.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 11\r\nConnection: close\r\n\r\n'+data)
            self.close_connection = True
    server = ThreadingHTTPServer(('127.0.0.1',0),Proxy)
    thread = threading.Thread(target=server.serve_forever,daemon=True); thread.start()
    try:
        yield 'http://127.0.0.1:'+str(server.server_port), records
    finally:
        server.shutdown(); server.server_close(); thread.join(2)


def test_real_https_connect_auth_and_live_disable(tls_proxy, monkeypatch):
    monkeypatch.setenv('NO_PROXY', '*')
    url, records = tls_proxy
    cfg = enabled(url,http_proxy_username='user@name',http_proxy_password='p@ss:word')
    monkeypatch.setattr(network._cfg,'load_config',lambda:dict(cfg))
    with network.open_external('https://api.themoviedb.org/3/configuration', timeout=3) as response:
        assert json.loads(response.read()) == {'ok':True}
    expected = 'Basic '+base64.b64encode(b'user@name:p@ss:word').decode()
    assert records[0] == ('api.themoviedb.org:443', expected)
    assert 'Proxy-Authorization' not in records[1]
    cfg['http_proxy_enabled'] = '0'
    direct = object()
    monkeypatch.setattr(urllib.request,'urlopen',lambda *a,**kw:direct)
    assert network.open_external('https://api.themoviedb.org/3') is direct
    assert len(records) == 2


def test_proxy_test_uses_draft_without_saving_or_sending_keys(monkeypatch):
    cfg = dict(config.DEFAULTS,tmdb_key='private-api-key')
    monkeypatch.setattr(settings,'load_config',lambda:dict(cfg))
    seen=[]
    def request(req, timeout, config):
        seen.append((req.full_url,config['http_proxy_url']))
        raise urllib.error.HTTPError(req.full_url,401,'Unauthorized',{},io.BytesIO())
    monkeypatch.setattr(network,'open_external',request)
    result=settings.api_test_proxy({'http_proxy_enabled':'1','http_proxy_url':'http://draft:7890'})
    assert result['status']=='success'
    assert seen==[('https://api.themoviedb.org/3/configuration','http://draft:7890')]
    assert cfg['http_proxy_enabled']=='0'


def test_bot_and_notifications_use_external_transport(monkeypatch):
    calls=[]
    class Response(io.BytesIO):
        status=200
    def transport(req,timeout):
        calls.append(req.full_url)
        return Response(b'{"ok":true}')
    monkeypatch.setattr(network,'open_external',transport)
    monkeypatch.setattr(tg,'_runtime_cfg',lambda:{'telegram_bot_token':'dummy','telegram_chat_id':'1'})
    assert bot._api('dummy','getMe')['ok'] is True
    assert tg.notify_telegram('fixture') is True
    assert calls==['https://api.telegram.org/botdummy/getMe','https://api.telegram.org/botdummy/sendMessage']


def test_emby_does_not_use_web_proxy(monkeypatch):
    from app import emby, engine
    monkeypatch.setattr(network,'open_external',lambda *a,**kw:pytest.fail('Emby used external proxy'))
    seen=[]
    def request(req,timeout):
        seen.append(req.full_url);return io.BytesIO(b'{"ServerName":"local-fixture"}')
    monkeypatch.setattr(emby.urllib.request,'urlopen',request)
    monkeypatch.setattr(emby,'_eng',lambda:SimpleNamespace(EMBY_HOST='http://192.168.1.5:8096',EMBY_KEY='fixture'))
    assert emby.emby_request('/System/Info',timeout=3,retries=0)['ServerName']=='local-fixture'
    assert seen==['http://192.168.1.5:8096/System/Info']


def test_poster_fixed_host_cache_auth_and_binary_content(monkeypatch):
    with posters._lock: posters._cache.clear()
    calls=[]
    class Response(io.BytesIO):
        headers={'Content-Type':'image/jpeg'}
    def request(url,timeout):
        calls.append(url);return Response(b'\xff\xd8\xfffixture')
    monkeypatch.setattr(network,'open_external',request)
    first=library.api_tmdb_poster('fixture.jpg')
    second=library.api_tmdb_poster('fixture.jpg')
    assert first.body==second.body==b'\xff\xd8\xfffixture'
    assert first.headers['cache-control'].startswith('private')
    assert calls==['https://image.tmdb.org/t/p/w500/fixture.jpg']
    with pytest.raises(ValueError):posters.get_tmdb_poster('../private.jpg')
    assert len(calls)==1


def test_proxy_config_survives_process_restart(tmp_path):
    env=dict(os.environ,AGENT_DATA=str(tmp_path/'data'))
    repo=Path(__file__).resolve().parents[2]
    writer="from app import config;c=config.load_config();c.update(http_proxy_enabled='1',http_proxy_url='http://proxy.test:7890',http_proxy_password='restart-fixture');config.save_config(c)"
    reader="from app import config;c=config.load_config();assert c['http_proxy_enabled']=='1' and c['http_proxy_url']=='http://proxy.test:7890' and c['http_proxy_password']=='restart-fixture';print('persisted')"
    subprocess.run([os.sys.executable,'-c',writer],env=env,cwd=repo,check=True,capture_output=True)
    result=subprocess.run([os.sys.executable,'-c',reader],env=env,cwd=repo,check=True,capture_output=True,text=True)
    assert result.stdout.strip()=='persisted'


def test_proxy_and_poster_routes_require_auth(monkeypatch):
    from app.main import app
    from app.routers.deps import auth
    from fastapi.testclient import TestClient
    monkeypatch.setattr(network,'open_external',lambda *a,**kw:pytest.fail('unauthenticated network access'))
    prior=app.dependency_overrides.pop(auth,None)
    try:
        client=TestClient(app)
        assert client.get('/api/tmdb/poster/fixture.jpg').status_code==401
        assert client.post('/api/config/test/proxy',json={}).status_code==401
    finally:
        if prior is not None:app.dependency_overrides[auth]=prior
