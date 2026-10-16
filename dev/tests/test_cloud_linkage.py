"""v1.3.6 boundary tests with real SQLite/publication and mocked remote services."""
import copy
import json
import time
from unittest.mock import Mock
import pytest
from dev.tests.test_cloud_workbench import cloud
from dev.tests.test_my_drive import client
from dev.tests.test_cloud_receiving import receiving, msg, MAGNET, channel
from app.cloud import channel_reader as reader, emby_library as emby, intake_automation as auto
from app.cloud import directories, services, store, credentials, receiving as intake, jobs, strm, recognition, settings, workflow
from app.cloud.errors import CloudError
from app import bot, tg


def connection(cloud, **patch):
    return emby.save(cloud.id,{"expected_revision":0,"connection":{"name":"独立影视库","url":"http://emby:8096/emby","key":"secret-key","enabled":True,"notify":True,"personal_root":"/strm","share_root":"/share",**patch}})


def asset(cloud):
    return strm.generate(cloud.id,cloud.remote.info('4'),recognition.recognize('Movie.2024.1080p.mkv'))


def login_session(cloud, monkeypatch):
    async def login(secret, action, code='', password=''):
        return {**secret,'session':'session-secret'}
    monkeypatch.setattr(reader,'_login',login)
    return reader.login(cloud.id,{'action':'import','expected_revision':0,'api_id':'12345','api_hash':'a'*32,'session':'s'*200})


def test_login_secrets_never_returned_and_cas_expiry(receiving,monkeypatch):
    async def login(secret,action,code='',password=''):
        return {**secret,'session':'session-secret','phone_code_hash':'private-code'}
    monkeypatch.setattr(reader,'_login',login)
    value=reader.login(receiving.id,{'action':'code','expected_revision':0,'api_id':'123','api_hash':'a'*32,'phone':'+8613800000000'})
    assert value['pending'] and not value['connected']
    assert 'private' not in json.dumps(value) and 'session-secret' not in json.dumps(value)
    raw=services.get(receiving.id,'telegram_reader',reader.DEFAULT)['config']
    assert 'private-code' not in json.dumps(raw)
    with pytest.raises(CloudError):reader.login(receiving.id,{'action':'verify','expected_revision':0,'code':'12345'})
    value=reader.login(receiving.id,{'action':'verify','expected_revision':1,'code':'12345'})
    assert value['connected'] and not value['pending']
    reader.login(receiving.id,{'action':'disconnect','expected_revision':2})
    assert not reader.get(receiving.id)['connected']


@pytest.mark.parametrize('count',[-1,1001,True,'100'])
def test_history_bounds_reject_before_remote(receiving,count):
    with pytest.raises(CloudError):channel(receiving,history_limit=count)


def test_bot_mode_cannot_pretend_to_read_history(receiving):
    with pytest.raises(CloudError,match='历史读取'):channel(receiving,history_limit=10)


def test_history_cursor_enqueues_old_messages_once_then_increments(receiving,monkeypatch):
    login_session(receiving,monkeypatch)
    rule=channel(receiving,mode='user',history_limit=10)
    calls=[]
    async def messages(secret,cfg,cursor,initialized):
        calls.append((cursor,initialized))
        old=msg(MAGNET,mid=10,channel=True);old['date']=1
        return ([old],10) if not initialized else ([old,msg(MAGNET,mid=11,channel=True)],11)
    monkeypatch.setattr(reader,'_messages',messages)
    reader.poll_one()
    assert len(intake.inbox(receiving.id))==1
    with store.db() as con:con.execute('UPDATE cloud_channel_cursors SET next_poll=0')
    reader.poll_one()
    assert calls==[(0,False),(10,True)] and len(intake.inbox(receiving.id))==2
    assert not intake.capture(msg(MAGNET,mid=12,channel=True),'scope',channel=True)
    assert all(i['state']=='queued' for i in intake.inbox(receiving.id))


def test_rule_change_while_reading_does_not_advance_or_enqueue(receiving,monkeypatch):
    login_session(receiving,monkeypatch);rule=channel(receiving,mode='user',history_limit=1)
    async def messages(*args):
        with store.db() as con:con.execute('UPDATE cloud_channels SET revision=revision+1')
        return [msg(MAGNET,channel=True)],1
    monkeypatch.setattr(reader,'_messages',messages)
    reader.poll_one()
    assert not intake.inbox(receiving.id)
    with store.db() as con:assert not con.execute('SELECT 1 FROM cloud_channel_cursors').fetchone()


def test_reader_failure_keeps_cursor_and_is_visible(receiving,monkeypatch):
    login_session(receiving,monkeypatch);channel(receiving,mode='user',history_limit=1)
    async def messages(*args):raise CloudError('频道不可读',409)
    monkeypatch.setattr(reader,'_messages',messages)
    reader.poll_one()
    assert intake.channels(receiving.id)[0]['reader_status']=='频道不可读'
    assert not intake.inbox(receiving.id)


def test_normal_bot_share_uses_web_transfer_not_legacy_bot_directory(receiving):
    directories.save(receiving.id,{'bot':'1'},directories.get(receiving.id)['revision'])
    data=intake.make_payload(receiving.id,[intake.parse('https://115.com/s/abcdef123')],'telegram')
    assert data['targets']['share']=='3'


def test_strm_command_captured_with_explicit_semantics_and_whitelist(receiving,monkeypatch):
    intake.save(receiving.id,{'tg_enabled':True},0)
    monkeypatch.setattr(bot,'_is_allowed',lambda user,chat:user==123)
    message=msg('/strm https://115.com/s/abcdef123')
    ids=bot._capture_resources('token',message)
    assert len(ids)==1
    with store.db() as con:raw=json.loads(con.execute('SELECT payload FROM cloud_inbox WHERE id=?',(ids[0],)).fetchone()[0])
    assert credentials.unseal(raw['sealed_message'])['share_direct'] is True
    message['from']['id']=456;message['message_id']=2
    assert not bot._capture_resources('token',message)


def test_strm_command_never_calls_transfer_or_offline(receiving,monkeypatch):
    from app.cloud import share_library
    intake.save(receiving.id,{'tg_enabled':True},0)
    ids=intake.capture(msg('/strm https://115.com/s/abcdef123'),'t',authorized=True)
    monkeypatch.setattr(workflow,'preview_share',lambda *a:{'preview_id':'p','has_more':False,'items':[{'id':'4'}]})
    monkeypatch.setattr(share_library,'preview',lambda *a:{'id':'share-plan'})
    direct=Mock(return_value='child-share');monkeypatch.setattr(share_library,'apply',direct)
    transfer=Mock();monkeypatch.setattr(workflow,'transfer_submit',transfer)
    intake.schedule()
    job=intake.inbox(receiving.id)[0]['job_id'];jobs.execute(job)
    assert direct.call_count==1 and not transfer.called and not receiving.remote.offline_add.called
    assert store.job(job)['result']['child_jobs']==['child-share']


@pytest.mark.parametrize('url',['file:///tmp','http://user:password@emby:8096','https://emby/x?api_key=secret','https://emby/#fragment'])
def test_emby_url_rejected(receiving,url):
    with pytest.raises(CloudError):connection(receiving,url=url)


def test_connections_secret_preservation_and_compare_swap(receiving):
    value=connection(receiving);cid=value['items'][0]['id']
    assert value['items'][0]['key_saved'] and 'secret-key' not in json.dumps(value)
    emby.save(receiving.id,{'expected_revision':1,'connection':{'id':cid,'key':'','name':'改名'}})
    secret=services.get(receiving.id,'emby_library',emby.DEFAULT)['config']['connections'][0]
    assert credentials.unseal(secret['sealed_key'])['key']=='secret-key'
    with pytest.raises(CloudError):emby.save(receiving.id,{'expected_revision':1,'connection':{'id':cid,'name':'过期'}})


def test_refresh_accepted_is_not_actual_ingestion_and_resume(receiving,monkeypatch):
    connection(receiving);published=asset(receiving)
    calls=[]
    def request(c,path,**kw):calls.append((path,kw));return {'Items':[]}
    monkeypatch.setattr(emby,'request',request)
    card=Mock();monkeypatch.setattr(tg,'notify_telegram_card',card)
    emby.poll_one()
    receipt=emby.records(receiving.id)[0]
    assert receipt['state']=='pending' and not card.called
    assert calls[0][0]=='/Library/Media/Updated' and calls[1][1]['params']['Path'].startswith('/strm/')
    emby.watch(published)
    assert len(emby.records(receiving.id))==1


@pytest.mark.parametrize('bad', ['path','tmdb','type'])
def test_same_title_or_wrong_identity_never_confirms(receiving,monkeypatch,bad):
    connection(receiving);published=asset(receiving)
    path=emby.mapped_path(published,services.get(receiving.id,'emby_library',emby.DEFAULT)['config']['connections'][0])
    item={'Id':'55','Type':'Movie','Path':path,'ProviderIds':{'Tmdb':'44'}}
    if bad=='path':item['Path']='/elsewhere/movie.strm'
    if bad=='tmdb':item['ProviderIds']['Tmdb']='999'
    if bad=='type':item['Type']='Series'
    monkeypatch.setattr(emby,'request',lambda *a,**kw:{'Items':[item]})
    emby.poll_one();assert emby.records(receiving.id)[0]['state']=='pending'


def test_confirmed_receipt_does_not_send_an_extra_poster(receiving,monkeypatch):
    connection(receiving);published=asset(receiving)
    settings.save({'notify_enabled':True})
    path=emby.mapped_path(published,services.get(receiving.id,'emby_library',emby.DEFAULT)['config']['connections'][0])
    monkeypatch.setattr(emby,'request',lambda *a,**kw:{'Items':[{'Id':'55','Type':'Movie','Path':path,'ProviderIds':{'Tmdb':'44'}}]})
    card=Mock(return_value=True);monkeypatch.setattr(tg,'notify_telegram_card',card)
    emby.poll_one();emby.poll_one()
    assert emby.records(receiving.id)[0]['state']=='confirmed'
    card.assert_not_called()


def test_tv_confirmation_requires_season_episode_and_series_identity(receiving,monkeypatch):
    cfg=connection(receiving)
    c=services.get(receiving.id,'emby_library',emby.DEFAULT)['config']['connections'][0]
    a={'payload':{'media':{'kind':'tv','tmdb_id':'44','season':0,'episode':1}}}
    item={'Id':'55','Type':'Episode','Path':'/tv.strm','SeriesId':'66','ParentIndexNumber':0,'IndexNumber':2}
    def request(c,path,**kw):return {'ProviderIds':{'Tmdb':'44'}} if path=='/Items/66' else {'Items':[item]}
    monkeypatch.setattr(emby,'request',request)
    assert emby.confirmed_item(c,a,'/tv.strm') is None
    item['IndexNumber']=1
    assert emby.confirmed_item(c,a,'/tv.strm')['Id']=='55'


def test_connection_change_invalidates_pending_receipt(receiving,monkeypatch):
    c=connection(receiving);asset(receiving)
    emby.save(receiving.id,{'expected_revision':1,'connection':{'id':c['items'][0]['id'],'personal_root':'/new'}})
    request=Mock();monkeypatch.setattr(emby,'request',request)
    emby.poll_one();assert emby.records(receiving.id)[0]['state']=='stale' and not request.called


def test_sources_dedup_and_schedule_reuses_rules(receiving):
    channel(receiving)
    result=auto.sources(receiving.id)
    assert len(result['items'])==2 and len(next(i for i in result['items'] if i['id']=='3')['roles'])>=5
    cfg={'schedule_mode':'cron','cron_expression':'0 * * * *','timezone':'Asia/Shanghai','interval_seconds':3600}
    first=auto.save(receiving.id,cfg,['1','3'],result['revision'])
    second=auto.save(receiving.id,cfg,['3'],result['revision'])
    assert second['ids'][0] in first['ids']
    with store.db() as con:assert con.execute('SELECT COUNT(*) FROM cloud_rules WHERE enabled=1').fetchone()[0]==1


def test_intake_archive_must_stay_outside_sources(receiving):
    receiving.remote.files['2']['parent_id']='3'
    with pytest.raises(CloudError,match='来源内部'):auto.save(receiving.id,{'schedule_mode':'interval','interval_seconds':3600},['3'],directories.get(receiving.id)['revision'])


@pytest.mark.parametrize('status',[0,1,-1,None])
def test_unfinished_or_failed_download_blocks_intake_organizer(receiving,monkeypatch,status):
    monkeypatch.setattr(receiving.remote,'offline_tasks',lambda page:{'items':[{'status':status,'wp_path_id':'3'}],'page_count':1})
    with pytest.raises(CloudError,match='未完成'):auto.ready(receiving.remote,'3')
    auto.ready(receiving.remote,'1')


def test_completed_download_is_ready(receiving,monkeypatch):
    monkeypatch.setattr(receiving.remote,'offline_tasks',lambda page:{'items':[{'status':2,'wp_path_id':'3'}],'page_count':1})
    auto.ready(receiving.remote,'3')


def test_edit_during_emby_read_does_not_confirm_or_notify(receiving,monkeypatch):
    configured=connection(receiving);published=asset(receiving)
    settings.save({'notify_enabled':True})
    path=emby.mapped_path(published,services.get(receiving.id,'emby_library',emby.DEFAULT)['config']['connections'][0])
    def request(c,endpoint,**kw):
        if endpoint=='/Items':
            emby.save(receiving.id,{'expected_revision':1,'connection':{'id':configured['items'][0]['id'],'enabled':False}})
        return {'Items':[{'Id':'55','Type':'Movie','Path':path,'ProviderIds':{'Tmdb':'44'}}]}
    monkeypatch.setattr(emby,'request',request)
    notify=Mock();monkeypatch.setattr(tg,'notify_telegram_card',notify)
    emby.poll_one();assert emby.records(receiving.id)[0]['state']=='stale' and not notify.called


def test_share_confirmation_is_also_web_only(receiving,monkeypatch):
    connection(receiving);published=asset(receiving)
    published['payload']['source_kind']='share';published['payload']['media']['poster_path']='/poster.jpg'
    with store.db() as con:
        con.execute('DELETE FROM cloud_ingestion')
        con.execute('UPDATE cloud_assets SET payload=? WHERE id=?',(store.encode(published['payload']),published['id']))
    emby.watch(published);settings.save({'notify_enabled':True})
    c=services.get(receiving.id,'emby_library',emby.DEFAULT)['config']['connections'][0]
    path=emby.mapped_path(published,c)
    monkeypatch.setattr(emby,'request',lambda *a,**kw:{'Items':[{'Id':'55','Type':'Movie','Path':path,'ProviderIds':{'Tmdb':'44'}}]})
    card=Mock(return_value=True);monkeypatch.setattr(tg,'notify_telegram_card',card)
    emby.poll_one();assert emby.records(receiving.id)[0]['state']=='confirmed'
    card.assert_not_called()


def test_new_account_api_endpoints_do_not_echo_secrets(receiving,client,monkeypatch):
    async def login(secret, action, code='', password=''):
        return {**secret,'session':'private-session'}
    monkeypatch.setattr(reader,'_login',login)
    base='/api/cloud/accounts/'+receiving.id
    response=client.post(base+'/telegram-reader',json={'action':'import','expected_revision':0,'api_id':'123','api_hash':'a'*32,'session':'s'*200})
    assert response.status_code==200 and response.json()['connected']
    assert 'private-session' not in response.text
    response=client.get(base+'/organizer-sources');assert response.status_code==200
    response=client.post(base+'/organizer-sources',json={'source_ids':['3'],'expected_revision':response.json()['revision'],'schedule_mode':'cron','cron_expression':'0 * * * *','timezone':'Asia/Shanghai'})
    assert response.status_code==200
    c=connection(receiving)['items'][0]
    response=client.get(base+'/emby-connections');assert response.status_code==200 and 'secret-key' not in response.text
    monkeypatch.setattr(emby,'request',lambda *a,**kw:{'Id':'server','ServerName':'Emby','Version':'4'})
    assert client.post(base+'/emby-connections/'+c['id']+'/test',json={}).status_code==200
    assert client.get(base+'/ingestion').status_code==200
