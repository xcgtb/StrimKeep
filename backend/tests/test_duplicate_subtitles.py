import asyncio
import hashlib
import json
from pathlib import Path

import httpx
import pytest

from strimkeep.organizer import Organizer
from strimkeep.store import Store
from test_duplicate import NAME, seed_copy as seed_current, job_for, item
from strimkeep.cloud115 import CloudError


async def seed_copy(tmp_path):
    """Exercise recovery of legacy saved plans that still merge subtitles."""
    result = await seed_current(tmp_path)
    engine = result[3]
    prepare = engine.upgrader.prepare_duplicate
    async def legacy(job_id, original, plan, config, candidates, policy):
        if not await prepare(job_id, original, plan, config, candidates, policy):
            return False
        if original.get('subtitle_warning'):
            raise CloudError('重复视频的外挂字幕关联不明确，保留视频和字幕，等待核查。', 'UPGRADE_UNCERTAIN')
        winner = plan['wash']['winner']['source']
        subtitle_plan = await engine.upgrader.plan_duplicate_subtitles(job_id, original, winner, config)
        plan['wash']['duplicate'].pop('discard_bound_subtitles', None)
        plan['wash']['duplicate'].update(subtitle_plan)
        return True
    engine.upgrader.prepare_duplicate = legacy
    return result


CONTENT=b'1\n00:00:00,000 --> 00:00:01,000\nsubtitle\n'


def test_new_duplicate_recycles_bound_subtitle_without_copying_it(tmp_path):
    async def scenario():
        store,cloud,http,engine,_,_=await seed_current(tmp_path)
        old=cloud.nodes['1000'].copy()
        add_subtitle(cloud)
        async def forbidden(*args): raise AssertionError('落败副本不应下载或归并字幕')
        engine.download=forbidden
        jid=job_for(store);await engine.run_job(jid)
        assert item(store,jid)['state']=='recycled'
        assert set(cloud.bin)=={'2000','2001'} and cloud.nodes['1000']==old
        assert cloud.writes.index(('recycle','2000')) < cloud.writes.index(('recycle','2001'))
        await http.aclose();store.close()
    asyncio.run(scenario())


def test_new_duplicate_resumes_after_video_recycle_without_replay(tmp_path):
    async def scenario():
        store,cloud,http,engine,_,_=await seed_current(tmp_path)
        add_subtitle(cloud)
        async def crash_after_video(fid, parent):
            await original(fid, parent)
            if fid == '2000':
                cloud.recycle = original
                raise asyncio.CancelledError()
        original = cloud.recycle
        cloud.recycle = crash_after_video
        jid=job_for(store)
        try: await engine.run_job(jid)
        except asyncio.CancelledError: pass
        store.close()
        store=Store(tmp_path/'data','admin','test-password-1234');cloud.store=store
        restarted=Organizer(store,cloud,http,tmp_path/'strm',engine.probe)
        await restarted.run_job(jid)
        assert item(store,jid)['state']=='recycled'
        assert cloud.writes.count(('recycle','2000')) == 1
        assert cloud.writes.count(('recycle','2001')) == 1
        assert '1000' in cloud.nodes
        await http.aclose();store.close()
    asyncio.run(scenario())


def add_subtitle(cloud, fid='2001', suffix='.chs.forced.srt', parent='100', base=None, content=CONTENT):
    sub=cloud.add_video(fid,(base or Path(NAME).stem)+suffix,parent)
    sub.update(size=len(content),sha1=hashlib.sha1(content).hexdigest())
    return sub


def test_existing_identical_subtitle_recycles_only_extra_subtitle_then_extra_video(tmp_path):
    async def scenario():
        store,cloud,http,engine,_,_=await seed_copy(tmp_path)
        old=cloud.nodes['1000'].copy()
        new_sub=add_subtitle(cloud)
        existing=add_subtitle(cloud,'3001',parent=old['parent_id'],base=Path(old['name']).stem).copy()
        jid=job_for(store);await engine.run_job(jid)
        assert item(store,jid)['state']=='recycled' and engine.job(jid)['state']=='completed'
        assert set(cloud.bin)=={'2000','2001'} and cloud.nodes['3001']==existing and cloud.nodes['1000']==old
        assert cloud.writes.index(('recycle','2001'))<cloud.writes.index(('recycle','2000'))
        assert not any(w[0]=='move' and w[1]=='2001' for w in cloud.writes)
        assert next((tmp_path/'strm').rglob('*.srt')).read_bytes()==CONTENT
        assert set(json.loads(item(store,jid)['plan'])['wash']['actions'])=={'2000','2001'}
        await http.aclose();store.close()
    asyncio.run(scenario())


def test_language_forced_and_paired_subtitle_suffixes_are_preserved(tmp_path):
    async def scenario():
        store,cloud,http,engine,_,_=await seed_copy(tmp_path)
        suffixes=['.chs.ass','.cht.forced.srt','.idx','.sub']
        for i,suffix in enumerate(suffixes):add_subtitle(cloud,str(2001+i),suffix)
        old=cloud.nodes['1000'].copy()
        jid=job_for(store);await engine.run_job(jid)
        assert engine.job(jid)['state']=='completed' and set(cloud.bin)=={'2000'}
        for i,suffix in enumerate(suffixes):
            sub=cloud.nodes[str(2001+i)]
            assert sub['parent_id']==old['parent_id'] and sub['name']==Path(old['name']).stem+suffix
            assert any(p.name==sub['name'] and p.read_bytes()==CONTENT for p in (tmp_path/'strm').rglob('*') if p.is_file())
        assert len(list((tmp_path/'strm').rglob('*.strm')))==1
        await http.aclose();store.close()
    asyncio.run(scenario())


def test_different_release_subtitle_merged_before_duplicate_video_recycled(tmp_path):
    async def scenario():
        store,cloud,http,engine,_,_=await seed_copy(tmp_path)
        old=cloud.nodes['1000'].copy()
        sub=add_subtitle(cloud,base='测试剧集 (2024) S01E03.OTHER',suffix='.chs.forced.srt')
        jid=job_for(store);await engine.run_job(jid)
        assert item(store,jid)['state']=='recycled' and '2000' in cloud.bin
        assert cloud.nodes[sub['id']]['name']==Path(old['name']).stem+'.chs.forced.srt'
        assert cloud.nodes[sub['id']]['parent_id']==old['parent_id']
        assert next((tmp_path/'strm').rglob('*.chs.forced.srt')).read_bytes()==CONTENT
        await http.aclose();store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('case',['cloud-content-conflict','local-content-conflict','download-fails','download-corrupt','missing-sha1','ambiguous-binding'])
def test_subtitle_failure_preserves_new_video_and_all_source_subtitles(case,tmp_path):
    async def scenario():
        store,cloud,http,engine,_,_=await seed_copy(tmp_path)
        new_sub=add_subtitle(cloud)
        old=cloud.nodes['1000']
        if case=='cloud-content-conflict':add_subtitle(cloud,'3001',parent=old['parent_id'],base=Path(old['name']).stem,content=b'other caption')
        if case=='local-content-conflict':
            local=next((tmp_path/'strm').rglob('*.strm')).with_name(Path(old['name']).stem+'.chs.forced.srt');local.write_bytes(b'user-caption')
        if case=='download-fails':
            async def fail(*args):raise httpx.ConnectError('offline')
            engine.download=fail
        if case=='download-corrupt':
            async def corrupt(*args):return b'incorrect download'
            engine.download=corrupt
        if case=='missing-sha1':new_sub['sha1']=''
        if case=='ambiguous-binding':cloud.add_video('2002',NAME[:-4]+'.mp4')['sha1']='d'*40
        jid=job_for(store)
        config=json.loads(engine.job(jid)['config']);config['pending_cid']=''
        engine.update_job(jid,config=json.dumps(config))
        await engine.run_job(jid)
        assert engine.job(jid)['state']=='partial'
        assert not set(cloud.bin).intersection({'1000','2000','2001'})
        assert cloud.nodes['2000']['parent_id']=='100' and cloud.nodes['2001']['parent_id']=='100'
        if case=='local-content-conflict':assert local.read_bytes()==b'user-caption'
        await http.aclose();store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('point',['rename','move','subtitle-recycle','video-recycle'])
def test_restart_during_subtitle_merge_never_replays_moves_or_recycles(point,tmp_path):
    async def scenario():
        store,cloud,http,engine,_,_=await seed_copy(tmp_path)
        add_subtitle(cloud)
        if point=='subtitle-recycle':
            old=cloud.nodes['1000'];add_subtitle(cloud,'3001',parent=old['parent_id'],base=Path(old['name']).stem)
        def crash():raise asyncio.CancelledError()
        if point=='rename':cloud.after_rename=crash
        elif point=='move':cloud.after_move=crash
        else:
            target='2001' if point=='subtitle-recycle' else '2000'
            original_recycle=cloud.recycle
            async def crashing(fid,parent):
                await original_recycle(fid,parent)
                if fid==target:
                    cloud.recycle=original_recycle
                    raise asyncio.CancelledError()
            cloud.recycle=crashing
        jid=job_for(store)
        try:await engine.run_job(jid)
        except asyncio.CancelledError:pass
        # Reset one-shot callbacks as a real process boundary would.
        cloud.after_rename=cloud.after_move=None
        store.close();store=Store(tmp_path/'data','admin','test-password-1234');cloud.store=store
        restarted=Organizer(store,cloud,http,tmp_path/'strm',engine.probe)
        await restarted.run_job(jid)
        assert item(store,jid)['state']=='recycled' and restarted.job(jid)['state']=='completed',dict(restarted.job(jid))
        assert cloud.writes.count(('recycle','2000'))==1 and '1000' in cloud.nodes
        if point=='subtitle-recycle':assert cloud.writes.count(('recycle','2001'))==1
        else:assert len([w for w in cloud.writes if w[0]=='move' and w[1]=='2001'])==1
        assert next((tmp_path/'strm').rglob('*.srt')).read_bytes()==CONTENT
        await http.aclose();store.close()
    asyncio.run(scenario())


def test_download_failure_then_resume_completes_same_merge_and_notifies_once(tmp_path):
    async def scenario():
        store,cloud,http,engine,requests,_=await seed_copy(tmp_path)
        add_subtitle(cloud);original_download=engine.download
        async def fail(*args):raise httpx.ConnectError('offline')
        engine.download=fail
        jid=job_for(store);await engine.run_job(jid); await engine.emby.drain_outbox(); await engine.emby.tick()
        assert engine.job(jid)['state']=='partial' and not cloud.bin
        requests.clear();engine.download=original_download
        await engine.run_job(jid); await engine.emby.drain_outbox(); await engine.emby.tick()
        assert engine.job(jid)['state']=='completed'
        assert len([r for r in requests if r.url.host=='emby' and r.url.path.endswith('/Library/Media/Updated')])==1
        assert cloud.writes.count(('recycle','2000'))==1
        await http.aclose();store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('missing',['cloud','local'])
def test_subtitle_disappearing_before_final_video_recycle_preserves_video(missing,tmp_path):
    async def scenario():
        store,cloud,http,engine,_,_=await seed_copy(tmp_path)
        add_subtitle(cloud);original_listing=cloud.recycle_files
        async def listing():
            row=store.db.execute("SELECT plan FROM organize_items WHERE file_id='2000' ORDER BY id DESC").fetchone()
            if row and any(s.get('ready') for s in json.loads(row['plan']).get('wash',{}).get('duplicate',{}).get('subtitles',[])):
                if missing=='cloud':cloud.nodes['2001']['parent_id']='300'
                else:next((tmp_path/'strm').rglob('*.srt')).unlink()
                cloud.recycle_files=original_listing
            return await original_listing()
        cloud.recycle_files=listing
        jid=job_for(store);await engine.run_job(jid)
        assert engine.job(jid)['state']=='partial' and not cloud.bin and '2000' in cloud.nodes
        await http.aclose();store.close()
    asyncio.run(scenario())
