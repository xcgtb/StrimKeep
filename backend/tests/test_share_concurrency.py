import asyncio
import time
import httpx
import pytest

from strimkeep import share_service
from strimkeep.cloud115 import Cloud115, CloudError
from strimkeep.store import Store
from test_phase2_share_pipeline import share_setup


def test_concurrent_share_checks_respect_shared_start_spacing(tmp_path):
    async def run():
        store=Store(tmp_path/'data','admin','test-password-1234')
        cloud=Cloud115(store)
        cloud.interval=lambda kind: 0.05
        starts=[]; active=0; peak=0
        async def request(**kwargs):
            nonlocal active,peak
            starts.append(time.monotonic());active+=1;peak=max(peak,active)
            await asyncio.sleep(.18)
            active-=1
            return {'state':True,'data':{'list':[]}}
        await asyncio.gather(*(cloud.call(request,_scope='share',_share_check=True) for _ in range(3)))
        assert peak>=2
        assert all(b-a>=.04 for a,b in zip(starts,starts[1:])), starts
        await cloud.close();store.close()
    asyncio.run(run())


def test_rate_limit_stops_waiting_share_starts(tmp_path):
    async def run():
        store=Store(tmp_path/'data','admin','test-password-1234')
        cloud=Cloud115(store);cloud.interval=lambda kind:0.1
        starts=[]
        async def limited(**kwargs):
            starts.append(time.monotonic())
            await asyncio.sleep(.01)
            return {'state':False,'errno':911,'error':'操作频繁'}
        results=await asyncio.gather(*(cloud.call(limited,_scope='share',_share_check=True) for _ in range(3)),return_exceptions=True)
        assert len(starts)==1
        assert all(isinstance(result,CloudError) and result.code=='RATE_LIMITED' for result in results)
        await cloud.close();store.close()
    asyncio.run(run())


def test_share_workers_are_bounded_and_coalesce_same_share(tmp_path):
    async def run():
        store,cloud,http,engine,_=share_setup(tmp_path)
        for share in ('a','b','c'):
            share_service.generate(store,engine.root,{'share_code':share,'receive_code':''},
                [{'id':share+str(i),'name':share+str(i)+'.mkv','size':5000,'path':'Work/'} for i in range(2)])
        active=0;peak=0;calls=[];started=asyncio.Event()
        async def status(*args):
            nonlocal active,peak
            calls.append(args);active+=1;peak=max(peak,active)
            if active==3:started.set()
            await asyncio.wait_for(started.wait(),1)
            await asyncio.sleep(.01)
            active-=1
            return 'accessible'
        cloud.share_availability=status
        result=await share_service.check_alive(engine)
        report=store.meta_json('share_check_history')[0]
        assert result['alive']==6 and result['share_checks']==3 and len(calls)==3 and peak==3
        assert report['concurrency']==3 and engine._share_check_progress['checked_shares']==3
        await http.aclose();store.close()
    asyncio.run(run())


def test_share_workers_respect_single_concurrency_setting(tmp_path):
    async def run():
        store,cloud,http,engine,_=share_setup(tmp_path)
        store.set_many({'share_settings':{'share_check_concurrency':1}})
        for share in ('a','b','c'):
            share_service.generate(store,engine.root,{'share_code':share,'receive_code':''},
                [{'id':share,'name':share+'.mkv','size':5000,'path':'Work/'}])
        active=0;peak=0
        async def status(*args):
            nonlocal active,peak
            active+=1;peak=max(peak,active)
            await asyncio.sleep(.01)
            active-=1
            return 'accessible'
        cloud.share_availability=status
        result=await share_service.check_alive(engine)
        assert result['share_checks']==3 and peak==1
        assert store.meta_json('share_check_history')[0]['concurrency']==1
        await http.aclose();store.close()
    asyncio.run(run())


def test_concurrency_settings_preserve_old_clients_and_validate(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from strimkeep.main import create_app
    monkeypatch.setenv('ADMIN_USERNAME','admin');monkeypatch.setenv('ADMIN_PASSWORD','test-password-1234')
    app=create_app(tmp_path/'data',tmp_path/'strm')
    with TestClient(app) as client:
        login=client.post('/api/v1/auth/login',json={'username':'admin','password':'test-password-1234'})
        client.headers['X-CSRF-Token']=login.json()['csrf']
        assert client.get('/api/v1/share/settings').json()['share_check_concurrency']==3
        assert client.post('/api/v1/share/settings',json={'share_check_concurrency':4}).json()['share_check_concurrency']==4
        assert client.post('/api/v1/share/settings',json={'share_check_hours':24}).json()['share_check_concurrency']==4
        assert client.post('/api/v1/share/settings',json={'share_check_concurrency':5}).status_code==422
        assert client.post('/api/v1/share/settings',json={'share_check_concurrency':0}).status_code==422


def test_completed_shares_are_processed_while_other_requests_wait(tmp_path):
    async def run():
        store,cloud,http,engine,_=share_setup(tmp_path)
        for share in ('fast','slow'):
            share_service.generate(store,engine.root,{'share_code':share,'receive_code':''},
                [{'id':share,'name':share+'.mkv','size':5000,'path':'Work/'}])
        waiting=asyncio.Event(); release=asyncio.Event()
        async def status(share,*args):
            if share=='slow':
                waiting.set();await release.wait()
                return 'accessible'
            return 'cancelled'
        cloud.share_availability=status
        task=asyncio.create_task(share_service.check_alive(engine))
        await asyncio.wait_for(waiting.wait(),1)
        for _ in range(100):
            if store.db.execute('SELECT count(*) FROM share_check_candidates').fetchone()[0]:break
            await asyncio.sleep(.01)
        assert store.db.execute('SELECT count(*) FROM share_check_candidates').fetchone()[0]==1
        assert not task.done()
        release.set();result=await task
        assert result['alive']==1 and result['skipped']==1 and result['share_checks']==2
        await http.aclose();store.close()
    asyncio.run(run())


def test_share_and_other_background_requests_share_the_same_spacing(tmp_path):
    async def run():
        store=Store(tmp_path/'data','admin','test-password-1234')
        cloud=Cloud115(store);cloud.interval=lambda kind:.05
        started=asyncio.Event();release=asyncio.Event();times={}
        async def slow_share(**kwargs):
            times['share']=time.monotonic();started.set();await release.wait()
            return {'state':True,'data':{'list':[]}}
        async def normal_read(**kwargs):
            times['normal']=time.monotonic();return {'state':True}
        first=asyncio.create_task(cloud.call(slow_share,_scope='share',_share_check=True))
        await started.wait()
        await cloud.call(normal_read)
        assert not first.done()
        assert times['normal']-times['share'] >= .04
        release.set();await first
        await cloud.close();store.close()
    asyncio.run(run())
