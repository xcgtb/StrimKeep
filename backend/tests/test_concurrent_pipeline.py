import asyncio
import json
import time
import pytest
from test_queue_priority import make


def test_workers_start_independently_and_claim_once(tmp_path):
    async def scenario():
        store, cloud, http, engine = make(tmp_path)
        now=time.time()
        for kind in ('transfer','organize','strm_sync'):
            with store.db:
                store.db.execute("INSERT INTO organize_jobs(state,config,created,updated) VALUES('queued',?,?,?)", (json.dumps({'job_kind':kind}),now,now))
        entered=[]; reached=asyncio.Event(); release=asyncio.Event()
        async def fake_run(jid):
            entered.append(jid)
            if len(entered)==3: reached.set()
            await release.wait()
            engine.update_job(jid,state='completed')
        engine.run_job=fake_run
        tasks=[asyncio.create_task(engine.loop(lane)) for lane in ('transfer','organize','sync')]
        try:
            await asyncio.wait_for(reached.wait(),2)
            assert len(set(entered))==3
        finally:
            release.set();engine.closing=True
            for task in tasks: task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
            await http.aclose();store.close()
    asyncio.run(scenario())


def test_publication_lock_blocks_sync_for_active_organize(tmp_path):
    async def scenario():
        store, cloud, http, engine = make(tmp_path)
        entered=asyncio.Event()
        async def sync_phase():
            async with engine.publication_gate:
                entered.set()
        async with engine.publication_gate:
            t=asyncio.create_task(sync_phase())
            await asyncio.sleep(0)
            assert not entered.is_set()
        await asyncio.wait_for(entered.wait(),1)
        await t
        await http.aclose();store.close()
    asyncio.run(scenario())


def test_emby_outbox_is_durable_and_revision_does_not_lose_new_events(tmp_path):
    async def scenario():
        store, cloud, http, engine = make(tmp_path)
        now=time.time()
        jid=store.db.execute("INSERT INTO organize_jobs(state,config,created,updated) VALUES('running','{}',?,?)",(now,now)).lastrowid
        engine.emby.enqueue(jid)
        started=asyncio.Event(); release=asyncio.Event()
        async def blocked(_):
            started.set(); await release.wait()
        engine.emby.submit=blocked
        task=asyncio.create_task(engine.emby.drain_outbox())
        await started.wait()
        engine.emby.enqueue(jid)
        release.set(); await task
        row=store.db.execute('SELECT revision FROM emby_outbox WHERE job_id=?',(jid,)).fetchone()
        assert row and row['revision']==2
        await http.aclose();store.close()
    asyncio.run(scenario())


def test_organize_stores_emby_event_then_background_sends(tmp_path):
    from test_organize import setup, new_job
    async def scenario():
        store, cloud, http, engine, requests = setup(tmp_path)
        cloud.add_video()
        jid = new_job(store)
        await engine.run_job(jid)
        assert engine.job(jid)['state'] == 'completed'
        assert store.db.execute('SELECT 1 FROM emby_outbox WHERE job_id=?',(jid,)).fetchone()
        assert not [r for r in requests if r.url.path.endswith('/Library/Media/Updated')]
        await engine.emby.drain_outbox()
        await engine.emby.tick()
        assert [r for r in requests if r.url.path.endswith('/Library/Media/Updated')]
        assert engine.emby.row(jid)['state'] == 'accepted'
        if engine.notice_tasks:
            await asyncio.gather(*engine.notice_tasks)
        await http.aclose(); store.close()
    asyncio.run(scenario())


def test_sync_checks_cleanup_with_scan_start_when_revision_changes(tmp_path):
    from test_organize import setup, new_job
    from test_phase5 import sync_config
    async def scenario():
        store, cloud, http, engine, _ = setup(tmp_path)
        cloud.add_video()
        config = {**sync_config(), 'notify_emby':False}
        jid = new_job(store,config)
        original_manifest = engine.sync.manifest
        async def manifest(*args):
            await original_manifest(*args)
            engine.sync.invalidate('1')
        engine.sync.manifest = manifest
        called=[]
        async def cleanup(*args,after=None):
            called.append(after)
            return 0, []
        engine.sync.cleanup = cleanup
        await engine.run_job(jid)
        assert len(called)==1 and called[0]>0
        assert engine.job(jid)['state'] == 'completed'
        assert '跳过失效清理' not in engine.job(jid)['message']
        await http.aclose(); store.close()
    asyncio.run(scenario())


def test_duplicate_waiting_syncs_merge_but_running_gets_followup(tmp_path):
    store, cloud, http, engine = make(tmp_path)
    config = {'job_kind':'strm_sync','account_id':'1','source_cids':['100']}
    first, merged = engine.tasks.enqueue(config)
    second, merged_second = engine.tasks.enqueue(config, scheduled=True)
    assert first == second and not merged and merged_second
    engine.update_job(first,state='running')
    third, merged_third = engine.tasks.enqueue(config)
    assert third != first and not merged_third
    asyncio.run(http.aclose()); store.close()


def test_real_transfer_can_finish_while_organize_file_is_waiting(tmp_path):
    from test_organize import setup, new_job, CONFIG
    from test_transfer import mock_share
    async def scenario():
        store, cloud, http, engine, _ = setup(tmp_path)
        cloud.add_video('1001','测试剧集 (2024) S01E01.mkv')
        calls = mock_share(cloud)
        organize_id = new_job(store, {**CONFIG,'notify_emby':False,'ffprobe_enabled':False,
                                      'identify_prefetch':False,'probe_prefetch':False})
        transfer_id = (await engine.transfer.enqueue('https://115.com/s/abc','100',False))[0]
        started=asyncio.Event(); resume=asyncio.Event()
        original=engine.process_item
        async def hold(job, row, config):
            started.set()
            await resume.wait()
            return await original(job,row,config)
        engine.process_item = hold
        organize=asyncio.create_task(engine.run_job(organize_id))
        await asyncio.wait_for(started.wait(),2)
        transfer=asyncio.create_task(engine.run_job(transfer_id))
        await asyncio.wait_for(transfer,3)
        assert engine.job(transfer_id)['state']=='completed'
        assert len(calls)==1 and not organize.done()
        resume.set(); await asyncio.wait_for(organize,3)
        if engine.notice_tasks:
            await asyncio.gather(*engine.notice_tasks)
        await http.aclose(); store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('legacy',[False,True])
def test_card_updates_in_place_after_emby_confirmation(tmp_path,legacy):
    from test_organize import setup,new_job
    import httpx
    async def scenario():
        store, cloud, http, engine, _ = setup(tmp_path)
        store.set_many({'notification_config':{'telegram_enabled':True,'bot_token':'1234:'+('a'*30),'chat_id':'7','categories':{'strm_summary':False,'strm_files':False,'task':False,'emby':True}}})
        sent=[]
        async def send(token,method,payload):
            sent.append((method,dict(payload)))
            return httpx.Response(200,json={'ok':True,'result':{'message_id':123}},request=httpx.Request('POST','https://api.telegram.org/test'))
        engine.notifications.send=send
        cloud.add_video()
        jid = new_job(store)
        await engine.run_job(jid)
        if engine.notice_tasks: await asyncio.gather(*engine.notice_tasks)
        cards=[x for x in sent if x[0]=='sendPhoto']
        assert len(cards)==1 and '状态：Emby 入库待核实' in cards[0][1]['caption']
        if legacy:
            with store.db:store.db.execute("UPDATE notifications SET message=replace(message,?,?) WHERE job_id=? AND event_key LIKE 'show_done:%'",('⏳ 状态：Emby 入库待核实','⏳ Emby：入库待核实',jid))
        await engine.notifications.drain_media()
        assert not [x for x in sent if x[0].startswith('editMessage')]
        await engine.emby.drain_outbox()
        delivery=engine.emby.row(jid);payload=json.loads(delivery['payload'])
        assert payload['targets']
        for target in payload['targets']:target['confirmed']=True
        engine.emby.save(jid,payload=json.dumps(payload))
        await engine.emby.finish(jid,'verified','已确认')
        engine.emby.enqueue(jid)  # 新路径仍在等待交付，不得根据旧确认提前更新
        await engine.notifications.drain_media()
        assert not [x for x in sent if x[0].startswith('editMessage')]
        with store.db:store.db.execute('DELETE FROM emby_outbox WHERE job_id=?',(jid,))
        from strimkeep.notifications import Notifications
        engine.notifications=Notifications(store,http)  # 模拟重启后从数据库恢复消息编号
        engine.notifications.send=send
        await engine.notifications.drain_media()
        edits=[x for x in sent if x[0]=='editMessageCaption']
        assert len(edits)==1 and edits[0][1]['message_id']==123
        assert '状态：Emby 已入库' in edits[0][1]['caption'] and '待核实' not in edits[0][1]['caption']
        assert len([x for x in sent if x[0]=='sendPhoto'])==1
        assert not store.db.execute("SELECT 1 FROM notifications WHERE event_key LIKE 'emby-result:%'").fetchone()
        await engine.notifications.drain_media()
        assert len([x for x in sent if x[0]=='editMessageCaption'])==1
        await http.aclose();store.close()
    asyncio.run(scenario())


def test_season_size_and_strm_report_arrive_after_background_publication(tmp_path):
    from test_organize import setup,new_job,CONFIG
    async def scenario():
        store,cloud,http,engine,_ = setup(tmp_path)
        for fid,episode in [('1000','01'),('1001','02')]:
            cloud.add_video(fid,f'测试剧集 (2024) S01E{episode}.mkv')['size']=2*1024**3
        cloud.add_video('1002','测试电影 (2024).mkv')
        job=new_job(store,{**CONFIG,'notify_emby':False,'identify_prefetch':False,'probe_prefetch':False})
        await engine.run_job(job)
        if engine.notice_tasks: await asyncio.gather(*engine.notice_tasks)
        await engine.notifications.drain_media()
        notices=store.db.execute('SELECT title,message FROM notifications WHERE job_id=?',(job,)).fetchall()
        assert any('整理完成' in n['title'] and '体积：4.00G' in n['message'] for n in notices)
        assert any('STRM 批量结果' in n['title'] and '新增 STRM：2 个' in n['message'] for n in notices)
        rows=store.db.execute('SELECT state,message FROM organize_items WHERE job_id=?',(job,)).fetchall()
        assert all(row['state']=='done' for row in rows), [dict(r) for r in rows]
        await engine.notifications.job(engine.job(job))
        notices=store.db.execute("SELECT event_key FROM notifications WHERE job_id=? AND event_key LIKE 'strm:%:files:%'",(job,)).fetchall()
        assert len(notices)==2
        await http.aclose();store.close()
    asyncio.run(scenario())


def test_group_strm_outbox_recovers_once_after_restart(tmp_path):
    from test_organize import setup,new_job,CONFIG
    async def scenario():
        store,cloud,http,engine,_=setup(tmp_path)
        cloud.add_video('1000','测试剧集 (2024) S01E01.mkv')
        jid=new_job(store,{**CONFIG,'notify_emby':False})
        await engine.run_job(jid)
        if engine.notice_tasks:await asyncio.gather(*engine.notice_tasks)
        item=store.db.execute('SELECT id FROM organize_items WHERE job_id=?',(jid,)).fetchone()['id']
        with store.db:
            store.db.execute("DELETE FROM notifications WHERE job_id=? AND event_key LIKE 'strm:%'",(jid,))
            store.db.execute('DELETE FROM strm_reported_items WHERE job_id=?',(jid,))
            store.db.execute('INSERT INTO strm_group_outbox VALUES(?,?,?)',(jid,'recovery',json.dumps([item])))
        await engine.notifications.job(engine.job(jid))
        await engine.notifications.job(engine.job(jid))
        assert not store.db.execute('SELECT 1 FROM strm_group_outbox WHERE job_id=?',(jid,)).fetchone()
        result=store.db.execute("SELECT event_key FROM notifications WHERE job_id=? AND event_key LIKE 'strm:%:files:%'",(jid,)).fetchall()
        assert len(result)==1 and ':group:recovery:' in result[0]['event_key']
        await http.aclose();store.close()
    asyncio.run(scenario())


def test_later_subtitle_update_resends_modified_path_without_losing_old_result(tmp_path):
    async def scenario():
        store, cloud, http, engine = make(tmp_path)
        now=time.time()
        jid=store.db.execute("INSERT INTO organize_jobs(state,config,created,updated) VALUES('running','{}',?,?)",(now,now)).lastrowid
        a=store.db.execute("INSERT INTO organize_items(job_id,file_id,original,state,plan) VALUES(?,'1','{}','done','{}')",(jid,)).lastrowid
        target={'relative':'A.strm','emby_path':'/strimkeep/A.strm','update_type':'Created','pid':'abc','kind':'movie','tmdb':1,'title':'A','confirmed':False}
        engine.emby.targets=lambda job, only_ids=None: [dict(target,update_type='Modified' if only_ids and a not in only_ids else 'Created')]
        engine.emby.enqueue(jid); await engine.emby.drain_outbox()
        payload=json.loads(engine.emby.row(jid)['payload']);payload['targets'][0]['confirmed']=True
        engine.emby.save(jid,state='verified',payload=json.dumps(payload))
        store.db.execute("INSERT INTO organize_items(job_id,file_id,original,state,plan) VALUES(?,'2','{}','recycled','{}')",(jid,))
        engine.emby.enqueue(jid); await engine.emby.drain_outbox()
        payload=json.loads(engine.emby.row(jid)['payload'])
        assert len(payload['targets'])==1 and not payload['targets'][0]['confirmed']
        assert payload['updates']==[{'Path':'/strimkeep/A.strm','UpdateType':'Modified'}]
        await http.aclose();store.close()
    asyncio.run(scenario())


def test_channel_transfer_warns_when_no_scanning_covers_target(tmp_path):
    from test_organize import setup
    from test_transfer import mock_share
    async def scenario():
        store, cloud, http, engine, _ = setup(tmp_path)
        mock_share(cloud)
        store.set_many({'organize_config':{'source_cids':['300']}, 'schedule_config':{'scan_enabled':True}})
        jid=(await engine.transfer.enqueue('https://115.com/s/abc','100',False,origin='channel'))[0]
        await engine.run_job(jid)
        assert engine.job(jid)['state']=='completed'
        assert '未被已配置的待整理目录覆盖' in engine.job(jid)['message']
        await http.aclose();store.close()
    asyncio.run(scenario())


def test_stale_cleanup_preserves_path_now_owned_by_another_file(tmp_path):
    from test_organize import setup
    async def scenario():
        store, cloud, http, engine, _ = setup(tmp_path)
        rel='library/episode.strm'; path=engine.root/rel
        path.parent.mkdir(parents=True,exist_ok=True);path.write_text('new source')
        store.db.execute("INSERT INTO owned_outputs(relative_path,account_id,file_id,digest) VALUES(?,'1','new','hash')", (rel,))
        store.set_many({'stale_strms':json.dumps([{'file_id':'old','relative_path':rel}])})
        cleaned, rest=await engine.sync.stale_cleanup()
        assert not cleaned and rest and path.read_text()=='new source'
        await http.aclose();store.close()
    asyncio.run(scenario())


def test_unconfirmed_card_edit_retries_without_sending_a_second_photo(tmp_path):
    from test_organize import setup,new_job
    import httpx
    async def scenario():
        store,cloud,http,engine,_=setup(tmp_path)
        store.set_many({'notification_config':{'telegram_enabled':True,'bot_token':'1234:'+('a'*30),'chat_id':'7','categories':{'strm_summary':False,'strm_files':False,'task':False,'emby':True}}})
        sent=[]; failures=[True]
        async def send(token,method,payload):
            sent.append((method,dict(payload)))
            code=503 if method=='editMessageCaption' and failures and failures.pop(0) else 200
            return httpx.Response(code,json={'ok':code==200,'result':{'message_id':321}},request=httpx.Request('POST','https://api.telegram.org/test'))
        engine.notifications.send=send
        cloud.add_video();jid=new_job(store);await engine.run_job(jid)
        if engine.notice_tasks:await asyncio.gather(*engine.notice_tasks)
        await engine.emby.drain_outbox()
        await engine.emby.finish(jid,'unconfirmed','入库未确认')
        await engine.notifications.drain_media()
        assert store.db.execute('SELECT 1 FROM media_notice_outbox WHERE job_id=?',(jid,)).fetchone()
        with store.db:store.db.execute('UPDATE telegram_message_refs SET next_try=0')
        await engine.notifications.drain_media()
        edits=[x for x in sent if x[0]=='editMessageCaption']
        assert len(edits)==2 and edits[-1][1]['message_id']==321
        assert '整理完成' in edits[-1][1]['caption'] and '入库未确认' in edits[-1][1]['caption']
        assert len([x for x in sent if x[0]=='sendPhoto'])==1
        assert not store.db.execute('SELECT 1 FROM media_notice_outbox WHERE job_id=?',(jid,)).fetchone()
        await http.aclose();store.close()
    asyncio.run(scenario())


def test_verified_poster_updates_while_job_and_other_targets_still_running(tmp_path):
    from test_organize import setup,new_job
    import httpx
    async def scenario():
        store,cloud,http,engine,_=setup(tmp_path)
        store.set_many({'notification_config':{'telegram_enabled':True,'bot_token':'1234:'+('a'*30),'chat_id':'7','categories':{'strm_summary':False,'strm_files':False,'task':False,'emby':True}}})
        sent=[]
        async def send(token,method,payload):
            sent.append((method,dict(payload)))
            return httpx.Response(200,json={'ok':True,'result':{'message_id':123}},request=httpx.Request('POST','https://api.telegram.org/test'))
        engine.notifications.send=send
        cloud.add_video();jid=new_job(store);await engine.run_job(jid)
        if engine.notice_tasks:await asyncio.gather(*engine.notice_tasks)
        await engine.emby.drain_outbox()
        delivery=engine.emby.row(jid);payload=json.loads(delivery['payload'])
        assert payload['targets'];payload['targets'][0]['confirmed']=True
        payload['targets'].append({'relative':'another/pending.strm','confirmed':False})
        engine.emby.save(jid,state='accepted',payload=json.dumps(payload))
        engine.update_job(jid,state='running',notify_state='accepted')
        with store.db:store.db.execute('DELETE FROM emby_outbox WHERE job_id=?',(jid,))
        await engine.notifications.drain_media()
        edits=[x for x in sent if x[0]=='editMessageCaption']
        assert len(edits)==1 and 'Emby 已入库' in edits[0][1]['caption']
        assert store.db.execute('SELECT 1 FROM media_notice_outbox WHERE job_id=?',(jid,)).fetchone()
        await engine.notifications.drain_media()
        assert len([x for x in sent if x[0]=='editMessageCaption'])==1
        await http.aclose();store.close()
    asyncio.run(scenario())


def test_unconfirmed_poster_waits_while_job_running(tmp_path):
    from test_organize import setup,new_job
    import httpx
    async def scenario():
        store,cloud,http,engine,_=setup(tmp_path)
        store.set_many({'notification_config':{'telegram_enabled':True,'bot_token':'1234:'+('a'*30),'chat_id':'7','categories':{'strm_summary':False,'strm_files':False,'task':False,'emby':True}}})
        sent=[]
        async def send(token,method,payload):
            sent.append((method,dict(payload)))
            return httpx.Response(200,json={'ok':True,'result':{'message_id':124}},request=httpx.Request('POST','https://api.telegram.org/test'))
        engine.notifications.send=send
        cloud.add_video();jid=new_job(store);await engine.run_job(jid)
        if engine.notice_tasks:await asyncio.gather(*engine.notice_tasks)
        await engine.emby.drain_outbox()
        delivery=engine.emby.row(jid);payload=json.loads(delivery['payload'])
        for target in payload['targets']:target['confirmed']=False
        engine.emby.save(jid,state='accepted',payload=json.dumps(payload))
        engine.update_job(jid,state='running',notify_state='accepted')
        with store.db:store.db.execute('DELETE FROM emby_outbox WHERE job_id=?',(jid,))
        await engine.notifications.drain_media()
        assert not [x for x in sent if x[0].startswith('editMessage')]
        assert store.db.execute('SELECT 1 FROM media_notice_outbox WHERE job_id=?',(jid,)).fetchone()
        await http.aclose();store.close()
    asyncio.run(scenario())


def test_grouped_poster_waits_for_each_episode_confirmation(tmp_path):
    from test_organize import setup,new_job,CONFIG
    import httpx
    async def scenario():
        store,cloud,http,engine,_=setup(tmp_path)
        store.set_many({'notification_config':{'telegram_enabled':True,'bot_token':'1234:'+('a'*30),'chat_id':'7','categories':{'strm_summary':False,'strm_files':False,'task':False,'emby':True}}})
        sent=[]
        async def send(token,method,payload):
            sent.append((method,dict(payload)))
            return httpx.Response(200,json={'ok':True,'result':{'message_id':125}},request=httpx.Request('POST','https://api.telegram.org/test'))
        engine.notifications.send=send
        cloud.add_video('1000','测试剧集 (2024) S01E01.mkv')
        cloud.add_video('1001','测试剧集 (2024) S01E02.mkv')
        jid=new_job(store,{**CONFIG,'notify_emby':True,'identify_prefetch':False,'probe_prefetch':False});await engine.run_job(jid)
        if engine.notice_tasks:await asyncio.gather(*engine.notice_tasks)
        await engine.emby.drain_outbox()
        delivery=engine.emby.row(jid);payload=json.loads(delivery['payload'])
        assert len(payload['targets'])==2
        payload['targets'][0]['confirmed']=True;payload['targets'][1]['confirmed']=False
        engine.emby.save(jid,state='accepted',payload=json.dumps(payload))
        engine.update_job(jid,state='running',notify_state='accepted')
        with store.db:store.db.execute('DELETE FROM emby_outbox WHERE job_id=?',(jid,))
        await engine.notifications.drain_media()
        assert not [x for x in sent if x[0].startswith('editMessage')]
        payload['targets'][1]['confirmed']=True;engine.emby.save(jid,payload=json.dumps(payload))
        await engine.notifications.drain_media()
        assert len([x for x in sent if x[0]=='editMessageCaption'])==1
        await http.aclose();store.close()
    asyncio.run(scenario())
