import asyncio,json,copy,time
import httpx,pytest
from test_organize import setup,CONFIG,new_job
from strimkeep.cloud115 import CloudError,Cloud115
from strimkeep.transfer import shares


def mock_share(cloud,mode='normal'):
    calls=[]
    top=[{'id':'9001','name':'Movie','is_dir':True,'size':0}]
    child=[{'id':'9002','name':'测试电影 (2024).mkv','is_dir':False,'size':1000}]
    async def items(secret,cid=0):return copy.deepcopy(child if str(cid)=='9001' else top)
    async def receive(secret,entries,dest):
        calls.append((secret,entries,dest))
        if mode=='limited':raise CloudError('操作过于频繁，请稍后再试','RATE_LIMITED',503)
        if mode=='already':raise CloudError('该文件已转存过','ALREADY_SAVED',422)
        folder=(await cloud.mkdir(dest,'Movie'))['cid']
        if mode!='incomplete':cloud.add_video('7000',child[0]['name'],folder)
        if mode=='lost':raise CloudError('lost response')
    bin_entries=[]
    async def recycle_files():return list(bin_entries)
    async def empty(cid,parent):
        assert not await cloud.children(cid)
        node=cloud.nodes.pop(cid)
        bin_entries.append({'id':cid,'rid':'800'+cid,'name':node['name'],'size':0,'parent_id':parent,'is_dir':True,'deleted_at':int(time.time())})
    cloud.recycle_files=recycle_files;cloud.recycle_empty_directory=empty
    cloud.share_items=items;cloud.share_receive=receive
    return calls


@pytest.mark.parametrize('text', ['https://115.com/s/abc?password=a123','分享 https://115cdn.com/s/abc 提取码：a123','https://anxia.com/s/abc\n密码 a123'])
def test_share_parser(text):
    assert shares(text)==[{'share_code':'abc','receive_code':'a123'}]


@pytest.mark.parametrize('text',['https://115.com.evil/s/abc','https://evil/115.com/s/abc','abc'])
def test_share_parser_rejects_other_hosts(text):
    with pytest.raises(ValueError):shares(text)


@pytest.mark.parametrize('mode',['normal','lost','incomplete'])
def test_transfer_receipt_resume_dedup_and_followup(tmp_path,mode):
    async def run():
        store,cloud,http,engine,_=setup(tmp_path)
        store.set_many({'organize_config':CONFIG});calls=mock_share(cloud,mode)
        ids=await engine.transfer.enqueue('https://115.com/s/abc?password=Z9q8','100',True,update='test:1')
        assert await engine.transfer.enqueue('https://115.com/s/abc?password=Z9q8','100',True,update='test:1')==ids
        assert b'Z9q8' not in store.path.read_bytes()
        jid=ids[0];await engine.run_job(jid)
        if mode=='incomplete':
            assert engine.job(jid)['state']=='needs_review'
            folder=next(n['id'] for n in cloud.nodes.values() if n['name']=='Movie')
            cloud.add_video('7000','测试电影 (2024).mkv',folder)
            engine.update_job(jid,state='queued');await engine.run_job(jid)
        assert engine.job(jid)['state']=='completed',dict(engine.job(jid))
        assert len(calls)==1
        receipt=json.loads(engine.job(jid)['config'])['receipt'];child=receipt['organize_job']
        assert engine.job(child)['state']=='queued'
        assert json.loads(engine.job(child)['config'])['source_cids']==['100']
        assert json.loads(engine.job(child)['config'])['source_entries']==receipt['received_entries']
        engine.update_job(jid,state='queued');await engine.run_job(jid)
        assert len(calls)==1
        assert store.db.execute("SELECT count(*) FROM organize_jobs WHERE json_extract(config,'$.transfer_parent')=?",(jid,)).fetchone()[0]==1
        await engine.run_job(child)
        assert engine.job(child)['state']=='completed',dict(engine.job(child))
        assert len(list(engine.root.rglob('*.strm')))==1
        await http.aclose();store.close()
    asyncio.run(run())


def test_bot_authorization_dedup_and_caption_links(tmp_path):
    async def run():
        store,cloud,http,engine,_=setup(tmp_path);mock_share(cloud)
        conf={'allowed_user_id':'7','target_cid':'100','auto_organize':False}
        message={'from':{'id':8},'chat':{'id':8,'type':'private'},'caption':'share','caption_entities':[{'type':'text_link','url':'https://115.com/s/abc?password=abcd'}]}
        await engine.bot.handle({'update_id':99,'message':message},conf,'bot')
        assert not store.db.execute('SELECT 1 FROM organize_jobs').fetchone()
        message['from']['id']=7;message['chat']['id']=7
        update={'update_id':99,'message':message}
        await engine.bot.handle(update,conf,'bot');await engine.bot.handle(update,conf,'bot')
        assert store.db.execute('SELECT count(*) FROM organize_jobs').fetchone()[0]==1
        await http.aclose();store.close()
    asyncio.run(run())


def test_media_photo_grouping_message_limits_and_restart_dedup(tmp_path):
    async def run():
        store,cloud,http,engine,_=setup(tmp_path)
        store.set_many({'notification_config':{'telegram_enabled':True,'bot_token':'12345:'+('X'*30),'chat_id':'7','categories':{'strm_files':True}}})
        sent=[]
        async def handler(request):sent.append((request.url.path,json.loads(request.content)));return httpx.Response(200,json={'ok':True,'result':{}})
        outbound=httpx.AsyncClient(transport=httpx.MockTransport(handler));engine.notifications.http=outbound
        jid=new_job(store);engine.update_job(jid,state='completed',notify_state='accepted')
        for n in range(1,53):
            source={'id':str(n),'name':f'Original {n}','size':1000}
            plan={'details':{'id':2253,'name':'测试剧集','first_air_date':'1996-01-01','poster_path':'/abc.jpg'},'hint':{'media_type':'tv','season':1,'episode':n,'end_episode':n},'media':{'height':2160,'codec':'hevc'},'parts':['剧集','日韩剧集'],'relative_path':f'Library/测试剧集/S01E{n:02d}.strm'}
            with store.db:store.db.execute("INSERT INTO organize_items(job_id,file_id,original,state,plan) VALUES(?,?,?,'done',?)",(jid,str(n),json.dumps(source),json.dumps(plan)))
        await engine.notifications.job(engine.job(jid));before=len(sent)
        await engine.notifications.job(engine.job(jid));assert len(sent)==before
        photo=[x for x in sent if x[0].endswith('/sendPhoto')];assert len(photo)==1
        assert 'E01-E52' in photo[0][1]['caption']
        details=[x[1]['text'] for x in sent if 'text' in x[1] and 'STRM 文件明细' in x[1]['text']]
        assert len(details)==3
        assert all(len(x.encode('utf-16-le'))//2<=4096 for x in details)
        assert all('token=' not in x for x in details)
        await outbound.aclose();await http.aclose();store.close()
    asyncio.run(run())


def test_actual_sdk_share_contract_read_and_receive_payload(tmp_path):
    from strimkeep.store import Store
    async def run():
        store=Store(tmp_path,'admin','test-password-1234');store.set_many({'cookie':'UID=1_A1; CID=x; SEID=x'})
        cloud=Cloud115(store);await cloud.http.aclose();requests=[]
        async def respond(request):
            requests.append(request)
            if request.url.path=='/share/snap':
                assert request.url.params['receive_code']=='a123'
                return httpx.Response(200,json={'state':True,'data':{'count':1,'list':[{'fid':'55','cid':'0','n':'a.mkv','s':'10','pc':'pc55'}]}})
            assert request.url.path=='/share/receive' and request.method=='POST'
            from urllib.parse import parse_qs
            payload=parse_qs(request.content.decode())
            assert payload['cid']==['100'] and payload['file_id']==['55']
            return httpx.Response(200,json={'state':True,'data':{}})
        cloud.http=httpx.AsyncClient(transport=httpx.MockTransport(respond))
        secret={'share_code':'abc','receive_code':'a123'}
        entries=await cloud.share_items(secret)
        assert entries==[{'id':'55','name':'a.mkv','size':10,'is_dir':False}]
        await cloud.share_receive(secret,entries,'100')
        assert len(requests)==2
        await cloud.close();store.close()
    asyncio.run(run())


def test_bot_bootstrap_offset_and_resume(tmp_path):
    async def run():
        store,cloud,http,engine,_=setup(tmp_path);mock_share(cloud)
        conf={'bot_enabled':True,'target_cid':'100','allowed_user_id':'7'}
        store.set_many({'transfer_config':conf,'notification_config':{'bot_token':'fake','telegram_enabled':True,'chat_id':'7'}})
        polls=[]
        async def respond(request):
            if request.url.path.endswith('/getUpdates'):
                payload=json.loads(request.content);polls.append(payload['offset'])
                ident=100 if len(polls)==1 else 101
                return httpx.Response(200,json={'ok':True,'result':[{'update_id':ident,'message':{'from':{'id':7},'chat':{'id':7,'type':'private'},'text':'https://115.com/s/abc?password=abcd'}}]})
            return httpx.Response(200,json={'ok':True,'result':{}})
        outbound=httpx.AsyncClient(transport=httpx.MockTransport(respond));engine.bot.http=outbound;engine.notifications.http=outbound
        await engine.bot.poll();assert not store.db.execute('SELECT 1 FROM organize_jobs').fetchone()
        await engine.bot.poll();assert store.db.execute('SELECT count(*) FROM organize_jobs').fetchone()[0]==1
        await engine.bot.poll();assert store.db.execute('SELECT count(*) FROM organize_jobs').fetchone()[0]==1
        assert polls==[-1,101,102]
        await outbound.aclose();await http.aclose();store.close()
    asyncio.run(run())


def test_transfer_api_auth_config_and_secret_masking(tmp_path,monkeypatch):
    from test_directory_readback import api_setup
    from fastapi.testclient import TestClient
    app,jid=api_setup(tmp_path,monkeypatch)
    with TestClient(app) as client:
        assert client.get('/api/v1/transfer/config').status_code==401
        login=client.post('/api/v1/auth/login',json={'username':'admin','password':'test-password-1234'}).json()
        assert client.post('/api/v1/transfer/jobs',json={'text':'https://115.com/s/abc'}).status_code==403
        client.headers['X-CSRF-Token']=login['csrf']
        assert client.put('/api/v1/transfer/config',json={'target_cid':'100','auto_organize':False}).status_code==200
        created=client.post('/api/v1/transfer/jobs',json={'text':'https://115.com/s/abc?password=X7r2'})
        assert created.status_code==200
        assert created.json()['items'][0]['kind']=='transfer'
        text=client.get('/api/v1/transfer/jobs').text
        assert 'X7r2' not in text and 'share_code' not in text and 'transfer-secret' not in text
        assert client.put('/api/v1/transfer/config',json={'target_cid':'0'}).status_code==422


def test_transfer_halts_on_rate_limit_and_stays_retryable(tmp_path):
    async def run():
        store,cloud,http,engine,_=setup(tmp_path)
        store.set_many({'organize_config':CONFIG});calls=mock_share(cloud,'limited')
        ids=await engine.transfer.enqueue('https://115.com/s/abc?password=Z9q8','100',True,update='t:limit')
        jid=ids[0]
        await engine.run_job(jid)
        job=engine.job(jid)
        assert job['state']=='needs_review',dict(job)
        assert '限流' in job['stage'],job['stage']
        receipt=json.loads(job['config']).get('receipt',{})
        assert 'phase' not in receipt            # 明确被拒，冷却后可以重新提交
        assert len(calls)==1
        # 冷却结束重新跑：不应该因为 phase 卡在 intent 而永远核不通过
        engine.update_job(jid,state='queued')
        mock_share(cloud,'normal')
        await engine.run_job(jid)
        assert engine.job(jid)['state']=='completed',dict(engine.job(jid))
        await http.aclose();store.close()
    asyncio.run(run())


def test_channel_transfer_wakes_the_scan_instead_of_organizing_immediately(tmp_path):
    async def run():
        store,cloud,http,engine,_=setup(tmp_path)
        store.set_many({'organize_config':CONFIG});mock_share(cloud,'normal')
        woke=[]
        engine.tasks.wake=lambda kind: woke.append(kind)
        ids=await engine.transfer.enqueue('https://115.com/s/abc?password=Z9q8','100',False,'channel','t:ch')
        await engine.run_job(ids[0])
        assert engine.job(ids[0])['state']=='completed'
        assert woke==['organize']                       # 只把下一轮扫描提前
        receipt=json.loads(engine.job(ids[0])['config'])['receipt']
        assert not receipt.get('organize_job')          # 没有立刻插整理任务
        await http.aclose();store.close()
    asyncio.run(run())


@pytest.mark.parametrize('text', [
    'magnet:?xt=urn:btih:abcdef&dn=test',
    'ed2k://|file|x.mkv|1000|ABCDEF|/',
    '/share magnet:?xt=urn:btih:abcdef',
])
def test_share_parser_tells_magnet_and_ed2k_users_to_use_offline(text):
    with pytest.raises(ValueError) as exc:
        shares(text)
    assert '115 分享链接' in str(exc.value) and '离线' in str(exc.value)


def test_transfer_resends_once_when_nothing_appears(tmp_path):
    """意图已落库、目录里却什么都没有：允许一次幂等重发，且只重发一次。"""
    async def run():
        store,cloud,http,engine,_=setup(tmp_path)
        store.set_many({'organize_config':CONFIG})
        calls=mock_share(cloud)                      # 正常转存（会真的建目录与文件）
        normal=cloud.share_receive
        seen={'n':0}
        async def flaky(secret,entries,dest):
            seen['n']+=1
            if seen['n']==1:
                calls.append((secret,entries,dest))  # 第一次：什么都没建就丢了响应
                raise CloudError('lost response')
            return await normal(secret,entries,dest)
        cloud.share_receive=flaky
        jid=(await engine.transfer.enqueue('https://115.com/s/abc?password=Z9q8','100',True,update='t:resend'))[0]
        await engine.run_job(jid)
        job=engine.job(jid)
        assert job['state']=='completed',dict(job)
        assert seen['n']==2, '只在「目录里没有本批产物」时重发一次'
        receipt=json.loads(job['config'])['receipt']
        assert receipt.get('resent') is True and receipt.get('accepted') is True
        await http.aclose();store.close()
    asyncio.run(run())


def test_transfer_does_not_resend_a_second_time(tmp_path):
    """重发过仍然没产物：停在「只检查、不重复转存」，不再来第三次。"""
    async def run():
        store,cloud,http,engine,_=setup(tmp_path)
        store.set_many({'organize_config':CONFIG})
        calls=mock_share(cloud)
        seen={'n':0}
        async def never(secret,entries,dest):
            seen['n']+=1
            calls.append((secret,entries,dest))
            raise CloudError('lost response')        # 每次都没有任何产物
        cloud.share_receive=never
        jid=(await engine.transfer.enqueue('https://115.com/s/abc?password=Z9q8','100',True,update='t:resend2'))[0]
        await engine.run_job(jid)
        job=engine.job(jid)
        assert job['state']=='needs_review',dict(job)
        assert '继续只检查，不重复转存' in job['message'],job['message']
        assert seen['n']==2, '重发只允许一次'
        await http.aclose();store.close()
    asyncio.run(run())


def test_incomplete_receipt_is_reported_as_accepted_not_lost(tmp_path):
    """115 已经受理、只是目录还没展开时，别让人以为要重新提交。"""
    async def run():
        store,cloud,http,engine,_=setup(tmp_path)
        store.set_many({'organize_config':CONFIG});mock_share(cloud,'incomplete')
        ids=await engine.transfer.enqueue('https://115.com/s/abc?password=Z9q8','100',True,update='t:inc')
        jid=ids[0];await engine.run_job(jid)
        job=engine.job(jid)
        assert job['state']=='needs_review'
        assert '已被 115 受理' in job['message'],job['message']
        assert '不要重复提交' in job['message']
        assert json.loads(job['config'])['receipt']['accepted'] is True
        await http.aclose();store.close()
    asyncio.run(run())


def test_password_after_second_link_does_not_attach_to_first():
    from strimkeep.transfer import shares
    result = shares('https://115.com/s/aaaa https://115.com/s/bbbb 提取码: 1234')
    assert result == [{'share_code':'aaaa','receive_code':''}, {'share_code':'bbbb','receive_code':'1234'}]


def test_transfer_followup_missing_received_entry_is_skipped(tmp_path):
    async def run():
        store,cloud,http,engine,_=setup(tmp_path)
        store.set_many({'organize_config':CONFIG});mock_share(cloud)
        jid=(await engine.transfer.enqueue('https://115.com/s/abc','100',True))[0]
        await engine.run_job(jid)
        receipt=json.loads(engine.job(jid)['config'])['receipt']
        child=receipt['organize_job']
        missing=receipt['received_entries'][0]['id']
        cloud.nodes.pop(missing)
        await engine.run_job(child)
        job=engine.job(child)
        assert job['state']=='completed', dict(job)
        assert '不在原目标目录' in job['message']
        assert not list(engine.root.rglob('*.strm'))
        await http.aclose();store.close()
    asyncio.run(run())


def test_deleted_after_scan_does_not_stop_other_files(tmp_path):
    async def run():
        store,cloud,http,engine,_=setup(tmp_path)
        cloud.add_video('1000','测试电影 (2024).mkv')
        cloud.add_video('1001','测试剧集 (2024) S01E03.WEB-DL.mkv')
        original_manifest=engine.manifest
        async def remove_after_scan(jid,config):
            await original_manifest(jid,config)
            cloud.nodes.pop('1000')
        engine.manifest=remove_after_scan
        original_file=cloud.file
        async def file(fid):
            if fid not in cloud.nodes:
                raise CloudError('文件不存在或无法访问。','FILE_NOT_FOUND',404)
            return await original_file(fid)
        cloud.file=file
        jid=new_job(store, {**CONFIG, 'notify_emby':False, 'probe_prefetch':False})
        await engine.run_job(jid)
        rows=store.db.execute('SELECT file_id,state,message FROM organize_items WHERE job_id=?',(jid,)).fetchall()
        states={r['file_id']:r['state'] for r in rows}
        assert states['1000']=='skipped', states
        assert states['1001']=='done', states
        assert '已跳过' in engine.job(jid)['message']
        await http.aclose();store.close()
    asyncio.run(run())


def test_duplicate_transfer_is_reported_precisely_and_never_resubmitted(tmp_path):
    """115 说「已转存过」时：不换目录重转、不反复提交，只把话说清楚。"""
    async def run():
        store,cloud,http,engine,_=setup(tmp_path)
        store.set_many({'organize_config':CONFIG})
        calls=mock_share(cloud,mode='already')
        jid=(await engine.transfer.enqueue('https://115.com/s/abc','100',True))[0]
        await engine.run_job(jid)
        job=engine.job(jid)
        assert job['state']=='needs_review', dict(job)
        assert '已转存过' in job['message'] and '换一个目标目录' in job['message']
        assert len(calls)==1, calls          # 只提交过一次，绝不重复
        await http.aclose();store.close()
    asyncio.run(run())


def test_share_snap_exposes_valid_sha_for_content_cache(tmp_path):
    from strimkeep.store import Store
    async def run():
        store=Store(tmp_path,'admin','test-password-1234');store.set_many({'cookie':'UID=1_A1; CID=x; SEID=x'})
        cloud=Cloud115(store);await cloud.http.aclose()
        async def respond(request):
            return httpx.Response(200,json={'state':True,'data':{'count':1,'list':[{'fid':'55','cid':'0','n':'a.mkv','s':'10','pc':'pc55','sha':'a'*40}]}})
        cloud.http=httpx.AsyncClient(transport=httpx.MockTransport(respond))
        entries=await cloud.share_items({'share_code':'abc','receive_code':'a123'})
        assert entries[0].get('sha1')=='A'*40
        await cloud.close();store.close()
    asyncio.run(run())
