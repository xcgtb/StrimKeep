import asyncio,json,time
import httpx,pytest
from test_organize import setup,new_job
from test_phase5 import sync_config


@pytest.mark.parametrize('status',[400,404,405])
def test_path_notification_error_never_falls_back_to_global_scan(tmp_path,status):
    async def run():
        store,cloud,http,engine,_=setup(tmp_path);cloud.add_video();calls=[]
        def respond(req):
            if req.url.path.endswith('/System/Info/Public'):
                return httpx.Response(200, json={'ServerName': 'test', 'Version': '4.8'})
            if req.url.path.endswith('/Library/VirtualFolders'):
                return httpx.Response(200, json=[])
            calls.append(req)
            # 0.7.16 起交付前有只读预检（Info/VirtualFolders），放行后再断言写请求。
            if req.url.path.endswith('/System/Info/Public'):
                return httpx.Response(200, json={'ServerName': 'test', 'Version': '4.8'})
            if req.url.path.endswith('/Library/VirtualFolders'):
                return httpx.Response(200, json=[])
            return httpx.Response(status)
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as api:
            engine.emby.http=api
            jid=new_job(store,sync_config());await engine.run_job(jid); await engine.emby.drain_outbox(); await engine.emby.tick()
            assert engine.job(jid)['state']=='completed'
            assert engine.job(jid)['notify_state']=='unconfirmed' and len([c for c in calls if c.url.path.endswith('/Library/Media/Updated')])==1
            assert '未回退全库扫描' in engine.emby.row(jid)['message']
            await asyncio.gather(*engine.notice_tasks)
        await http.aclose();store.close()
    asyncio.run(run())


def test_deletion_only_sync_notifies_deleted_paths_without_global_scan(tmp_path):
    async def run():
        store,cloud,http,engine,_=setup(tmp_path);cloud.add_video();calls=[]
        def respond(req):
            if req.url.path.endswith('/System/Info/Public'):
                return httpx.Response(200, json={'ServerName': 'test', 'Version': '4.8'})
            if req.url.path.endswith('/Library/VirtualFolders'):
                return httpx.Response(200, json=[])
            calls.append(req)
            assert req.url.path.endswith('/Library/Media/Updated')
            return httpx.Response(204)
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as api:
            engine.emby.http=api
            await engine.run_job(new_job(store,sync_config())); await engine.emby.drain_outbox(); await engine.emby.tick()
            relative=engine.outputs.get('1','1000')['relative_path'];cloud.nodes.pop('1000');calls.clear()
            orphan=engine.root/relative;orphan=orphan.parent/'orphan.strm';orphan.write_text('http://old')
            jid=new_job(store,sync_config());await engine.run_job(jid); await engine.emby.drain_outbox(); await engine.emby.tick()
            updates=json.loads(calls[0].content)['Updates']
            assert len([c for c in calls if c.url.path.endswith('/Library/Media/Updated')])==1 and all(u['UpdateType']=='Deleted' for u in updates)
            assert {u['Path'] for u in updates}=={'/strimkeep/'+relative}
            assert engine.emby.row(jid)['state']=='scan_only' and orphan.exists()
        await http.aclose();store.close()
    asyncio.run(run())


def test_rename_notifies_old_and_new_paths_and_unchanged_sync_sends_nothing(tmp_path):
    async def run():
        store,cloud,http,engine,_=setup(tmp_path);cloud.add_video(name='Old.mkv');calls=[]
        def respond(req):
            if req.url.path.endswith('/System/Info/Public'):
                return httpx.Response(200, json={'ServerName': 'test', 'Version': '4.8'})
            if req.url.path.endswith('/Library/VirtualFolders'):
                return httpx.Response(200, json=[])
            calls.append(req);return httpx.Response(204)
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as api:
            engine.emby.http=api
            await engine.run_job(new_job(store,sync_config())); await engine.emby.drain_outbox(); await engine.emby.tick()
            old=engine.outputs.get('1','1000')['relative_path'];cloud.nodes['1000']['name']='New.mkv';calls.clear()
            await engine.run_job(new_job(store,sync_config())); await engine.emby.drain_outbox(); await engine.emby.tick()
            current=engine.outputs.get('1','1000')['relative_path']
            assert json.loads(calls[0].content)['Updates']==sorted([{'Path':'/strimkeep/'+old,'UpdateType':'Deleted'},{'Path':'/strimkeep/'+current,'UpdateType':'Created'}],key=lambda u:u['Path'])
            calls.clear();await engine.run_job(new_job(store,sync_config())); await engine.emby.drain_outbox(); await engine.emby.tick();assert not calls
        await http.aclose();store.close()
    asyncio.run(run())


def test_quick_first_check_confirms_existing_item_without_resending_notice(tmp_path):
    async def run():
        store,cloud,http,engine,_=setup(tmp_path);cloud.add_video(name='Movie.mkv');calls=[]
        def respond(req):
            if req.url.path.endswith('/System/Info/Public'):
                return httpx.Response(200, json={'ServerName': 'test', 'Version': '4.8'})
            if req.url.path.endswith('/Library/VirtualFolders'):
                return httpx.Response(200, json=[])
            calls.append(req)
            if req.method=='POST':return httpx.Response(204)
            plan=json.loads(store.db.execute('SELECT plan FROM organize_items ORDER BY id DESC LIMIT 1').fetchone()[0])
            return httpx.Response(200,json={'Items':[{'Path':'/strimkeep/'+plan['relative_path']}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as api:
            engine.emby.http=api
            jid=new_job(store,sync_config());await engine.run_job(jid); await engine.emby.drain_outbox(); await engine.emby.tick()
            row=engine.emby.row(jid);payload=json.loads(row['payload'])
            assert row['next_due']<=payload['sent_at']+3.1
            await engine.emby.tick(time.time()+4)
            assert engine.job(jid)['notify_state']=='verified'
            assert len([c for c in calls if c.method=='POST'])==1
            assert '不是 Emby 精确处理耗时' in engine.emby.row(jid)['message']
            await asyncio.gather(*engine.notice_tasks)
        await http.aclose();store.close()
    asyncio.run(run())


def test_incremental_delete_after_first_delivery_is_not_lost(tmp_path):
    async def run():
        store,cloud,http,engine,_=setup(tmp_path)
        jid=new_job(store,sync_config())
        with store.db:
            store.db.execute('INSERT INTO strm_changes VALUES(?,?,?)',(jid,'removed','old/a.strm'))
        await engine.emby.submit(jid)
        row=engine.emby.row(jid)
        assert [u['Path'] for u in json.loads(row['payload'])['updates']]==['/strimkeep/old/a.strm']
        engine.emby.save(jid,state='scan_only')
        with store.db:
            store.db.execute('INSERT INTO strm_changes VALUES(?,?,?)',(jid,'removed','old/b.strm'))
        await engine.emby.submit(jid)
        row=engine.emby.row(jid)
        assert row['state']=='pending'
        assert '/strimkeep/old/b.strm' in [u['Path'] for u in json.loads(row['payload'])['updates']]
        await http.aclose();store.close()
    asyncio.run(run())
