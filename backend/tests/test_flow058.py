"""User-visible directory/queue failures: real SDK responses and full workflows."""
import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from strimkeep.cloud115 import Cloud115, CloudError
from strimkeep.organizer import Organizer
from strimkeep.store import Store
from strimkeep.main import create_app
from test_organize import CONFIG, new_job, setup, MemoryCloud
from test_upgrade import upgraded_setup, seed, policy


def test_sdk_advisory_counts_offsets_and_partial_pages_do_not_omit_children(tmp_path):
    async def run():
        store = Store(tmp_path, 'admin', 'test-password-1234')
        store.set_many({'cookie': 'UID=1_A1; CID=x; SEID=x'})
        cloud = Cloud115(store)
        await cloud.http.aclose()
        offsets = []
        def respond(request):
            assert request.url.params['cur'] == '1'
            offset = int(request.url.params['offset']); offsets.append(offset)
            rows = [{'cid':'11', 'pid':'100', 'n':'folder'}, {'fid':'12','cid':'100','n':'a.mkv','s':42}]
            # Server caps pages to one row, echoes zero and excludes folders
            # from count. None of these fields is an actual EOF signal.
            return httpx.Response(200, json={'state':True,'count':1,'offset':0,'data':rows[offset:offset+1]})
        cloud.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        result = await cloud.children('100')
        assert [e['id'] for e in result] == ['11', '12']
        assert offsets == [0,1,2]
        await cloud.close(); store.close()
    asyncio.run(run())


def test_sdk_missing_breadcrumb_uses_current_directory_metadata(tmp_path):
    async def run():
        store = Store(tmp_path, 'admin', 'test-password-1234')
        store.set_many({'cookie':'UID=1_A1; CID=x; SEID=x'})
        cloud = Cloud115(store); await cloud.http.aclose()
        cid = '3546308101340661627'
        def respond(request):
            if request.url.path == '/files':
                return httpx.Response(200,json={'state':True,'data':[],'count':9,'path':[{'cid':'0','name':''}]})
            assert request.url.path == '/category/get'
            assert request.url.params['cid'] == cid
            return httpx.Response(200,json={'state':True,'file_id':cid,'file_name':'归档','sha1':'',
                'paths':[{'file_id':'0','file_name':''},{'file_id':'100','file_name':'媒体'}]})
        cloud.http=httpx.AsyncClient(transport=httpx.MockTransport(respond))
        assert await cloud.directory(cid) == ['0','100',cid]
        await cloud.close(); store.close()
    asyncio.run(run())


def test_wrong_parent_rows_cannot_authorize_mutation(tmp_path):
    async def run():
        store=Store(tmp_path,'admin','test-password-1234');store.set_many({'cookie':'UID=1_A1; CID=x; SEID=x'})
        cloud=Cloud115(store);await cloud.http.aclose()
        cloud.http=httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={
            'state':True,'count':1,'data':[{'fid':'9','cid':'999','n':'wrong.mkv','s':42}]})))
        with pytest.raises(CloudError,match='其他目录'):
            await cloud.children('100')
        await cloud.close();store.close()
    asyncio.run(run())


def test_wash_after_all_catalog_and_directory_cache_records_are_removed(tmp_path):
    async def run():
        store,cloud,http,engine,_,media = await seed(tmp_path)
        old_parent=cloud.nodes['1000']['parent_id']
        with store.db:
            store.db.execute('DELETE FROM archive_entries')
            store.db.execute('DELETE FROM archive_snapshots')
            store.db.execute('DELETE FROM directory_cache')
            store.db.execute('DELETE FROM organized_files')
        # Existing cloud work remains discoverable by names, with no full-index job.
        source=cloud.add_video('2000');source['size']=2000
        async def forbidden(*args): raise AssertionError('full archive scan required')
        engine.index.refresh=forbidden
        jid=new_job(store,{**CONFIG,'upgrade_policy':policy('size')})
        await engine.run_job(jid)
        assert engine.job(jid)['state']=='completed',dict(engine.job(jid))
        assert '1000' in cloud.bin and cloud.nodes['2000']['parent_id']==old_parent
        assert len(list(engine.root.rglob('*.strm')))==1
        await http.aclose();store.close()
    asyncio.run(run())


def test_recreated_directory_and_reingested_file_need_no_manual_index(tmp_path):
    async def run():
        store,cloud,http,engine,_ = setup(tmp_path)
        cloud.add_video();first=new_job(store);await engine.run_job(first)
        old_parent=cloud.nodes['1000']['parent_id']
        old_dir=cloud.nodes.pop(old_parent)
        new_parent=str((await cloud.mkdir(old_dir['parent_id'],old_dir['name']))['cid'])
        cloud.nodes['1000']['parent_id']='100'
        # Stale directory/cache entries point at a deleted/recreated folder.
        second=new_job(store);await engine.run_job(second)
        assert engine.job(second)['state']=='completed',dict(engine.job(second))
        assert cloud.nodes['1000']['parent_id']==new_parent
        assert store.db.execute('SELECT count(*) FROM organize_jobs WHERE json_extract(config,\'$.job_kind\')=\'archive_index\'').fetchone()[0]==0
        await http.aclose();store.close()
    asyncio.run(run())


@pytest.mark.parametrize('held',['paused','waiting_login','needs_review'])
def test_held_head_does_not_block_runnable_fifo(tmp_path, held):
    async def run():
        store,cloud,http,engine,_=setup(tmp_path)
        head=new_job(store);engine.update_job(head,state=held)
        second=new_job(store);third=new_job(store);order=[]
        async def perform(jid):
            order.append(jid);engine.update_job(jid,state='completed')
            if jid==third:engine.closing=True
        engine.run_job=perform
        await asyncio.wait_for(engine.loop(),2)
        assert order==[second,third] and engine.job(head)['state']==held
        await http.aclose();store.close()
    asyncio.run(run())


@pytest.mark.parametrize('pending',['','300'])
def test_failed_recognition_retains_original_or_moves_directly_to_optional_folder(tmp_path, pending):
    async def run():
        store,cloud,http,engine,_=setup(tmp_path)
        source=cloud.add_video('1000','无法识别.mkv').copy()
        cloud.add_video('1001','测试剧集 (2024) S01E04.mkv')
        jid=new_job(store,{**CONFIG,'pending_cid':pending});await engine.run_job(jid)
        assert engine.job(jid)['state']=='partial'
        assert cloud.nodes['1000']['name']==source['name']
        assert cloud.nodes['1000']['parent_id']==(pending or source['parent_id'])
        assert not any(w[0]=='rename' and w[1]=='1000' for w in cloud.writes)
        assert not any(w[0]=='mkdir' and w[-1]=='1000' for w in cloud.writes)
        assert store.db.execute("SELECT state FROM organize_items WHERE file_id='1001'").fetchone()[0]=='done'
        await http.aclose();store.close()
    asyncio.run(run())


def test_unidentified_folder_collision_keeps_source_name_and_location(tmp_path):
    async def run():
        store,cloud,http,engine,_=setup(tmp_path)
        source=cloud.add_video('1000','无法识别.mkv').copy()
        cloud.add_video('2000',source['name'],'300')
        jid=new_job(store);await engine.run_job(jid)
        assert cloud.nodes['1000']==source
        assert not any(w[0] in ('rename','move') for w in cloud.writes)
        assert store.db.execute('SELECT state FROM organize_items WHERE job_id=?',(jid,)).fetchone()[0]=='skipped'
        await http.aclose();store.close()
    asyncio.run(run())



def test_delayed_transfer_listing_recovers_and_enqueues_organization_once(tmp_path):
    from test_transfer import mock_share
    async def run():
        store,cloud,http,engine,_=setup(tmp_path);calls=mock_share(cloud)
        store.set_many({'organize_config':CONFIG})
        children=cloud.children; received=False; delay=2
        receive=cloud.share_receive
        async def submit(*args):
            nonlocal received
            await receive(*args);received=True
        async def lag(cid):
            nonlocal delay
            if received and cid not in CONFIG['source_cids'] and delay:
                delay-=1;return []
            return await children(cid)
        cloud.share_receive=submit;cloud.children=lag
        jid=(await engine.transfer.enqueue('https://115.com/s/abc','100',True))[0]
        await engine.run_job(jid)
        assert engine.job(jid)['state']=='completed',dict(engine.job(jid))
        receipt=json.loads(engine.job(jid)['config'])['receipt']
        await engine.run_job(receipt['organize_job'])
        assert engine.job(receipt['organize_job'])['state']=='completed'
        assert len(calls)==1
        await http.aclose();store.close()
    asyncio.run(run())


def test_playback_request_runs_while_background_request_is_still_in_flight(tmp_path):
    from strimkeep.cloud115 import playback_request
    async def run():
        store=Store(tmp_path,'admin','test-password-1234')
        cloud=Cloud115(store);started=asyncio.Event();release=asyncio.Event();sessions=[]
        async def background(**kwargs):
            sessions.append(kwargs['session']);started.set();await release.wait();return {'state':True}
        async def playing(**kwargs):
            sessions.append(kwargs['session']);return {'state':True}
        task=asyncio.create_task(cloud.call(background));await started.wait()
        token=playback_request.set(True)
        try:
            async with asyncio.timeout(1):await cloud.call(playing)
        finally:playback_request.reset(token)
        assert not task.done(), 'playback must not wait for a transfer/scan request to finish'
        assert sessions==[cloud.http,cloud.play_http] and cloud.http is not cloud.play_http
        release.set();await task;await cloud.close();store.close()
    asyncio.run(run())


def test_playback_link_reuse_is_isolated_by_session_and_ua(tmp_path):
    """直链缓存按「账号 + 文件 + UA」隔离，并按时效过期。

    119：115 的 CDN 地址**绑定签发时的 UA**——同一条链接用 Infuse 取 206、
    用 Emby/Lavf/空 UA 取 403（0.9.59 去掉 UA 后现场立刻出现 Emby 播放 500）。
    所以换 UA 必须重取；换账号更不能借用别人的链接。
    """
    from strimkeep.cloud115 import playback_request
    async def run():
        store=Store(tmp_path,'admin','test-password-1234');store.set_many({'cookie':'UID=1_A1; CID=one; SEID=x'})
        cloud=Cloud115(store);calls=[]
        async def request(*args,**kwargs):calls.append(kwargs);return 'https://cdn.example/video?link='+str(len(calls))
        cloud.call=request
        token=playback_request.set(True)
        try:
            first=await cloud.url('pc','Infuse')
            assert await cloud.url('pc','Infuse')==first and len(calls)==1
            # 换 UA 必须重取：CDN 只认签发时那个 UA。
            assert await cloud.url('pc','Lavf/57.28.100')!=first and len(calls)==2
            # 换账号（Cookie）同样必须重取，不能把上一个账号的链接递出去。
            store.set_many({'cookie':'UID=1_A1; CID=two; SEID=y'})
            assert await cloud.url('pc','Infuse')!=first and len(calls)==3
            await asyncio.sleep(0)
            # Expiration is a real clock boundary, without a 15-second test wait.
            # 链接里没有 `t` 时退回 15 秒保守值。
            import strimkeep.cloud115 as module
            original_time=module.time.monotonic
            from unittest.mock import patch
            with patch.object(module.time,'monotonic',return_value=original_time()+16):
                await cloud.url('pc','Infuse')
            assert len(calls)==4
        finally:playback_request.reset(token)
        await cloud.url('pc','Infuse');assert len(calls)==5, 'background probing must not borrow playback cache'
        await cloud.close();store.close()
    asyncio.run(run())

def test_optional_unidentified_directory_saves_and_enqueues_without_a_third_root(tmp_path, monkeypatch):
    monkeypatch.setenv('ADMIN_USERNAME','admin');monkeypatch.setenv('ADMIN_PASSWORD','test-password-1234')
    monkeypatch.setattr(Organizer,'start',lambda self:None)
    app=create_app(tmp_path/'data',tmp_path/'strm',MemoryCloud)
    app.state.store.set_many({'cookie':'UID=1_A1; CID=x; SEID=x','tmdb_key':'test','playback_base':'https://play.example'})
    with TestClient(app) as client:
        csrf=client.post('/api/v1/auth/login',json={'username':'admin','password':'test-password-1234'}).json()['csrf']
        client.headers['X-CSRF-Token']=csrf
        result=client.put('/api/v1/organize/config',json={**CONFIG,'pending_cid':''})
        assert result.status_code==200,result.text
        assert result.json()['config']['pending_cid']==''
        result=client.post('/api/v1/organize/jobs')
        assert result.status_code==200 and result.json()['state']=='queued',result.text
        invalid=client.put('/api/v1/organize/config',json={**CONFIG,'pending_cid':'100'})
        assert invalid.status_code==422


def test_playback_route_uses_independent_channel_and_does_not_depend_on_worker_queue(tmp_path, monkeypatch):
    from test_app import FakeCloud, sample
    from strimkeep.cloud115 import playback_request
    from urllib.parse import urlsplit
    monkeypatch.setenv('ADMIN_USERNAME','admin');monkeypatch.setenv('ADMIN_PASSWORD','test-password-1234')
    monkeypatch.setattr(Organizer,'start',lambda self:None)
    app=create_app(tmp_path/'data',tmp_path/'strm',FakeCloud)
    priorities=[]
    with TestClient(app) as client:
        url=app.state.cloud.url
        async def observed(*args):
            priorities.append(playback_request.get());return await url(*args)
        app.state.cloud.url=observed
        csrf=client.post('/api/v1/auth/login',json={'username':'admin','password':'test-password-1234'}).json()['csrf']
        client.headers['X-CSRF-Token']=csrf
        result=sample(client);parts=urlsplit(result['url'])
        # A paused queue head is irrelevant to /play.
        jid=new_job(app.state.store);app.state.organizer.update_job(jid,state='paused')
        assert client.get(parts.path+'?'+parts.query,follow_redirects=False).status_code==302
        assert priorities==[True]
        assert app.state.organizer.job(jid)['state']=='paused'
    assert playback_request.get() is False
