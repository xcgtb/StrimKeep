"""v1.3.7 continuation: actual SQLite/worker/files; controlled 115/TG/Emby only."""
import copy
import json
import time
from unittest.mock import Mock
import pytest
from dev.tests.test_cloud_workbench import cloud
from dev.tests.test_my_drive import client
from dev.tests.test_cloud_receiving import receiving, channel, msg, MAGNET, ED2K
from dev.tests.test_cloud_linkage import connection, login_session
from app import tg, bot
from app.cloud import receiving as intake, directories, store, jobs, workflow, settings, intake_automation as auto, channel_reader, strm, recognition, emby_library as emby, services, notifications, events
from app.cloud.errors import CloudError

REAL_PAUSE=workflow._operation_pause


def test_direct_resource_submit_is_idempotent_and_keeps_credentials_sealed(receiving,client):
    body={'account_id':receiving.id,'text':MAGNET+'\n'+ED2K,'expected_revision':1,'request_id':'c'*32,'confirmed':True}
    response=client.post('/api/cloud/resources/receive',json=body)
    assert response.status_code==202
    jid=response.json()['job_id']
    assert client.post('/api/cloud/resources/receive',json=body).json()['job_id']==jid
    with store.db() as con:
        raw=con.execute('SELECT sealed FROM cloud_transfer_requests').fetchone()[0]
    assert MAGNET not in raw and ED2K not in raw
    assert client.post('/api/cloud/resources/receive',json={**body,'text':MAGNET}).status_code==409
    jobs.execute(jid)
    assert store.job(jid)['state']=='success' and receiving.remote.offline_add.call_count==2
    assert client.post('/api/cloud/resources/receive',json=body).json()['job_id']==jid
    assert receiving.remote.offline_add.call_count==2


def test_channel_direct_to_its_directory_then_separate_organizer(receiving):
    # Ordinary bot shares remain under 3; the channel explicitly saves under 1.
    receiving.remote.files['4']['parent_id']='3'
    receiving.remote.files['5']['parent_id']='3'
    rule=channel(receiving,target_cid='1',organize=True)
    assert intake.allowed_message('anything',rule)[0]
    ids=intake.capture(msg('https://115.com/s/abcdef123',channel=True),'token',channel=True)
    assert len(ids)==1
    intake.schedule();jid=intake.inbox(receiving.id)[0]['job_id'];jobs.execute(jid)
    child=store.job(jid)['result']['child_jobs'][0]
    with store.db() as con:
        payload=json.loads(con.execute('SELECT payload FROM cloud_jobs WHERE id=?',(child,)).fetchone()[0])
    assert payload['target_cid']=='1' and payload['organize_target'] is None
    jobs.execute(child)
    assert store.job(child)['state']=='success' and not store.job(child)['result'].get('organization')
    manual=auto.run_now(receiving.id,1)
    assert auto.run_now(receiving.id,1)==manual
    jobs.execute(manual)
    result=store.job(manual)
    assert result['state']=='success' and result['result']['generated'], result
    assert all(item['parent_id']!='1' for item in result['result']['files'])
    messages='\n'.join(row['message'] for row in __import__('app.runtime_logs',fromlist=['read']).read(source='cloud',limit=500)['logs'])
    assert '转存已核实' in messages and '命名' in messages and 'STRM' in messages


def test_poll_defaults_and_empty_filter(receiving,monkeypatch):
    login_session(receiving,monkeypatch)
    rule=channel(receiving,mode='user')
    assert rule['history_limit']==20 and rule['poll_seconds']==300
    assert intake.allowed_message('任意电影 https://115.com/s/abcdef123',rule)[0]
    checked=[]
    async def messages(secret,cfg,cursor,initialized):
        checked.append((cfg['history_limit'],cursor,initialized))
        return [msg(MAGNET,mid=i,channel=True) for i in range(1,21)],20
    monkeypatch.setattr(channel_reader,'_messages',messages)
    now=time.time();channel_reader.poll_one()
    assert checked==[(20,0,False)] and len(intake.inbox(receiving.id))==20
    with store.db() as con:cursor=con.execute('SELECT cursor,next_poll FROM cloud_channel_cursors').fetchone()
    assert cursor[0]==20 and now+299<cursor[1]<now+302
    channel_reader.poll_one();assert len(checked)==1


def test_stop_pauses_schedules_and_stops_queued_manual_work(receiving,client):
    auto.save(receiving.id,{'schedule_mode':'interval','interval_seconds':300,'timezone':'Asia/Shanghai'},['1'],1)
    run=client.post('/api/cloud/accounts/'+receiving.id+'/organizer/run',json={'expected_revision':1})
    assert run.status_code==202
    jid=run.json()['job_id']
    response=client.post('/api/cloud/accounts/'+receiving.id+'/organizer/stop',json={})
    assert response.status_code==200 and jid in response.json()['stopped']
    assert store.job(jid)['state']=='stopped'
    with store.db() as con:assert not con.execute('SELECT 1 FROM cloud_rules WHERE enabled=1').fetchone()
    jobs.execute(jid);assert not receiving.remote.writes


def test_operation_pause_uses_fractional_seconds(receiving,monkeypatch):
    settings.save({'operation_delay_seconds':1.5,'strm_delay_seconds':1.5})
    pause=Mock();monkeypatch.setattr(time,'sleep',pause)
    REAL_PAUSE();pause.assert_called_once_with(1.5)


@pytest.mark.parametrize('value',[-1,True,61,float('nan'),float('inf')])
def test_bad_operation_delay_rejected(receiving,value):
    with pytest.raises(CloudError):settings.save({'operation_delay_seconds':value})


def test_persisted_linkage_off_skips_strm_in_automatic_workflow(receiving):
    settings.save({'generate_strm_enabled':False})
    jid=auto.run_now(receiving.id,1);jobs.execute(jid)
    assert store.job(jid)['state']=='success' and not store.job(jid)['result']['generated']
    assert any(r[0]=='move' for r in receiving.remote.writes)


def test_tmdb_cards_validate_and_filter_metadata_fields(receiving):
    c={'id':'movie:44','label':'Movie','year':'2024','poster_path':'/poster.jpg','resolution':'2160p','dynamic_range':'DV','keywords':'中字'}
    rule=channel(receiving,tmdb_ids=['movie:44'],rule_cards=[c])
    assert intake.allowed_message('Movie 2024 4K DV 中字 https://themoviedb.org/movie/44',rule)[0]
    assert not intake.allowed_message('Movie 2024 1080p SDR 中字 https://themoviedb.org/movie/44',rule)[0]
    assert not intake.allowed_message('Movie 2023 4K DV 中字 https://themoviedb.org/movie/44',rule)[0]
    with pytest.raises(CloudError):channel(receiving,tmdb_ids=['movie:44'],rule_cards=[{**c,'poster_path':'//evil/path'}])


def test_compatibility_share_strm_command_does_not_transfer(receiving):
    intake.save(receiving.id,{'tg_enabled':True},0)
    ids=intake.capture(msg('/create_115share_strm https://115.com/s/abcdef123'),'token',authorized=True)
    assert len(ids)==1
    with store.db() as con:payload=json.loads(con.execute('SELECT payload FROM cloud_inbox WHERE id=?',(ids[0],)).fetchone()[0])
    from app.cloud import credentials
    assert credentials.unseal(payload['sealed_message'])['share_direct']
    intake.schedule();jobs.execute(intake.inbox(receiving.id)[0]['job_id'])
    assert not receiving.remote.writes


def test_confirmed_season_is_one_card_with_paths_and_counts(receiving,monkeypatch):
    connection(receiving);settings.save({'notify_enabled':True,'nfo_enabled':False})
    origin=jobs.enqueue('workflow',{'account_id':receiving.id})
    token=events._JOB.set(origin)
    assets=[]
    try:
        for episode in (1,2,5):
            media=recognition.recognize('Movie.2024.1080p.mkv')
            media.update(kind='tv',title='<Season>',season=27,episode=episode,directory='动漫/欧美动漫/Show/Season 27',name='Show.S27E'+str(episode)+'.mkv',poster_path='/poster.jpg')
            row={**receiving.remote.info('4'),'id':str(80+episode),'name':media['name'],'size':1024**3}
            assets.append(strm.generate(receiving.id,row,media))
    finally:events._JOB.reset(token)
    sent=Mock(return_value=True);monkeypatch.setattr(tg,'notify_telegram_card',sent)
    scans=[]
    monkeypatch.setattr(emby,'request',lambda c,path,**kw:scans.append((path,kw)) or {})
    monkeypatch.setattr(emby,'confirmed_item',lambda *a:{'Id':'55'})
    for i in range(3):emby.poll_one()
    assert not sent.called,'wait for the publication batch to finish'
    with store.db() as con:
        con.execute("UPDATE cloud_jobs SET state='success' WHERE id=?",(origin,))
        group=json.loads(con.execute('SELECT payload FROM cloud_ingestion LIMIT 1').fetchone()[0])['notification_group']
    c=services.get(receiving.id,'emby_library',emby.DEFAULT)['config']['connections'][0]
    notifications.send_confirmed(group,c);notifications.send_confirmed(group,c)
    assert sent.call_count==1
    text=sent.call_args.args[0]
    assert '&lt;Season&gt;' in text and 'E01-E02, E05' in text and '文件：3 个' in text and '3.00 GB' in text and 'TMDB 44' in text
    assert all(path=='/Library/Media/Updated' and args['body']['Updates'][0]['UpdateType']=='Created' for path,args in scans)


def test_strm_details_are_batched_without_truncating_files(receiving,monkeypatch):
    settings.save({'notify_enabled':True,'notify_strm_details':True})
    sent=[];monkeypatch.setattr(tg,'notify_telegram',lambda text:sent.append(text) or True)
    jid=jobs.enqueue('sync',{})
    paths=[{'path':'<Movie>.'+str(i)+'.strm'} for i in range(45)]
    with store.db() as con:con.execute('UPDATE cloud_jobs SET result=? WHERE id=?',(store.encode({'generated':paths}),jid))
    jobs.notify(jid,'sync','success','完成')
    assert len(sent)==4 and 'STRM 更新：45 个' in sent[0]
    assert all(sum(p['path'].replace('<','&lt;').replace('>','&gt;') in v for v in sent[1:])==1 for p in paths)
