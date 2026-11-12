"""Only verified content duplicates may bypass a quality tie or unknown field."""
import asyncio
import copy
import hashlib
import json

import pytest

from strimkeep.cloud115 import CloudError
from strimkeep.organizer import Organizer
from strimkeep.quality import SUGGESTED_ORDER, UpgradePolicy
from strimkeep.store import Store
from test_organize import CONFIG
from test_upgrade import upgraded_setup, policy


HASH = 'a17b9c30584e6f2d1798a63bc51d0ef47a8c2b90'
NAME = '测试剧集 (2024) S01E03.WEB-DL-Team.mkv'


async def seed_copy(tmp_path, digest=HASH):
    store,cloud,http,engine,requests,media = upgraded_setup(tmp_path)
    cloud.add_video(name=NAME)['sha1'] = digest
    from test_organize import new_job
    await engine.run_job(new_job(store))
    cloud.add_video('2000',NAME)['sha1'] = digest.upper() if digest else ''
    return store,cloud,http,engine,requests,media


def job_for(store, selected=None):
    from test_organize import new_job
    return new_job(store,{**CONFIG,'upgrade_policy':selected or policy('resolution')})


def item(store,jid):
    return store.db.execute('SELECT * FROM organize_items WHERE job_id=?',(jid,)).fetchone()


def test_identical_copy_bypasses_all_nine_rules_including_unknown_group_preserving_old_outputs(tmp_path):
    async def scenario():
        store,cloud,http,engine,_,_=await seed_copy(tmp_path)
        old=cloud.nodes['1000'].copy()
        outputs={str(p):p.read_bytes() for p in (tmp_path/'strm').rglob('*.strm')}
        poster=next((tmp_path/'strm').rglob('*.strm')).parent/'poster.jpg';poster.write_bytes(b'plugin-poster')
        selected=UpgradePolicy().model_dump()
        selected['mode']='upgrade'
        for rule in selected['rules']:
            rule['enabled']=True
            if rule['key']=='group':rule['order']=['preferred']  # Neither filename matches this quality preference.
            elif rule['key'] in SUGGESTED_ORDER:rule['order']=SUGGESTED_ORDER[rule['key']]
        selected=UpgradePolicy.model_validate(selected).model_dump()
        jid=job_for(store,selected);await engine.run_job(jid)
        row=item(store,jid);plan=json.loads(row['plan'])
        assert row['state']=='recycled' and engine.job(jid)['state']=='completed'
        assert plan['wash']['reason']=='identical_copy'
        assert (plan['wash']['duplicate']['sha1'],plan['wash']['duplicate']['size'])==(HASH,1000)
        assert cloud.nodes['1000']==old and '2000' in cloud.bin
        assert {str(p):p.read_bytes() for p in (tmp_path/'strm').rglob('*.strm')}==outputs
        assert poster.read_bytes()==b'plugin-poster'
        assert store.db.execute("SELECT revoked FROM playbacks WHERE file_id='1000'").fetchone()[0]==0
        assert store.db.execute("SELECT count(*) FROM playbacks WHERE file_id='2000'").fetchone()[0]==0
        assert plan['wash']['actions']['2000']['state']=='confirmed'
        await http.aclose();store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('case',['same-size-different-hash','missing-old','missing-new','invalid-hash','same-hash-different-size'])
def test_quality_tie_is_not_content_equality(case,tmp_path):
    async def scenario():
        digest='' if case=='missing-old' else 'not-a-real-sha1' if case=='invalid-hash' else HASH
        store,cloud,http,engine,_,_=await seed_copy(tmp_path,digest)
        if case=='same-size-different-hash':cloud.nodes['2000']['sha1']='b'*40
        if case=='missing-new':cloud.nodes['2000']['sha1']=''
        if case=='same-hash-different-size':cloud.nodes['2000']['size']=2000
        jid=job_for(store);await engine.run_job(jid)
        row=item(store,jid)
        assert row['state']=='recycled' and '1000' in cloud.nodes and '2000' in cloud.bin
        assert json.loads(row['plan'])['wash'].get('reason') != 'identical_copy'
        await http.aclose();store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('mode',['pending','skip','legacy-upgrade'])
def test_modes_and_old_task_snapshot_do_not_gain_automatic_deduplication(mode,tmp_path):
    async def scenario():
        store,cloud,http,engine,_,_=await seed_copy(tmp_path)
        selected=policy('resolution')
        if mode=='legacy-upgrade':selected.pop('deduplicate_identical')
        else:selected['mode']=mode
        jid=job_for(store,selected);await engine.run_job(jid)
        assert '1000' in cloud.nodes
        assert item(store,jid)['state']==('skipped' if mode=='skip' else 'recycled' if mode=='legacy-upgrade' else 'pending')
        if mode == 'legacy-upgrade':
            assert json.loads(item(store,jid)['plan'])['wash']['reason'] == 'equal_rules'
        else:
            assert not cloud.bin
        await http.aclose();store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('episode',['S01E04','S01E03-E04'])
def test_hash_match_does_not_override_episode_identity_or_overlap(episode,tmp_path):
    async def scenario():
        store,cloud,http,engine,_,_=await seed_copy(tmp_path)
        cloud.nodes['2000']['name']=NAME.replace('S01E03',episode)
        jid=job_for(store);await engine.run_job(jid)
        assert not cloud.bin and '1000' in cloud.nodes and '2000' in cloud.nodes
        assert item(store,jid)['state']==('done' if episode=='S01E04' else 'pending')
        await http.aclose();store.close()
    asyncio.run(scenario())


def test_unique_new_subtitles_merge_to_original_and_local_before_video_recycle(tmp_path):
    async def scenario():
        store,cloud,http,engine,_,_=await seed_copy(tmp_path)
        content=b'1\n00:00:00,000 --> 00:00:01,000\nsubtitle\n'
        sub=cloud.add_video('2001',NAME[:-4]+'.chs.srt');sub.update(size=len(content),sha1=hashlib.sha1(content).hexdigest())
        old=cloud.nodes['1000'].copy()
        jid=job_for(store);await engine.run_job(jid); await engine.emby.drain_outbox(); await engine.emby.tick(); await engine.emby.drain_outbox(); await engine.emby.tick()
        assert item(store,jid)['state']=='recycled' and engine.job(jid)['state']=='completed'
        assert '2000' in cloud.bin and '2001' in cloud.bin and cloud.nodes['1000']==old
        assert cloud.writes.index(('recycle','2000')) < cloud.writes.index(('recycle','2001'))
        assert not list((tmp_path/'strm').rglob('*.srt'))
        await http.aclose();store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('which',['1000','2000'])
def test_missing_fingerprint_at_final_check_stops_recycle_and_can_resume(which,tmp_path):
    async def scenario():
        store,cloud,http,engine,_,_=await seed_copy(tmp_path)
        original_listing=cloud.recycle_files
        async def interrupt():
            # Preflight occurs before a plan exists; mutate only after proof has
            # been saved, at the per-file recycle baseline read.
            row=store.db.execute('SELECT plan FROM organize_items WHERE file_id=? ORDER BY id DESC',('2000',)).fetchone()
            if row and json.loads(row['plan']).get('wash',{}).get('reason')=='identical_copy':
                cloud.nodes[which]['sha1']='';cloud.recycle_files=original_listing
            return await original_listing()
        cloud.recycle_files=interrupt
        jid=job_for(store);await engine.run_job(jid)
        assert engine.job(jid)['state']=='partial' and not cloud.bin
        cloud.nodes[which]['sha1']=HASH
        await engine.run_job(jid)
        assert item(store,jid)['state']=='recycled' and '1000' in cloud.nodes
        assert cloud.writes.count(('recycle','2000'))==1
        await http.aclose();store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('case',['lost-response','restart-after-recycle','uncertain-result'])
def test_duplicate_recycle_keeps_durable_recovery_and_never_replays(case,tmp_path):
    async def scenario():
        store,cloud,http,engine,_,_=await seed_copy(tmp_path)
        jid=job_for(store)
        calls=[]
        if case=='uncertain-result':
            async def unknown(fid,parent):calls.append(fid);raise CloudError('结果不明')
            cloud.recycle=unknown
        else:
            error=asyncio.CancelledError() if case=='restart-after-recycle' else CloudError('响应丢失')
            cloud.after_recycle=lambda:(_ for _ in ()).throw(error)
        try:await engine.run_job(jid)
        except asyncio.CancelledError:pass
        store.close();store=Store(tmp_path/'data','admin','test-password-1234');cloud.store=store
        restarted=Organizer(store,cloud,http,tmp_path/'strm',engine.probe)
        await restarted.run_job(jid)
        if case=='uncertain-result':
            assert calls==['2000'] and restarted.job(jid)['state']=='partial'
            assert '1000' in cloud.nodes and '2000' in cloud.nodes
        else:
            assert item(store,jid)['state']=='recycled' and '1000' in cloud.nodes
            assert cloud.writes.count(('recycle','2000'))==1
        await http.aclose();store.close()
    asyncio.run(scenario())
