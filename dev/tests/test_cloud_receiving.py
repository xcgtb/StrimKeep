"""Real SQLite/queues and stateful 115/Bot fixtures; no external writes."""
import copy
import json
import time
from unittest.mock import Mock
import pytest
from dev.tests.test_cloud_workbench import cloud
from dev.tests.test_my_drive import client
from app import bot, config, tg
from app.cloud import receiving as r, maintenance as m, services, directories, settings, credentials, jobs, store, provider115, workflow, accounts
from app.cloud.errors import CloudError

MAGNET='magnet:?xt=urn:btih:'+'a'*40+'&dn=Movie'
ED2K='ed2k://|file|Movie.mkv|100|'+('b'*32)+'|/'
INSTANT='115://Movie.mkv|100|'+('a'*40)+'|'+('b'*40)
REAL_REFRESH=workflow.refresh_emby


@pytest.fixture
def receiving(cloud,monkeypatch):
    from app.routers import cloud as routes
    monkeypatch.setattr(routes,'for_account',lambda aid:cloud.remote)
    monkeypatch.setattr(directories,'for_account',lambda aid:cloud.remote)
    directories.save(cloud.id,{'source':'1','target':'2','transfer':'3','magnet':'3','ed2k':'3','instant':'3','bot':'3','channel':'3'},0)
    monkeypatch.setattr(cloud.remote,'offline_add',Mock(return_value={'accepted':True,'completed':False,'info_hash':'a'*40}),raising=False)
    monkeypatch.setattr(cloud.remote,'offline_tasks',lambda page=1:{'items':[],'page':page},raising=False)
    monkeypatch.setattr(cloud.remote,'instant_receive',Mock(return_value={'file_id':'9','completed':True}),raising=False)
    monkeypatch.setattr(cloud.remote,'sign_in',Mock(return_value={'accepted':True}),raising=False)
    cloud.bin=[{'id':'90','name':'old','deleted_at':time.time()-10*86400},{'id':'91','name':'new','deleted_at':time.time()},{'id':'92','name':'unknown','deleted_at':0}]
    monkeypatch.setattr(cloud.remote,'recycle_items',lambda:copy.deepcopy(cloud.bin),raising=False)
    cloud.purged=[]
    def purge(ids,password=''):
        cloud.purged.append((list(ids),password));cloud.bin[:]=[x for x in cloud.bin if x['id'] not in ids]
    monkeypatch.setattr(cloud.remote,'recycle_delete',purge,raising=False)
    monkeypatch.setattr(config,'load_config',lambda:{'telegram_bot_token':'test-token','telegram_allowed_users':'123','telegram_chat_id':'1'})
    def api(token,method,params,**kw):
        if method=='getMe':return {'ok':True,'result':{'id':88}}
        if method=='getChat':return {'ok':True,'result':{'id':-100123456,'type':'channel'}}
        if method=='getChatMember':return {'ok':True,'result':{'status':'administrator'}}
        raise AssertionError('unexpected Telegram method '+method)
    monkeypatch.setattr(bot,'_api',api)
    return cloud


def run(jid):
    jobs.execute(jid);return store.job(jid)


def channel(cloud,**patch):
    return r.save_channel(cloud.id,{'name':'测试频道','address':'@channelname','target_cid':'3','enabled':True,**patch})


def msg(text=MAGNET,mid=1,channel=False):
    return {'message_id':mid,'date':int(time.time())+1,'chat':{'id':-100123456 if channel else 123,'username':'channelname'},'from':{'id':123},'text':text}


@pytest.mark.parametrize('url,kind',[(MAGNET,'magnet'),(ED2K,'ed2k'),(INSTANT,'instant'),('https://115.com/s/abcdef123?password=abcd','share')])
def test_typed_links(url,kind):
    assert r.parse(url)['kind']==kind


@pytest.mark.parametrize('url',['http://127.0.0.1/x','file:///data/config','https://evil.test/s/abcdef123','magnet:?xt=bad','ed2k://|file|x|0|'+('a'*32)+'|/','115://../bad|100|'+('a'*40)+'|'+('b'*40),'115://x|bad|a|b',MAGNET+'\nHeader:x'])
def test_invalid_links_do_not_reach_remote(url):
    with pytest.raises(CloudError):r.parse(url)


def test_extract_hidden_links_password_and_dedup_without_swallowing_tmdb():
    rows=r.extract('分享 https://115.com/s/abcdef123 提取码:passSECRET\n'+MAGNET,[{'type':'text_link','url':MAGNET},{'type':'text_link','url':'https://www.themoviedb.org/movie/44'}])
    assert len(rows)==2 and rows[0]['password']=='passSECRET'


def test_manual_preview_is_read_only_encrypted_and_apply_is_single_use(receiving,client):
    p=r.preview(receiving.id,MAGNET+'\n'+ED2K+'\n'+INSTANT)
    assert [i['kind'] for i in p['items']]==['magnet','ed2k','instant']
    assert not receiving.remote.offline_add.called and not receiving.remote.instant_receive.called
    with store.db() as con:raw=con.execute('SELECT payload FROM cloud_plans WHERE id=?',(p['id'],)).fetchone()[0]
    assert MAGNET not in raw and '115://' not in raw
    with pytest.raises(CloudError):r.apply(p['id'],False)
    jid=r.apply(p['id'],True)
    with pytest.raises(CloudError):r.apply(p['id'],True)
    result=run(jid)
    assert result['state']=='success' and len(result['result']['accepted'])==3
    assert receiving.remote.offline_add.call_count==2
    assert '等待' in result['message'] and not result['result']['accepted'][0]['completed']


def test_share_link_enqueues_verified_transfer_child_once(receiving):
    jid=r.apply(r.preview(receiving.id,'https://115.com/s/abcdef123?password=passSECRET')['id'],True)
    result=run(jid);assert result['state']=='success'
    child=result['result']['child_jobs'][0]
    assert store.job(child)['kind']=='transfer' and not receiving.remote.writes
    with store.db() as con:
        raw=con.execute('SELECT payload FROM cloud_jobs WHERE id=?',(child,)).fetchone()[0]
    assert 'passSECRET' not in raw
    assert run(child)['state']=='success'
    jobs.execute(jid)
    assert len([w for w in receiving.remote.writes if w[0]=='receive'])==1


def test_missing_or_changed_directories_and_changed_credentials_block_before_write(receiving):
    p=r.preview(receiving.id,MAGNET)
    directories.save(receiving.id,{'magnet':'1'},1)
    assert run(r.apply(p['id'],True))['state']=='failed' and not receiving.remote.offline_add.called
    p=r.preview(receiving.id,MAGNET)
    with store.db() as con:con.execute('UPDATE cloud_accounts SET revision=revision+1 WHERE id=?',(receiving.id,))
    assert run(r.apply(p['id'],True))['state']=='failed'
    directories.save(receiving.id,{'ed2k':None},2)
    with pytest.raises(CloudError):r.preview(receiving.id,ED2K)


def test_uncertain_offline_request_is_never_replayed(receiving):
    receiving.remote.offline_add.side_effect=CloudError('结果待核实',502,uncertain=True)
    jid=r.apply(r.preview(receiving.id,MAGNET)['id'],True)
    assert run(jid)['state']=='uncertain'
    jobs.execute(jid);assert receiving.remote.offline_add.call_count==1
    assert jobs.details(jid)['steps'][0]['state']=='dispatched'


def test_tg_default_off_allowlist_and_durable_duplicate_capture(receiving,monkeypatch):
    assert r.capture(msg(),authorized=True)==[]
    r.save(receiving.id,{'tg_enabled':True},0)
    assert r.capture(msg(),authorized=False)==[]
    old=msg();old['date']=0
    assert not r.capture(old,authorized=True)
    first=r.capture(msg(),authorized=True);again=r.capture(msg(),authorized=True)
    assert first==again and len(first)==1
    r.schedule();r.schedule()
    history=r.inbox(receiving.id)
    assert len(history)==1 and history[0]['job_id']
    assert run(history[0]['job_id'])['state']=='success'
    assert receiving.remote.offline_add.call_count==1
    with store.db() as con:raw=con.execute('SELECT payload FROM cloud_inbox').fetchone()[0]
    assert 'magnet:' not in raw and 'test-token' not in raw


def test_tg_receiver_exclusive_account_and_config_cas(receiving):
    r.save(receiving.id,{'tg_enabled':True},0)
    with pytest.raises(CloudError):r.save(receiving.id,{'tg_enabled':False},0)
    other=accounts.save_authorization('999',{}, {'cookie':'UID=999_H1_1; CID=x; SEID=y'})
    directories.save(other,{'bot':'3'},0)
    with pytest.raises(CloudError):r.save(other,{'tg_enabled':True},0)


def test_tg_queued_message_config_change_blocks_dispatch(receiving):
    r.save(receiving.id,{'tg_enabled':True},0)
    r.capture(msg(),authorized=True);r.schedule()
    r.save(receiving.id,{'tg_enabled':False},1)
    jid=r.inbox(receiving.id)[0]['job_id']
    assert run(jid)['state']=='failed' and not receiving.remote.offline_add.called


def test_bot_command_uses_allowlist_and_preserves_existing_commands(receiving,monkeypatch):
    r.save(receiving.id,{'tg_enabled':True},0)
    sent=[]
    monkeypatch.setattr(bot,'_send',lambda *a,**k:sent.append(a[2]))
    monkeypatch.setattr(bot,'_schedule_delete',lambda *a:None)
    monkeypatch.setattr(bot,'_is_allowed',lambda u,c:u==123)
    bot._handle_command('token',msg('/receive '+MAGNET))
    assert '已登记' in sent[-1] and len(r.inbox(receiving.id))==1
    bad=msg(MAGNET,mid=2);bad['from']['id']=456
    bot._handle_command('token',bad)
    assert '没有权限' in sent[-1] and len(r.inbox(receiving.id))==1


def test_channel_validates_admin_and_scopes_to_verified_id(receiving,monkeypatch):
    rule=channel(receiving)
    assert rule['chat_id']=='-100123456'
    wrong=msg(channel=True);wrong['chat']['id']=-100555555
    assert not r.capture(wrong,channel=True)
    assert r.capture(msg(channel=True),channel=True)
    monkeypatch.setattr(bot,'_api',lambda *a,**kw:{'ok':False})
    with pytest.raises(CloudError,match='管理员'):channel(receiving,address='@different')


def test_channel_keywords_regex_and_tmdb_filters():
    rule={'include':['电影'],'exclude':['广告'],'regex':[r'2160p'],'tmdb_ids':['movie:44'],'kinds':['movie'],'countries':[],'genres':[]}
    assert r.allowed_message('电影 2160p https://www.themoviedb.org/movie/44',rule)[0]
    assert not r.allowed_message('电影 广告 2160p https://www.themoviedb.org/movie/44',rule)[0]
    assert not r.allowed_message('电影 1080p https://www.themoviedb.org/movie/44',rule)[0]
    assert not r.allowed_message('电影 2160p https://www.themoviedb.org/movie/45',rule)[0]
    assert not r.allowed_message('剧集 2160p https://www.themoviedb.org/tv/44',rule)[0]


def test_channel_records_filtered_and_transfers_matched_once(receiving):
    c=channel(receiving,include=['想看'])
    r.capture(msg(channel=True),channel=True);r.schedule()
    result=run(r.inbox(receiving.id)[0]['job_id'])
    assert result['state']=='success' and result['result']['filtered'] and not receiving.remote.offline_add.called
    r.capture(msg('想看 '+MAGNET,mid=2,channel=True),channel=True);r.schedule()
    result=run(r.inbox(receiving.id)[0]['job_id'])
    assert result['state']=='success' and receiving.remote.offline_add.call_count==1
    assert r.inbox(receiving.id)[0]['result']['accepted']


def test_channel_revision_delete_and_invalid_regex_fail_closed(receiving):
    c=channel(receiving)
    r.capture(msg(channel=True),channel=True);r.schedule()
    c.pop('chat_id');c.pop('start_after');c['enabled']=False
    r.save_channel(receiving.id,c)
    assert run(r.inbox(receiving.id)[0]['job_id'])['state']=='failed' and not receiving.remote.offline_add.called
    with pytest.raises(CloudError):r.save_channel(receiving.id,c)
    with pytest.raises(CloudError):channel(receiving,regex=['['])


def test_maintenance_defaults_off_and_secret_never_returned(receiving,client):
    value=m.get(receiving.id)
    assert not value['config']['signin_enabled'] and not value['config']['recycle_enabled']
    m.schedule();assert not store.jobs()
    with pytest.raises(CloudError):m.save(receiving.id,{'recycle_enabled':True},0)
    value=m.save(receiving.id,{'recycle_enabled':True,'password':'123456'},0,True)
    assert value['config']['password_saved'] and '123456' not in str(value)
    assert 'sealed_password' not in client.get('/api/cloud/accounts/'+receiving.id+'/maintenance').text
    with store.db() as con:raw=con.execute("SELECT payload FROM cloud_service_settings WHERE kind='maintenance'").fetchone()[0]
    assert '123456' not in raw
    m.save(receiving.id,{'password':''},1)
    assert m.get(receiving.id)['config']['password_saved']


def test_signin_once_per_local_day_even_after_restart(receiving):
    first=m.enqueue(receiving.id,'signin',True);second=m.enqueue(receiving.id,'signin',True)
    assert first==second and run(first)['state']=='success'
    assert m.enqueue(receiving.id,'signin',True)==first
    assert receiving.remote.sign_in.call_count==1


def test_recycle_retains_new_and_unknown_times_and_only_deletes_exact_ids(receiving):
    m.save(receiving.id,{'password':'123456','retention_days':7},0)
    result=run(m.enqueue(receiving.id,'recycle',True))
    assert result['state']=='success' and result['result']['deleted_count']==1
    assert receiving.purged==[(['90'],'123456')] and {x['id'] for x in receiving.bin}=={'91','92'}


def test_recycle_zero_retention_still_retains_unverified_time(receiving):
    m.save(receiving.id,{'retention_days':0},0)
    result=run(m.enqueue(receiving.id,'recycle',True))
    assert result['state']=='success' and {x['id'] for x in receiving.bin}=={'92'}


def test_recycle_uncertain_never_replays_and_read_only_verify_unblocks(receiving,monkeypatch):
    original=receiving.remote.recycle_delete
    def unknown(ids,password=''):
        original(ids,password);raise CloudError('超时',502,uncertain=True)
    monkeypatch.setattr(receiving.remote,'recycle_delete',unknown)
    jid=m.enqueue(receiving.id,'recycle',True)
    assert run(jid)['state']=='uncertain'
    jobs.execute(jid);assert len(receiving.purged)==1
    with pytest.raises(CloudError):m.enqueue(receiving.id,'recycle',True)
    assert m.verify(jid)=={'verified':True,'resent':False}
    assert store.job(jid)['state']=='success' and len(receiving.purged)==1


def test_recycle_pending_source_receipt_and_snapshot_change_block_delete(receiving,monkeypatch):
    with store.db() as con:con.execute('INSERT INTO cloud_deletions VALUES(?,?,?,?,?,?,?)',(receiving.id,'4','asset','uncertain','{}','{}',time.time()))
    with pytest.raises(CloudError):m.enqueue(receiving.id,'recycle',True)
    with store.db() as con:con.execute('DELETE FROM cloud_deletions')
    original=receiving.remote.recycle_items;calls=[0]
    def changed():
        calls[0]+=1
        if calls[0]==2:receiving.bin[0]['name']='changed'
        return original()
    monkeypatch.setattr(receiving.remote,'recycle_items',changed)
    assert run(m.enqueue(receiving.id,'recycle',True))['state']=='failed' and not receiving.purged


def test_maintenance_cron_persists_due_coalesces_and_pauses_credential_change(receiving):
    m.save(receiving.id,{'signin_enabled':True,'recycle_enabled':True},0,True)
    with store.db() as con:
        data=m.raw(receiving.id)['config'];data.update(next_signin=0,next_recycle=0)
        con.execute("UPDATE cloud_service_settings SET payload=? WHERE account_id=? AND kind='maintenance'",(store.encode(data),receiving.id))
    m.schedule();m.schedule()
    assert len(store.jobs())==2
    with store.db() as con:con.execute('UPDATE cloud_accounts SET revision=revision+1 WHERE id=?',(receiving.id,))
    m.schedule();assert not m.get(receiving.id)['config']['signin_enabled']


def test_module_notifications_and_legacy_master_are_independent(receiving,monkeypatch):
    sent=[];monkeypatch.setattr(tg,'notify_telegram',lambda text:sent.append(text) or True)
    jid=jobs.enqueue('intake',{})
    jobs.notify(jid,'intake','success','已提交');assert not sent
    settings.save({'notify_enabled':True,'notify_transfer':False,'notify_channel':True})
    jobs.notify(jid,'intake','success','已提交');assert not sent
    jobs.notify(jid,'channel','success','已提交');assert len(sent)==1
    settings.save({'notify_maintenance':False});jobs.notify(jid,'maintenance','success','已完成');assert len(sent)==1


def test_notification_details_are_opt_in_and_escaped(receiving,monkeypatch):
    sent=[];monkeypatch.setattr(tg,'notify_telegram',lambda text:sent.append(text) or True)
    settings.save({'notify_enabled':True})
    jid=jobs.enqueue('sync',{})
    with store.db() as con:con.execute('UPDATE cloud_jobs SET result=? WHERE id=?',(store.encode({'generated':[{'path':'<movie>.strm'}]}),jid))
    jobs.notify(jid,'sync','success','完成');assert 'movie' not in sent[-1]
    settings.save({'notify_strm_details':True});jobs.notify(jid,'sync','success','完成');assert '&lt;movie&gt;.strm' in sent[-1]


def test_sdk_calls_use_cookie_methods_and_explicit_ids():
    p=object.__new__(provider115.Provider115);p.call=Mock(return_value={'state':True})
    assert not p.offline_add(MAGNET,'3')['completed']
    p.call.assert_called_with('clouddownload_task_add_url',{'url':MAGNET,'wp_path_id':'3'},write=True)
    p.sign_in();p.call.assert_called_with('user_points_sign_post',write=True)
    p.recycle_delete(['9','10'],'123456');p.call.assert_called_with('recyclebin_clean',{'tid':'9,10'},password='123456',write=True)
    with pytest.raises(CloudError):p.recycle_delete([])
    with pytest.raises(CloudError):p.recycle_delete(['bad'])


def test_cookie_instant_does_not_fake_random_challenge_or_upload():
    p=object.__new__(provider115.Provider115)
    def invoke(method,**kw):
        assert method=='upload_file_init' and kw['pid']=='3'
        assert kw['read_range_bytes_or_hash']('0-131071')=='B'*40
        with pytest.raises(CloudError):kw['read_range_bytes_or_hash']('15-67')
        return {'state':True,'status':1,'reuse':False}
    p.call=invoke
    with pytest.raises(CloudError,match='未命中'):p.instant_receive(r.parse(INSTANT),'3')


def test_recycle_adapter_complete_pagination_and_time_fields():
    p=object.__new__(provider115.Provider115)
    p.call=Mock(return_value={'state':True,'data':{'count':1,'list':[{'rid':'9','file_name':'old','dtime':1234}]}})
    assert p.recycle_items()==[{'id':'9','name':'old','deleted_at':1234}]
    p.call.return_value={'state':True,'count':2,'data':[]}
    with pytest.raises(CloudError):p.recycle_items()


def test_resource_maintenance_and_catalog_endpoints(receiving,client):
    p=client.post('/api/cloud/resources/preview',json={'account_id':receiving.id,'text':MAGNET})
    assert p.status_code==200
    response=client.post('/api/cloud/resources/apply',json={'plan_id':p.json()['plan']['id'],'confirmed':True})
    assert response.status_code==202 and run(response.json()['job_id'])['state']=='success'
    assert client.get('/api/cloud/resources/catalog').json()['genres']
    assert client.get('/api/cloud/resources/offline',params={'account_id':receiving.id}).status_code==200
    assert client.post('/api/cloud/accounts/'+receiving.id+'/maintenance/run',json={'action':'recycle','confirmed':False}).status_code==400
    assert client.post('/api/cloud/accounts/'+receiving.id+'/maintenance',json={'config':{'recycle_enabled':True},'expected_revision':0,'confirmed':False}).status_code==409


def test_link_limit_rejects_without_silently_truncating():
    text='\n'.join('magnet:?xt=urn:btih:'+format(i,'040x') for i in range(21))
    with pytest.raises(CloudError,match='20条'):r.extract(text)


def test_channel_paused_during_tmdb_filter_stops_before_remote_write(receiving,monkeypatch):
    rule=channel(receiving)
    r.capture(msg(channel=True),channel=True);r.schedule()
    def pause(text,cfg):
        with store.db() as con:
            data={**cfg,'enabled':False}
            con.execute('UPDATE cloud_channels SET revision=revision+1,payload=? WHERE id=?',(store.encode(data),rule['id']))
        return True,'matched'
    monkeypatch.setattr(r,'allowed_message',pause)
    assert run(r.inbox(receiving.id)[0]['job_id'])['state']=='failed'
    assert not receiving.remote.offline_add.called


def test_account_credential_change_during_share_read_stops_child_enqueue(receiving,monkeypatch):
    original=workflow.preview_share
    def changed(*args,**kw):
        result=original(*args,**kw)
        with store.db() as con:con.execute('UPDATE cloud_accounts SET revision=revision+1 WHERE id=?',(receiving.id,))
        return result
    monkeypatch.setattr(workflow,'preview_share',changed)
    jid=r.apply(r.preview(receiving.id,'https://115.com/s/abcdef123')['id'],True)
    assert run(jid)['state']=='failed' and len(store.jobs())==1


def test_maintenance_disabled_mid_verification_preserves_bin(receiving,monkeypatch):
    original=receiving.remote.recycle_items;calls=[0]
    def changed():
        calls[0]+=1
        if calls[0]==2:m.save(receiving.id,{'retention_days':30},0)
        return original()
    monkeypatch.setattr(receiving.remote,'recycle_items',changed)
    assert run(m.enqueue(receiving.id,'recycle',True))['state']=='failed' and not receiving.purged


def test_tmdb_country_genre_catalog_filter_and_unrecognized_message(receiving,monkeypatch):
    rule={k:[] for k in ('include','exclude','regex','tmdb_ids','kinds','countries','genres')}
    rule.update(countries=['CN'],genres=['16'])
    monkeypatch.setattr(r.recognition,'tmdb',lambda:type('Tmdb',(),{'get':lambda self,*a:{'production_countries':[{'iso_3166_1':'CN'}],'genres':[{'id':16}]}})())
    assert r.allowed_message('https://www.themoviedb.org/movie/44',rule)[0]
    assert not r.allowed_message('https://www.themoviedb.org/movie/44',{**rule,'countries':['KR']})[0]
    with pytest.raises(CloudError):r.allowed_message(MAGNET,rule)


def test_disabled_emby_refresh_does_not_contact_emby(receiving,monkeypatch):
    from app import engine
    call=Mock(side_effect=AssertionError('no Emby request'))
    monkeypatch.setattr(engine,'emby_request',call)
    settings.save({'emby_refresh_enabled':False})
    assert REAL_REFRESH() is True and not call.called


def test_bot_receives_channel_caption_and_hidden_link(receiving):
    channel(receiving)
    message=msg('',channel=True);message['caption']='电影推荐';message['caption_entities']=[{'type':'text_link','url':MAGNET}]
    bot._handle_update('token',{'channel_post':message})
    assert len(r.inbox(receiving.id))==1
    bot._handle_update('token',{'channel_post':message})
    assert len(r.inbox(receiving.id))==1


def test_uncertain_signin_is_not_replayed_today_and_does_not_disable_next_day(receiving):
    m.save(receiving.id,{'signin_enabled':True},0)
    receiving.remote.sign_in.side_effect=CloudError('签到结果未知',502,uncertain=True)
    jid=m.enqueue(receiving.id,'signin',True);assert run(jid)['state']=='uncertain'
    assert m.enqueue(receiving.id,'signin',True)==jid
    with store.db() as con:
        p=json.loads(con.execute('SELECT payload FROM cloud_jobs WHERE id=?',(jid,)).fetchone()[0]);p['day']='2000-01-01'
        con.execute('UPDATE cloud_jobs SET payload=? WHERE id=?',(store.encode(p),jid))
        cfg=m.raw(receiving.id)['config'];cfg['next_signin']=0
        con.execute("UPDATE cloud_service_settings SET payload=? WHERE account_id=? AND kind='maintenance'",(store.encode(cfg),receiving.id))
    m.schedule();assert len(store.jobs())==2 and receiving.remote.sign_in.call_count==1
