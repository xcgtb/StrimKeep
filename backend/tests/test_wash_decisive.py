import asyncio
import json

from strimkeep.quality import compare, facts, group_key
from test_organize import CONFIG, MEDIA, new_job
from test_upgrade import policy, seed, upgraded_setup


def test_priority_unknown_and_tie_are_distinguished():
    old = facts('电影.WEB-DL.mkv', {**MEDIA, 'width':1920, 'height':1080}, 1000)
    new = facts('电影.WEB-DL.mkv', {**MEDIA, 'width':3840, 'height':2160}, 1000)
    p = policy('dynamic_range', 'resolution')
    result, evidence = compare({**new, 'dynamic_range':None}, old, p, skip_unknown=True)
    assert result == 1 and evidence[0]['result'] == 'unknown' and evidence[1]['result'] == 1
    result, evidence = compare(new, old, policy('resolution', 'dynamic_range'), skip_unknown=True)
    assert result == 1 and len(evidence) == 1
    result, evidence = compare({**new, 'dynamic_range':None}, {**new, 'dynamic_range':None}, p, skip_unknown=True)
    assert result == 0 and evidence[0]['result'] == 'unknown'


def test_audio_comparison_uses_known_tracks_and_skips_unknown_to_next_rule():
    p = policy('audio', 'size')
    next(r for r in p['rules'] if r['key'] == 'audio')['order'] = ['eac3', 'aac']
    known = facts('电影.mkv', {**MEDIA, 'audio': [{'codec':'eac3', 'channels':6},
                                                {'codec':None, 'channels':10}]}, 2000)
    other = facts('电影.mkv', {**MEDIA, 'audio': [{'codec':'aac', 'channels':2}]}, 1000)
    result, evidence = compare(known, other, p, skip_unknown=True)
    assert result == 1 and evidence[0]['rule'] == 'audio' and evidence[0]['result'] == 1
    unknown = {**known, 'audio': [{'codec':None, 'channels':10}]}
    result, evidence = compare(unknown, other, p, skip_unknown=True)
    assert result == 1 and evidence[0]['result'] == 'unknown' and evidence[1]['rule'] == 'size'
    result, evidence = compare(unknown, {**other, 'audio':[]}, policy('audio'), skip_unknown=True)
    assert result == 0 and evidence[0]['result'] == 'unknown'


def test_new_file_with_unknown_additional_audio_is_organized(tmp_path):
    async def scenario():
        store, cloud, http, engine, _, media = upgraded_setup(tmp_path)
        cloud.add_video('1000')
        media['1000'] = {**MEDIA, 'audio': [{'codec':'aac', 'channels':2},
                                             {'codec':None, 'channels':10}]}
        rules = policy('audio')
        next(r for r in rules['rules'] if r['key'] == 'audio')['order'] = ['eac3', 'aac']
        jid = new_job(store, {**CONFIG, 'upgrade_policy':rules})
        await engine.run_job(jid)
        row = store.db.execute('SELECT state,plan FROM organize_items WHERE job_id=?', (jid,)).fetchone()
        assert row['state'] == 'done', dict(row)
        assert 'AAC 2.0' in json.loads(row['plan'])['target_name']
        await http.aclose(); store.close()
    asyncio.run(scenario())


def test_dolby_coexist_and_single_group_follow_resolution_priority():
    dolby = facts('电影.WEB-DL.mkv', {**MEDIA, 'width':3840, 'height':2160}, 1000)
    hdr = facts('电影.WEB-DL.mkv', {**MEDIA, 'width':3840, 'height':2160, 'dynamic_range':'HDR PQ'}, 1000)
    p = policy('resolution', 'dynamic_range', dynamic_groups='dovi')
    for rule in p['rules']:
        if rule['key'] == 'dynamic_range': rule['preferences']=['dovi','hdr10','sdr']
    assert group_key(dolby, p) != group_key(hdr, p)
    p['dynamic_groups']='off'
    assert group_key(dolby, p) == group_key(hdr, p)
    low = {**dolby, 'resolution':1080}
    result, evidence = compare(hdr, low, p, skip_unknown=True)
    assert result == 1 and len(evidence) == 1 and evidence[0]['rule'] == 'resolution'


def test_unknown_new_dolby_group_keeps_known_old(tmp_path):
    async def scenario():
        store,cloud,http,engine,_,media=await seed(tmp_path)
        old=cloud.nodes['1000'].copy()
        cloud.add_video('2000')
        media['2000']={**MEDIA,'dynamic_range':'未知'}
        jid=new_job(store,{**CONFIG,'upgrade_policy':policy('resolution',dynamic_groups='dovi')})
        await engine.run_job(jid)
        row=store.db.execute('SELECT state,plan FROM organize_items WHERE job_id=?',(jid,)).fetchone()
        assert row['state']=='recycled' and cloud.nodes['1000']==old and '2000' in cloud.bin
        proof=json.loads(row['plan'])['wash']
        assert proof['reason']=='unknown_fallback'
        await http.aclose();store.close()
    asyncio.run(scenario())


def test_old_unknown_dolby_group_replaced_only_after_new_output(tmp_path):
    async def scenario():
        store,cloud,http,engine,_,media=upgraded_setup(tmp_path)
        cloud.add_video('1000')
        media['1000']={**MEDIA,'dynamic_range':'未知'}
        await engine.run_job(new_job(store))
        cloud.add_video('2000')
        media['2000']=MEDIA.copy()
        jid=new_job(store,{**CONFIG,'upgrade_policy':policy('resolution',dynamic_groups='dovi')})
        await engine.run_job(jid)
        row=store.db.execute('SELECT state,plan FROM organize_items WHERE job_id=?',(jid,)).fetchone()
        assert row['state']=='done' and '1000' in cloud.bin and '2000' in cloud.nodes
        assert (engine.root / json.loads(row['plan'])['relative_path']).is_file()
        await http.aclose();store.close()
    asyncio.run(scenario())


def test_existing_video_comparison_reuses_fingerprinted_media_without_probe(tmp_path):
    async def scenario():
        store,cloud,http,engine,_,_=upgraded_setup(tmp_path)
        source=cloud.add_video('1000');source['sha1']='A'*40
        jid=new_job(store,CONFIG)
        engine.measurements.put('1', source, MEDIA, source['name'])
        async def forbidden(*args): raise AssertionError('已缓存的旧视频不应重新探测')
        engine.probe=forbidden
        media, name, origin = engine.measurements.comparison_facts(jid, source, source['name'])
        assert origin == 'cached' and media['width']==3840 and name==source['name']
        changed={**source,'sha1':'B'*40}
        _, _, origin = engine.measurements.comparison_facts(jid, changed, source['name'])
        assert origin == 'filename'
        await http.aclose();store.close()
    asyncio.run(scenario())
