import asyncio
import copy
import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from strimkeep.cloud115 import Cloud115, CloudError
from strimkeep.main import create_app
from strimkeep.media import NeedsReview
from strimkeep.organizer import Organizer, dumps
from strimkeep.quality import UpgradePolicy, compare, facts, group_key, version_layout
from strimkeep.store import Store
from test_app import FakeCloud
from test_organize import setup, new_job, MemoryCloud, CONFIG, MEDIA


def policy(*keys, **overrides):
    value = UpgradePolicy().model_dump()
    if 'revision' in keys:  # Retain coverage of old, already persisted jobs.
        value['rules'].append({'key':'revision','enabled':False,'preferences':[], 'platforms':[], 'descending':True,'order':[]})
    value.update(mode='upgrade', **overrides)
    value['rules'].sort(key=lambda r: keys.index(r['key']) if r['key'] in keys else 100)
    for rule in value['rules']:
        rule['enabled'] = rule['key'] in keys
    return value


class RecycleCloud(MemoryCloud):
    def __init__(self, store):
        super().__init__(store)
        self.bin = {}
        self.after_recycle = None
        self.fail_listing = False
        self.reads = []

    async def file(self, fid):
        self.reads.append(fid)
        if fid not in self.nodes:
            raise CloudError('文件不存在。', 'FILE_NOT_FOUND', 404)
        return await super().file(fid)

    async def recycle_files(self):
        if self.fail_listing:
            raise CloudError('回收站核查失败。', 'RECYCLE_UNVERIFIED', 409)
        return [dict(v) for v in self.bin.values()]

    async def recycle(self, fid, parent):
        assert fid in self.nodes and not self.nodes[fid]['is_dir']
        assert self.nodes[fid]['parent_id'] == parent
        self.writes.append(('recycle', fid))
        node = self.nodes.pop(fid)
        self.bin[fid] = {'id': fid, 'rid': '9' + fid, 'name': node['name'], 'size': node['size']}
        if self.after_recycle:
            callback, self.after_recycle = self.after_recycle, None
            callback()


def upgraded_setup(tmp_path):
    store, _, http, engine, requests = setup(tmp_path)
    cloud = RecycleCloud(store)
    engine.cloud = engine.index.cloud = engine.upgrader.cloud = cloud
    media = {}
    async def probe(url, headers):
        fid = url.rsplit('pc', 1)[-1]
        return copy.deepcopy(media.get(fid, MEDIA))
    engine.probe = probe
    return store, cloud, http, engine, requests, media


async def seed(tmp_path, subtitle=False):
    store, cloud, http, engine, requests, media = upgraded_setup(tmp_path)
    cloud.add_video()
    if subtitle:
        cloud.add_video('1001', '测试剧集 (2024) S01E03.WEB-DL.chs.srt')['size'] = 20
    jid = new_job(store)
    await engine.run_job(jid)
    assert engine.job(jid)['state'] == 'completed', dict(engine.job(jid))
    return store, cloud, http, engine, requests, media


def test_winning_video_not_blocked_by_conflicting_subtitles(tmp_path):
    async def scenario():
        store,cloud,http,engine,_,_=await seed(tmp_path)
        cloud.add_video('2000', '测试剧集 (2024) S01E03.2160p.GROUP.mkv')['size']=2000
        cloud.add_video('2001', '测试剧集 (2024) S01E03.A.chs.srt')['size']=50
        cloud.add_video('2002', '测试剧集 (2024) S01E03.B.chs.srt')['size']=50
        jid=new_job(store,{**CONFIG,'upgrade_policy':policy('size'),'notify_emby':False})
        await engine.run_job(jid)
        row=store.db.execute('SELECT * FROM organize_items WHERE job_id=?',(jid,)).fetchone()
        assert row['state']=='done', dict(row)
        assert '1000' not in cloud.nodes and '2000' in cloud.nodes
        assert cloud.nodes['2001']['parent_id']==cloud.nodes['2000']['parent_id']
        assert '2002' in cloud.bin and '2002' not in cloud.nodes
        assert '字幕目标名称冲突' in str(row['plan'])
        await http.aclose();store.close()
    asyncio.run(scenario())


def test_conflicting_subtitle_recycle_recovers_after_interruption(tmp_path):
    async def scenario():
        store,cloud,http,engine,_,_=await seed(tmp_path)
        cloud.add_video('2000', '测试剧集 (2024) S01E03.2160p.GROUP.mkv')['size']=2000
        cloud.add_video('2001', '测试剧集 (2024) S01E03.A.chs.srt')['size']=50
        cloud.add_video('2002', '测试剧集 (2024) S01E03.B.chs.srt')['size']=50
        recycle = cloud.recycle
        async def interrupt(fid, parent):
            await recycle(fid, parent)
            if fid == '2002': raise asyncio.CancelledError()
        cloud.recycle = interrupt
        jid=new_job(store,{**CONFIG,'upgrade_policy':policy('size'),'notify_emby':False})
        with pytest.raises(asyncio.CancelledError): await engine.run_job(jid)
        cloud.recycle = recycle
        await engine.run_job(jid)
        row=store.db.execute('SELECT * FROM organize_items WHERE job_id=?',(jid,)).fetchone()
        assert row['state']=='done',dict(row)
        assert len([w for w in cloud.writes if w == ('recycle','2002')])==1
        assert '2002' in cloud.bin and cloud.nodes['2001']['parent_id']==cloud.nodes['2000']['parent_id']
        await http.aclose();store.close()
    asyncio.run(scenario())


def test_quality_rules_are_ordered_unknown_is_not_tie_and_crop_fps_are_normalized():
    old = facts('Movie (2024).WEB-DL.V1-Team.mkv', {**MEDIA, 'width':1920,'height':800, 'fps_raw':'24000/1001'}, 1000)
    new = facts('Movie (2024).WEB-DL.V2-Team.mkv', {**MEDIA, 'width':3840,'height':1600,'fps_raw':'24/1'}, 900)
    assert (old['resolution'], new['resolution']) == (1080,2160)
    assert old['fps'] == new['fps'] == 24
    result, evidence = compare(new, old, policy('resolution','size'))
    assert result == 1 and len(evidence) == 1
    assert compare(new, old, policy('size','resolution'))[0] == -1
    assert compare(new, old, policy('fps','revision'))[0] == 1
    with pytest.raises(NeedsReview, match='未知'):
        compare({**new,'bit_depth':None}, {**old,'bit_depth':None}, policy('bit_depth','size'))
    dolby, plain = {**new, 'dynamic_range':'dovi'}, {**new, 'dynamic_range':'hdr10'}
    assert group_key(dolby, policy('size',dynamic_groups='dovi')) == 'DoVi'
    assert group_key(plain, policy('size',dynamic_groups='dovi')) == 'NonDoVi'
    assert group_key(plain, policy('size')) == 'default'
    with pytest.raises(NeedsReview, match='动态范围'):
        group_key({**new,'dynamic_range':None}, policy('size',dynamic_groups='dovi'))


def test_explicit_preference_source_platform_profile_and_representative_audio():
    value=policy('source','audio','dynamic_range','codec','group')
    prefs={'source':['uhd-remux','web-dl'], 'audio':['truehd','eac3'],
           'dynamic_range':['dovi-p7','dovi-p5','sdr'], 'codec':['hevc','h264'], 'group':['preferred','other']}
    for rule in value['rules']:
        if rule['key'] in prefs: rule['preferences']=prefs[rule['key']]
        if rule['key']=='source': rule['platforms']=['nf','amzn']
    left=facts('Movie (2024).NF.WEB-DL.V2-Preferred.mkv', MEDIA,1000)
    right=facts('Movie (2024).AMZN.WEB-DL.V1-Other.mkv', MEDIA,1000)
    assert compare(left,right,value)[0]==1
    audio_policy=policy('audio')
    audio_policy['rules'][0]['preferences']=['truehd','eac3']
    tracks=[{'codec':'eac3','channels':8,'language':'eng'}, {'codec':'truehd','channels':6,'language':'zho'}]
    result,evidence=compare({**left,'audio':tracks},right,audio_policy)
    assert result==1 and evidence[0]['new']['codec']=='truehd'
    with pytest.raises(NeedsReview):
        compare({**left,'audio':[{'codec':'dts','channels':6}]},right,policy('audio'))
    assert UpgradePolicy.model_validate(value).mode=='upgrade'
    with pytest.raises(ValidationError): UpgradePolicy.model_validate({'mode':'upgrade'})
    bad=copy.deepcopy(value);bad['rules'][1]['key']=bad['rules'][0]['key']
    with pytest.raises(ValidationError): UpgradePolicy.model_validate(bad)


@pytest.mark.parametrize('outcome', ['better','worse','tie','unknown','skip'])
def test_five_decisions_real_pipeline(tmp_path,outcome):
    async def scenario():
        store,cloud,http,engine,requests,media=await seed(tmp_path)
        old=cloud.nodes['1000'].copy()
        old_file=next((tmp_path/'strm').rglob('*.strm'))
        old_url=old_file.read_text()
        original=cloud.add_video('2000')['name']
        cloud.nodes['2000']['size']={'better':2000,'worse':500}.get(outcome,1000)
        selected=policy('size')
        if outcome=='unknown':
            selected=policy('bit_depth','size');media['2000']={**MEDIA,'bit_depth':None}
        if outcome=='skip': selected=UpgradePolicy(mode='skip').model_dump()
        jid=new_job(store,{**CONFIG,'upgrade_policy':selected})
        await engine.run_job(jid)
        row=store.db.execute('SELECT * FROM organize_items WHERE job_id=?',(jid,)).fetchone()
        if outcome=='better':
            assert row['state']=='done',dict(engine.job(jid))
            assert '1000' not in cloud.nodes and '2000' in cloud.nodes
            assert cloud.nodes['2000']['name']==old['name'] and cloud.nodes['2000']['parent_id']==old['parent_id']
            assert cloud.bin['1000']['name']==old['name']
            assert old_file.read_text()!=old_url
            assert store.db.execute("SELECT revoked FROM playbacks WHERE file_id='1000'").fetchone()[0]==1
            assert store.db.execute('SELECT file_id FROM organized_files').fetchone()[0]=='2000'
            assert len(list((tmp_path/'strm').rglob('*.strm')))==1
            assert json.loads(row['plan'])['wash']['actions']['1000']['state']=='confirmed'
        elif outcome in ('worse','tie','unknown'):
            assert row['state']=='recycled' and '2000' not in cloud.nodes
            assert cloud.nodes['1000']==old and old_file.read_text()==old_url
            if outcome == 'tie': assert json.loads(row['plan'])['wash']['reason'] == 'equal_rules'
            if outcome == 'unknown': assert json.loads(row['plan'])['wash']['reason'] == 'unknown_fallback'
        elif outcome=='skip':
            assert row['state']=='skipped' and cloud.nodes['2000']['parent_id']=='100'
            assert cloud.nodes['2000']['name']==original and not cloud.bin
        else:
            assert row['state']=='pending' and not cloud.bin
            assert cloud.nodes['1000']==old and cloud.nodes['2000']['name']==original
        assert engine.job(jid)['state'] in ('completed','partial'),dict(engine.job(jid))
        await http.aclose();store.close()
    asyncio.run(scenario())


def test_dovi_and_non_dovi_coexist_without_recycling(tmp_path):
    async def scenario():
        store,cloud,http,engine,_,media=await seed(tmp_path)
        old=cloud.nodes['1000'].copy()
        cloud.add_video('2000')
        media['2000']={**MEDIA,'dynamic_range':'SDR'}
        cfg=policy('size',dynamic_groups='dovi')
        jid=new_job(store,{**CONFIG,'upgrade_policy':cfg})
        await engine.run_job(jid)
        assert engine.job(jid)['state']=='completed',dict(engine.job(jid))
        assert cloud.nodes['1000']==old and not cloud.bin
        assert len(list((tmp_path/'strm').rglob('*.strm')))==2
        plan=json.loads(store.db.execute('SELECT plan FROM organize_items WHERE job_id=?',(jid,)).fetchone()[0])
        assert ' - S01E03 - NonDoVi.' in plan['target_name']
        assert plan['parts'][-1]=='Season 1'
        await http.aclose();store.close()
    asyncio.run(scenario())


def test_overlapping_merged_episode_never_recycles(tmp_path):
    async def scenario():
        store,cloud,http,engine,_,_=await seed(tmp_path)
        cloud.add_video('2000','测试剧集 (2024) S01E03-E04.WEB-DL.mkv')['size']=2000
        jid=new_job(store,{**CONFIG,'upgrade_policy':policy('size')})
        await engine.run_job(jid)
        row=store.db.execute('SELECT * FROM organize_items WHERE job_id=?',(jid,)).fetchone()
        assert row['state']=='pending' and '合并集' in row['message']
        assert not cloud.bin and '1000' in cloud.nodes
        await http.aclose();store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('breakpoint', ['lost-response','crash-after-recycle','crash-after-stage','crash-after-final-name'])
def test_replacement_resumes_without_duplicate_recycle_or_move(tmp_path,breakpoint):
    async def scenario():
        store,cloud,http,engine,_,_=await seed(tmp_path)
        cloud.add_video('2000')['size']=2000
        jid=new_job(store,{**CONFIG,'upgrade_policy':policy('size')})
        if breakpoint=='lost-response':
            cloud.after_recycle=lambda: (_ for _ in ()).throw(CloudError('响应丢失。'))
        elif breakpoint=='crash-after-recycle':
            cloud.after_recycle=lambda: (_ for _ in ()).throw(asyncio.CancelledError())
        elif breakpoint=='crash-after-stage':
            cloud.after_move=lambda: (_ for _ in ()).throw(asyncio.CancelledError())
        else:
            original_rename=cloud.rename
            async def crashing(fid,name):
                await original_rename(fid,name)
                if '.sknew-' not in name:
                    cloud.rename=original_rename
                    raise asyncio.CancelledError()
            cloud.rename=crashing
        try: await engine.run_job(jid)
        except asyncio.CancelledError: pass
        # Simulate replacing the process's DB connection and worker objects.
        store.close()
        reopened=Store(tmp_path/'data','admin','test-password-1234')
        cloud.store=reopened
        restarted=Organizer(reopened,cloud,http,tmp_path/'strm',engine.probe)
        await restarted.run_job(jid)
        assert restarted.job(jid)['state']=='completed',dict(restarted.job(jid))
        assert len([w for w in cloud.writes if w==('recycle','1000')])==1
        assert len([w for w in cloud.writes if w[0]=='move' and w[1]=='2000'])==1
        assert '1000' not in cloud.nodes and '.sknew-' not in cloud.nodes['2000']['name']
        assert len(list((tmp_path/'strm').rglob('*.strm')))==1
        await http.aclose();reopened.close()
    asyncio.run(scenario())


def test_unconfirmed_recycle_is_not_resent_and_new_output_remains(tmp_path):
    async def scenario():
        store,cloud,http,engine,_,_=await seed(tmp_path)
        cloud.add_video('2000')['size']=2000
        calls=[]
        async def uncertain_delete(fid,parent):
            calls.append(fid)
            raise CloudError('无法确认是否送达。')
        cloud.recycle=uncertain_delete
        jid=new_job(store,{**CONFIG,'upgrade_policy':policy('size')})
        for _ in range(2):
            await engine.run_job(jid)
            row = engine.job(jid)
            items = [(i['state'], i['step']) for i in store.db.execute(
                'SELECT * FROM organize_items WHERE job_id=?', (jid,))]
            assert row['state']=='partial', (row['state'], row['stage'], items)
        assert calls==['1000'] and '1000' in cloud.nodes and '2000' in cloud.nodes
        assert next((tmp_path/'strm').rglob('*.strm')).is_file()
        await http.aclose();store.close()
    asyncio.run(scenario())


def test_subtitle_download_failure_does_not_block_wash_and_unowned_files_are_preserved(tmp_path):
    async def scenario():
        store,cloud,http,engine,_,_=await seed(tmp_path,subtitle=True)
        old_strm=next((tmp_path/'strm').rglob('*.strm'))
        protected={old_strm.with_suffix('.nfo'):b'plugin-nfo',old_strm.parent/'poster.jpg':b'plugin-poster',old_strm.parent/'user.srt':b'manual-sub'}
        for p,content in protected.items():p.write_bytes(content)
        cloud.add_video('2000')['size']=2000
        cloud.add_video('2001','测试剧集 (2024) S01E03.WEB-DL.chs.srt')['size']=20
        original_download=engine.download
        async def failed(*args):raise httpx.ConnectError('test')
        engine.download=failed
        jid=new_job(store,{**CONFIG,'upgrade_policy':policy('size')})
        await engine.run_job(jid)
        row=store.db.execute('SELECT * FROM organize_items WHERE job_id=?',(jid,)).fetchone()
        assert row['state']=='done',dict(row)
        assert engine.job(jid)['state'] in ('completed','partial')
        assert '外挂字幕下载失败' in row['message']
        engine.download=original_download
        assert set(cloud.bin)=={'1000','1001'} and '2000' in cloud.nodes and '2001' in cloud.nodes
        assert all(p.read_bytes()==content for p,content in protected.items())
        assert len([w for w in cloud.writes if w[0]=='move' and w[1] in ('2000','2001')])==2
        await http.aclose();store.close()
    asyncio.run(scenario())


def test_source_external_change_or_recycle_listing_failure_prevents_deletion(tmp_path):
    async def scenario():
        store,cloud,http,engine,_,_=await seed(tmp_path)
        cloud.add_video('2000')['size']=2000
        cloud.fail_listing=True
        jid=new_job(store,{**CONFIG,'upgrade_policy':policy('size')})
        await engine.run_job(jid)
        assert engine.job(jid)['state']=='partial' and not cloud.bin
        assert '1000' in cloud.nodes
        await http.aclose();store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['revoked','missing-strm','changed-old'])
def test_fresh_proofs_guard_every_recycle(tmp_path,change):
    async def scenario():
        store,cloud,http,engine,_,media=await seed(tmp_path)
        cloud.add_video('2000')['size']=2000
        selected=policy('size')
        if change=='changed-old':
            original_listing=cloud.recycle_files
            count=0
            async def listing():
                nonlocal count
                count+=1
                if count==1:cloud.nodes['1000']['size']+=1
                return await original_listing()
            cloud.recycle_files=listing
        else:
            export=engine.export
            async def changed(*args):
                await export(*args)
                plan=args[-1]
                if change=='revoked':
                    with store.db:store.db.execute('UPDATE playbacks SET revoked=1 WHERE id=?',(plan['playback_id'],))
                else:(engine.root/plan['relative_path']).unlink()
            engine.export=changed
        jid=new_job(store,{**CONFIG,'upgrade_policy':selected})
        await engine.run_job(jid)
        assert engine.job(jid)['state'] == 'partial',dict(engine.job(jid))
        assert not cloud.bin and '1000' in cloud.nodes and '2000' in cloud.nodes
        await http.aclose();store.close()
    asyncio.run(scenario())


def test_skip_known_conflict_needs_no_probe(tmp_path):
    async def scenario():
        store,cloud,http,engine,_,_=await seed(tmp_path)
        cloud.add_video('2000')
        async def forbidden(*args):raise AssertionError('skip mode must not probe an identified conflict')
        engine.probe=forbidden
        jid=new_job(store,{**CONFIG,'upgrade_policy':UpgradePolicy(mode='skip').model_dump()})
        await engine.run_job(jid)
        assert engine.job(jid)['state']=='completed',dict(engine.job(jid))
        assert store.db.execute('SELECT state FROM organize_items WHERE job_id=?',(jid,)).fetchone()[0]=='skipped'
        assert cloud.nodes['2000']['parent_id']=='100' and not cloud.bin
        await http.aclose();store.close()
    asyncio.run(scenario())


def test_movie_replacement_cleans_old_spec_strm_but_preserves_user_metadata(tmp_path):
    async def scenario():
        store,cloud,http,engine,_,media=upgraded_setup(tmp_path)
        cloud.add_video('1000','测试电影 (2024).WEB-DL.mkv')
        media['1000']={**MEDIA,'width':1920,'height':800}
        jid=new_job(store);await engine.run_job(jid)
        assert engine.job(jid)['state']=='completed'
        old_path=next(engine.root.rglob('*.strm'))
        nfo=old_path.with_suffix('.nfo');nfo.write_text('plugin metadata')
        cloud.add_video('2000','测试电影 (2024).WEB-DL.mkv')
        media['2000']=copy.deepcopy(MEDIA)
        jid=new_job(store,{**CONFIG,'upgrade_policy':policy('resolution')})
        await engine.run_job(jid)
        assert engine.job(jid)['state']=='completed',dict(engine.job(jid))
        assert set(cloud.bin)=={'1000'} and not old_path.exists()
        relative = str(old_path.relative_to(engine.root))
        assert store.db.execute('SELECT 1 FROM strm_changes WHERE job_id=? AND action=? AND path=?', (jid,'removed',relative)).fetchone()
        updates=engine.emby.updates(jid, {'targets':engine.emby.targets(jid)})
        assert {'Path':'/strimkeep/'+relative,'UpdateType':'Deleted'} in updates
        await engine.notifications.media_report(engine.job(jid))
        messages = [r['message'] for r in store.db.execute("SELECT message FROM notifications WHERE job_id=? AND event_key LIKE ?", (jid, 'strm:%:files:removed:%'))]
        assert any('🧹' in msg and old_path.name in msg for msg in messages)
        assert nfo.read_text()=='plugin metadata'
        assert len(list(engine.root.rglob('*.strm')))==1
        await http.aclose();store.close()
    asyncio.run(scenario())


def test_same_batch_new_winner_enters_index_before_second_comparison(tmp_path):
    async def scenario():
        store,cloud,http,engine,_,_=await seed(tmp_path)
        cloud.add_video('2000')['size']=2000
        cloud.add_video('3000')['size']=3000
        jid=new_job(store,{**CONFIG,'upgrade_policy':policy('size')})
        await engine.run_job(jid)
        assert engine.job(jid)['state']=='completed',dict(engine.job(jid))
        assert set(cloud.bin)=={'1000','2000'} and '3000' in cloud.nodes
        assert len(list(engine.root.rglob('*.strm')))==1
        assert store.db.execute('SELECT file_id FROM organized_files').fetchone()[0]=='3000'
        await http.aclose();store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('complete', [False,True])
def test_output_repair_never_replays_cloud_upgrade_actions(tmp_path,complete):
    async def scenario():
        store,cloud,http,engine,_,_=await seed(tmp_path)
        cloud.add_video('2000')['size']=2000
        if not complete:
            async def uncertain_delete(fid,parent):raise CloudError('未确认。')
            cloud.recycle=uncertain_delete
        jid=new_job(store,{**CONFIG,'upgrade_policy':policy('size')})
        await engine.run_job(jid)
        assert engine.job(jid)['state']==('completed' if complete else 'partial')
        writes=cloud.writes.copy()
        cfg=json.loads(engine.job(jid)['config']);cfg['repair_only']=True
        engine.update_job(jid,config=dumps(cfg),state='queued')
        with store.db:store.db.execute("UPDATE organize_items SET state='archived' WHERE job_id=?",(jid,))
        await engine.run_job(jid)
        assert engine.job(jid)['state'] in ('completed','partial'),dict(engine.job(jid))
        assert cloud.writes==writes
        assert ('1000' not in cloud.nodes)==complete
        await http.aclose();store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('bad', ['missing-count','duplicate','truncated','wrong-offset'])
def test_recycle_pagination_never_accepts_incomplete_or_ambiguous_data(tmp_path,bad):
    async def scenario():
        store=Store(tmp_path,'admin','test-password-1234');store.set_many({'cookie':'UID=1_A1; CID=test; SEID=test'})
        cloud=Cloud115(store);await cloud.http.aclose()
        row={'id':'1000','rid':'91000','file_name':'video.mkv','file_size':'1000'}
        data={'state':True,'count':1,'offset':0,'data':[row]}
        if bad=='missing-count':data.pop('count')
        elif bad=='duplicate':data.update(count=2,data=[row,row])
        elif bad=='truncated':data.update(count=2,data=[])
        else:data['offset']=1
        cloud.http=httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,json=data)))
        with pytest.raises(CloudError) as exc:await cloud.recycle_files()
        assert exc.value.code=='RECYCLE_UNVERIFIED'
        await cloud.close();store.close()
    asyncio.run(scenario())


def test_policy_api_validates_and_snapshots_new_tasks(tmp_path,monkeypatch):
    monkeypatch.setenv('ADMIN_USERNAME','admin');monkeypatch.setenv('ADMIN_PASSWORD','test-password-1234')
    app=create_app(tmp_path/'data',tmp_path/'strm',FakeCloud)
    with TestClient(app) as client:
        assert client.get('/api/v1/upgrade/config').status_code==401
        csrf=client.post('/api/v1/auth/login',json={'username':'admin','password':'test-password-1234'}).json()['csrf']
        client.headers['X-CSRF-Token']=csrf
        initial=client.get('/api/v1/upgrade/config').json()['config']
        assert initial['mode']=='pending' and not any(r['enabled'] for r in initial['rules'])
        assert client.put('/api/v1/upgrade/config',json={'mode':'upgrade'}).status_code==422
        selected=policy('resolution','size')
        assert client.put('/api/v1/upgrade/config',json=selected).status_code==200
        store=app.state.store
        store.set_many({'cookie':'UID=1_A1; CID=test; SEID=test','tmdb_key':'private-key','playback_base':'https://play.example','organize_config':CONFIG})
        job=client.post('/api/v1/organize/jobs').json()
        assert job['config']['upgrade_policy']==selected
        assert client.put('/api/v1/upgrade/config',json=initial).status_code==200
        assert json.loads(app.state.organizer.job(job['id'])['config'])['upgrade_policy']==selected
        text=client.get(f'/api/v1/organize/jobs/{job["id"]}').text
        assert 'private-key' not in text and 'SEID' not in text


def test_recycled_old_token_cannot_overwrite_new_version_and_regeneration_updates_ownership(tmp_path,monkeypatch):
    async def scenario():
        store,cloud,http,engine,_,_=await seed(tmp_path)
        old_pid=store.db.execute("SELECT id FROM playbacks WHERE file_id='1000'").fetchone()[0]
        cloud.add_video('2000')['size']=2000
        jid=new_job(store,{**CONFIG,'upgrade_policy':policy('size')})
        await engine.run_job(jid)
        assert engine.job(jid)['state']=='completed'
        new_pid=store.db.execute("SELECT id FROM playbacks WHERE file_id='2000'").fetchone()[0]
        await http.aclose();store.close()
        return old_pid,new_pid,jid
    old_pid,new_pid,jid=asyncio.run(scenario())
    monkeypatch.setenv('ADMIN_USERNAME','admin');monkeypatch.setenv('ADMIN_PASSWORD','test-password-1234')
    app=create_app(tmp_path/'data',tmp_path/'strm',FakeCloud)
    with TestClient(app) as client:
        csrf=client.post('/api/v1/auth/login',json={'username':'admin','password':'test-password-1234'}).json()['csrf']
        client.headers['X-CSRF-Token']=csrf
        path=next((tmp_path/'strm').rglob('*.strm'));current=path.read_bytes()
        assert client.post(f'/api/v1/playbacks/{old_pid}/regenerate').status_code==409
        assert path.read_bytes()==current
        assert client.post(f'/api/v1/playbacks/{new_pid}/regenerate').status_code==200
        plan=json.loads(app.state.store.db.execute('SELECT plan FROM organize_items WHERE job_id=?',(jid,)).fetchone()[0])
        assert plan['local_files'][plan['relative_path']]==hashlib.sha256(path.read_bytes()).hexdigest()


def test_real_sdk_recycle_wire_contract_and_strict_pagination(tmp_path):
    async def scenario():
        store=Store(tmp_path,'admin','test-password-1234');store.set_many({'cookie':'UID=1_A1; CID=test; SEID=test'})
        cloud=Cloud115(store);await cloud.http.aclose()
        requests=[]
        def handler(request):
            requests.append(request)
            assert request.headers['cookie']=='UID=1_A1; CID=test; SEID=test'
            if request.url.path=='/rb/delete':
                assert parse_qs(request.content.decode())=={'fid':['1000'],'pid':['200'],'ignore_warn':['0']}
                return httpx.Response(200,json={'state':True})
            assert request.url.path=='/rb' and request.url.params['aid']=='7'
            return httpx.Response(200,json={'state':True,'count':1,'offset':0,'data':[{'id':'1000','rid':'91000','file_name':'video.mkv','file_size':'1000'}]})
        cloud.http=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        assert (await cloud.recycle_files())[0]['rid']=='91000'
        cloud.next_request=0
        await cloud.recycle('1000','200')
        assert not any('clean' in r.url.path or 'revert' in r.url.path for r in requests)
        cloud.http._transport=httpx.MockTransport(lambda r:httpx.Response(200,json={'state':True,'data':[]}))
        cloud.next_request=0
        with pytest.raises(CloudError) as exc:await cloud.recycle_files()
        assert exc.value.code=='RECYCLE_UNVERIFIED'
        await cloud.close();store.close()
    asyncio.run(scenario())


def test_two_wash_replacements_are_batched_before_directory_publication(tmp_path):
    async def scenario():
        store, cloud, http, engine, _, _ = upgraded_setup(tmp_path)
        for i in (1, 2):
            cloud.add_video(str(1000+i), f'测试剧集 (2024) S01E0{i}.WEB-DL.mkv')['size'] = 1000
        first = new_job(store, {**CONFIG, 'notify_emby': False})
        await engine.run_job(first)
        assert engine.job(first)['state'] == 'completed'
        for i in (1, 2):
            cloud.add_video(str(2000+i), f'测试剧集 (2024) S01E0{i}.WEB-DL.mkv')['size'] = 2000
        calls = []
        async def rename_many(pairs):
            calls.append(('rename', len(pairs)))
            for fid, name in pairs:
                cloud.nodes[fid]['name'] = name
        async def move_many(ids, dest):
            calls.append(('move', len(ids)))
            for fid in ids:
                cloud.nodes[fid]['parent_id'] = dest
        cloud.rename_many, cloud.move_many = rename_many, move_many
        jid = new_job(store, {**CONFIG, 'notify_emby': False, 'upgrade_policy': policy('size')})
        await engine.run_job(jid)
        rows = store.db.execute('SELECT state,message FROM organize_items WHERE job_id=?', (jid,)).fetchall()
        assert engine.job(jid)['state'] == 'completed', (dict(engine.job(jid)), [dict(r) for r in rows])
        assert calls == [('rename', 2), ('move', 2)]
        assert set(cloud.bin) == {'1001', '1002'}
        assert len(list(engine.root.rglob('*.strm'))) == 2
        queued = store.db.execute("SELECT count(*) FROM events WHERE kind='organize' AND message LIKE ?", (f'任务 {jid}：归档目录 %已加入后台 STRM 队列%',)).fetchone()[0]
        assert queued == 1
        await http.aclose(); store.close()
    asyncio.run(scenario())


def test_washes_recycle_old_files_before_batch_move_like_upstream(tmp_path):
    async def scenario():
        store,cloud,http,engine,_,_=upgraded_setup(tmp_path)
        for i in (1,2):cloud.add_video(str(1000+i),f'测试剧集 (2024) S01E0{i}.WEB-DL.mkv')['size']=1000
        first=new_job(store,{**CONFIG,'notify_emby':False});await engine.run_job(first)
        for i in (1,2):cloud.add_video(str(2000+i),f'测试剧集 (2024) S01E0{i}.WEB-DL.mkv')['size']=2000
        order=[]
        async def rename_many(pairs):
            order.append('rename')
            for fid,name in pairs:cloud.nodes[fid]['name']=name
        async def move_many(ids,dest):
            order.append('move')
            for fid in ids:cloud.nodes[fid]['parent_id']=dest
        batches=[]
        async def recycle_many(ids,parent):
            order.append('recycle')
            batches.append(list(ids))
            for fid in ids:await cloud.recycle(fid,parent)
        cloud.rename_many=rename_many;cloud.move_many=move_many;cloud.recycle_many=recycle_many
        jid=new_job(store,{**CONFIG,'notify_emby':False,'upgrade_policy':policy('size')});await engine.run_job(jid)
        assert engine.job(jid)['state']=='completed',dict(engine.job(jid))
        assert len(batches)==1 and set(batches[0])=={'1001','1002'}
        assert order == ['rename','recycle','move']
        assert set(cloud.bin)=={'1001','1002'}
        await http.aclose();store.close()
    asyncio.run(scenario())


def test_upstream_order_lost_recycle_response_does_not_claim_completion(tmp_path):
    async def scenario():
        store,cloud,http,engine,_,_=upgraded_setup(tmp_path)
        for i in (1,2):cloud.add_video(str(1000+i),f'测试剧集 (2024) S01E0{i}.WEB-DL.mkv')['size']=1000
        first=new_job(store,{**CONFIG,'notify_emby':False});await engine.run_job(first)
        for i in (1,2):cloud.add_video(str(2000+i),f'测试剧集 (2024) S01E0{i}.WEB-DL.mkv')['size']=2000
        async def rename_many(pairs):
            for fid,name in pairs:cloud.nodes[fid]['name']=name
        async def move_many(ids,dest):
            for fid in ids:cloud.nodes[fid]['parent_id']=dest
        batches=[]
        async def recycle_many(ids,parent):
            batches.append(list(ids))
            for fid in ids:await cloud.recycle(fid,parent)
            raise CloudError('response lost','CLOUD_UNAVAILABLE')
        cloud.rename_many=rename_many;cloud.move_many=move_many;cloud.recycle_many=recycle_many
        jid=new_job(store,{**CONFIG,'notify_emby':False,'upgrade_policy':policy('size')});await engine.run_job(jid)
        assert engine.job(jid)['state']=='partial',dict(engine.job(jid))
        assert len(batches)==1 and set(batches[0])=={'1001','1002'}
        assert len([x for x in cloud.writes if x[0]=='recycle'])==2
        await http.aclose();store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('outcome', ['success', 'lost_response', 'winner_missing'])
def test_incoming_discards_are_batch_guarded_and_reconciled(tmp_path, outcome):
    async def scenario():
        store,cloud,http,engine,_,_=upgraded_setup(tmp_path)
        for i in (1,2):cloud.add_video(str(1000+i),f'测试剧集 (2024) S01E0{i}.WEB-DL.mkv')['size']=2000
        first=new_job(store,{**CONFIG,'notify_emby':False});await engine.run_job(first)
        assert engine.job(first)['state']=='completed'
        for i in (1,2):cloud.add_video(str(2000+i),f'测试剧集 (2024) S01E0{i}.WEB-DL.mkv')['size']=1000
        async def move_many(ids,dest):
            for fid in ids:cloud.nodes[fid]['parent_id']=dest
        cloud.move_many=move_many; batches=[]; listings=0
        original_list=cloud.recycle_files
        async def recycle_files():
            nonlocal listings
            listings+=1;return await original_list()
        cloud.recycle_files=recycle_files
        async def recycle_many(ids,parent):
            batches.append(list(ids))
            for fid in ids:await cloud.recycle(fid,parent)
            if outcome=='lost_response':raise CloudError('response lost','CLOUD_UNAVAILABLE')
        cloud.recycle_many=recycle_many
        if outcome=='winner_missing':
            original=engine.upgrader.discard_many
            async def missing(job,entries,config):
                del cloud.nodes['1001']
                return await original(job,entries,config)
            engine.upgrader.discard_many=missing
        jid=new_job(store,{**CONFIG,'notify_emby':False,'upgrade_policy':policy('size')});await engine.run_job(jid)
        rows=store.db.execute('SELECT file_id,state FROM organize_items WHERE job_id=? ORDER BY file_id',(jid,)).fetchall()
        if outcome=='winner_missing':
            assert not batches and '2001' in cloud.nodes
        else:
            assert len(batches)==1 and set(batches[0])=={'2001','2002'}
            assert all(r['state']=='recycled' for r in rows),[dict(r) for r in rows]
            assert set(cloud.bin)=={'2001','2002'} and {'1001','1002'} <= set(cloud.nodes)
            if outcome=='success':assert listings==2
        await http.aclose();store.close()
    asyncio.run(scenario())


def test_identical_sha_discards_are_batched(tmp_path):
    async def scenario():
        store,cloud,http,engine,_,_=upgraded_setup(tmp_path)
        for i in (1,2):
            old=cloud.add_video(str(1000+i),f'测试剧集 (2024) S01E0{i}.WEB-DL.mkv');old['sha1']=str(i)*40
        first=new_job(store,{**CONFIG,'notify_emby':False});await engine.run_job(first)
        for i in (1,2):
            new=cloud.add_video(str(2000+i),f'测试剧集 (2024) S01E0{i}.WEB-DL.mkv');new['sha1']=str(i)*40
        async def move_many(ids,dest):
            for fid in ids:cloud.nodes[fid]['parent_id']=dest
        async def recycle_many(ids,parent):
            calls.append(list(ids))
            for fid in ids:await cloud.recycle(fid,parent)
        calls=[];cloud.move_many=move_many;cloud.recycle_many=recycle_many
        jid=new_job(store,{**CONFIG,'notify_emby':False,'upgrade_policy':policy('size')});await engine.run_job(jid)
        rows=store.db.execute('SELECT state,plan FROM organize_items WHERE job_id=?',(jid,)).fetchall()
        assert len(calls)==1 and set(calls[0])=={'2001','2002'}
        assert all(r['state']=='recycled' and json.loads(r['plan'])['wash']['reason']=='identical_copy' for r in rows)
        assert {'1001','1002'} <= set(cloud.nodes)
        await http.aclose();store.close()
    asyncio.run(scenario())
