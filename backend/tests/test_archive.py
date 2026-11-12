import asyncio
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from strimkeep.cloud115 import CloudError
from strimkeep.main import create_app
from strimkeep.organizer import Organizer
from strimkeep.store import Store
from test_organize import CONFIG, MOVIE, TV, MemoryCloud, good_probe, handler, new_job, setup


async def directory(cloud, parent, name):
    result = await cloud.mkdir(parent, name)
    return str(result['cid'])


def index_job(store, **kwargs):
    return new_job(store, {**CONFIG, 'job_kind': 'archive_index', **kwargs})


def test_index_reads_legacy_folders_and_does_not_modify_or_probe(tmp_path):
    async def scenario():
        store, cloud, http, engine, requests = setup(tmp_path)
        series = await directory(cloud, '200', '测试剧集 (2024) {tmdb-10}')
        season = await directory(cloud, series, 'Season 1')
        cloud.add_video('2010', 'release.S01E03.mkv', season)
        cloud.add_video('2020', '测试电影 (2024).WEB-DL.mp4', '200')
        cloud.add_video('2030', '延后.iso', '200')
        before = {k: dict(v) for k,v in cloud.nodes.items()}; cloud.writes.clear()
        async def forbidden(*args):
            raise AssertionError('Archive indexing must not probe media')
        engine.probe = forbidden
        jid = index_job(store); await engine.run_job(jid)
        assert engine.job(jid)['state'] == 'completed', dict(engine.job(jid))
        snapshot = engine.index.latest('1', '200')
        assert (snapshot['total'], snapshot['resolved'], snapshot['unsupported']) == (3,2,1)
        entries = {r['file_id']: dict(r) for r in store.db.execute('SELECT * FROM archive_entries')}
        assert entries['2010']['tmdb_id'] == 10 and entries['2010']['season'] == 1 and entries['2010']['episode'] == 3
        assert entries['2020']['media_type'] == 'movie'
        assert cloud.nodes == before and cloud.writes == []
        assert not list((tmp_path/'strm').rglob('*'))
        assert not any(r.url.host in ('cdn.example', 'emby', 'image.tmdb.org') for r in requests)
        await http.aclose(); store.close()
    asyncio.run(scenario())


def test_global_index_prevents_duplicate_in_different_legacy_folder(tmp_path):
    async def scenario():
        store, cloud, http, engine, _ = setup(tmp_path)
        parent = await directory(cloud, '200', '以前的分类')
        parent = await directory(cloud, parent, '测试剧集 (2024) {tmdb-10}')
        season = await directory(cloud, parent, 'Season 1')
        old = cloud.add_video('2000', '测试剧集 (2024) S01E03.old.mkv', season).copy()
        first = index_job(store); await engine.run_job(first)
        source = cloud.add_video().copy()
        jid = new_job(store); await engine.run_job(jid)
        assert engine.job(jid)['state'] == 'partial'
        assert cloud.nodes['2000'] == old
        assert cloud.nodes['1000']['name'] == source['name']
        assert cloud.nodes['1000']['parent_id'] == '300'
        item = store.db.execute('SELECT * FROM organize_items').fetchone()
        assert item['state'] == 'pending' and '当前归档目录' in item['message']
        assert not [w for w in cloud.writes if w[0]=='rename']
        await http.aclose(); store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', ['listing', 'login', 'tmdb', 'cancel'])
def test_incomplete_attempt_keeps_complete_snapshot_and_never_deletes(tmp_path, failure):
    async def scenario():
        store, cloud, http, engine, _ = setup(tmp_path)
        cloud.add_video('2000', '测试电影 (2024).mkv', '200')
        first = index_job(store); await engine.run_job(first)
        previous = engine.index.latest('1','200')['id']
        new_folder = await directory(cloud,'200','new')
        cloud.add_video('2001','测试电影 (2024).new.mkv',new_folder)
        cloud.writes.clear()
        second = index_job(store)
        children, tmdb = cloud.children, engine.tmdb
        async def maybe_children(cid):
            if failure in ('listing','login') and cid == new_folder:
                raise CloudError('分页失败。' if failure=='listing' else '登录失效。', 'INCOMPLETE_LIST' if failure=='listing' else 'LOGIN_INVALID',502 if failure=='listing' else 401)
            if failure=='cancel' and cid==new_folder:
                engine.update_job(second,state='cancelled')
            return await children(cid)
        async def maybe_tmdb(*args, **kwargs):
            raise CloudError('TMDB 请求失败。','TMDB_UNAVAILABLE')
        cloud.children = maybe_children
        if failure=='tmdb': engine.tmdb = maybe_tmdb
        await engine.run_job(second)
        assert engine.job(second)['state'] == ('waiting_login' if failure=='login' else 'cancelled' if failure=='cancel' else 'needs_review')
        assert engine.index.latest('1','200')['id']==previous
        assert store.db.execute('SELECT count(*) FROM archive_entries WHERE snapshot_id=?',(previous,)).fetchone()[0]==1
        assert cloud.writes == []
        cloud.children, engine.tmdb = children, tmdb
        if failure != 'cancel':
            engine.update_job(second,state='queued')
            await engine.run_job(second)
            assert engine.job(second)['state']=='completed'
            assert engine.index.latest('1','200')['total']==2
        await http.aclose(); store.close()
    asyncio.run(scenario())


def test_unrelated_unresolved_archive_does_not_block_new_work(tmp_path):
    async def scenario():
        store, cloud, http, engine, _ = setup(tmp_path)
        old=cloud.add_video('2000','不知道是什么.mkv','200').copy()
        cloud.add_video()
        real_tmdb = engine.tmdb
        async def fake(path, params=None):
            if path.endswith('/alternative_titles'): return {'titles':[],'results':[]}
            return await real_tmdb(path,params)
        engine.tmdb = fake
        first = index_job(store); await engine.run_job(first)
        jid = new_job(store); await engine.run_job(jid)
        assert engine.job(jid)['state']=='completed',dict(engine.job(jid))
        assert engine.index.latest('1','200')['unresolved']==1
        assert cloud.nodes['2000']==old
        assert store.db.execute('SELECT state FROM organize_items WHERE job_id=?',(jid,)).fetchone()[0]=='done'
        await http.aclose(); store.close()
    asyncio.run(scenario())


def test_same_episode_in_current_batch_updates_index_and_prevents_second_archive(tmp_path):
    async def scenario():
        store, cloud, http, engine, _ = setup(tmp_path)
        cloud.add_video(); cloud.add_video('1001','测试剧集 (2024) S01E03.1080p.mkv')
        jid=new_job(store); await engine.run_job(jid)
        counts=dict(store.db.execute('SELECT state,count(*) FROM organize_items GROUP BY state').fetchall())
        assert counts=={'done':1,'pending':1}
        snapshot=engine.index.latest('1','200')
        assert snapshot['resolved']==1
        assert store.db.execute('SELECT file_id FROM archive_entries WHERE snapshot_id=?',(snapshot['id'],)).fetchone()[0]=='1000'
        assert len([w for w in cloud.writes if w[0]=='rename'])==1
        await http.aclose(); store.close()
    asyncio.run(scenario())


def test_external_candidate_move_out_of_archive_is_reconciled_automatically(tmp_path):
    async def scenario():
        store, cloud, http, engine, _ = setup(tmp_path)
        old=cloud.add_video('2000','测试剧集 (2024) S01E03.mkv','200')
        first=index_job(store);await engine.run_job(first)
        cloud.add_video()
        async def probe_and_change(*args):
            cloud.nodes['2000']['parent_id']='300'
            return await good_probe()
        engine.probe=probe_and_change
        jid=new_job(store); await engine.run_job(jid)
        assert engine.job(jid)['state']=='completed',dict(engine.job(jid))
        assert cloud.nodes['2000']['parent_id']=='300'
        assert cloud.nodes['1000']['parent_id']!='100'
        await http.aclose(); store.close()
    asyncio.run(scenario())


def test_index_namespaces_and_account_scope_are_independent(tmp_path):
    async def scenario():
        store, cloud, http, engine, _ = setup(tmp_path)
        cloud.add_video('2000','测试剧集 (2024) S01E03.mkv','200')
        movie=await directory(cloud,'200','测试电影 (2024) {tmdb-10}')
        cloud.add_video('2001','测试电影 (2024).mkv',movie)
        real_tmdb=engine.tmdb
        async def serve(path, params=None):
            if path=='movie/10': return {**MOVIE,'id':10}
            return await real_tmdb(path,params)
        engine.tmdb=serve
        jid=index_job(store); await engine.run_job(jid)
        rows=store.db.execute('SELECT media_type,tmdb_id FROM archive_entries ORDER BY file_id').fetchall()
        assert [tuple(r) for r in rows]==[('tv',10),('movie',10)]
        assert engine.index.latest('2','200') is None
        assert engine.index.latest('1','300') is None
        store.set_many({'cookie':'UID=2_A1; CID=x; SEID=x'})
        jid2=index_job(store,account_id='1'); await engine.run_job(jid2)
        assert engine.job(jid2)['state']=='needs_review'
        assert engine.index.latest('2','200') is None
        await http.aclose(); store.close()
    asyncio.run(scenario())


def test_archive_api_background_task_and_no_private_payloads(tmp_path, monkeypatch):
    monkeypatch.setenv('ADMIN_USERNAME','admin'); monkeypatch.setenv('ADMIN_PASSWORD','test-password-1234')
    app=create_app(tmp_path/'data',tmp_path/'strm',MemoryCloud)
    with TestClient(app) as client:
        assert client.get('/api/v1/archive/index').status_code==401
        login=client.post('/api/v1/auth/login',json={'username':'admin','password':'test-password-1234'}).json()
        client.headers['X-CSRF-Token']=login['csrf']
        client.put('/api/v1/settings',json={'cookie':'UID=1_A1; CID=secret-cookie; SEID=secret-cookie','tmdb_key':'secret-tmdb'})
        app.state.organizer.http=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        assert client.put('/api/v1/organize/config',json=CONFIG).status_code==200
        for fid in range(2000,2061):
            app.state.cloud.add_video(str(fid),'测试电影 (2024).'+str(fid)+'.mkv','200')
        response=client.post('/api/v1/archive/jobs');assert response.status_code==200,response.text
        jid=response.json()['id']
        assert client.post('/api/v1/organize/jobs').status_code==409
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            job=client.get(f'/api/v1/organize/jobs/{jid}').json()['job']
            if job['state'] in ('completed','partial','needs_review'): break
            time.sleep(.02)
        assert job['state']=='completed',job
        assert job['kind']=='archive_index' and job['total']==61
        summary=client.get('/api/v1/archive/index');assert summary.json()['snapshot']['resolved']==61
        listing=client.get('/api/v1/archive/entries?limit=50');assert len(listing.json()['items'])==50 and listing.json()['has_more']
        assert len(client.get('/api/v1/archive/entries?offset=50').json()['items'])==11
        assert client.get('/api/v1/archive/entries?state=unresolved').json()['total']==0
        assert client.get('/api/v1/archive/entries?q=20').json()['total']==61
        assert client.get('/api/v1/archive/entries?state=invalid').status_code==422
        for sensitive in ('secret-cookie','secret-tmdb','pickcode','account_id','token_cipher'):
            assert sensitive not in summary.text+listing.text
        assert app.state.cloud.writes==[]
        client.portal.call(app.state.organizer.http.aclose)


def test_interrupted_index_job_recovers_on_worker_restart(tmp_path):
    async def scenario():
        store, cloud, http, engine, _ = setup(tmp_path)
        cloud.add_video('2000','测试电影 (2024).mkv','200')
        initial=index_job(store);await engine.run_job(initial)
        old_snapshot=engine.index.latest('1','200')['id']
        jid=index_job(store)
        children=cloud.children
        async def interrupted(cid):
            raise asyncio.CancelledError()
        cloud.children=interrupted
        with pytest.raises(asyncio.CancelledError):
            await engine.run_job(jid)
        assert engine.job(jid)['state']=='running'
        assert engine.index.latest('1','200')['id']==old_snapshot
        nodes={key:dict(value) for key,value in cloud.nodes.items()}
        await http.aclose();store.close()
        # Same persistent DB/key, new worker, credentials and task identity retained.
        reopened=Store(tmp_path/'data','different-admin','different-password-1234')
        assert reopened.authenticate('admin','test-password-1234')
        new_cloud=MemoryCloud(reopened);new_cloud.nodes=nodes
        new_http=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        restarted=Organizer(reopened,new_cloud,new_http,tmp_path/'strm',good_probe)
        restarted.start()
        deadline=time.monotonic()+5
        while time.monotonic()<deadline and restarted.job(jid)['state'] not in ('completed','partial','needs_review'):
            await asyncio.sleep(.02)
        assert restarted.job(jid)['state']=='completed'
        assert restarted.index.latest('1','200')['id']>old_snapshot
        assert new_cloud.writes==[]
        assert reopened.db.execute('SELECT count(*) FROM organize_jobs').fetchone()[0]==2
        await restarted.close();await new_http.aclose();reopened.close()
    asyncio.run(scenario())


def test_duplicate_ids_across_archive_branches_do_not_publish(tmp_path):
    async def scenario():
        store, cloud, http, engine, _ = setup(tmp_path)
        other=await directory(cloud,'200','other')
        video=cloud.add_video('2000','测试电影 (2024).mkv','200')
        children=cloud.children
        async def repeated(cid):
            return [dict(video)] if cid==other else await children(cid)
        cloud.children=repeated;cloud.writes.clear()
        jid=index_job(store);await engine.run_job(jid)
        assert engine.job(jid)['state']=='needs_review'
        assert engine.index.latest('1','200') is None
        assert cloud.writes==[]
        await http.aclose();store.close()
    asyncio.run(scenario())


def test_deep_archive_tree_is_iterative_and_preserves_cloud_nodes(tmp_path):
    async def scenario():
        store, cloud, http, engine, _ = setup(tmp_path)
        parent='200'
        for number in range(1100):
            parent=await directory(cloud,parent,'folder'+str(number))
        cloud.add_video('9000','测试电影 (2024).mkv',parent)
        cloud.writes.clear()
        jid=index_job(store);await engine.run_job(jid)
        assert engine.job(jid)['state']=='completed',dict(engine.job(jid))
        assert engine.index.latest('1','200')['resolved']==1
        assert cloud.writes==[]
        await http.aclose();store.close()
    asyncio.run(scenario())


def test_pre_phase3_managed_identity_survives_truncated_folder_name(tmp_path):
    async def scenario():
        store, cloud, http, engine, _ = setup(tmp_path)
        real_tmdb=engine.tmdb
        async def long_title(path, params=None):
            result=await real_tmdb(path,params)
            return {**result,'name':'非常长的剧集中文名称'*7} if path=='tv/10' else result
        engine.tmdb=long_title
        cloud.add_video(name='已确认作品 (2024) {tmdb-10} S01E03.mkv')
        jid=new_job(store,{**CONFIG,'account_id':'1'});await engine.run_job(jid)
        assert engine.job(jid)['state']=='completed',dict(engine.job(jid))
        # Simulate a 0.2.x database: organized_files is present, index tables empty.
        with store.db:
            store.db.execute('DELETE FROM archive_entries')
            store.db.execute('DELETE FROM archive_snapshots')
        async def no_new_requests(*args):
            raise AssertionError('Known identity must not be guessed again from truncated title')
        engine.tmdb=no_new_requests
        new_id=index_job(store,account_id='1');await engine.run_job(new_id)
        assert engine.job(new_id)['state']=='completed'
        assert engine.index.latest('1','200')['resolved']==1
        entry=store.db.execute('SELECT metadata FROM archive_entries').fetchone()
        assert json.loads(entry['metadata'])['details']['name']=='非常长的剧集中文名称'*7
        await http.aclose();store.close()
    asyncio.run(scenario())


def test_related_archive_uses_fresh_parent_listing_and_checks_missing_ids(tmp_path):
    async def scenario():
        store,cloud,http,engine,_=setup(tmp_path)
        for i in (1,2,3):cloud.add_video(str(1000+i),f'测试剧集 (2024) S01E0{i}.WEB-DL.mkv')
        jid=new_job(store,{**CONFIG,'notify_emby':False});await engine.run_job(jid)
        assert engine.job(jid)['state']=='completed'
        row=store.db.execute('SELECT plan FROM organize_items WHERE job_id=? ORDER BY id LIMIT 1',(jid,)).fetchone()
        plan=json.loads(row['plan']);incoming=cloud.add_video('2001','测试剧集 (2024) S01E01.NEW.mkv')
        jid2=new_job(store,{**CONFIG,'notify_emby':False,'keep_existing_names':True});engine.update_job(jid2,state='running')
        season=cloud.nodes['1001']['parent_id'];work=cloud.nodes[season]['parent_id']
        reads=[];children=cloud.children
        async def count_children(cid):
            reads.append(cid);return await children(cid)
        cloud.children=count_children
        calls=[];real=cloud.live_file_exists;finds=[];real_find=engine.find_dir
        async def counted(fid):calls.append(fid);return await real(fid)
        async def count_find(job,parent,name):
            finds.append((parent,name));return await real_find(job,parent,name)
        cloud.live_file_exists=counted;engine.find_dir=count_find;engine._archive_directory_reads={}
        await engine.index.refresh_related(jid2,plan,incoming)
        assert not calls and finds
        assert reads.count(work)==1 and reads.count(season)==1
        assert set(plan['current_archive_ids']) >= {'1001','1002','1003'}
        finds.clear();reads.clear()
        await engine.index.refresh_related(jid2,plan,incoming)
        assert not finds and reads.count(work)==1 and reads.count(season)==1
        cloud.nodes[work]['name']='外部改名的作品目录'
        finds.clear()
        await engine.index.refresh_related(jid2,plan,incoming)
        assert finds  # Cached path changed: fall back to fresh lookup.
        cloud.nodes[work]['name']=plan['parts'][-2]
        moved=await directory(cloud,'200','外部移动')
        cloud.nodes['1002']['parent_id']=moved
        calls.clear();engine._archive_directory_reads={}
        await engine.index.refresh_related(jid2,plan,incoming)
        assert '1002' in calls and moved in plan['conflict_parent_ids']
        assert '1002' in plan['current_archive_ids']
        await http.aclose();store.close()
    asyncio.run(scenario())
