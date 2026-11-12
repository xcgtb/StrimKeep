"""Editor options must control actual selection; legacy task snapshots survive."""
import asyncio
import copy
import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from strimkeep.main import create_app
from strimkeep.media import NeedsReview, technical_name
from strimkeep.quality import KEYS, Rule, UpgradePolicy, compare, facts, public_policy
from test_app import FakeCloud
from test_organize import CONFIG, MEDIA, new_job
from test_upgrade import policy, seed


def ordered(key, options, *following):
    result = policy(key, *following)
    next(r for r in result['rules'] if r['key'] == key)['order'] = options
    return UpgradePolicy.model_validate(result).model_dump()


def test_nine_rules_only_and_defaults_are_off():
    value = public_policy()
    assert [r['key'] for r in value['rules']] == list(KEYS)
    assert len(KEYS) == 9 and 'revision' not in KEYS
    assert value['dynamic_groups'] == 'off' and value['season_layout'] == 'standard'
    assert not value['resolution_groups'] and not value['fps_groups']
    assert not any(r['enabled'] for r in value['rules'])


@pytest.mark.parametrize('feature', ['resolution', 'fps', 'three', 'spec', 'revision'])
def test_removed_settings_do_not_rewrite_legacy_jobs_or_silently_enable_new_elimination(feature):
    old = policy('size', 'revision')
    next(r for r in old['rules'] if r['key'] == 'revision')['enabled'] = feature == 'revision'
    if feature == 'resolution': old['resolution_groups'] = True
    if feature == 'fps': old['fps_groups'] = True
    if feature == 'three': old['dynamic_groups'] = 'three'
    if feature == 'spec': old.update(season_layout='spec', dynamic_groups='dovi')
    snapshot = copy.deepcopy(old)
    simplified = public_policy(old)
    assert simplified['mode'] == 'pending' and len(simplified['rules']) == 9
    assert old == snapshot and len(UpgradePolicy.model_validate(old).rules) == 10


@pytest.mark.parametrize('key,left,right,preference', [
    ('resolution', {'resolution':1080}, {'resolution':2160}, ['1080','2160']),
    ('fps', {'fps':24}, {'fps':60}, ['24','60']),
    ('bit_depth', {'bit_depth':8}, {'bit_depth':10}, ['8','10']),
    ('codec', {'codec':'h264'}, {'codec':'hevc'}, ['h264','hevc']),
    ('group', {'group':'preferred'}, {'group':'other'}, ['preferred','other']),
    ('dynamic_range', {'dynamic_range':'sdr'}, {'dynamic_range':'dovi','dv_profile':7}, ['sdr','dovi-p7']),
])
def test_custom_order_controls_real_comparison_and_unknown_never_skips_to_size(key,left,right,preference):
    baseline = facts('Movie.2024.WEB-DL-Team.mkv',MEDIA,1000)
    new, old = {**baseline,**left}, {**baseline,**right}
    assert compare(new,old,ordered(key,preference))[0] == 1
    assert compare(new,old,ordered(key,list(reversed(preference))))[0] == -1
    with pytest.raises(NeedsReview): compare({**new,key:None},old,ordered(key,preference,'size'))


def test_sources_and_platforms_have_one_explicit_order():
    selected = ordered('source',['web-dl:nf','uhd-remux','web-dl:amzn','web-dl:other'])
    names = ['NF.WEB-DL', 'UHD.Remux', 'AMZN.WEB-DL', 'WEB-DL']
    items = [facts(f'Movie.2024.{n}-Team.mkv',MEDIA,1000) for n in names]
    for first,second in zip(items,items[1:]): assert compare(first,second,selected)[0] == 1
    ambiguous = facts('Movie.2024.NF.AMZN.WEB-DL-Team.mkv',MEDIA,1000)
    with pytest.raises(NeedsReview,match='多个平台'): compare(ambiguous,items[0],selected)
    with pytest.raises(NeedsReview): compare({**items[0],'source':None},items[-1],selected)


@pytest.mark.parametrize('token,expected', [('IQIYI','iq'),('Hulu','hulu'),('Bilibili','bili'),('WeTV','wetv'),('PMTP','pmtp')])
def test_platform_aliases(token,expected):
    assert facts(f'Movie.2024.{token}.WEB-DL-Team.mkv',MEDIA,1000)['platform'] == expected


def test_audio_custom_order_and_1440p():
    new = facts('Movie.2024.WEB-DL.mkv',{**MEDIA,'width':2560,'height':1440,'audio':[{'codec':'eac3','channels':8}]},1000)
    old = {**new,'audio':[{'codec':'truehd','channels':6}]}
    assert new['resolution'] == 1440
    assert technical_name({**MEDIA,'width':2560,'height':1440},'Movie.WEB-DL.mkv').startswith('1440p.')
    assert compare(new,old,ordered('audio',['eac3','truehd']))[0] == 1
    with pytest.raises(NeedsReview):
        compare({**new,'audio':[{'codec':None,'channels':6}]},old,ordered('audio',['eac3','truehd'],'size'))


@pytest.mark.parametrize('bad', [
    {'key':'codec','order':['hevc','HEVC']}, {'key':'codec','order':['unknown']},
    {'key':'codec','order':[['hevc','h264']]}, {'key':'codec','order':['hevc'],'preferences':['h264']},
    {'key':'source','order':['web-dl','web-dl:nf']}, {'key':'size','order':['big','small']},
    {'key':'codec','tiers':[['hevc','h264']]},
])
def test_parallel_or_ambiguous_orders_are_rejected(bad):
    with pytest.raises(ValidationError): Rule.model_validate(bad)


def test_api_nine_rules_flat_catalogue_rejects_removed_controls_and_snapshots_order(tmp_path,monkeypatch):
    monkeypatch.setenv('ADMIN_USERNAME','admin');monkeypatch.setenv('ADMIN_PASSWORD','test-password-1234')
    app = create_app(tmp_path/'data',tmp_path/'strm',FakeCloud)
    with TestClient(app) as client:
        csrf=client.post('/api/v1/auth/login',json={'username':'admin','password':'test-password-1234'}).json()['csrf']
        client.headers['X-CSRF-Token']=csrf
        data=client.get('/api/v1/upgrade/config').json()
        assert len(data['labels']) == 9 and 'revision' not in data['labels']
        assert not any(isinstance(v,list) for v in data['suggested_order']['audio'])
        selected=ordered('resolution',['1080','2160'])
        assert client.put('/api/v1/upgrade/config',json=selected).status_code == 200
        app.state.store.set_many({'cookie':'UID=1_A1; CID=test; SEID=test','tmdb_key':'private','playback_base':'https://play.example','organize_config':CONFIG})
        job=client.post('/api/v1/organize/jobs').json()
        assert client.put('/api/v1/upgrade/config',json=policy('size','revision')).status_code == 422
        # 分辨率 / 帧率 / 三档动态范围共存已移除：旧字段仍在模型里兼容，提交后静默收敛为零。
        for legacy in (policy('size',resolution_groups=True),policy('size',fps_groups=True),policy('size',dynamic_groups='three')):
            assert client.put('/api/v1/upgrade/config',json=legacy).status_code == 200
        assert client.put('/api/v1/upgrade/config',json=ordered('resolution',['2160','1080'])).status_code == 200
        assert json.loads(app.state.organizer.job(job['id'])['config'])['upgrade_policy'] == selected


def test_actual_pipeline_obeys_custom_1080p_preference_instead_of_implicit_bigger_wins(tmp_path):
    async def scenario():
        store,cloud,http,engine,_,media=await seed(tmp_path)
        cloud.add_video('2000')
        media['1000']={**MEDIA,'width':3840,'height':2160}
        media['2000']={**MEDIA,'width':1920,'height':1080}
        jid=new_job(store,{**CONFIG,'upgrade_policy':ordered('resolution',['1080','2160'])})
        await engine.run_job(jid)
        assert engine.job(jid)['state']=='completed',dict(engine.job(jid))
        assert '1000' in cloud.bin and '2000' in cloud.nodes
        await http.aclose();store.close()
    asyncio.run(scenario())


def test_dovi_non_dovi_coexist_and_unknown_protects_both(tmp_path):
    async def scenario():
        store,cloud,http,engine,_,media=await seed(tmp_path)
        cloud.add_video('2000');media['2000']={**MEDIA,'dynamic_range':'SDR'}
        jid=new_job(store,{**CONFIG,'upgrade_policy':policy('size',dynamic_groups='dovi')})
        await engine.run_job(jid)
        assert engine.job(jid)['state']=='completed' and not cloud.bin
        cloud.add_video('3000');media['3000']={**MEDIA,'dynamic_range':'未知'}
        jid2=new_job(store,{**CONFIG,'upgrade_policy':policy('size',dynamic_groups='dovi')})
        await engine.run_job(jid2)
        assert '3000' in cloud.bin and all(fid in cloud.nodes for fid in ('1000','2000'))
        assert store.db.execute('SELECT state FROM organize_items WHERE job_id=?',(jid2,)).fetchone()[0]=='recycled'
        await http.aclose();store.close()
    asyncio.run(scenario())
