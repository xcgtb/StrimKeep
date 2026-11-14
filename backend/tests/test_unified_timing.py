import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from strimkeep import share_service
from strimkeep.cloud115 import Cloud115, CloudError
from strimkeep.main import create_app
from strimkeep.offline_api import OfflinePostProcess
from strimkeep.store import Store
from strimkeep.telegram_bot import TelegramBot
from test_channel_monitor import make
from test_offline_retry import make_db, engine_on, FakeNotifications
from test_rate_limits import make_cloud, accepted
from test_share_ingest_endpoint import IngestCloud
from test_telegram_share_cmd import Notices, update


def test_share_settings_preserve_legacy_scan_and_generate_respects_auto(tmp_path, monkeypatch):
    monkeypatch.setenv('ADMIN_USERNAME', 'admin')
    monkeypatch.setenv('ADMIN_PASSWORD', 'test-password-1234')
    app=create_app(tmp_path/'data',tmp_path/'strm',IngestCloud)
    with TestClient(app) as client:
        login=client.post('/api/v1/auth/login',json={'username':'admin','password':'test-password-1234'}).json()
        client.headers['X-CSRF-Token']=login['csrf']
        store=app.state.store
        store.set_many({'share_scan_minutes':10,'share_settings':{'auto_organize':False}})
        settings=client.get('/api/v1/share/settings').json()
        assert settings['share_scan_minutes']==10
        legacy=client.post('/api/v1/share/settings',json={'auto_organize':True}).json()
        assert legacy['share_scan_minutes']==10
        changed=client.post('/api/v1/share/settings',json={'auto_organize':False,'share_scan_minutes':0}).json()
        assert changed['share_scan_minutes']==0
        events=[]
        monkeypatch.setattr(share_service,'generate',lambda *a,**k:{'created':['one'],'skipped':[]})
        monkeypatch.setattr(share_service,'enqueue_pending_organize',lambda *a,**k:events.append(k['origin']) or 42)
        payload={'share_code':'abc','receive_code':'','files':[{'id':'1','name':'movie.mkv','pickcode':'p','size':1}]}
        response=client.post('/api/v1/share/generate',json=payload)
        assert response.status_code==200,response.text
        assert response.json()['job_id'] is None and events==[]
        client.post('/api/v1/share/settings',json={'auto_organize':True,'share_scan_minutes':0})
        response=client.post('/api/v1/share/generate',json=payload)
        assert response.status_code==200,response.text
        assert response.json()['job_id']==42 and events==['web']
        def fail(*args,**kwargs): raise RuntimeError('queue unavailable')
        monkeypatch.setattr(share_service,'enqueue_pending_organize',fail)
        response=client.post('/api/v1/share/generate',json=payload)
        assert response.status_code==200 and response.json()['count']==1
        assert '已生成' in response.json()['organize_error']


def test_channel_share_obeys_auto_switch(tmp_path,monkeypatch):
    async def run():
        store,monitor,_,_=make(tmp_path,{}, {'enabled':True,'notify':False,'channels':[]})
        async def videos(*args,**kwargs): return [{'id':'1'}]
        monkeypatch.setattr(share_service,'list_videos',videos)
        monkeypatch.setattr(share_service,'generate',lambda *a,**k:{'created':['one'],'skipped':[]})
        events=[]
        monkeypatch.setattr(share_service,'enqueue_pending_organize',lambda *a,**k:events.append(k['origin']) or 42)
        store.set_many({'share_settings':{'auto_organize':False}})
        await monitor._strm('testchan','',{'id':1}, {}, [{'share_code':'abc','receive_code':''}])
        assert events==[]
        store.set_many({'share_settings':{'auto_organize':True}})
        await monitor._strm('testchan','',{'id':2}, {}, [{'share_code':'def','receive_code':''}])
        assert events==['channel']
        store.close()
    asyncio.run(run())


def test_bot_share_generates_only_when_auto_disabled(tmp_path,monkeypatch):
    async def run():
        store=Store(tmp_path/'data','admin','test-password-1234')
        store.set_many({'share_settings':{'auto_organize':False}})
        captured=[]; notes=Notices()
        monkeypatch.setattr(share_service,'enqueue_organize',lambda *a,**k:captured.append(k) or 42)
        bot=TelegramBot(SimpleNamespace(store=store,http=None,notifications=notes))
        await bot.handle(update(1,'/share https://115.com/s/abc'),{'allowed_user_id':'42'},'bot1')
        assert captured[0]['generate_only'] is True
        assert '自动整理已关闭' in notes.items[-1][0][2]
        store.close()
    asyncio.run(run())


def test_generate_only_persists_pending_without_starting_organize(tmp_path):
    from test_phase2_share_pipeline import share_setup
    async def run():
        store,cloud,http,engine,_=share_setup(tmp_path)
        secret={'share_code':'only','receive_code':''}
        video={'id':'501','name':'Example S01E01.mkv','size':5000,'path':'','is_dir':False}
        cloud.share_tree={'only':{'0':[video]}}
        jid=share_service.enqueue_organize(engine,origin='telegram',secret=secret,generate_only=True)
        await engine.run_job(jid)
        job=engine.job(jid)
        assert job['state']=='completed' and job['stage']=='分享 STRM 已生成'
        assert share_service.pending_count(store)==1
        assert store.db.execute('SELECT count(*) FROM organize_items WHERE job_id=?',(jid,)).fetchone()[0]==0
        await http.aclose();store.close()
    asyncio.run(run())


def test_new_pending_request_coalesces_waiting_but_not_running_job(tmp_path):
    from test_phase2_share_pipeline import share_setup
    async def run():
        store,cloud,http,engine,_=share_setup(tmp_path)
        first=share_service.enqueue_pending_organize(engine,origin='channel')
        assert share_service.enqueue_pending_organize(engine,origin='web')==first
        store.db.execute("UPDATE organize_jobs SET state='running' WHERE id=?",(first,))
        second=share_service.enqueue_pending_organize(engine,origin='channel')
        assert second!=first
        assert share_service.enqueue_pending_organize(engine,origin='web')==second
        await http.aclose();store.close()
    asyncio.run(run())


def test_offline_grace_starts_when_finished_first_seen():
    async def run():
        conn=make_db(age=3600); post=engine_on(conn)
        OfflinePostProcess.__init__(post,post)
        digest='a'*40
        conn.execute('UPDATE offline_submissions SET urls=? WHERE id=1',(json.dumps(['magnet:?xt=urn:btih:'+digest]),))
        class Cloud:
            async def offline_tasks(self,*args): return ({'tasks':[{'info_hash':digest,'percentDone':100}]},None)
        post.cloud=Cloud(); post.notifications=FakeNotifications()
        await post.check(conn.execute('SELECT * FROM offline_submissions WHERE id=1').fetchone())
        row=conn.execute('SELECT * FROM offline_submissions WHERE id=1').fetchone()
        assert row['state']=='submitted' and row['finished_seen_at']>0
        conn.execute('UPDATE offline_submissions SET finished_seen_at=? WHERE id=1',(time.time()-301,))
        await post.check(conn.execute('SELECT * FROM offline_submissions WHERE id=1').fetchone())
        assert conn.execute('SELECT state FROM offline_submissions WHERE id=1').fetchone()[0]=='completed'
        conn.close()
    asyncio.run(run())


def test_play_reservations_are_spaced_under_concurrency(tmp_path):
    async def run():
        store,cloud,clock,_=make_cloud(tmp_path)
        waits=[]
        async def sleep(seconds): waits.append(seconds)
        cloud.sleep=sleep
        await asyncio.gather(*[cloud.reserve_play_slot() for _ in range(4)])
        assert waits==pytest.approx([.05,.10,.15])
        await cloud.http.aclose();await cloud.play_http.aclose();store.close()
    asyncio.run(run())


def test_batch_gap_slows_after_limit_and_numeric_code_is_recognized(tmp_path):
    async def run():
        store,cloud,clock,_=make_cloud(tmp_path)
        cloud.throttle_scale=2
        await cloud.call(accepted,_kind='write',_batch=10)
        assert cloud.next_request-clock()==pytest.approx(2)
        async def limited(*args,**kwargs): return {'state':0,'errno':911}
        with pytest.raises(CloudError) as exc: await cloud.call(limited)
        assert exc.value.code=='RATE_LIMITED'
        await cloud.http.aclose();await cloud.play_http.aclose();store.close()
    asyncio.run(run())
