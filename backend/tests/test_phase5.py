import asyncio
import copy
import hashlib
import json
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from strimkeep.cloud115 import CloudError
from strimkeep.main import create_app
from strimkeep.media import classify
from strimkeep.organizer import dumps
from test_organize import CONFIG, MEDIA, MOVIE, TV, MemoryCloud, setup, new_job
from test_upgrade import upgraded_setup, policy

@pytest.mark.parametrize('country,region',[('CN','国产'),('HK','国产'),('TW','国产'),('JP','日韩'),('KR','日韩'),('US','欧美'),('FR','欧美'),('IN','其他')])
def test_animation_film_is_regional_movie(country,region):
    details={**MOVIE,'origin_country':[country],'genres':[{'id':16}]}
    assert classify(details,'movie')==('电影',region)
    assert classify(details,'tv')==('动漫',region)

@pytest.mark.parametrize('country,region', [('US','欧美'), ('IN','其他')])
def test_normal_organize_still_archives_animation_movie_in_regional_film_folder(tmp_path, country, region):
    async def scenario():
        store, cloud, http, engine, _ = setup(tmp_path)
        cloud.add_video(name='测试电影 (2024).mkv')
        identify = engine.identify
        async def animated(hint):
            details = await identify(hint)
            return {**details, 'origin_country':[country], 'genres':[{'id':16}]}
        engine.identify = animated
        jid = new_job(store, {**CONFIG, 'account_id':'1'})
        await engine.run_job(jid)
        assert engine.job(jid)['state'] == 'completed', dict(engine.job(jid))
        registered = engine.outputs.get('1','1000')
        path = engine.root / registered['relative_path']
        assert path.parts[-4:-2] == ('电影', region+'电影')
        text = path.read_text()
        assert path.is_file() and text.startswith('https://play.example/play115/')
        # 身份段来自云端条目本身：pickcode/fid/sha1/size 都在地址里
        assert '/play115/pc1000/1000/hash1000/1000/' in text
        parent = cloud.nodes['1000']['parent_id']
        assert cloud.nodes[cloud.nodes[parent]['parent_id']]['name'] == region+'电影'
        regional = cloud.nodes[parent]['parent_id']
        assert cloud.nodes[cloud.nodes[regional]['parent_id']]['name'] == '电影'
        await http.aclose()
        store.close()
    asyncio.run(scenario())

def sync_config(roots=None,base='https://play.example'):
    # 手动同步默认已改为巡检；这里显式要全量，因为下面这些用例测的就是全量路径。
    return {'job_kind':'strm_sync','source_cids':roots or ['100'],'archive_cid':'200','account_id':'1','playback_base':base,'notify_emby':True,'full_scan':True}

def test_sync_reuses_token_changes_base_rename_move_subtitles_and_no_change_notification(tmp_path):
    async def scenario():
        store,cloud,http,engine,requests=setup(tmp_path)
        source=cloud.add_video(name='Movie.2024.1080p.mkv');source['sha1']='A'*40
        cloud.add_video('1001','Movie.2024.1080p.chs.forced.srt')['size']=50
        jid=new_job(store,sync_config());await engine.run_job(jid); await engine.emby.drain_outbox(); await engine.emby.tick()
        assert engine.job(jid)['state']=='completed',dict(engine.job(jid))
        registry=engine.outputs.get('1','1000');pid=registry['playback_id']
        first=Path(engine.root/registry['relative_path']);oldurl=first.read_text()
        assert first.with_suffix('.chs.forced.srt').is_file()
        assert not cloud.writes
        assert len([r for r in requests if r.url.host=='emby' and r.url.path.endswith('/Library/Media/Updated')])==1
        jid=new_job(store,sync_config());await engine.run_job(jid); await engine.emby.drain_outbox(); await engine.emby.tick()
        assert engine.job(jid)['state']=='completed'
        assert len([r for r in requests if r.url.host=='emby' and r.url.path.endswith('/Library/Media/Updated')])==1
        cloud.nodes['1000']['name']='New.2160p.mkv';cloud.nodes['1001']['name']='New.2160p.chs.forced.srt'
        folder=(await cloud.mkdir('100','inside'))['cid']
        cloud.nodes['1000']['parent_id']=folder;cloud.nodes['1001']['parent_id']=folder
        writes=cloud.writes.copy()
        jid=new_job(store,sync_config(base='https://new.example'));await engine.run_job(jid); await engine.emby.drain_outbox(); await engine.emby.tick()
        assert engine.job(jid)['state']=='completed',dict(engine.job(jid))
        current=engine.outputs.get('1','1000')
        assert current['playback_id']==pid and not first.exists() and not first.with_suffix('.chs.forced.srt').exists()
        text=(engine.root/current['relative_path']).read_text()
        from strimkeep.playaddr import identity_of, play115_url
        # 换基址只换前缀、改名只换名称段；播放映射（pid）不再进 STRM，
        # 但台账里仍然复用了同一个 pid，所以上面那条断言依然成立。
        node = cloud.nodes['1000']
        expected = play115_url('https://new.example', *identity_of(node), node['name'])
        assert identity_of(node) and text == expected and cloud.writes == writes
        await http.aclose();store.close()
    asyncio.run(scenario())

@pytest.mark.parametrize('case',['delete','outside','listing_failure','auth_failure','modified_local','new_owner'])
def test_sync_cleanup_is_scoped_and_never_guesses_absence(tmp_path,case):
    async def scenario():
        store,cloud,http,engine,_=setup(tmp_path);cloud.add_video()
        jid=new_job(store,sync_config());await engine.run_job(jid)
        registered=engine.outputs.get('1','1000');path=engine.root/registered['relative_path']
        if case=='outside':cloud.nodes['1000']['parent_id']='300'
        else:cloud.nodes.pop('1000')
        if case in ('listing_failure','auth_failure'):
            async def fail(*args):raise CloudError('模拟读取失败','LOGIN_INVALID' if case=='auth_failure' else 'INCOMPLETE_LIST')
            cloud.children=fail
        if case=='modified_local':path.write_text('user edit')
        if case=='new_owner':
            with store.db:store.db.execute('UPDATE owned_outputs SET file_id=? WHERE relative_path=?',('other',registered['relative_path']))
        jid=new_job(store,sync_config());await engine.run_job(jid)
        if case in ('delete','outside'):assert not path.exists() and engine.job(jid)['state']=='completed'
        else:assert path.exists()
        if case=='modified_local':
            assert path.read_text()=='user edit'
            assert engine.job(jid)['state']=='partial'
            assert engine.outputs.get('1','1000')['active']
        if case=='outside':assert engine.job(jid)['state']=='completed' and not engine.outputs.get('1','1000')['active']
        if case.endswith('failure'):assert not engine.job(jid)['manifest']
        if case in ('delete','outside'):
            assert store.db.execute('SELECT 1 FROM strm_changes WHERE job_id=? AND action=? AND path=?',(jid,'removed',registered['relative_path'])).fetchone()
            await engine.notifications.media_report(engine.job(jid))
            notices = store.db.execute("SELECT message FROM notifications WHERE job_id=? AND event_key LIKE 'strm:%:files:removed:%'",(jid,)).fetchall()
            assert any(path.name in row['message'] for row in notices)
        assert not cloud.writes
        await http.aclose();store.close()
    asyncio.run(scenario())

def test_sync_does_not_revive_revoked_or_changed_token(tmp_path):
    async def scenario():
        store,cloud,http,engine,_=setup(tmp_path);cloud.add_video()
        jid=new_job(store,sync_config());await engine.run_job(jid)
        reg=engine.outputs.get('1','1000');path=engine.root/reg['relative_path'];before=path.read_bytes()
        with store.db:store.db.execute('UPDATE playbacks SET revoked=1 WHERE id=?',(reg['playback_id'],))
        jid=new_job(store,sync_config(base='https://new.example'));await engine.run_job(jid)
        assert path.read_bytes()==before
        assert store.db.execute('SELECT revoked FROM playbacks WHERE id=?',(reg['playback_id'],)).fetchone()[0]==1
        assert store.db.execute('SELECT state FROM organize_items WHERE job_id=?',(jid,)).fetchone()[0]=='skipped'
        await http.aclose();store.close()
    asyncio.run(scenario())


def test_interrupted_strm_unlink_is_reported_as_unconfirmed(tmp_path, monkeypatch):
    async def scenario():
        store,cloud,http,engine,_=setup(tmp_path)
        jid=new_job(store,sync_config())
        rel='影视媒体库/测试/one.strm';path=engine.root/rel
        path.parent.mkdir(parents=True,exist_ok=True);path.write_text('sample')
        unlink=Path.unlink
        def interrupted(self,*args,**kwargs):
            result=unlink(self,*args,**kwargs)
            if self==path:raise RuntimeError('interrupted after unlink')
            return result
        monkeypatch.setattr(Path,'unlink',interrupted)
        with pytest.raises(RuntimeError):engine.outputs.unlink_recorded(jid,rel,path)
        monkeypatch.setattr(Path,'unlink',unlink)
        assert not path.exists()
        assert store.db.execute('SELECT 1 FROM strm_changes WHERE job_id=? AND action=? AND path=?',(jid,'remove_pending',rel)).fetchone()
        assert not store.db.execute('SELECT 1 FROM strm_changes WHERE job_id=? AND action=? AND path=?',(jid,'removed',rel)).fetchone()
        await engine.notifications.media_report(engine.job(jid))
        msg=store.db.execute("SELECT message FROM notifications WHERE job_id=? AND event_key LIKE 'strm:%:files:pending:%'",(jid,)).fetchone()
        assert msg and '待核实' in msg['message'] and 'one.strm' in msg['message']
        await http.aclose();store.close()
    asyncio.run(scenario())

def test_sync_manifest_crash_then_pause_after_publish_recovers_same_token(tmp_path):
    async def scenario():
        store,cloud,http,engine,_=setup(tmp_path);cloud.add_video()
        jid=new_job(store,sync_config());original=engine.outputs.remember
        def pause(*args):engine.update_job(jid,state='paused');return original(*args)
        engine.outputs.remember=pause;await engine.run_job(jid)
        assert engine.job(jid)['state']=='paused'
        reg=engine.outputs.get('1','1000');before=(engine.root/reg['relative_path']).read_bytes()
        engine.outputs.remember=original;engine.update_job(jid,state='queued');await engine.run_job(jid)
        assert engine.job(jid)['state']=='completed' and (engine.root/reg['relative_path']).read_bytes()==before
        assert store.db.execute('SELECT count(*) FROM playbacks').fetchone()[0]==1
        await http.aclose();store.close()
    asyncio.run(scenario())

@pytest.mark.parametrize('change',['none','sha','size','account','missing_field'])
def test_media_cache_uses_content_and_account_and_required_fields(tmp_path,change):
    async def scenario():
        store,cloud,http,engine,_=setup(tmp_path);source=cloud.add_video(name='Movie.2024.WEB-DL-GROUP.mkv');source['sha1']='A'*40
        jid=new_job(store,{**CONFIG,'account_id':'1'});engine.update_job(jid,state='running')
        first,raw,origin=await engine.measurements.measure(jid,source);assert origin=='probed'
        count=0
        async def probe(*args):
            nonlocal count
            count+=1;return MEDIA.copy()
        engine.probe=probe
        if change=='sha':source['sha1']='B'*40
        if change=='size':source['size']+=1
        if change=='account':store.set_many({'cookie':'UID=2_A1; CID=x; SEID=x'});engine.update_job(jid,config=dumps({**CONFIG,'account_id':'2'}))
        if change=='missing_field':
            with store.db:store.db.execute('UPDATE media_cache SET media=?',(dumps({**MEDIA,'fps':None}),))
        result,name,origin=await engine.measurements.measure(jid,source,policy=policy('fps'))
        assert count==(0 if change=='none' else 1) and name==source['name'] and result['width']==3840
        await http.aclose();store.close()
    asyncio.run(scenario())

def test_repeated_organize_uses_local_snapshot_and_does_not_walk_unrelated_archive(tmp_path):
    async def scenario():
        store,cloud,http,engine,_=setup(tmp_path);cloud.add_video()
        jid=new_job(store);await engine.run_job(jid);snapshot=engine.index.latest('1','200')['id']
        unrelated=(await cloud.mkdir('200','unrelated'))['cid'];cloud.add_video('2000','测试电影 (2024).mkv')
        read=[];children=cloud.children
        async def record(cid):read.append(cid);return await children(cid)
        cloud.children=record;jid=new_job(store);await engine.run_job(jid)
        assert engine.job(jid)['state']=='completed',dict(engine.job(jid))
        assert engine.index.latest('1','200')['id']==snapshot and unrelated not in read
        await http.aclose();store.close()
    asyncio.run(scenario())

def test_timers_fifo_coalescing_restart_and_separate_sync_scope(tmp_path):
    store,cloud,http,engine,_=setup(tmp_path)
    store.set_many({'organize_config':CONFIG,'sync_config':{'source_cids':['100'],'notify_emby':True},'schedule_config':{'scan_enabled':True,'scan_interval_minutes':5,'sync_enabled':True,'sync_interval_minutes':5,'sync_source_cids':['200']}})
    engine.tasks.tick(now=1000);assert not store.db.execute('SELECT 1 FROM organize_jobs').fetchone()
    engine.tasks.tick(now=1300);jobs=store.db.execute('SELECT * FROM organize_jobs ORDER BY id').fetchall()
    assert len(jobs)==2 and json.loads(jobs[0]['config'])['job_kind']=='organize' and json.loads(jobs[1]['config'])['source_cids']==['200']
    engine.tasks.tick(now=10000);assert store.db.execute('SELECT count(*) FROM organize_jobs').fetchone()[0]==2
    assert dict(store.db.execute('SELECT kind,merged FROM task_schedules')) == {'organize': 0, 'strm_sync': 1}
    engine.update_job(jobs[0]['id'],state='running');engine.tasks.tick(now=10300)
    assert store.db.execute('SELECT count(*) FROM organize_jobs').fetchone()[0]==2
    assert engine.tasks.enqueue(engine.tasks.config('strm_sync'))[0]>jobs[1]['id']
    asyncio.run(http.aclose());store.close()

def test_station_and_telegram_notification_dedup_and_ambiguous_send_not_retried(tmp_path):
    async def scenario():
        store,cloud,http,engine,_=setup(tmp_path)
        store.set_many({'notification_config':{'telegram_enabled':True,'bot_token':'12345:'+('x'*30),'chat_id':'12345'}})
        calls=[]
        async def handler(request):calls.append(request);raise httpx.ReadTimeout('lost response')
        outbound=httpx.AsyncClient(transport=httpx.MockTransport(handler));engine.notifications.http=outbound
        assert await engine.notifications.publish('same','title','safe message')=='unknown'
        assert await engine.notifications.publish('same','title','safe message')=='duplicate'
        assert len(calls)==1 and store.db.execute('SELECT telegram_state FROM notifications').fetchone()[0]=='unknown'
        assert 'bot_token' not in store.db.execute('SELECT message FROM notifications').fetchone()[0]
        await outbound.aclose();await http.aclose();store.close()
    asyncio.run(scenario())

@pytest.mark.parametrize('manifest', [0, 1])
def test_retired_classification_job_never_reads_or_modifies_cloud(tmp_path, manifest):
    async def scenario():
        store, cloud, http, engine, requests = setup(tmp_path)
        source = cloud.add_video(name='测试电影 (2024).mkv')
        jid = new_job(store, {**CONFIG, 'job_kind': 'classification_migration'})
        source_copy = copy.deepcopy(cloud.nodes)
        with store.db:
            store.db.execute('UPDATE organize_jobs SET manifest=? WHERE id=?', (manifest, jid))
            if manifest:
                store.db.execute('INSERT INTO organize_items(job_id,file_id,original,plan) VALUES(?,?,?,?)',
                    (jid, source['id'], dumps(source), dumps({'dest':'200','target_name':'旧名称.mkv'})))
        before = [dict(r) for r in store.db.execute('SELECT * FROM organize_items WHERE job_id=?', (jid,))]
        async def unexpected(*args):
            raise AssertionError('retired migration must not call cloud')
        cloud.status = cloud.file = cloud.children = cloud.directory = unexpected
        await engine.run_job(jid)
        assert engine.job(jid)['state'] == 'cancelled'
        assert engine.job(jid)['manifest'] == manifest
        assert '已移除' in engine.job(jid)['message']
        # 0.8.19 起，任务进终态会收口「还没开始处理」的条目（见 Store.settle_terminal_job）：
        # 退休任务被取消时，它未处理的条目从 discovered 变成 skipped 并写清原因。
        # 仍然要求：条目的其余字段（plan/original/file_id）一字不动，且云端零改动。
        expected = [dict(row) for row in before]
        for row in expected:
            if row['state'] in ('discovered', 'planned'):
                row['state'] = 'skipped'
                row['step'] = row['message'] = '任务结束未处理'
        assert expected == [dict(r) for r in store.db.execute('SELECT * FROM organize_items WHERE job_id=?', (jid,))]
        assert cloud.nodes == source_copy and not cloud.writes and not requests
        assert not list(engine.root.rglob('*.strm'))
        with pytest.raises(ValueError, match='任务类型无效'):
            engine.tasks.config('classification_migration')
        await http.aclose()
        store.close()
    asyncio.run(scenario())


def test_phase5_admin_api_queue_scheduler_notification_mask_and_csrf(tmp_path,monkeypatch):
    monkeypatch.setenv('ADMIN_USERNAME','admin');monkeypatch.setenv('ADMIN_PASSWORD','test-password-1234')
    app=create_app(tmp_path/'data',tmp_path/'strm',MemoryCloud)
    with TestClient(app) as client:
        assert client.get('/api/v1/tasks').status_code==401
        csrf=client.post('/api/v1/auth/login',json={'username':'admin','password':'test-password-1234'}).json()['csrf']
        assert client.put('/api/v1/sync/config',json={'source_cids':['100']}).status_code==403
        client.headers['X-CSRF-Token']=csrf
        store=app.state.store
        store.set_many({'cookie':'UID=1_A1; CID=x; SEID=x','tmdb_key':'private-key','playback_base':'https://play.example','organize_config':CONFIG})
        app.state.organizer.closing=True
        assert client.put('/api/v1/sync/config',json={'source_cids':['0']}).status_code==422
        assert client.put('/api/v1/sync/config',json={'source_cids':['100']}).status_code==200
        first=client.post('/api/v1/sync/jobs').json();second=client.post('/api/v1/organize/jobs').json();third=client.post('/api/v1/archive/jobs').json()
        assert client.post('/api/v1/archive/classification/jobs').status_code in (404, 405)
        active=client.get('/api/v1/tasks').json()['active']
        assert [j['id'] for j in active]==[first['id'],second['id'],third['id']]
        assert client.post(f'/api/v1/organize/jobs/{third["id"]}/cancel').json()['state']=='cancelled'
        body={'scan_enabled':True,'sync_enabled':True,'sync_source_cids':['200'],'scan_interval_minutes':5,'sync_interval_minutes':10}
        assert client.put('/api/v1/system/schedules',json=body).status_code==200
        assert client.get('/api/v1/system/schedules').json()['config']['sync_source_cids']==['200']
        secret='12345:'+('x'*30)
        assert client.put('/api/v1/notifications/config',json={'telegram_enabled':True,'bot_token':secret,'chat_id':'1234'}).status_code==200
        assert secret not in client.get('/api/v1/notifications/config').text
        assert secret.encode() not in store.path.read_bytes()
        assert 'account_id' not in client.get('/api/v1/tasks').text and 'token_cipher' not in client.get('/api/v1/tasks').text
        assert client.post('/api/v1/notifications/read').status_code==200


def test_real_worker_fifo_scheduler_enqueues_while_active_and_cancelled_queue_skipped(tmp_path):
    async def scenario():
        store,cloud,http,engine,_=setup(tmp_path)
        store.set_many({'organize_config':CONFIG,'sync_config':{'source_cids':['100']},'schedule_config':{'scan_enabled':False,'sync_enabled':True,'sync_interval_minutes':5,'sync_source_cids':['200']}})
        engine.tasks.tick(now=time.time()-400)
        order=[];started=asyncio.Event();release=asyncio.Event()
        async def process(jid):
            engine.update_job(jid,state='running');order.append(jid)
            if len(order)==1:started.set();await release.wait()
            engine.update_job(jid,state='completed',message='controlled worker')
        engine.run_job=process
        first=new_job(store,sync_config());cancelled=new_job(store,sync_config());engine.update_job(cancelled,state='cancelled')
        engine.start();await asyncio.wait_for(started.wait(),2)
        for _ in range(30):
            if store.db.execute('SELECT count(*) FROM organize_jobs').fetchone()[0]>=3:break
            await asyncio.sleep(.05)
        assert store.db.execute('SELECT count(*) FROM organize_jobs').fetchone()[0]>=3
        release.set()
        for _ in range(60):
            if len(order)>=2:break
            await asyncio.sleep(.05)
        assert order[0]==first and cancelled not in order
        await engine.close();await http.aclose();store.close()
    asyncio.run(scenario())


def test_subtitle_prefix_ambiguity_is_not_assigned_to_two_videos():
    from strimkeep.media import bind_subtitles
    files=[{'id':'1','name':'Film.mkv','parent_id':'100'},{'id':'2','name':'Film.chs.mkv','parent_id':'100'},{'id':'3','name':'Film.chs.srt','parent_id':'100'}]
    videos=bind_subtitles(files)
    assert all(not v['subtitles'] and v['subtitle_warning'] for v in videos)


def test_sync_rejects_unknown_same_named_local_file_and_retains_nfo(tmp_path):
    async def scenario():
        store,cloud,http,engine,_=setup(tmp_path);cloud.add_video(name='Movie.mkv')
        path=engine.root/'StrimKeep同步'/'待整理-100'/'Movie.strm';path.parent.mkdir(parents=True)
        path.write_text('user content');(path.parent/'movie.nfo').write_text('NFO')
        jid=new_job(store,sync_config());await engine.run_job(jid)
        assert engine.job(jid)['state']=='partial' and path.read_text()=='user content'
        assert (path.parent/'movie.nfo').read_text()=='NFO' and not cloud.writes
        await http.aclose();store.close()
    asyncio.run(scenario())


def test_sync_same_stem_different_extensions_never_overwrite_each_other(tmp_path):
    async def scenario():
        store,cloud,http,engine,_=setup(tmp_path);cloud.add_video(name='Movie.mkv');cloud.add_video('1001',name='Movie.mp4')
        jid=new_job(store,sync_config());await engine.run_job(jid)
        assert engine.job(jid)['state']=='partial'
        assert len(list(engine.root.rglob('*.strm')))==1
        assert engine.outputs.get('1','1000') and not engine.outputs.get('1','1001')
        assert not cloud.writes
        await http.aclose();store.close()
    asyncio.run(scenario())


def test_first_sync_imports_legacy_owned_output_and_cleans_confirmed_missing_video(tmp_path):
    async def scenario():
        store,cloud,http,engine,_=setup(tmp_path);cloud.add_video()
        jid=new_job(store,{**CONFIG,'account_id':'1'});await engine.run_job(jid)
        path=engine.root/engine.outputs.get('1','1000')['relative_path']
        with store.db:
            store.db.execute('DELETE FROM output_registry');store.db.execute('DELETE FROM owned_outputs')
        cloud.nodes.pop('1000')
        sync=new_job(store,sync_config(['200']));await engine.run_job(sync)
        assert engine.job(sync)['state']=='completed',dict(engine.job(sync))
        assert not path.exists()
        await http.aclose();store.close()
    asyncio.run(scenario())


def test_sync_adds_new_subtitle_to_unchanged_video(tmp_path):
    async def scenario():
        store, cloud, http, engine, _ = setup(tmp_path)
        cloud.add_video(name='Movie.2024.mkv')['sha1'] = 'A' * 40
        first = new_job(store, sync_config()); await engine.run_job(first)
        registry = engine.outputs.get('1', '1000')
        path = engine.root / registry['relative_path']
        sub = cloud.add_video('1001', 'Movie.2024.chs.srt')
        sub['sha1'], sub['size'] = 'B' * 40, 50
        second = new_job(store, sync_config()); await engine.run_job(second)
        assert engine.job(second)['state'] == 'completed'
        assert path.with_suffix('.chs.srt').is_file()
        await http.aclose(); store.close()
    asyncio.run(scenario())
