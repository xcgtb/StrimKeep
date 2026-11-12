"""Behavioral regressions for batch isolation, bounded delivery and sync reuse."""
import asyncio
import copy
import json
import time

import httpx
import pytest

from strimkeep.audio import naming_track
from strimkeep.media import technical_name, NeedsReview
from strimkeep.organizer import Organizer
from strimkeep.quality import facts, rule_value, version_layout
from test_organize import setup, CONFIG, MEDIA, new_job
from test_phase5 import sync_config
from test_transfer import mock_share


@pytest.mark.parametrize('order,expected', [(['truehd','eac3'],'TrueHD 7.1'), (['eac3','truehd'],'DDP 5.1')])
def test_naming_and_comparison_use_same_configured_track(order, expected):
    media = {**MEDIA, 'audio':[{'codec':'eac3','channels':6}, {'codec':'truehd','channels':8}]}
    rule = {'key':'audio','enabled':True,'order':order,'preferences':[]}
    policy = {'rules':[rule], 'season_layout':'spec'}
    chosen = naming_track(media, policy)
    score, compared = rule_value(facts('a.mkv',media,100), rule)
    assert compared['codec'] == chosen['codec'] and compared['channels'] == chosen['channels']
    assert expected in technical_name(media,'a.mkv',policy)
    plan = {'hint':{'media_type':'movie'},'parts':['电影','欧美电影','A (2024)'], 'media':media}
    version_layout(plan, 'a.mkv', '2160', policy)
    assert expected in plan['target_name']


def test_dts_profile_naming_ignores_incomplete_extra_track():
    media = {**MEDIA,'audio':[{'codec':'aac','channels':2}, {'codec':'dts','profile':'DTS-HD MA','channels':8}]}
    assert 'DTS-HD MA 7.1' in technical_name(media,'a.mkv')
    rule = {'key':'audio','enabled':True,'order':['dts_hd_ma','aac'],'preferences':[]}
    media['audio'].append({'codec':'unknown','channels':None})
    assert 'DTS-HD MA 7.1' in technical_name(media,'a.mkv',{'rules':[rule]})


def test_transfer_followup_leaves_existing_and_later_batches_alone(tmp_path):
    async def run():
        store,cloud,http,engine,_ = setup(tmp_path); mock_share(cloud)
        store.set_many({'organize_config':CONFIG})
        existing = cloud.add_video('6000','旧的待整理 (2024).mkv').copy()
        jid = (await engine.transfer.enqueue('https://115.com/s/abc','100',True))[0]
        await engine.run_job(jid)
        receipt = json.loads(engine.job(jid)['config'])['receipt']
        later = cloud.add_video('6001','后放的资源 (2024).mkv').copy()
        await engine.run_job(receipt['organize_job'])
        assert engine.job(receipt['organize_job'])['state']=='completed'
        assert cloud.nodes['6000']==existing and cloud.nodes['6001']==later
        ids = [row[0] for row in store.db.execute('SELECT file_id FROM organize_items WHERE job_id=?',(receipt['organize_job'],))]
        assert ids == ['7000']
        assert receipt['received_entries'][0]['id'] not in cloud.nodes  # only this batch folder removed
        await http.aclose(); store.close()
    asyncio.run(run())


def test_transfer_batch_moved_before_organizing_stops_without_touching_siblings(tmp_path):
    async def run():
        store,cloud,http,engine,_ = setup(tmp_path);mock_share(cloud)
        store.set_many({'organize_config':CONFIG})
        jid = (await engine.transfer.enqueue('https://115.com/s/abc','100',True))[0]
        await engine.run_job(jid)
        receipt=json.loads(engine.job(jid)['config'])['receipt']
        cloud.nodes[receipt['received_entries'][0]['id']]['parent_id']='300'
        before=copy.deepcopy(cloud.nodes)
        await engine.run_job(receipt['organize_job'])
        assert engine.job(receipt['organize_job'])['state']=='completed'
        assert cloud.nodes==before
        await http.aclose();store.close()
    asyncio.run(run())


async def tracked_job(tmp_path):
    store,cloud,http,engine,_ = setup(tmp_path)
    cloud.add_video(name='测试电影 (2024).mkv')
    jid=new_job(store,{**CONFIG,'account_id':'1'})
    await engine.run_job(jid); await engine.emby.drain_outbox(); await engine.emby.tick()
    assert engine.job(jid)['state']=='completed'
    return store,cloud,http,engine,jid


def test_emby_exact_strm_path_confirmation_and_task_result(tmp_path):
    async def run():
        store,cloud,http,engine,jid=await tracked_job(tmp_path)
        target=json.loads(engine.emby.row(jid)['payload'])['targets'][0]
        requests=[]
        def respond(req):
            requests.append(req)
            assert req.url.params['AnyProviderIdEquals']=='tmdb.20'
            return httpx.Response(200,json={'Items':[{'Path':'/different/mount/'+target['relative']}], 'TotalRecordCount':1})
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as api:
            engine.emby.http=api
            await engine.emby.tick(time.time()+31)
            await engine.emby.tick(time.time()+61)
        assert engine.job(jid)['notify_state']=='verified'
        assert engine.job(jid)['state']=='completed' and len(requests)==1
        await asyncio.gather(*engine.notice_tasks)
        await http.aclose();store.close()
    asyncio.run(run())


def test_emby_same_tmdb_old_version_is_not_confirmation_and_deadline_expires(tmp_path):
    async def run():
        store,cloud,http,engine,jid=await tracked_job(tmp_path)
        target=json.loads(engine.emby.row(jid)['payload'])['targets'][0]
        def respond(req):return httpx.Response(200,json={'Items':[{'Path':'/mount/'+target['relative'].replace('2160p','1080p')}], 'TotalRecordCount':1})
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as api:
            engine.emby.http=api
            await engine.emby.tick(time.time()+31)
            assert engine.job(jid)['notify_state']=='accepted'
            await engine.emby.tick(time.time()+1900)
        assert engine.job(jid)['notify_state']=='unconfirmed'
        assert engine.job(jid)['state']=='completed'
        await asyncio.gather(*engine.notice_tasks)
        await http.aclose();store.close()
    asyncio.run(run())


def test_emby_response_lost_checks_library_before_repeating_refresh(tmp_path):
    async def run():
        store,cloud,http,engine,_=setup(tmp_path);cloud.add_video(name='测试电影 (2024).mkv')
        posts=[]
        def respond(req):
            if req.method=='POST':
                posts.append(req)
                raise httpx.ReadTimeout('lost response',request=req)
            row=store.db.execute("SELECT plan FROM organize_items WHERE job_id=1").fetchone()
            relative=json.loads(row['plan'])['relative_path']
            return httpx.Response(200,json={'Items':[{'Path':'/mount/'+relative}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as api:
            engine.emby.http=api
            jid=new_job(store,{**CONFIG,'account_id':'1'});await engine.run_job(jid); await engine.emby.drain_outbox(); await engine.emby.tick()
            assert engine.job(jid)['notify_state']=='retrying'
            # Recreate the service to model restart with a persisted uncertain attempt.
            recovered=Organizer(store,cloud,http,engine.root)
            recovered.emby.http=api
            await recovered.emby.tick(time.time()+31)
            assert recovered.job(jid)['notify_state']=='verified' and len(posts)==1
            await asyncio.gather(*recovered.notice_tasks)
        await http.aclose();store.close()
    asyncio.run(run())


def test_emby_retries_bounded_and_never_halts_fifo(tmp_path):
    async def run():
        store,cloud,http,engine,_=setup(tmp_path);cloud.add_video(name='测试电影 (2024).mkv')
        posts=[]
        def respond(req):
            if req.method=='POST':posts.append(req);return httpx.Response(503)
            return httpx.Response(200,json={'Items':[]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as api:
            engine.emby.http=api
            jid=new_job(store,{**CONFIG,'account_id':'1'});await engine.run_job(jid); await engine.emby.drain_outbox(); await engine.emby.tick()
            assert engine.job(jid)['state']=='completed'
            next_job=new_job(store,{**CONFIG,'account_id':'1'});await engine.run_job(next_job); await engine.emby.drain_outbox(); await engine.emby.tick()
            assert engine.job(next_job)['state']=='completed'
            await engine.emby.tick(time.time()+31)
            await engine.emby.tick(time.time()+100)
            await engine.emby.tick(time.time()+200)
            # Other jobs can be selected, but this job never exceeds 3 sends.
            assert engine.emby.row(jid)['attempts']==3
            assert engine.job(jid)['notify_state']=='checking'
            await engine.emby.tick(time.time()+1900)
            assert engine.job(jid)['notify_state']=='unconfirmed'
            assert len([r for r in posts])<=4
            await asyncio.gather(*engine.notice_tasks)
        await http.aclose();store.close()
    asyncio.run(run())


def test_sync_second_full_pass_avoids_all_unchanged_file_detail_requests(tmp_path):
    async def run():
        store,cloud,http,engine,_=setup(tmp_path)
        for i in range(120):cloud.add_video(str(1000+i),f'Movie{i}.mkv')['sha1']=f'{i:040x}'
        jid=new_job(store,sync_config());await engine.run_job(jid)
        count=0;original=cloud.file
        async def counted(fid):
            nonlocal count
            count+=1;return await original(fid)
        cloud.file=counted
        second=new_job(store,sync_config());await engine.run_job(second)
        assert engine.job(second)['state']=='completed' and count==0
        assert all(json.loads(row[0]).get('sync_unchanged') for row in store.db.execute('SELECT plan FROM organize_items WHERE job_id=?',(second,)))
        await http.aclose();store.close()
    asyncio.run(run())


@pytest.mark.parametrize('reason',['fresh','expired','clock_back','changed','manual','missing_output'])
def test_sync_always_refreshes_cloud_manifest_for_both_triggers(tmp_path,reason):
    async def run():
        store,cloud,http,engine,_=setup(tmp_path)
        # 旧配置不能让定时同步绕过云端发现。
        store.set_many({'sync_sweep_enabled':0})
        cloud.add_video(name='Movie.mkv')['sha1']='A'*40
        jid=new_job(store,sync_config());await engine.run_job(jid)
        if reason in ('expired','clock_back'):
            with store.db:store.db.execute('UPDATE sync_snapshots SET checked=?',(time.time()+100 if reason=='clock_back' else time.time()-1900,))
        if reason=='changed':engine.sync.invalidate('1')
        if reason=='missing_output':
            reg=engine.outputs.get('1','1000');(engine.root/reg['relative_path']).unlink()
        calls=[];original=cloud.children
        async def counted(cid):calls.append(cid);return await original(cid)
        cloud.children=counted
        config={**sync_config(),'full_scan':False,'scheduled':reason!='manual'}
        second=new_job(store,config);await engine.run_job(second)
        assert engine.job(second)['state']=='completed',dict(engine.job(second))
        cached=json.loads(engine.job(second)['config'])['sync_mode']=='cached'
        assert not cached
        assert bool(calls)
        await http.aclose();store.close()
    asyncio.run(run())


def test_scheduled_sweeps_while_manual_scan_discovers_external_changes(tmp_path):
    """定时同步只做本地巡检，发现云端增删靠手动全量。

    0.7.7 起定时同步不再枚举云端（省 115 配额），代价是「云端删了/加了东西」
    它看不到。这条用例把这个取舍钉住：定时保留下载的 STRM，手动才会清掉。
    """
    async def run():
        store,cloud,http,engine,_=setup(tmp_path)
        cloud.add_video(name='Movie.mkv')['sha1']='A'*40
        first=new_job(store,sync_config());await engine.run_job(first)
        old=engine.outputs.get('1','1000');path=engine.root/old['relative_path']
        cloud.nodes.pop('1000');cloud.add_video('1001','External.mkv')['sha1']='B'*40
        scheduled=new_job(store,{**sync_config(),'full_scan':False,'scheduled':True});await engine.run_job(scheduled)
        assert json.loads(engine.job(scheduled)['config'])['sync_mode']=='sweep'
        assert path.exists(),'本地巡检不枚举云端，云端删除不会被它发现'
        full=new_job(store,sync_config());await engine.run_job(full)
        assert not path.exists() and engine.outputs.get('1','1001')['active']
        assert engine.job(full)['state']=='completed'
        await http.aclose();store.close()
    asyncio.run(run())


def test_emby_same_strm_path_with_stale_playback_mapping_is_not_confirmation():
    from strimkeep.emby import EmbyDelivery
    target={'relative':'library/a.strm','pid':'a'*32}
    assert not EmbyDelivery.matches({'Path':'/media/library/a.strm','MediaSources':[{'Path':'http://play/play/'+'b'*32+'?token=hidden'}]},target)
    assert EmbyDelivery.matches({'Path':'/media/library/a.strm','MediaSources':[{'Path':'http://play/play/'+'a'*32+'?token=hidden'}]},target)


def test_emby_series_queries_once_and_confirms_every_episode(tmp_path):
    async def run():
        store,cloud,http,engine,_=setup(tmp_path)
        for i in range(1,5):cloud.add_video(str(1000+i),f'测试剧集 (2024) S01E{i:02d}.mkv')
        jid=new_job(store,{**CONFIG,'account_id':'1'});await engine.run_job(jid); await engine.emby.drain_outbox(); await engine.emby.tick()
        targets=json.loads(engine.emby.row(jid)['payload'])['targets'];assert len(targets)==4
        gets=[]
        def respond(req):
            gets.append(req)
            if req.url.params.get('IncludeItemTypes')=='Series':
                assert req.url.params['AnyProviderIdEquals']=='tmdb.10'
                return httpx.Response(200,json={'Items':[{'Id':'series10'}]})
            assert req.url.path.endswith('/Shows/series10/Episodes'), str(req.url)
            return httpx.Response(200,json={'Items':[{'Path':'/mount/'+t['relative']} for t in targets]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as api:
            engine.emby.http=api;await engine.emby.tick(time.time()+31)
        assert engine.job(jid)['notify_state']=='verified' and len(gets)==2
        await asyncio.gather(*engine.notice_tasks)
        await http.aclose();store.close()
    asyncio.run(run())


def test_emby_changed_connection_does_not_send_old_batch_to_new_server(tmp_path):
    async def run():
        store,cloud,http,engine,jid=await tracked_job(tmp_path)
        store.set_many({'emby_url':'http://different:8096'})
        calls=[]
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: calls.append(req) or httpx.Response(500))) as api:
            engine.emby.http=api;await engine.emby.tick(time.time()+31)
        assert not calls and engine.job(jid)['notify_state']=='unconfirmed'
        await asyncio.gather(*engine.notice_tasks)
        await http.aclose();store.close()
    asyncio.run(run())


def test_emby_public_progress_hides_delivery_payload_and_credentials(tmp_path, monkeypatch):
    from test_directory_readback import api_setup
    from fastapi.testclient import TestClient
    app,jid=api_setup(tmp_path,monkeypatch)
    payload={'connection':'private-hash','targets':[{'confirmed':True,'pid':'private-pid','relative':'hidden.strm'},{'confirmed':False}]}
    with app.state.store.db:
        app.state.store.db.execute('INSERT INTO emby_deliveries(job_id,state,payload,attempts,next_due,deadline,message) VALUES(?,?,?,?,?,?,?)',(jid,'accepted',json.dumps(payload),1,time.time(),time.time()+1800,'后台核对中'))
    with TestClient(app) as client:
        assert client.get('/api/v1/tasks').status_code==401
        client.post('/api/v1/auth/login',json={'username':'admin','password':'test-password-1234'})
        response=client.get('/api/v1/tasks');assert response.status_code==200
        job=(response.json()['active']+response.json()['history'])[0]
        assert job['emby']=={'state':'accepted','attempts':1,'total':2,'confirmed':1,'message':'后台核对中'}
        assert not any(secret in response.text for secret in ('private-hash','private-pid','hidden.strm'))


def test_compatible_table_addition_preserves_settings_history_and_key(tmp_path):
    from strimkeep.store import Store
    store=Store(tmp_path,'admin','test-password-1234')
    store.set_many({'cookie':'UID=1_A1; CID=x; SEID=x','custom':'retain'})
    jid=new_job(store,CONFIG);key=(tmp_path/'config/master.key').read_bytes()
    with store.db:
        store.db.execute('DROP TABLE emby_deliveries');store.db.execute('DROP TABLE sync_snapshots')
    store.close()
    upgraded=Store(tmp_path,'different','different-password-123')
    assert upgraded.get('custom')=='retain' and upgraded.meta('username')=='admin'
    assert (tmp_path/'config/master.key').read_bytes()==key
    assert upgraded.db.execute('SELECT config FROM organize_jobs WHERE id=?',(jid,)).fetchone()
    assert upgraded.db.execute('SELECT count(*) FROM emby_deliveries').fetchone()[0]==0
    assert upgraded.db.execute('SELECT count(*) FROM sync_snapshots').fetchone()[0]==0
    upgraded.close()
