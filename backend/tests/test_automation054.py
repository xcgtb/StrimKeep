import asyncio
from types import SimpleNamespace
from test_organize import setup, new_job
from strimkeep.cloud115 import CloudError


def test_fresh_organization_does_not_inventory_unrelated_archive(tmp_path):
    async def run():
        store,cloud,http,engine,_=setup(tmp_path)
        other=(await cloud.mkdir('200','Unrelated'))['cid']
        cloud.add_video('2000','unknown.mkv',other);cloud.add_video()
        children=cloud.children
        async def read(cid):
            assert cid!=other
            return await children(cid)
        cloud.children=read
        jid=new_job(store);await engine.run_job(jid)
        assert engine.job(jid)['state']=='completed',dict(engine.job(jid))
        assert engine.index.latest('1','200')['state']=='local'
        await http.aclose();store.close()
    asyncio.run(run())


def test_missing_subtitle_does_not_abort_video_sync(tmp_path):
    from test_phase5 import sync_config
    async def run():
        store,cloud,http,engine,_=setup(tmp_path)
        cloud.add_video(name='A.mkv');cloud.add_video('1001','A.chs.srt')['size']=50
        cloud.add_video('2000','B.mkv')
        read=cloud.file
        async def file(fid):
            if fid=='1001':raise CloudError('subtitle disappeared','FILE_NOT_FOUND',404)
            return await read(fid)
        cloud.file=file
        jid=new_job(store,sync_config());await engine.run_job(jid)
        assert engine.job(jid)['state']=='partial',dict(engine.job(jid))
        assert len(list(engine.root.rglob('*.strm')))==2
        assert not cloud.writes
        await http.aclose();store.close()
    asyncio.run(run())


def test_schedule_patch_isolation_on_backend(tmp_path,monkeypatch):
    from test_directory_readback import api_setup
    from fastapi.testclient import TestClient
    app,jid=api_setup(tmp_path,monkeypatch)
    with TestClient(app) as client:
        csrf=client.post('/api/v1/auth/login',json={'username':'admin','password':'test-password-1234'}).json()['csrf']
        client.headers['X-CSRF-Token']=csrf
        assert client.put('/api/v1/system/schedules',json={'scan_interval_minutes':17}).status_code==200
        assert client.put('/api/v1/system/schedules',json={'sync_interval_minutes':41}).status_code==200
        cfg=client.get('/api/v1/system/schedules').json()['config']
        assert cfg['scan_interval_minutes']==17 and cfg['sync_interval_minutes']==41


def test_manual_move_back_and_deleted_cached_directory_recover(tmp_path):
    async def run():
        store,cloud,http,engine,_=setup(tmp_path);cloud.add_video()
        jid=new_job(store);await engine.run_job(jid)
        assert engine.job(jid)['state']=='completed'
        oldparent=cloud.nodes['1000']['parent_id']
        cloud.nodes['1000']['parent_id']='100';cloud.nodes.pop(oldparent)
        path_info=cloud.path_info
        async def info(cid):
            if cid==oldparent:raise CloudError('directory removed','FILE_NOT_FOUND',404)
            return await path_info(cid)
        cloud.path_info=info
        jid=new_job(store);await engine.run_job(jid)
        assert engine.job(jid)['state']=='completed',dict(engine.job(jid))
        assert cloud.nodes['1000']['parent_id'] not in ('100',oldparent)
        await http.aclose();store.close()
    asyncio.run(run())


def test_low_disk_prevents_new_cloud_mutations(tmp_path,monkeypatch):
    async def run():
        store,cloud,http,engine,_=setup(tmp_path);cloud.add_video()
        monkeypatch.setattr('strimkeep.organizer.shutil.disk_usage',lambda p:SimpleNamespace(free=1))
        jid=new_job(store);await engine.run_job(jid)
        assert engine.job(jid)['state']=='needs_review'
        assert not cloud.writes
        await http.aclose();store.close()
    asyncio.run(run())


def test_delay_uses_persisted_deadline_after_pause(tmp_path,monkeypatch):
    import json
    from test_organize import CONFIG
    async def run():
        store,cloud,http,engine,_=setup(tmp_path);cloud.add_video()
        now=[1000000000.0];waits=[]
        monkeypatch.setattr('strimkeep.organizer.time.time',lambda:now[0])
        jid=new_job(store,{**CONFIG,'strm_delay':3})
        async def pause(delay):
            if delay == 0:
                return
            waits.append(delay);now[0]+=delay
            engine.update_job(jid,state='paused')
        monkeypatch.setattr('strimkeep.organizer.asyncio.sleep',pause)
        await engine.run_job(jid)
        assert engine.job(jid)['state']=='paused'
        plan=json.loads(store.db.execute('SELECT plan FROM organize_items WHERE job_id=?',(jid,)).fetchone()[0])
        assert plan['ready_at']==1000000003.0
        now[0]+=10;waits.clear();engine.update_job(jid,state='queued')
        await engine.run_job(jid)
        assert engine.job(jid)['state']=='completed',dict(engine.job(jid))
        assert waits==[]
        await http.aclose();store.close()
    asyncio.run(run())
