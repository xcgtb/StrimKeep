"""Contract fixtures, real local HTTP transport and disposable SQLite state.

These verify adapter boundaries, not a live TgtoDrive response schema.
"""
import contextlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app import config, upstream as u, state_store
from app.routers import upstream as routes


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg = dict(config.DEFAULTS, tgto_enabled='1', tgto_url='http://fixture', tgto_token='fixture-secret')
    monkeypatch.setattr(config, 'DATA_DIR', tmp_path / 'data')
    monkeypatch.setattr(config, 'load_config', lambda: dict(cfg))
    monkeypatch.setattr(config, 'save_config', lambda body: cfg.update(body))
    monkeypatch.setattr(u, '_handles', {})
    monkeypatch.setattr(u, '_cap_cache', {})
    monkeypatch.setattr(u, '_WRITE', threading.Lock())
    monkeypatch.setattr(u, '_NETWORK', threading.BoundedSemaphore(2))
    return cfg


@contextlib.contextmanager
def server(reply, code=200, headers=None):
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append({'path':self.path,'auth':self.headers.get('Authorization')})
            self.send_response(code)
            for key, value in (headers or {'Content-Type':'application/json'}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(reply if isinstance(reply, bytes) else json.dumps(reply).encode())
        def log_message(self, *a):
            pass
    service = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=service.serve_forever, daemon=True)
    thread.start()
    try:
        yield 'http://127.0.0.1:%s' % service.server_port, calls
    finally:
        service.shutdown(); service.server_close(); thread.join()


def mock_remote(monkeypatch, search=None, transfer=None):
    calls = []
    def request(action, payload=None, query=None, cfg=None):
        calls.append((action, payload, query))
        if action == 'spec':
            return {'paths':{path:{method.lower():{}} for method,path in u.ROUTES.values()}}
        if action == 'search': return search if search is not None else {'items':[]}
        if action == 'transfer':
            if isinstance(transfer, Exception): raise transfer
            return transfer or {'status':'success'}
        if action == 'history': return {'data':{'records':[{'id':7,'title':'样本'}]}}
        if action == 'detail': return {'data':{'record':{'id':7,'title':'样本','target_path':'/media/样本',
                                                       'cookie':'secret-cookie','token':'secret-token','share_url':'secret-link'}}}
        raise AssertionError(action)
    monkeypatch.setattr(u, 'request', request)
    return calls


def run_workers(monkeypatch):
    workers = []
    class Worker:
        def __init__(self, target, args, **kw): workers.append(lambda: target(*args))
        def start(self): pass
    monkeypatch.setattr(u.threading, 'Thread', Worker)
    return workers


def resource(monkeypatch):
    calls = mock_remote(monkeypatch, {'resources':[{'title':'样本 S01E02', 'source':'fixture', 'slug':'r1'}]})
    result = u.search({'title':'样本', 'media_type':'tv','tmdb_id':'7','season':'1','episode':'2'})
    return calls, result['items'][0]['handle']


@pytest.mark.parametrize('url', ['ftp://host','http://u:p@host','http://host?token=x',
    'http://host/#x','http://host/../api','http://host/%2e%2e/api','http://host:99999','http://host/\npath'])
def test_url_validation(url):
    with pytest.raises(u.UpstreamError): u.url_value(url)


def test_subpath_and_token_preservation(env):
    assert u.url_value('http://192.168.3.55:8080/tgto/') == 'http://192.168.3.55:8080/tgto'
    assert u.settings({'token':''})['token'] == 'fixture-secret'
    with pytest.raises(u.UpstreamError): u.settings({'url':'http://another'})
    result = u.save_settings({'url':'http://another','token':'new-secret','enabled':True,'max_points':2})
    assert result['has_token'] and 'token' not in result
    assert env['tgto_token'] == 'new-secret'
    assert 'tgto_token' not in config.EDITABLE_KEYS
    assert config.mask_config(env)['tgto_token'] != 'new-secret'
    with pytest.raises(u.UpstreamError): u.settings({'clear_token':True})
    u.save_settings({'clear_token':True,'enabled':False})
    assert env['tgto_token'] == ''


def test_real_http_headers_subpath_proxy_bypass(env, monkeypatch):
    monkeypatch.setenv('http_proxy', 'http://127.0.0.1:1')
    with server({'items':[]}) as (url,calls):
        cfg = u.settings({'url':url+'/prefix','token':'new-secret'})
        assert u.request('history', query={'keyword':'样本','limit':20}, cfg=cfg) == {'items':[]}
        assert calls[0]['auth'] == 'Bearer new-secret'
        assert calls[0]['path'].startswith('/prefix/api/organize-history?')
        assert '%E6%A0%B7%E6%9C%AC' in calls[0]['path']


def test_redirect_does_not_forward_credentials(env):
    with server({'items':[]}) as (destination, target_calls):
        with server(b'',302,{'Location':destination}) as (url,calls):
            with pytest.raises(u.UpstreamError): u.request('spec', cfg=u.settings({'url':url,'token':'new-secret'}))
            assert len(calls) == 1 and not target_calls


@pytest.mark.parametrize('body,headers', [(b'<html>login</html>',{'Content-Type':'text/html'}),
    (b'broken',{'Content-Type':'application/json'}), (b'x'*(u.MAX_RESPONSE+1),{'Content-Type':'application/json'})])
def test_invalid_transport_rejected(env, body, headers):
    with server(body,headers=headers) as (url,_):
        with pytest.raises(u.UpstreamError): u.request('spec', cfg=u.settings({'url':url,'token':'new-secret'}))


def test_http_error_does_not_echo_secrets(env):
    with server({'message':'fixture-secret'},401) as (url,_):
        with pytest.raises(u.UpstreamError) as caught:
            u.request('spec', cfg=u.settings({'url':url,'token':'new-secret'}))
        assert '令牌' in str(caught.value) and 'secret' not in str(caught.value)


def test_network_concurrency_limit(env):
    assert u._NETWORK.acquire(False) and u._NETWORK.acquire(False)
    with pytest.raises(u.UpstreamError) as caught: u.request('spec')
    assert caught.value.code == 429
    u._NETWORK.release(); u._NETWORK.release()


def test_disabled_and_capabilities_no_background_calls(env, monkeypatch):
    calls = mock_remote(monkeypatch)
    env['tgto_enabled'] = '0'
    with pytest.raises(u.UpstreamError): u.search({'title':'样本','media_type':'tv'})
    assert not calls
    env['tgto_enabled'] = '1'
    u.search({'title':'样本','media_type':'tv'})
    u.search({'title':'样本','media_type':'tv'})
    assert [c[0] for c in calls] == ['spec','search','search']


def test_search_scope_handles_and_unknown_structure(env, monkeypatch):
    calls, handle = resource(monkeypatch)
    assert calls[-1][1] == {'title':'样本','media_type':'tv','tmdb_id':7,'season':1,'episode':2}
    assert u._handles[handle]['identity'] == {'source':'fixture','slug':'r1'}
    mock_remote(monkeypatch, {'data':{'unexpected':[1]}})
    with pytest.raises(u.UpstreamError, match='结构暂不兼容'): u.search({'title':'样本','media_type':'tv'})


@pytest.mark.parametrize('body', [{'title':'','media_type':'tv'}, {'title':'样本','media_type':'movie','season':1},
    {'title':'样本','media_type':'tv','episode':1}, {'title':'样本','media_type':'tv','season':True}])
def test_invalid_search_inputs(env, monkeypatch, body):
    mock_remote(monkeypatch)
    with pytest.raises(u.UpstreamError): u.search(body)


def test_search_bare_id_not_turned_into_transfer_identity(env, monkeypatch):
    mock_remote(monkeypatch, {'items':[{'id':77,'title':'样本'},{'source':'s','slug':'r'}]})
    result=u.search({'title':'样本','media_type':'tv'})
    assert not result['items'][0]['transferable'] and not result['items'][0]['handle']
    assert result['items'][1]['transferable']


def test_resource_metadata_whitelist(env, monkeypatch):
    mock_remote(monkeypatch, {'items':[{'source':'RED','slug':'one','provider':'115','resolution':'2160p',
        'release_type':'WEB-DL','video_effect':'HDR10','codec':'HEVC','release_group':'Fixture',
        'file_count':3,'publisher':'山间晚风','reviewed_count':0,'points':0,'subtitles':['简中','内封'],
        'verification_status':'有效','notes':'内封字幕','share_url':'secret-link','cookie':'secret-cookie','token':'secret-token'}]})
    item = u.search({'title':'样本','media_type':'movie'})['items'][0]
    assert item['provider'] == '115' and item['resolution'] == '2160p'
    assert item['release_type'] == 'WEB-DL' and item['file_count'] == '3'
    assert item['video_effect'] == 'HDR10' and item['release_group'] == 'Fixture'
    assert item['publisher'] == '山间晚风' and item['reviewed_count'] == item['points'] == '0'
    assert item['subtitles'] == '简中 · 内封' and item['verification'] == '有效' and item['note'] == '内封字幕'
    assert 'secret' not in json.dumps(item)


@pytest.mark.parametrize('reply', [
    {'data':{'transfer_targets':[{'id':'115','name':'115 网盘','token':'secret'},{'id':'disabled','enabled':False}]}},
    {'targets':{'115':{'name':'115 网盘','token':'secret'}}},
])
def test_targets_explicit_groups_only(env, monkeypatch, reply):
    calls = []
    monkeypatch.setattr(u, 'capabilities', lambda cfg: {'providers':True})
    monkeypatch.setattr(u, 'request', lambda action, **kw: calls.append(action) or reply)
    assert u.providers() == {'items':[{'id':'115','name':'115 网盘'}]}
    assert calls == ['providers']


def test_targets_unknown_structure_not_guessed(env, monkeypatch):
    monkeypatch.setattr(u, 'capabilities', lambda cfg: {'providers':True})
    monkeypatch.setattr(u, 'request', lambda *a, **k: {'providers':[{'id':'RED','name':'资源源'}]})
    with pytest.raises(u.UpstreamError, match='暂未识别'):
        u.providers()


def test_transfer_durable_dedupe_points_and_no_media_projection(env, monkeypatch):
    calls,handle=resource(monkeypatch)
    workers=run_workers(monkeypatch)
    body={'confirmed':True,'handle':handle,'provider':'115','operation_id':'operation_id_0001'}
    first=u.transfer(body)
    assert first['state']=='submitting' and len(workers)==1
    assert u.transfer(body)['id']==first['id']
    assert u.transfer(dict(body,operation_id='operation_id_0002'))['id']==first['id']
    assert len(workers)==1
    workers[0]()
    assert u.get_jobs()[0]['state']=='submitted'
    payload=[c[1] for c in calls if c[0]=='transfer'][0]
    assert payload=={'source':'fixture','slug':'r1','provider':'115','enforce_points_limit':True,'max_points':0}
    assert 'identity' not in u.get_jobs()[0] and 'connection' not in u.get_jobs()[0]
    assert not list(config.DATA_DIR.rglob('*.strm'))
    assert u.transfer(body)['state']=='submitted' and len(workers)==1


def test_public_resource_fields_preserve_zero_and_bound_lists():
    assert u.text(0) == '0'
    assert u.resource_text({'publisher': {'token': 'secret'}}, 'publisher') == ''
    assert u.resource_text({'subtitles': ['简中', '繁中', {'secret': 'x'}]}, 'subtitles') == '简中 · 繁中'
    assert len(u.resource_text({'notes': 'x' * 1000}, 'notes', limit=600)) == 600


def test_timeout_unknown_restart_and_acknowledgement(env, monkeypatch):
    _,handle=resource(monkeypatch)
    mock_remote(monkeypatch, transfer=TimeoutError())
    workers=run_workers(monkeypatch)
    body={'confirmed':True,'handle':handle,'provider':'115','operation_id':'operation_id_0001'}
    first=u.transfer(body); workers[0]()
    assert u.get_jobs()[0]['state']=='unknown'
    assert u.transfer(dict(body,operation_id='operation_id_0002'))['id']==first['id']
    with pytest.raises(u.UpstreamError): u.acknowledge(first['id'],False)
    u.acknowledge(first['id'],True)
    assert not u.get_jobs()
    state_store.save(u._job_path(),{'crash':{'id':'crash','created':1,'state':'submitting'}})
    assert u.get_jobs()[0]['state']=='unknown' and not u._WRITE.locked()


def test_expired_handle_changed_connection_and_confirmation(env, monkeypatch):
    _,handle=resource(monkeypatch)
    body={'confirmed':True,'handle':handle,'provider':'115','operation_id':'operation_id_0001'}
    with pytest.raises(u.UpstreamError): u.transfer(dict(body,confirmed=False))
    with pytest.raises(u.UpstreamError): u.transfer(dict(body,handle='forged'))
    env['tgto_token']='changed'
    with pytest.raises(u.UpstreamError,match='连接已变更'): u.transfer(body)
    env['tgto_token']='fixture-secret'
    u._handles[handle]['time']-=u.HANDLE_TTL
    with pytest.raises(u.UpstreamError,match='过期'): u.transfer(body)


def test_history_does_not_expose_secrets_or_authorize_deletion(env, monkeypatch):
    calls=mock_remote(monkeypatch)
    assert u.history('样本')[0]['id']=='7'
    details=u.detail('7')
    assert details=={'id':'7','title':'样本','target_path':'/media/样本'}
    assert 'secret' not in json.dumps(details)
    assert all(action not in ('delete','sync') for action,_,_ in calls)
    assert all('delete' not in path for _,path in u.ROUTES.values())


def test_all_routes_authenticated_and_no_store(env, monkeypatch):
    mock_remote(monkeypatch)
    app=FastAPI(); app.include_router(routes.router)
    with TestClient(app) as client:
        for route in ['/config','/jobs','/history?keyword=x','/history/7']:
            assert client.get('/api/upstream'+route).status_code==401
        for route in ['/config','/test','/search','/transfer','/jobs/example/acknowledge']:
            assert client.post('/api/upstream'+route,json={}).status_code==401
        r=client.get('/api/upstream/config',auth=('admin','test-pass-123'))
        assert r.status_code==200 and r.headers['cache-control']=='no-store'
        assert 'fixture-secret' not in r.text


def test_failed_job_persistence_never_submits(env, monkeypatch):
    calls,handle=resource(monkeypatch)
    workers=run_workers(monkeypatch)
    monkeypatch.setattr(state_store,'save',lambda *a: (_ for _ in ()).throw(OSError('storage unavailable')))
    with pytest.raises(OSError):
        u.transfer({'confirmed':True,'handle':handle,'provider':'115','operation_id':'operation_id_0001'})
    assert not workers and not u._WRITE.locked()
    assert not any(c[0]=='transfer' for c in calls)


def test_candidate_limit_and_single_write_concurrency(env, monkeypatch):
    rows=[{'source':'s','slug':str(i)} for i in range(75)]
    mock_remote(monkeypatch, {'items':rows})
    result=u.search({'title':'样本','media_type':'tv'})
    assert len(result['items'])==50 and result['truncated']
    workers=run_workers(monkeypatch)
    body={'confirmed':True,'handle':result['items'][0]['handle'],'provider':'115','operation_id':'operation_id_0001'}
    u.transfer(body)
    with pytest.raises(u.UpstreamError,match='已有转存'):
        u.transfer(dict(body,handle=result['items'][1]['handle'],operation_id='operation_id_0002'))
    assert len(workers)==1
    workers[0]()
