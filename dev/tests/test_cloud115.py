"""Disposable files and contract fixtures, never live 115/NAS mutations."""
import hashlib
import json
import sqlite3
import urllib.parse
from pathlib import Path
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app import cloud115 as service, cloud115_store as repo, config, engine, plan_guard, storage, wash
from app.providers import p115_open as provider
from app.routers import cloud115 as routes


@pytest.fixture
def env(tmp_path,monkeypatch):
    root=tmp_path/'local';root.mkdir()
    data=tmp_path/'data';data.mkdir()
    monkeypatch.setattr(engine,'DATA_DIR',data)
    monkeypatch.setattr(engine,'STATE_DIR',data/'state')
    monkeypatch.setattr(engine,'L_ROOT',root)
    monkeypatch.setattr(engine,'LOCK_FILE',data/'mutation.lock')
    monkeypatch.setattr(engine,'WASH_RESIDUAL_FILE',data/'state'/'residuals.json')
    cfg=dict(config.DEFAULTS,cloud115_playback_base='http://nas:8321')
    monkeypatch.setattr(config,'load_config',lambda:dict(cfg))
    monkeypatch.setattr(config,'save_config',lambda body:cfg.update(body))
    monkeypatch.setattr(engine,'_invalidate_lib_cache',lambda:None)
    key=repo.save_account('123','样本','secret-access','secret-refresh',0)
    files={'10':{'id':'10','name':'样本.S01E01.2160p.mkv','size':50,'sha1':'A'*40,'pickcode':'abcd123456','is_dir':False},
           '11':{'id':'11','name':'样本.S01E01.ass','size':3,'sha1':'B'*40,'pickcode':'abcd123457','is_dir':False}}
    writes=[]
    class FakeDriver:
        delay=False
        uncertain=False
        denied=False
        def __init__(self,account_id):
            self.account_id=account_id
        def validate_account(self):
            if self.denied:raise provider.ProviderError('账号不一致',409)
            if self.account_id!=key:raise provider.ProviderError('账号不一致',409)
            return '123'
        def stat(self,fid):
            value=files.get(str(fid))
            return dict(value) if value else None
        def delete(self,fid):
            writes.append(fid)
            if self.uncertain:
                files.pop(fid,None)
                raise provider.ProviderError('超时')
            if not self.delay:files.pop(fid,None)
        def download(self,pickcode,user_agent='',file_id=None):return 'https://download.example/video.mkv?t=9999999999&f=0'
        def file_id_for_pickcode(self,pc):
            found=[x for x in files.values() if x['pickcode']==pc]
            if len(found)!=1:raise provider.ProviderError('无法确认身份',409)
            return dict(found[0])
    monkeypatch.setattr(service,'Driver',FakeDriver)
    return {'root':root,'account':key,'files':files,'writes':writes,'driver':FakeDriver,'cfg':cfg}


def generate(env,**extra):
    return service.generate(dict(account_id=env['account'],file_id='10',
                                 relative_path='剧集/样本/Season 01/样本.S01E01.2160p.strm',**extra))


def cleanup(env,path,dry_run=False):
    with engine.mutation_lock():
        return engine.safe_delete_files([Path(path)],env['root'],env['root'].parent/'mount-not-present',dry_run=dry_run)


def test_schema_preserves_existing_records(env):
    storage.db_doc_set('existing',{'key':'kept'})
    assert storage.db_doc_get('existing')=={'key':'kept'}
    with storage._transaction() as con:
        assert con.execute('PRAGMA user_version').fetchone()[0]>=5


def test_generate_persists_identity_secret_hash_and_owned_fingerprint(env):
    result=generate(env,sidecar_ids=['11'])
    path=Path(result['strm_path']);record=repo.binding(path=path)
    assert record['file_id']=='10' and record['sidecars'][0]['id']=='11'
    query=urllib.parse.parse_qs(urllib.parse.urlsplit(path.read_text().strip()).query)
    secret=query['token'][0]
    assert record['token_hash']==hashlib.sha256(secret.encode()).hexdigest()
    assert record['fingerprint']==plan_guard.fingerprint(path,[env['root']])
    assert secret not in json.dumps(service.public_status())
    assert 'secret-access' not in json.dumps(service.public_status())


def test_bound_delete_uses_official_ids_without_cd2_mount(env,monkeypatch):
    result=generate(env,sidecar_ids=['11'])
    monkeypatch.setattr(engine,'cloud_videos',lambda *a:pytest.fail('must never use filename matching'))
    stats=cleanup(env,result['strm_path'])
    assert env['writes']==['10','11']
    assert stats['strm_removed']==1 and stats['cloud_removed']==1 and stats['cloud_sidecars_removed']==1
    assert not stats['errors'] and not Path(result['strm_path']).exists()
    assert repo.binding(path=result['strm_path'])['state']=='deleted'


@pytest.mark.parametrize('change',['size','sha1','account','strm','binding_state'])
def test_conflicts_do_not_delete_or_fallback(env,monkeypatch,change):
    result=generate(env)
    path=Path(result['strm_path'])
    if change=='size':env['files']['10']['size']=51
    if change=='sha1':env['files']['10']['sha1']='C'*40
    if change=='account':env['driver'].denied=True
    if change=='strm':path.write_text('http://changed.example/video')
    if change=='binding_state':repo.set_binding_state(result['binding_id'],'needs_review')
    monkeypatch.setattr(engine,'cloud_videos',lambda *a:pytest.fail('must not fallback'))
    stats=cleanup(env,path)
    assert stats['errors'] and stats['strm_removed']==0 and path.exists() and not env['writes']


def test_sidecar_conflict_prevents_video_mutation(env):
    result=generate(env,sidecar_ids=['11'])
    env['files']['11']['size']=8
    stats=cleanup(env,result['strm_path'])
    assert stats['errors'] and not env['writes'] and Path(result['strm_path']).exists()


def test_timeout_receipt_reconciles_without_repeat_delete(env):
    result=generate(env)
    env['driver'].uncertain=True
    first=cleanup(env,result['strm_path'])
    assert first['errors'] and Path(result['strm_path']).exists()
    assert repo.receipt(result['binding_id'],'10')['state']=='unknown'
    second=cleanup(env,result['strm_path'])
    assert second['strm_removed']==1 and env['writes']==['10']


def test_still_visible_after_accepted_delete_is_not_success(env):
    result=generate(env)
    env['driver'].delay=True
    assert cleanup(env,result['strm_path'])['errors']
    assert cleanup(env,result['strm_path'])['errors']
    assert env['writes']==['10'] and Path(result['strm_path']).exists()
    env['files'].pop('10')
    assert cleanup(env,result['strm_path'])['strm_removed']==1
    assert env['writes']==['10']


def test_local_failure_after_remote_delete_can_resume(env,monkeypatch):
    result=generate(env)
    original=wash._remove_strm
    monkeypatch.setattr(wash,'_remove_strm',lambda *a:(_ for _ in ()).throw(OSError('fixture failure')))
    assert cleanup(env,result['strm_path'])['errors']
    assert repo.binding(path=result['strm_path'])['state']=='remote_deleted'
    preview=cleanup(env,result['strm_path'],True)
    assert preview['strm_removed']==1 and preview['cloud_removed']==0 and not preview['errors']
    monkeypatch.setattr(wash,'_remove_strm',original)
    resumed=cleanup(env,result['strm_path'])
    assert resumed['strm_removed']==1 and resumed['cloud_removed']==0
    assert env['writes']==['10']


def test_partial_confirmed_source_deletion_reports_fact_without_double_count(env,monkeypatch):
    result=generate(env,sidecar_ids=['11'])
    original=env['driver'].delete
    def fail_sidecar(self,fid):
        if fid=='11':
            env['writes'].append(fid)
            raise provider.ProviderError('fixture timeout')
        return original(self,fid)
    monkeypatch.setattr(env['driver'],'delete',fail_sidecar)
    first=cleanup(env,result['strm_path'])
    assert first['errors'] and first['strm_removed']==0 and first['cloud_removed']==1
    second=cleanup(env,result['strm_path'])
    assert second['errors'] and second['cloud_removed']==0 and env['writes']==['10','11']
    env['files'].pop('11')
    third=cleanup(env,result['strm_path'])
    assert third['strm_removed']==1 and third['cloud_removed']==0 and third['cloud_sidecars_removed']==1


def test_missing_source_before_any_delete_is_not_success(env):
    result=generate(env)
    env['files'].pop('10')
    assert cleanup(env,result['strm_path'])['errors']
    assert Path(result['strm_path']).exists() and not env['writes']


def test_dry_run_has_no_receipts_or_mutations(env):
    result=generate(env)
    stats=cleanup(env,result['strm_path'],True)
    assert stats['strm_removed']==1 and not env['writes'] and Path(result['strm_path']).exists()
    assert repo.receipt(result['binding_id'],'10') is None


def test_share_only_never_calls_remote(env,monkeypatch,tmp_path):
    share=tmp_path/'share';share.mkdir()
    path=share/'sample.strm';path.write_text('http://nas/play/115/anything?token=x')
    monkeypatch.setattr(service,'delete_source',lambda *a,**k:pytest.fail('share must not delete remote'))
    stats=engine.safe_delete_files([path],share,None)
    assert stats['strm_removed']==1 and not env['writes']


@pytest.mark.parametrize('strict,content',[(True,'http://old/video'),(False,'http://nas/play/115/missing?token=x')])
def test_unbound_strict_or_owned_url_never_matches_paths(env,monkeypatch,strict,content):
    env['cfg']['cloud115_require_binding']='1' if strict else '0'
    path=env['root']/'old.strm';path.write_text(content)
    monkeypatch.setattr(engine,'cloud_videos',lambda *a:pytest.fail('must not guess'))
    assert cleanup(env,path)['errors'] and path.exists()


@pytest.mark.parametrize('relative',['../bad.strm','/tmp/bad.strm','a/../bad.strm','a/video.mkv','a\\bad.strm'])
def test_generate_rejects_path_escape(env,relative):
    with pytest.raises(provider.ProviderError):
        service.generate({'account_id':env['account'],'file_id':'10','relative_path':relative})


def test_generation_never_overwrites_or_reuses_source(env):
    result=generate(env)
    original=Path(result['strm_path']).read_bytes()
    with pytest.raises(provider.ProviderError):generate(env)
    with pytest.raises(provider.ProviderError):
        service.generate({'account_id':env['account'],'file_id':'10','relative_path':'other.strm'})
    assert Path(result['strm_path']).read_bytes()==original


def test_deleted_path_can_bind_new_source_without_reusing_old_receipts(env):
    old=generate(env);assert cleanup(env,old['strm_path'])['strm_removed']==1
    env['files']['20']=dict(env['files']['11'],id='20',name='new.mkv',pickcode='abcd123458')
    relative=str(Path(old['strm_path']).relative_to(env['root']))
    new=service.generate({'account_id':env['account'],'file_id':'20','relative_path':relative})
    assert new['binding_id']!=old['binding_id']
    assert repo.binding(path=old['strm_path'])['id']==new['binding_id']
    assert repo.receipt(old['binding_id'],'10')['state']=='confirmed'
    assert cleanup(env,new['strm_path'])['strm_removed']==1 and env['writes']==['10','20']


def test_sidecar_cannot_be_owned_by_two_bindings(env):
    generate(env,sidecar_ids=['11'])
    env['files']['12']=dict(env['files']['10'],id='12',pickcode='abcd123458')
    with pytest.raises(provider.ProviderError):
        service.generate({'account_id':env['account'],'file_id':'12','sidecar_ids':['11'],'relative_path':'other.strm'})
    assert not (env['root']/'other.strm').exists()


def test_adopt_resolves_explicit_pickcode_without_rewriting(env):
    path=env['root']/'old.strm';path.write_text('https://upstream/play?pickcode=abcd123456')
    before=path.read_bytes()
    result=service.adopt({'account_id':env['account'],'relative_path':'old.strm'})
    assert repo.binding(path=path)['file_id']=='10' and path.read_bytes()==before
    assert cleanup(env,path)['strm_removed']==1 and env['writes']==['10']


@pytest.mark.parametrize('url',['https://upstream/abcd123456','https://upstream/x?pickcode=bad',
    'https://upstream/x?pickcode=abcd123456&pick_code=abcd123457','http://[invalid'])
def test_adoption_never_guesses_from_filename_or_ambiguous_url(env,url):
    path=env['root']/'old.strm';path.write_text(url)
    with pytest.raises(provider.ProviderError):service.adopt({'account_id':env['account'],'relative_path':'old.strm'})
    assert repo.binding(path=path) is None


def test_playback_token_identity_and_302(env):
    result=generate(env)
    url=Path(result['strm_path']).read_text().strip()
    secret=urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)['token'][0]
    app=FastAPI();app.include_router(routes.play_router)
    with TestClient(app) as client:
        target='/play/115/'+result['binding_id']
        assert client.get(target+'?token=wrong',follow_redirects=False).status_code==403
        response=client.get(target+'?token='+secret,follow_redirects=False)
        assert response.status_code==302 and response.headers['cache-control']=='no-store'
        assert client.head(target+'?token='+secret,follow_redirects=False).status_code==302
        env['files']['10']['size']=90
        assert client.get(target+'?token='+secret,follow_redirects=False).status_code==409


def test_public_management_requires_auth(env):
    app=FastAPI();app.include_router(routes.router)
    with TestClient(app) as client:
        assert client.get('/api/cloud115/status').status_code==401
        response=client.get('/api/cloud115/status',auth=('admin','test-pass-123'))
        assert response.status_code==200 and 'secret-access' not in response.text


def test_account_tokens_are_verified_and_never_projected(env,monkeypatch):
    monkeypatch.setattr(service,'transport',lambda *a,**k:{'state':True,'data':{'user_id':'456'}})
    result=service.save_account({'name':'新账号','access_token':'new-access','refresh_token':'new-refresh'})
    assert repo.account(result['id'])['uid']=='456'
    assert 'new-access' not in json.dumps(service.public_status())


def test_unknown_stat_response_is_not_absence():
    with pytest.raises(provider.ProviderError):provider.normalize_file(None,'10')
    with pytest.raises(provider.ProviderError) as caught:provider.check({'state':False,'errno':99999},'stat')
    assert not caught.value.missing
    with pytest.raises(provider.ProviderError) as caught:provider.check({'state':False,'errno':20004},'stat')
    assert caught.value.missing


def test_refresh_rotation_is_saved_before_retry_and_uid_is_checked(env,monkeypatch):
    calls=[]
    def transport(action,payload=None,access_token=''):
        calls.append((action,access_token))
        if action=='refresh':return {'state':True,'data':{'access_token':'rotated','refresh_token':'rotated-refresh','expires_in':3600}}
        if action=='user':return {'state':True,'data':{'user_id':'123'}}
        if access_token=='secret-access':raise provider.ProviderError('expired',401,auth=True)
        return {'state':True,'data':[]}
    monkeypatch.setattr(provider,'transport',transport)
    provider.Driver(env['account']).files()
    cfg=repo.account(env['account'])
    assert cfg['access_token']=='rotated' and cfg['refresh_token']=='rotated-refresh' and cfg['enabled']
    assert calls==[('files','secret-access'),('refresh',''),('user','rotated'),('files','rotated')]


def test_ambiguous_refresh_does_not_reuse_consumed_token(env,monkeypatch):
    calls=[]
    def transport(action,payload=None,access_token=''):
        calls.append(action)
        raise provider.ProviderError('ambiguous') if action=='refresh' else provider.ProviderError('expired',401,auth=True)
    monkeypatch.setattr(provider,'transport',transport)
    with pytest.raises(provider.ProviderError):provider.Driver(env['account']).files()
    with pytest.raises(provider.ProviderError):provider.Driver(env['account']).files()
    assert calls==['files','refresh'] and not repo.account(env['account'])['enabled']


@pytest.mark.parametrize('url',['http://[invalid','http://nas:abc','http://nas:0','http://nas:65536','http://user:pass@nas','http://nas/x/../y','http://nas/x?token=secret'])
def test_policy_rejects_malformed_or_credential_urls(env,url):
    with pytest.raises(provider.ProviderError) as caught:
        service.save_policy({'require_binding':False,'playback_base':url})
    assert caught.value.code==400 and env['cfg']['cloud115_playback_base']=='http://nas:8321'


def test_generation_rejects_symlink_ancestor_without_external_write(env,tmp_path):
    other=tmp_path/'external';other.mkdir()
    (env['root']/'escape').symlink_to(other,target_is_directory=True)
    with pytest.raises(provider.ProviderError):
        service.generate({'account_id':env['account'],'file_id':'10','relative_path':'escape/video.strm'})
    assert list(other.iterdir())==[] and not env['writes']


def test_generation_interrupted_after_write_can_be_reconciled(env,monkeypatch):
    original=repo.set_binding_state
    def fail_activate(key,state,fingerprint=None):
        if state=='active':raise OSError('fixture commit interruption')
        return original(key,state,fingerprint)
    monkeypatch.setattr(repo,'set_binding_state',fail_activate)
    with pytest.raises(OSError):generate(env,sidecar_ids=['11'])
    binding=repo.bindings()[0]
    assert binding['state']=='needs_review' and Path(binding['strm_path']).is_file()
    assert cleanup(env,binding['strm_path'])['errors']
    monkeypatch.setattr(repo,'set_binding_state',original)
    assert service.reconcile(binding['id'])['state']=='active'
    assert not env['writes']


def test_reconcile_pending_requires_all_sidecar_identities(env):
    result=generate(env,sidecar_ids=['11'])
    repo.set_binding_state(result['binding_id'],'needs_review')
    env['files']['11']['size']=4
    with pytest.raises(provider.ProviderError):service.reconcile(result['binding_id'])
    assert repo.binding(binding_id=result['binding_id'])['state']=='needs_review' and not env['writes']


def test_reconcile_unknown_delete_never_resubmits_or_unlinks(env):
    result=generate(env);env['driver'].uncertain=True
    assert cleanup(env,result['strm_path'])['errors']
    assert service.reconcile(result['binding_id'])['state']=='remote_deleted'
    assert env['writes']==['10'] and Path(result['strm_path']).is_file()
    assert cleanup(env,result['strm_path'])['strm_removed']==1 and env['writes']==['10']


def test_reconcile_visible_unknown_stays_pending(env):
    result=generate(env);env['driver'].delay=True
    cleanup(env,result['strm_path'])
    assert service.reconcile(result['binding_id'])['state']=='active'
    assert repo.receipt(result['binding_id'],'10')['state']=='unknown' and env['writes']==['10']


def test_reappeared_confirmed_id_never_redeleted(env):
    result=generate(env)
    repo.begin_delete(result['binding_id'],repo.binding(binding_id=result['binding_id'])['remote'])
    repo.finish_delete(result['binding_id'],'10','confirmed')
    with pytest.raises(provider.ProviderError):service.reconcile(result['binding_id'])
    assert cleanup(env,result['strm_path'])['errors'] and not env['writes']


def test_reconcile_rejects_changed_root(env,monkeypatch,tmp_path):
    result=generate(env)
    root=tmp_path/'new-root';root.mkdir();monkeypatch.setattr(engine,'L_ROOT',root)
    with pytest.raises(provider.ProviderError):service.reconcile(result['binding_id'])
    assert not env['writes']


def test_final_state_failure_reports_actual_local_deletion_and_recovers(env,monkeypatch):
    result=generate(env)
    monkeypatch.setattr(service,'finish_local',lambda *a:(_ for _ in ()).throw(OSError('fixture final state failure')))
    first=cleanup(env,result['strm_path'])
    assert first['errors'] and first['strm_removed']==1 and not Path(result['strm_path']).exists()
    assert repo.binding(binding_id=result['binding_id'])['state']=='remote_deleted'
    assert service.reconcile(result['binding_id'])['state']=='deleted'
    assert env['writes']==['10']


@pytest.fixture
def device(env,monkeypatch):
    from app import cloud115_auth as device_auth
    now=[1000.0];calls=[];status=[2];exchange_error=[False];verify_error=[False]
    monkeypatch.setattr(device_auth.time,'time',lambda:now[0])
    def transport(action,payload=None,access_token=''):
        calls.append((action,payload,access_token))
        if action=='device_start':return {'code':0,'data':{'uid':'qr-nonce','time':1000,'sign':'signature','qrcode':'http://115.com/scan/dg-sample'}}
        if action=='device_status':return {'code':0,'data':{'status':status[0]}}
        if action=='device_exchange':
            if exchange_error[0]:raise provider.ProviderError('network unknown')
            return {'code':0,'data':{'access_token':'qr-access','refresh_token':'qr-refresh'}}
        if action=='user':
            if verify_error[0]:raise provider.ProviderError('read failure')
            return {'state':True,'data':{'user_id':'123'}}
        pytest.fail(action)
    monkeypatch.setattr(device_auth,'transport',transport)
    return {'auth':device_auth,'now':now,'calls':calls,'status':status,'exchange_error':exchange_error,'verify_error':verify_error}


def test_device_pkce_qr_and_tokens_do_not_escape(device):
    import base64
    auth=device['auth'];start=auth.start({'app_id':'789','name':'扫码账号'})
    row=auth._sessions()[start['session_id']]
    challenge=base64.b64encode(hashlib.sha256(row['verifier'].encode()).digest()).decode()
    assert device['calls'][0][1]=={'client_id':'789','code_challenge':challenge,'code_challenge_method':'sha256'}
    assert row['verifier'] not in json.dumps(start) and row['uid'] not in json.dumps(start)
    assert b'<svg' in auth.qr_svg(start['session_id'])
    result=auth.poll(start['session_id'])
    assert result['state']=='done' and result['account']['uid']=='123'
    assert 'qr-access' not in json.dumps(result) and 'verifier' not in auth._sessions()[start['session_id']]
    assert auth.poll(start['session_id'])==result
    assert [x[0] for x in device['calls']].count('device_exchange')==1


def test_device_unknown_exchange_never_repeated(device):
    auth=device['auth'];start=auth.start({'app_id':'789'})
    device['exchange_error'][0]=True
    with pytest.raises(provider.ProviderError):auth.poll(start['session_id'])
    device['exchange_error'][0]=False
    assert auth.poll(start['session_id'])['state']=='unknown'
    assert [x[0] for x in device['calls']].count('device_exchange')==1


def test_device_verification_failure_resumes_read_only(device):
    auth=device['auth'];start=auth.start({'app_id':'789'})
    device['verify_error'][0]=True
    with pytest.raises(provider.ProviderError):auth.poll(start['session_id'])
    assert auth._sessions()[start['session_id']]['state']=='verifying'
    device['verify_error'][0]=False
    assert auth.poll(start['session_id'])['state']=='done'
    assert [x[0] for x in device['calls']].count('device_exchange')==1


def test_device_poll_throttles_and_expires(device):
    auth=device['auth'];device['status'][0]=1
    start=auth.start({'app_id':'789'})
    assert auth.poll(start['session_id'])['state']=='scanned'
    assert auth.poll(start['session_id'])['state']=='scanned'
    assert [x[0] for x in device['calls']].count('device_status')==1
    device['now'][0]+=301
    with pytest.raises(provider.ProviderError) as caught:auth.poll(start['session_id'])
    assert caught.value.code==410


def test_device_sessions_are_bounded(device):
    for _ in range(10):device['auth'].start({'app_id':'789'})
    with pytest.raises(provider.ProviderError) as caught:device['auth'].start({'app_id':'789'})
    assert caught.value.code==429


@pytest.mark.parametrize('flag,ua,allowed',[('0','Player/1',True),('1','Player/1',True),('1','',False),('3','Player/1',False)])
def test_download_uses_player_ua_and_rejects_cookie_links(env,monkeypatch,flag,ua,allowed):
    calls=[]
    def transport(action,payload=None,access_token='',user_agent=None):
        calls.append(user_agent)
        return {'state':True,'data':{'10':{'url':{'url':'https://cdn.example/movie?f='+flag}}}}
    monkeypatch.setattr(provider,'transport',transport)
    driver=provider.Driver(env['account'])
    if allowed:assert driver.download('abcd123456',user_agent=ua).startswith('https://cdn.example')
    else:
        with pytest.raises(provider.ProviderError):driver.download('abcd123456',user_agent=ua)
    assert calls==[ua]


def test_access_log_filter_removes_playback_and_device_secrets():
    import logging
    from app.security import Cloud115AccessFilter
    filt=Cloud115AccessFilter()
    for target in ('/play/115/binding?token=secret','/api/cloud115/authorization/secret/qr.svg',
                   '/strimkeep/play/115/binding?token=secret','/strimkeep/api/cloud115/authorization/secret/qr.svg'):
        row=logging.LogRecord('uvicorn.access',20,'',0,'%s %s %s %s %s',('ip','GET',target,'1.1',200),None)
        assert filt.filter(row) and 'secret' not in row.getMessage()


def test_download_refuses_other_file_id(env,monkeypatch):
    monkeypatch.setattr(provider,'transport',lambda *a,**k:{'state':True,'data':{'99':{'url':{'url':'https://cdn.example/other'}}}})
    with pytest.raises(provider.ProviderError):provider.Driver(env['account']).download('abcd123456',file_id='10')


@pytest.fixture
def organize_env(env,monkeypatch):
    from app import cloud115_organize as organize
    for item in env['files'].values():item['parent_id']='1'
    env['files']['1']={'id':'1','name':'来源','parent_id':'0','is_dir':True,'size':0,'sha1':'','pickcode':''}
    env['files']['2']={'id':'2','name':'目标','parent_id':'0','is_dir':True,'size':0,'sha1':'','pickcode':''}
    writes=[]
    class Driver(env['driver']):
        fail_action=None
        apply_on_error=True
        def files(self,parent_id='0',offset=0,limit=50):
            rows=[dict(x) for x in env['files'].values() if x.get('parent_id')==parent_id]
            page=rows[offset:offset+limit]
            return {'items':page,'next_offset':offset+len(page) if len(page)==limit else None}
        def move(self,fid,parent):
            writes.append(('move',fid,parent))
            if self.fail_action!='move' or self.apply_on_error:env['files'][fid]['parent_id']=parent
            if self.fail_action=='move':raise provider.ProviderError('fixture unknown move')
        def rename(self,fid,name):
            writes.append(('rename',fid,name))
            if self.fail_action!='rename' or self.apply_on_error:env['files'][fid]['name']=name
            if self.fail_action=='rename':raise provider.ProviderError('fixture unknown rename')
    monkeypatch.setattr(organize,'Driver',Driver)
    monkeypatch.setattr(service,'Driver',Driver)
    env.update(driver=Driver,organize=organize,organize_writes=writes)
    return env


def organize_preview(env,**extra):
    return env['organize'].preview(dict(account_id=env['account'],file_id='10',target_parent='2',new_name='标准.S01E01.2160p.mkv',**extra))


def test_organize_preview_is_readonly_and_execution_preserves_strm_identity(organize_env):
    e=organize_env;bound=generate(e,sidecar_ids=['11']);path=Path(bound['strm_path']);original=path.read_bytes()
    preview=organize_preview(e,sidecars=[{'file_id':'11','new_name':'标准.S01E01.2160p.ass'}])
    assert preview['step_count']==4 and not e['organize_writes']
    assert e['files']['10']['parent_id']=='1'
    assert e['organize'].execute(preview['plan_id'])['state']=='done'
    assert path.read_bytes()==original
    binding=repo.binding(binding_id=bound['binding_id'])
    assert binding['remote']['name']=='标准.S01E01.2160p.mkv' and binding['remote']['parent_id']=='2'
    assert binding['sidecars'][0]['name']=='标准.S01E01.2160p.ass'
    assert [x[:2] for x in e['organize_writes']]==[('move','10'),('rename','10'),('move','11'),('rename','11')]
    assert cleanup(e,path)['strm_removed']==1 and e['writes']==['10','11']


def test_completed_organize_confirmation_is_idempotent(organize_env):
    e=organize_env;plan=organize_preview(e)['plan_id']
    e['organize'].execute(plan);first=list(e['organize_writes'])
    assert e['organize'].execute(plan)['state']=='done' and e['organize_writes']==first


@pytest.mark.parametrize('name',['../video.mkv','new.mp4',' new.mkv','new?.mkv','bad\\video.mkv'])
def test_organize_never_accepts_path_or_extension_change(organize_env,name):
    e=organize_env
    with pytest.raises(provider.ProviderError):e['organize'].preview({'account_id':e['account'],'file_id':'10','target_parent':'2','new_name':name})
    assert not e['organize_writes']


@pytest.mark.parametrize('field,value',[('name','externally-changed.mkv'),('parent_id','0'),('size',999),('sha1','C'*40)])
def test_organize_stale_source_plan_never_writes(organize_env,field,value):
    e=organize_env;plan=organize_preview(e)['plan_id'];e['files']['10'][field]=value
    with pytest.raises(provider.ProviderError):e['organize'].execute(plan)
    assert not e['organize_writes']


@pytest.mark.parametrize('timing',['preview','execute'])
@pytest.mark.parametrize('name',['标准.S01E01.2160p.mkv','样本.S01E01.2160p.mkv'])
def test_organize_rejects_destination_collision_at_each_stage(organize_env,timing,name):
    e=organize_env
    if timing=='execute':plan=organize_preview(e)['plan_id']
    e['files']['99']=dict(e['files']['10'],id='99',name=name,parent_id='2')
    with pytest.raises(provider.ProviderError):
        if timing=='preview':organize_preview(e)
        else:e['organize'].execute(plan)
    assert not e['organize_writes']


def test_organize_target_directory_change_invalidates_preview(organize_env):
    e=organize_env;plan=organize_preview(e)['plan_id'];e['files']['2']['name']='另一个用途'
    with pytest.raises(provider.ProviderError):e['organize'].execute(plan)
    assert not e['organize_writes']


def test_organize_unknown_move_blocks_governance_and_resumes_only_new_steps(organize_env):
    e=organize_env;bound=generate(e);plan=organize_preview(e)['plan_id'];e['driver'].fail_action='move'
    with pytest.raises(provider.ProviderError):e['organize'].execute(plan)
    assert cleanup(e,bound['strm_path'])['errors'] and not e['writes']
    with pytest.raises(provider.ProviderError):e['organize'].execute(plan)
    assert e['organize_writes']==[('move','10','2')]
    e['driver'].fail_action=None
    assert e['organize'].reconcile(plan)['state']=='awaiting_resume'
    assert e['organize'].execute(plan)['state']=='done'
    assert [x[0] for x in e['organize_writes']]==['move','rename']
    assert cleanup(e,bound['strm_path'])['strm_removed']==1


def test_organize_unknown_rename_reconciles_without_writes(organize_env):
    e=organize_env;plan=organize_preview(e)['plan_id'];e['driver'].fail_action='rename'
    with pytest.raises(provider.ProviderError):e['organize'].execute(plan)
    before=list(e['organize_writes'])
    assert e['organize'].reconcile(plan)['state']=='done' and e['organize_writes']==before


def test_organize_unknown_unchanged_source_is_never_retried(organize_env):
    e=organize_env;plan=organize_preview(e)['plan_id'];e['driver'].fail_action='move';e['driver'].apply_on_error=False
    with pytest.raises(provider.ProviderError):e['organize'].execute(plan)
    e['driver'].fail_action=None
    with pytest.raises(provider.ProviderError):e['organize'].reconcile(plan)
    with pytest.raises(provider.ProviderError):organize_preview(e)
    with pytest.raises(provider.ProviderError):generate(e)
    assert e['organize_writes']==[('move','10','2')] and not repo.bindings()


def test_organize_does_not_write_when_intent_cannot_be_saved(organize_env,monkeypatch):
    e=organize_env;plan=organize_preview(e)['plan_id'];original=e['organize']._save
    def fail(rows):
        if any(s['state']=='submitted' for p in rows.values() for s in p['steps']):raise OSError('fixture storage failure')
        return original(rows)
    monkeypatch.setattr(e['organize'],'_save',fail)
    with pytest.raises(OSError):e['organize'].execute(plan)
    assert not e['organize_writes']


def test_organize_rejects_foreign_binding_companion(organize_env):
    e=organize_env;e['files']['12']=dict(e['files']['10'],id='12',pickcode='abcd123459')
    service.generate({'account_id':e['account'],'file_id':'12','sidecar_ids':['11'],'relative_path':'other.strm'})
    with pytest.raises(provider.ProviderError):organize_preview(e,sidecars=[{'file_id':'11'}])
    assert not e['organize_writes']


def test_organize_refuses_any_existing_delete_intent(organize_env):
    e=organize_env;bound=generate(e);binding=repo.binding(binding_id=bound['binding_id'])
    repo.begin_delete(bound['binding_id'],binding['remote'])
    with pytest.raises(provider.ProviderError):organize_preview(e)
    assert not e['organize_writes']


def test_organize_never_assumes_truncated_destination_is_empty(organize_env):
    e=organize_env
    for i in range(1000):e['files'][str(10000+i)]=dict(e['files']['11'],id=str(10000+i),name=str(i)+'.ass',parent_id='2')
    with pytest.raises(provider.ProviderError):organize_preview(e)
    assert not e['organize_writes']


def test_organize_missing_parent_id_is_not_inferred_from_path(organize_env):
    e=organize_env;e['files']['10'].pop('parent_id')
    with pytest.raises(provider.ProviderError):organize_preview(e)
    assert not e['organize_writes']


@pytest.mark.parametrize('action',['move','rename'])
def test_provider_never_refreshes_or_retries_organize_writes(env,monkeypatch,action):
    calls=[]
    def transport(*args,**kwargs):
        calls.append(args[0]);raise provider.ProviderError('expired',401,auth=True)
    monkeypatch.setattr(provider,'transport',transport)
    driver=provider.Driver(env['account'])
    with pytest.raises(provider.ProviderError):
        if action=='move':driver.move('10','2')
        else:driver.rename('10','new.mkv')
    assert calls==[action] and repo.account(env['account'])['enabled']


def test_organize_execute_requires_explicit_confirmation(env,monkeypatch):
    from types import SimpleNamespace
    app=FastAPI();app.include_router(routes.router);calls=[]
    monkeypatch.setattr(routes,'spawn',lambda *a,**k:(calls.append(a) or SimpleNamespace(id='job')))
    with TestClient(app) as client:
        url='/api/cloud115/organize/id/execute'
        assert client.post(url,json={'confirm':True}).status_code==401
        assert client.post(url,json={},auth=('admin','test-pass-123')).status_code==400
        assert not calls
        assert client.post(url,json={'confirm':True},auth=('admin','test-pass-123')).status_code==202
        assert len(calls)==1 and calls[0][2]=='id'


def test_organize_binding_change_requires_new_preview(organize_env):
    e=organize_env;plan=organize_preview(e)['plan_id'];generate(e)
    with pytest.raises(provider.ProviderError):e['organize'].execute(plan)
    assert not e['organize_writes']


def test_unsubmitted_plan_can_cancel_without_remote_mutation(organize_env):
    e=organize_env;plan=organize_preview(e)['plan_id']
    assert e['organize'].cancel(plan)['state']=='cancelled'
    with pytest.raises(provider.ProviderError):e['organize'].execute(plan)
    assert not e['organize_writes']


def test_submitted_unknown_plan_cannot_be_cancelled_or_retried(organize_env):
    e=organize_env;plan=organize_preview(e)['plan_id'];e['driver'].fail_action='move'
    with pytest.raises(provider.ProviderError):e['organize'].execute(plan)
    with pytest.raises(provider.ProviderError):e['organize'].cancel(plan)
    assert e['organize_writes']==[('move','10','2')]


def test_named_detail_response_preserves_explicit_parent_and_hash():
    row=provider.normalize_file({'file_id':'10','file_category':1,'file_name':'video.mkv',
        'file_size':'50','file_sha1':'a'*40,'category_id':'2','pick_code':'abcd123456'},'10')
    assert row['parent_id']=='2' and row['size']==50 and row['sha1']=='A'*40


def test_directory_detail_never_uses_category_id_as_video_identity():
    row=provider.normalize_file({'category_id':'2','file_category':0,'category_name':'目标','parent_id':'0'},'2')
    assert row['is_dir'] and row['id']=='2' and row['parent_id']=='0'
    with pytest.raises(provider.ProviderError):provider.normalize_file({'category_id':'2','file_category':1,'file_name':'video.mkv','file_size':50},'2')


@pytest.fixture
def transfer_env(organize_env,monkeypatch):
    from app import cloud115_transfer as transfer
    e=organize_env;calls=[];mkdirs=[];source={'100':dict(e['files']['10'],id='100'),'101':dict(e['files']['11'],id='101')}
    options={'user_uid':'123','receive_error':False,'apply_receive':True,'mkdir_error':False,'apply_mkdir':True,'foreign':False}
    counter=[500,600]
    class Driver(e['driver']):
        def mkdir(self,parent,name):
            fid=str(counter[0]);counter[0]+=1;mkdirs.append((parent,name,fid))
            if options['apply_mkdir']:e['files'][fid]={'id':fid,'name':name,'parent_id':parent,'is_dir':True,'size':0,'sha1':'','pickcode':''}
            if options['mkdir_error']:raise provider.ProviderError('fixture mkdir timeout')
            return fid
    def transport(action,payload=None,web_cookie=''):
        calls.append((action,payload,web_cookie))
        if action=='user':return {'state':True,'data':{'user_id':options['user_uid']}}
        if action=='share':
            rows=[{'fid':x['id'],'cid':'0','n':x['name'],'s':x['size'],'sha':x['sha1']} for x in source.values()]
            rows.append({'cid':'888','pid':'0','n':'共享目录'})
            return {'state':True,'data':{'list':rows}}
        if action=='receive':
            if options['apply_receive']:
                for sid in payload['file_id'].split(','):
                    fid=str(counter[1]);counter[1]+=1
                    e['files'][fid]=dict(source[sid],id=fid,parent_id=payload['cid'],pickcode='target115'+fid)
                if options['foreign']:e['files']['999']=dict(e['files']['11'],id='999',parent_id=payload['cid'])
            if options['receive_error']:raise provider.ProviderError('fixture receive timeout')
            return {'state':True}
        pytest.fail(action)
    monkeypatch.setattr(transfer,'Driver',Driver);monkeypatch.setattr(service,'Driver',Driver);monkeypatch.setattr(e['organize'],'Driver',Driver)
    monkeypatch.setattr(transfer.web,'transport',transport)
    e.update(driver=Driver,transfer=transfer,transfer_calls=calls,mkdirs=mkdirs,source=source,options=options)
    transfer.save_cookie({'account_id':e['account'],'cookie':'UID=123_A1; CID=secret-cookie-cid; SEID=secret-cookie-seid'})
    return e


def transfer_preview(e,sidecars=None):
    view=e['transfer'].inspect({'account_id':e['account'],'share_url':'https://115.com/s/sharecodeabc?password=c1d2'})
    return e['transfer'].preview({'handle':view['handle'],'file_id':'100','sidecar_ids':sidecars or [],'target_parent':'2'})


def test_transfer_cookie_is_separate_verified_and_never_projected(transfer_env):
    e=transfer_env;status=service.public_status()
    assert status['accounts'][0]['has_transfer_cookie'] and status['capabilities']['transfer_auth']=='web_cookie'
    assert 'secret-cookie' not in json.dumps(status) and 'secret-access' not in json.dumps(status)
    e['options']['user_uid']='456'
    with pytest.raises(provider.ProviderError):e['transfer'].save_cookie({'account_id':e['account'],'cookie':'UID=123_A1; CID=other; SEID=other'})
    assert e['transfer']._read('credentials')[e['account']]['revision']==1


def test_transfer_previews_do_not_write_or_expose_access_code(transfer_env):
    e=transfer_env;plan=transfer_preview(e,['101'])
    assert len(plan['files'])==2 and plan['mkdir_state']=='pending'
    assert not e['mkdirs'] and not any(x[0]=='receive' for x in e['transfer_calls'])
    assert 'c1d2' not in json.dumps(plan) and 'cookie' not in json.dumps(plan)


def test_transfer_maps_new_ids_and_auto_registers_only_captured_companions(transfer_env):
    e=transfer_env;plan=transfer_preview(e,['101']);result=e['transfer'].execute(plan['plan_id'])
    assert [x['file_id'] for x in result['targets']]==['600','601']
    assert [x['source_id'] for x in result['targets']]==['100','101']
    assert repo.received(e['account'],'600')['related_ids']==['601']
    generated=service.generate({'account_id':e['account'],'file_id':'600','relative_path':'转存/标准.strm'})
    binding=repo.binding(binding_id=generated['binding_id'])
    assert [x['id'] for x in binding['sidecars']]==['601']
    assert binding['remote']['provenance']['source_file_id']=='100'
    assert cleanup(e,generated['strm_path'])['strm_removed']==1
    assert e['writes']==['600','601'] and '10' in e['files'] and '100' in e['source']


def test_received_provenance_survives_pruned_task_history(transfer_env):
    e=transfer_env;plan=transfer_preview(e,['101']);e['transfer'].execute(plan['plan_id'])
    e['transfer']._save('plans',{})
    assert e['transfer'].known_sidecars(e['account'],'600')==['601']
    generated=service.generate({'account_id':e['account'],'file_id':'600','relative_path':'durable.strm'})
    assert repo.binding(binding_id=generated['binding_id'])['remote']['provenance']['source_file_id']=='100'


def test_transfer_unknown_receive_is_readonly_reconciled_without_duplicate(transfer_env):
    e=transfer_env;plan=transfer_preview(e,['101']);e['options']['receive_error']=True
    with pytest.raises(provider.ProviderError):e['transfer'].execute(plan['plan_id'])
    with pytest.raises(provider.ProviderError):e['transfer'].execute(plan['plan_id'])
    with pytest.raises(provider.ProviderError):transfer_preview(e,['101'])
    with pytest.raises(provider.ProviderError):service.generate({'account_id':e['account'],'file_id':'600','relative_path':'unsafe.strm'})
    e['options']['receive_error']=False
    assert e['transfer'].reconcile(plan['plan_id'])['state']=='done'
    assert len([x for x in e['transfer_calls'] if x[0]=='receive'])==1 and len(e['mkdirs'])==1


def test_unknown_mkdir_recovers_exact_intent_and_only_receives_once(transfer_env):
    e=transfer_env;plan=transfer_preview(e);e['options']['mkdir_error']=True
    with pytest.raises(provider.ProviderError):e['transfer'].execute(plan['plan_id'])
    assert not any(x[0]=='receive' for x in e['transfer_calls'])
    assert e['transfer'].reconcile(plan['plan_id'])['state']=='awaiting_resume'
    e['options']['mkdir_error']=False
    assert e['transfer'].execute(plan['plan_id'])['state']=='done'
    assert len(e['mkdirs'])==1 and len([x for x in e['transfer_calls'] if x[0]=='receive'])==1


def test_mkdir_with_no_confirmed_object_is_never_repeated(transfer_env):
    e=transfer_env;plan=transfer_preview(e);e['options'].update(mkdir_error=True,apply_mkdir=False)
    with pytest.raises(provider.ProviderError):e['transfer'].execute(plan['plan_id'])
    with pytest.raises(provider.ProviderError):e['transfer'].reconcile(plan['plan_id'])
    with pytest.raises(provider.ProviderError):e['transfer'].cancel(plan['plan_id'])
    assert len(e['mkdirs'])==1 and not any(x[0]=='receive' for x in e['transfer_calls'])


def test_unknown_receive_with_no_objects_is_never_repeated(transfer_env):
    e=transfer_env;plan=transfer_preview(e);e['options'].update(receive_error=True,apply_receive=False)
    with pytest.raises(provider.ProviderError):e['transfer'].execute(plan['plan_id'])
    with pytest.raises(provider.ProviderError):e['transfer'].reconcile(plan['plan_id'])
    with pytest.raises(provider.ProviderError):e['transfer'].cancel(plan['plan_id'])
    assert len([x for x in e['transfer_calls'] if x[0]=='receive'])==1


def test_foreign_object_in_transfer_folder_blocks_mapping_and_binding(transfer_env):
    e=transfer_env;plan=transfer_preview(e);e['options']['foreign']=True
    with pytest.raises(provider.ProviderError):e['transfer'].execute(plan['plan_id'])
    assert not repo.received(e['account'],'600')
    with pytest.raises(provider.ProviderError):service.generate({'account_id':e['account'],'file_id':'600','relative_path':'unsafe.strm'})
    assert not repo.bindings()


def test_already_received_source_is_not_copied_again(transfer_env):
    e=transfer_env;plan=transfer_preview(e);e['transfer'].execute(plan['plan_id'])
    before=len(e['transfer_calls']);assert e['transfer'].execute(plan['plan_id'])['state']=='done'
    assert len(e['transfer_calls'])==before
    with pytest.raises(provider.ProviderError):transfer_preview(e)
    assert len(e['mkdirs'])==1


def test_deleted_received_copy_may_be_transferred_to_new_id(transfer_env):
    e=transfer_env;first=transfer_preview(e);e['transfer'].execute(first['plan_id']);e['files'].pop('600')
    second=transfer_preview(e);result=e['transfer'].execute(second['plan_id'])
    assert result['targets'][0]['file_id']=='601' and len(e['mkdirs'])==2


@pytest.mark.parametrize('change',['share','cookie','target'])
def test_transfer_stale_confirmation_never_creates_or_receives(transfer_env,change):
    e=transfer_env;plan=transfer_preview(e)
    if change=='share':e['source']['100']['sha1']='C'*40
    elif change=='cookie':e['transfer'].save_cookie({'account_id':e['account'],'cookie':'UID=123_A1; CID=new; SEID=new'})
    else:e['files']['2']['name']='changed'
    with pytest.raises(provider.ProviderError):e['transfer'].execute(plan['plan_id'])
    assert not e['mkdirs'] and not any(x[0]=='receive' for x in e['transfer_calls'])


def test_transfer_cannot_select_directory_or_forged_file_id(transfer_env):
    e=transfer_env;view=e['transfer'].inspect({'account_id':e['account'],'share_url':'https://115.com/s/sharecodeabc'})
    for fid in ('888','999'):
        with pytest.raises(provider.ProviderError):e['transfer'].preview({'handle':view['handle'],'file_id':fid,'target_parent':'2'})
    assert not e['mkdirs']


def test_duplicate_source_content_is_not_mapped_by_name(transfer_env):
    e=transfer_env;e['source']['101'].update(size=50,sha1='A'*40)
    with pytest.raises(provider.ProviderError):transfer_preview(e,['101'])
    assert not e['mkdirs']


def test_transfer_foreign_provenance_cannot_be_manually_paired(transfer_env):
    e=transfer_env;first=transfer_preview(e,['101']);e['transfer'].execute(first['plan_id'])
    with pytest.raises(provider.ProviderError):service.generate({'account_id':e['account'],'file_id':'10','sidecar_ids':['601'],'relative_path':'foreign.strm'})
    assert not repo.bindings()


def test_organize_uses_captured_companions_without_filename_pairing(transfer_env):
    e=transfer_env;plan=transfer_preview(e,['101']);e['transfer'].execute(plan['plan_id'])
    e['files']['3']=dict(e['files']['2'],id='3',name='整理目录')
    preview=e['organize'].preview({'account_id':e['account'],'file_id':'600','target_parent':'3','new_name':'标准.S01E01.mkv'})
    assert [x['file_id'] for x in preview['files']]==['600','601']
    assert e['organize'].execute(preview['plan_id'])['state']=='done'
    assert e['files']['600']['parent_id']=='3' and e['files']['601']['parent_id']=='3'


def test_transfer_cancellation_preserves_created_empty_folder(transfer_env):
    e=transfer_env;plan=transfer_preview(e);e['options']['mkdir_error']=True
    with pytest.raises(provider.ProviderError):e['transfer'].execute(plan['plan_id'])
    e['transfer'].reconcile(plan['plan_id'])
    assert e['transfer'].cancel(plan['plan_id'])['state']=='cancelled' and '500' in e['files']
    assert not e['writes'] and not any(x[0]=='receive' for x in e['transfer_calls'])


def test_adoption_retains_captured_transfer_associations(transfer_env):
    e=transfer_env;plan=transfer_preview(e,['101']);e['transfer'].execute(plan['plan_id'])
    path=e['root']/'old-transferred.strm';path.write_text('http://upstream/play?pickcode=target115600')
    adopted=service.adopt({'account_id':e['account'],'relative_path':path.name})
    binding=repo.binding(binding_id=adopted['binding_id'])
    assert [x['id'] for x in binding['sidecars']]==['601']
    assert cleanup(e,path)['strm_removed']==1 and e['writes']==['600','601']


def test_known_transfer_metadata_cannot_be_relabelled_as_video(transfer_env):
    e=transfer_env;plan=transfer_preview(e,['101']);e['transfer'].execute(plan['plan_id']);e['files']['601']['name']='false-video.mkv'
    with pytest.raises(provider.ProviderError):service.generate({'account_id':e['account'],'file_id':'601','relative_path':'false.strm'})
    assert not repo.bindings()


def test_transfer_metadata_save_failure_reconciles_without_receiving_twice(transfer_env,monkeypatch):
    e=transfer_env;plan=transfer_preview(e,['101']);original=repo.save_received
    monkeypatch.setattr(repo,'save_received',lambda *a:(_ for _ in ()).throw(OSError('fixture metadata persistence failure')))
    with pytest.raises(provider.ProviderError):e['transfer'].execute(plan['plan_id'])
    assert not repo.received(e['account'],'600')
    monkeypatch.setattr(repo,'save_received',original)
    assert e['transfer'].reconcile(plan['plan_id'])['state']=='done'
    assert len([x for x in e['transfer_calls'] if x[0]=='receive'])==1


@pytest.mark.parametrize('url',['https://evil.test/s/sharecodeabc','https://user:pass@115.com/s/sharecodeabc','https://115.com/s/sharecodeabc?password=ab12&password=cd34','http://[invalid','https://115.com/s/sharecodeabc?password=abc'])
def test_share_url_scope_is_fixed_and_unambiguous(url):
    from app.providers import p115_web
    with pytest.raises(provider.ProviderError):p115_web.share_link(url)


@pytest.mark.parametrize('value',['UID=123_A1; CID=c','UID=123_A1; CID=c; SEID=s\r\nX: unsafe','UID=123_A1; CID=c; SEID=s; other=value'])
def test_transfer_cookie_header_scope_and_controls(value):
    from app.providers import p115_web
    with pytest.raises(provider.ProviderError):p115_web.cookie(value)
