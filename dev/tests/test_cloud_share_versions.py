"""Non-destructive share-library version choice, real SQLite and signed local bytes."""

import copy
import json
import time
from pathlib import Path

from dev.tests.test_cloud_workbench import cloud
from app.cloud import share_versions, settings, store, strm


def media(name):
    return {'kind':'movie','tmdb_id':'44','season':None,'episode':None,
            'name':'Film.2024.mkv','directory':'电影/Film (2024)','quality_source_name':name}


def item(file_id, resolution):
    name='Film.2024.'+resolution+'.mkv'
    return {'source':{'id':file_id,'parent_id':'0','name':name,'size':1000,
                      'sha1':'A'*40,'is_dir':False},'media':media(name),'followers':[]}


def cfg():
    config=copy.deepcopy(settings.DEFAULTS)
    config['quality8_enabled']=True
    config['quality8_mode']='upgrade'
    return config


def old_asset(cloud,base,kind='1080p',fid='40'):
    row=item(fid,kind)
    path=base / '旧版' / 'film.strm'
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text('signed-fake-file\n')
    payload={'source_kind':'share','output_root':str(base),
             'root_identity':[base.stat().st_dev,base.stat().st_ino],
             'source':row['source'],'media':row['media'],
             'digest':strm.digest(path.read_bytes())}
    with store.db() as con:
        con.execute('INSERT INTO cloud_assets VALUES(?,?,?,?,?,?,?,?)',
                    ('a'*32,cloud.id,'share:old:'+fid,str(path),store.encode(payload),'b'*48,'active',time.time()))
    return path


def test_disabled_mode_never_changes_default_share_publication(cloud,tmp_path):
    rows=[item('4','1080p'),item('5','2160p')]
    result,held=share_versions.select(rows,cloud.id,settings.DEFAULTS,tmp_path,'s1')
    assert result is rows and not held


def test_only_best_candidate_is_generated_and_origins_unchanged(cloud,tmp_path):
    rows=[item('4','1080p'),item('5','2160p')]
    original=copy.deepcopy(rows)
    selected,held=share_versions.select(rows,cloud.id,cfg(),tmp_path,'s1')
    assert len(selected)==1 and selected[0]['source']['id']=='5'
    assert len(held)==1 and held[0]['file_id']=='4'
    assert rows[0]==original[0]
    assert not list(tmp_path.rglob('*.strm'))
    assert not cloud.remote.writes


def test_old_better_or_tied_blocks_new_without_remote_deletion(cloud,tmp_path):
    old=old_asset(cloud,tmp_path,'2160p')
    selected,held=share_versions.select([item('5','1080p')],cloud.id,cfg(),tmp_path,'s1')
    assert not selected and '未优于' in held[0]['reason']
    selected,held=share_versions.select([item('5','2160p')],cloud.id,cfg(),tmp_path,'s1')
    assert not selected and '平局' in held[0]['reason']
    assert old.read_text()=='signed-fake-file\n' and not cloud.remote.writes


def test_new_better_publishes_to_independent_virtual_version_folder(cloud,tmp_path):
    old=old_asset(cloud,tmp_path,'1080p')
    selected,held=share_versions.select([item('5','2160p')],cloud.id,cfg(),tmp_path,'s1')
    assert len(selected)==1 and not held
    assert '版本-' in selected[0]['media']['directory']
    assert '人工' in selected[0]['quality_decision']
    assert old.exists() and old.read_text()=='signed-fake-file\n'
    assert not cloud.remote.writes


def test_modified_old_strm_fails_closed(cloud,tmp_path):
    old=old_asset(cloud,tmp_path,'1080p')
    old.write_text('modified reference')
    selected,held=share_versions.select([item('5','2160p')],cloud.id,cfg(),tmp_path,'s1')
    assert not selected and '发生变化' in held[0]['reason']
    assert old.read_text()=='modified reference'


def test_unready_existing_asset_blocks_version_selection(cloud,tmp_path):
    old_asset(cloud,tmp_path)
    with store.db() as con:
        con.execute('UPDATE cloud_assets SET state=?',('publishing',))
    selected,held=share_versions.select([item('5','2160p')],cloud.id,cfg(),tmp_path,'s1')
    assert not selected and '未就绪' in held[0]['reason']


def test_publication_recheck_stops_if_existing_asset_changed_or_new_one_appeared(cloud,tmp_path):
    old=old_asset(cloud,tmp_path,'1080p')
    configuration=cfg()
    chosen,held=share_versions.select([item('5','2160p')],cloud.id,configuration,tmp_path,'s1')
    assert chosen and not held
    share_versions.recheck(chosen[0],cloud.id,configuration,tmp_path)
    old.write_text('changed by another process')
    from app.cloud.errors import CloudError
    import pytest
    with pytest.raises(CloudError,match='变化'):
        share_versions.recheck(chosen[0],cloud.id,configuration,tmp_path)


def test_empty_existing_guard_detects_new_post_preview_version(cloud,tmp_path):
    chosen,held=share_versions.select([item('5','2160p')],cloud.id,cfg(),tmp_path,'s1')
    assert chosen and chosen[0]['quality_guard']==[]
    old_asset(cloud,tmp_path,'1080p')
    from app.cloud.errors import CloudError
    import pytest
    with pytest.raises(CloudError,match='变化'):
        share_versions.recheck(chosen[0],cloud.id,cfg(),tmp_path)


def test_share_preview_and_real_strm_publication_respect_batch_quality_decision(cloud,monkeypatch,tmp_path):
    """Exercises share_library.preview -> queue -> signed local file with remote read-only adapter."""
    from app.cloud import share_library, workflow, jobs, recognition
    base=tmp_path/'share_versions_output'
    base.mkdir()
    settings.save({'share_output_root':str(base),'quality8_enabled':True,'quality8_mode':'upgrade',
                   'nfo_enabled':False,'artwork_enabled':False})
    low=item('4','1080p')['source']
    high=item('6','2160p')['source']
    rows=[low,high]
    def page(code,password,parent='0',offset=0):
        assert code=='secretshare'
        selected=[r.copy() for r in rows if r['parent_id']==parent]
        return {'items':selected[offset:offset+100],'total':len(selected),'offset':offset,'has_more':False}
    monkeypatch.setattr(cloud.remote,'share_page',page,raising=False)
    monkeypatch.setattr(recognition,'recognize',lambda name,*args,**kwargs: media(name))
    first=workflow.preview_share(cloud.id,'secretshare','abcd')
    plan=share_library.preview(first['preview_id'],['4','6'])
    assert [x['source']['id'] for x in plan['items']]==['6']
    assert not plan['skipped'] and len(plan['quality_held'])==1 and plan['quality_held'][0]['file_id']=='4'
    jid=share_library.apply(plan['id'],True)
    jobs.execute(jid)
    result=store.job(jid)
    assert result['state']=='success', result
    created=result['result']['generated']
    assert len(created)==1
    assert result['result']['quality_held'] and not result['result']['skipped']
    asset=store.asset(created[0]['asset_id'])
    assert Path(asset['local_path']).is_relative_to(base)
    assert '/api/cloud/play/' in Path(asset['local_path']).read_text()
    assert not cloud.remote.writes
