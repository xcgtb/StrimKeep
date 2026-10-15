"""Real SQLite + owned STRM + stateful remote; replacement is never inferred by path."""

import copy
import json
from pathlib import Path
import pytest
from dev.tests.test_cloud_workbench import cloud
from dev.tests.test_my_drive import client
from app.cloud import workflow, settings, jobs, store, strm, replacement, governance, automation
from app.cloud.errors import CloudError


@pytest.fixture
def versions(cloud, monkeypatch):
    cloud.remote.files['4']['name'] = 'Movie.2024.1080p.SDR.mkv'
    cloud.remote.files['5']['name'] = 'Movie.2024.1080p.SDR.zh.srt'
    deleted, proof_fail = {}, set()
    def delete(fid):
        deleted[fid] = cloud.remote.files.pop(fid)
        cloud.remote.writes.append(('delete', fid))
    def proof(fid, source, acknowledged=False):
        if fid in proof_fail:
            return None
        if fid in deleted and fid not in cloud.remote.files:
            return {'file_id': fid, 'acknowledged': acknowledged, 'parent_complete_absent': True}
        return None
    monkeypatch.setattr(cloud.remote, 'delete', delete, raising=False)
    monkeypatch.setattr(cloud.remote, 'removed_receipt', proof, raising=False)
    monkeypatch.setattr(cloud.remote, 'require', lambda op: None, raising=False)
    cloud.deleted, cloud.proof_fail = deleted, proof_fail
    return cloud


def execute(plan, generate=True):
    jid = workflow.apply(plan['id'], True, generate)
    jobs.execute(jid)
    return store.job(jid)


def seed(cloud):
    plan = workflow.plan(cloud.id, '1', '2')
    result = execute(plan)
    assert result['state'] == 'success'
    aid = result['result']['generated'][0]['asset_id']
    return store.asset(aid)


def incoming(cloud, name='Movie.2024.2160p.SDR.mkv', fid='6', size=200):
    cloud.remote.files[fid] = {**cloud.remote.info('4'), 'id': fid, 'parent_id': '1', 'name': name, 'size': size}


def enable(**patch):
    settings.save({'quality8_enabled': True, 'quality8_mode': 'upgrade', 'official_delete_enabled': True, **patch})


def test_upgrade_publishes_new_before_deleting_exact_old_sources_and_owned_files(versions, monkeypatch):
    old = seed(versions);old_path = Path(old['local_path']);incoming(versions);enable()
    generate = strm.generate
    def check_order(*args, **kwargs):
        assert old_path.exists() and '4' in versions.remote.files and '5' in versions.remote.files
        return generate(*args, **kwargs)
    monkeypatch.setattr(strm, 'generate', check_order)
    plan = workflow.plan(versions.id, '1', '2')
    assert len(plan['items'][0]['upgrades']) == 1 and not plan['quality8']['preview_only']
    assert not [w for w in versions.remote.writes if w[0] == 'delete']
    result = execute(plan)
    assert result['state'] == 'success' and result['result']['upgrades'][0]['state'] == 'done'
    assert '4' not in versions.remote.files and '5' not in versions.remote.files
    assert '6' in versions.remote.files and not old_path.exists()
    assert store.asset(old['id'])['state'] == 'revoked'
    assert len(list(versions.root.rglob('*.strm'))) == 1
    assert '非杜比' in result['result']['generated'][0]['path']


@pytest.mark.parametrize('name,size', [('Movie.2024.720p.SDR.mkv', 9999), ('Movie.2024.1080p.SDR.mkv', 100)])
def test_lower_and_tied_versions_remain_in_source_without_delete(versions, name, size):
    old = seed(versions);incoming(versions,name,size=size);enable()
    plan = workflow.plan(versions.id, '1', '2')
    assert not plan['items'] and plan['quality_held'] and not plan['quality_held'][0]['blocked']
    assert versions.remote.info('6')['parent_id'] == '1' and Path(old['local_path']).exists()
    assert not versions.deleted


def test_zero_byte_higher_resolution_placeholder_cannot_replace_real_video(versions):
    seed(versions);incoming(versions,size=0);enable()
    plan=workflow.plan(versions.id,'1','2')
    assert not plan['items'] and plan['quality_held'][0]['blocked'] and not versions.deleted


def test_batch_selects_best_even_when_templates_produce_same_name(versions):
    seed(versions);incoming(versions);incoming(versions,'Movie.2024.2160p.SDR.B.mkv','7',300);enable()
    plan = workflow.plan(versions.id,'1','2')
    assert [i['source']['id'] for i in plan['items']] == ['7']
    assert plan['quality_held'][0]['file_id'] == '6'
    assert execute(plan)['state'] == 'success'
    assert versions.remote.info('6')['parent_id'] == '1'


def test_dv_non_dv_and_unknown_have_separate_paths_and_only_same_bucket_is_replaced(versions):
    old = seed(versions);incoming(versions,'Movie.2024.2160p.DV.mkv');enable()
    plan = workflow.plan(versions.id,'1','2')
    assert not plan['items'][0]['upgrades'] and '杜比' in plan['items'][0]['media']['directory']
    first = execute(plan);assert first['state'] == 'success' and not versions.deleted
    dv_id = first['result']['generated'][0]['asset_id']
    incoming(versions,'Movie.2024.2160p.SDR.mkv','7',300)
    assert execute(workflow.plan(versions.id,'1','2'))['state'] == 'success'
    assert store.asset(old['id'])['state'] == 'revoked' and store.asset(dv_id)['state'] == 'active'
    assert '6' in versions.remote.files and '7' in versions.remote.files
    # A dynamic-range unknown is not assumed to be non-DV.
    versions.remote.files['8'] = {**versions.remote.info('7'), 'id': '8', 'parent_id': '1', 'name': 'Movie.2024.2160p.mkv'}
    plan = workflow.plan(versions.id,'1','2')
    assert not plan['items'][0]['upgrades'] and '动态范围未知' in plan['items'][0]['media']['directory']
    assert execute(plan)['state'] == 'success'


def test_same_name_uses_new_version_directory_and_preserves_custom_filename(versions):
    settings.save({'naming_style':'template','movie_file_template':'{title}{ext}'})
    old = seed(versions);incoming(versions);enable(quality8_coexist_dv=False)
    plan = workflow.plan(versions.id,'1','2')
    assert plan['items'][0]['media']['directory'].endswith('版本-6')
    assert plan['items'][0]['media']['name'] == 'Movie.mkv'
    assert execute(plan)['state'] == 'success' and not Path(old['local_path']).exists()


def test_failed_publication_does_not_delete_old(versions, monkeypatch):
    old = seed(versions);incoming(versions);enable();plan=workflow.plan(versions.id,'1','2')
    monkeypatch.setattr(strm,'generate',lambda *a,**kw: (_ for _ in ()).throw(CloudError('写入失败',409)))
    result=execute(plan)
    assert result['state']=='needs_review' and not versions.deleted and Path(old['local_path']).exists()


def test_old_change_after_preview_stops_before_moving_new(versions):
    seed(versions);incoming(versions);enable();plan=workflow.plan(versions.id,'1','2')
    versions.remote.files['4']['size'] += 1
    result=execute(plan)
    assert result['state']=='needs_review' and versions.remote.info('6')['parent_id']=='1' and not versions.deleted


def test_modified_old_strm_blocks_preview_and_foreign_metadata_is_preserved(versions):
    old = seed(versions);incoming(versions);enable();oldpath=Path(old['local_path']);content=oldpath.read_bytes()
    oldpath.write_text('foreign')
    plan=workflow.plan(versions.id,'1','2');assert not plan['items'] and plan['quality_held'][0]['blocked']
    oldpath.write_bytes(content)
    metadata=Path(old['payload']['owned'][0]['path']);metadata.write_text('foreign metadata')
    result=execute(workflow.plan(versions.id,'1','2'))
    assert result['state']=='success' and metadata.read_text()=='foreign metadata'
    assert str(metadata) in result['result']['upgrades'][0]['retained_sidecars']


def test_partial_delete_receipt_resume_never_resends_old_request(versions, client):
    old=seed(versions);incoming(versions);enable();versions.proof_fail.add('4')
    result=execute(workflow.plan(versions.id,'1','2'));jid=result['id']
    assert result['state']=='uncertain' and Path(old['local_path']).exists() and '5' in versions.remote.files
    assert '4' not in versions.remote.files
    assert client.post(f'/api/cloud/jobs/{jid}/upgrades/continue',json={'confirmed':1}).status_code==400
    assert client.post(f'/api/cloud/jobs/{jid}/upgrades/continue',json={'confirmed':True}).status_code==409
    assert len([w for w in versions.remote.writes if w==('delete','4')])==1
    versions.proof_fail.clear()
    response=client.post(f'/api/cloud/jobs/{jid}/upgrades/continue',json={'confirmed':True})
    assert response.status_code==200 and response.json()['upgrades'][0]['state']=='done'
    assert not Path(old['local_path']).exists()
    assert len([w for w in versions.remote.writes if w==('delete','4')])==1
    assert len([w for w in versions.remote.writes if w==('delete','5')])==1
    assert client.post(f'/api/cloud/jobs/{jid}/upgrades/continue',json={'confirmed':True}).status_code==200


def test_delayed_upgrade_keeps_old_until_new_strm_is_due_and_published(versions):
    old=seed(versions);incoming(versions);enable(strm_delay_seconds=60)
    result=execute(workflow.plan(versions.id,'1','2'))
    assert result['state']=='success' and not versions.deleted and Path(old['local_path']).exists()
    jid=result['result']['deferred'][0]['job_id']
    with store.db() as con:
        con.execute('UPDATE cloud_job_due SET due=0 WHERE job_id=?',(jid,))
    jobs.execute(jid)
    assert store.job(jid)['state']=='success' and store.job(jid)['result']['upgrades'][0]['state']=='done'
    assert not Path(old['local_path']).exists()


def test_delayed_old_change_keeps_both_and_records_recovery(versions):
    old=seed(versions);incoming(versions);enable(strm_delay_seconds=60)
    result=execute(workflow.plan(versions.id,'1','2'));jid=result['result']['deferred'][0]['job_id']
    versions.remote.files['4']['size']+=1
    with store.db() as con:
        con.execute('UPDATE cloud_job_due SET due=0 WHERE job_id=?',(jid,))
    jobs.execute(jid)
    assert store.job(jid)['state']=='failed' and not versions.deleted and Path(old['local_path']).exists()
    assert replacement.summary(jid)[0]['state']=='needs_review'


def test_switching_off_execution_before_delayed_publication_prevents_old_deletion(versions):
    old=seed(versions);incoming(versions);enable(strm_delay_seconds=60)
    result=execute(workflow.plan(versions.id,'1','2'));jid=result['result']['deferred'][0]['job_id']
    settings.save({'quality8_mode':'preview'})
    with store.db() as con:
        con.execute('UPDATE cloud_job_due SET due=0 WHERE job_id=?',(jid,))
    jobs.execute(jid)
    assert not versions.deleted and Path(old['local_path']).exists()
    assert replacement.summary(jid)[0]['state']=='needs_review'


def test_upgrade_requires_new_strm_and_snapshot_changes_invalidate_plan(versions):
    seed(versions);incoming(versions);enable();plan=workflow.plan(versions.id,'1','2')
    with pytest.raises(CloudError,match='不能关闭生成'):
        workflow.apply(plan['id'],True,False)
    settings.save({'quality8_mode':'preview'})
    with pytest.raises(CloudError,match='重新预览'):
        workflow.apply(plan['id'],True)


def test_preview_mode_never_selects_for_deletion_or_changes_layout(versions):
    seed(versions);incoming(versions);settings.save({'quality8_enabled':True})
    plan=workflow.plan(versions.id,'1','2')
    assert 'upgrades' not in plan['items'][0] and plan['quality8']['preview_only']
    assert '非杜比' not in plan['items'][0]['media']['directory']
    assert execute(plan)['state']=='success' and not versions.deleted


def test_registered_companion_shared_by_other_asset_blocks_replacement(versions):
    old=seed(versions);incoming(versions);enable()
    with store.db() as con:
        other=copy.deepcopy(old['payload']);other['source']={**other['source'],'id':'99'}
        other['media']['tmdb_id']='999'
        con.execute('INSERT INTO cloud_assets VALUES(?,?,?,?,?,?,?,?)',('9'*32,versions.id,'99',str(versions.root/'Other.strm'),store.encode(other),'nonce','active',0))
    plan=workflow.plan(versions.id,'1','2')
    assert not plan['items'] and '其他播放引用' in plan['quality_held'][0]['reason'] and not versions.deleted


def test_subsequent_sync_preserves_version_layout_even_after_preview_mode_restored(versions):
    seed(versions);incoming(versions);enable();result=execute(workflow.plan(versions.id,'1','2'))
    aid=result['result']['generated'][0]['asset_id'];path=store.asset(aid)['local_path']
    settings.save({'quality8_mode':'preview'})
    jid=jobs.enqueue('sync',{'account_id':versions.id,'account_revision':store.account(versions.id)['revision'],'source_cid':'2'})
    jobs.execute(jid)
    assert store.job(jid)['state']=='success' and store.asset(aid)['local_path']==path


def test_legacy_registered_layout_is_not_migrated_just_by_enabling_execution(versions):
    old=seed(versions);enable()
    jid=jobs.enqueue('sync',{'account_id':versions.id,'account_revision':store.account(versions.id)['revision'],'source_cid':'2'})
    jobs.execute(jid)
    assert store.job(jid)['state']=='success' and store.asset(old['id'])['local_path']==old['local_path']


def test_revoking_old_reference_after_preview_prevents_source_deletion(versions):
    old=seed(versions);incoming(versions);enable();plan=workflow.plan(versions.id,'1','2')
    strm.revoke(old['id'])
    result=execute(plan)
    assert result['state']=='needs_review' and not versions.deleted and versions.remote.info('6')['parent_id']=='1'


def test_new_owned_metadata_changed_after_publication_blocks_old_deletion(versions,monkeypatch):
    old=seed(versions);incoming(versions);enable();generate=strm.generate
    def changed(*a,**kw):
        asset=generate(*a,**kw);Path(asset['payload']['owned'][0]['path']).write_text('changed')
        return asset
    monkeypatch.setattr(strm,'generate',changed)
    result=execute(workflow.plan(versions.id,'1','2'))
    assert result['state']=='needs_review' and not versions.deleted and Path(old['local_path']).exists()


def test_multiple_old_versions_replace_only_after_all_new_checks(versions):
    old=seed(versions)
    # A second registered version is created using the existing preview-only organizer.
    incoming(versions,'Movie.2024.720p.SDR.mkv','7',50)
    second=execute(workflow.plan(versions.id,'1','2'))
    second_id=second['result']['generated'][0]['asset_id']
    incoming(versions);enable()
    plan=workflow.plan(versions.id,'1','2')
    assert len(plan['items'][0]['upgrades'])==2
    result=execute(plan)
    assert result['state']=='success' and len(result['result']['upgrades'])==2
    assert store.asset(old['id'])['state']==store.asset(second_id)['state']=='revoked'


@pytest.mark.parametrize('change',['source','ancestor','cookie_flag','sha1'])
def test_preflight_blocks_changed_scope_authorization_and_incomplete_fingerprint(versions,change):
    seed(versions);incoming(versions);enable()
    if change=='source':
        versions.remote.files['4']['name']='Renamed.mkv'
    elif change=='ancestor':
        parent=versions.remote.info('4')['parent_id'];versions.remote.files[parent]['parent_id']='0'
    elif change=='cookie_flag':
        settings.save({'official_delete_enabled':False})
    else:
        versions.remote.files['6']['sha1']=''
    plan=workflow.plan(versions.id,'1','2')
    assert not plan['items'] and plan['quality_held'][0]['blocked'] and not versions.deleted


def test_pending_delayed_same_identity_blocks_a_second_replacement(versions):
    seed(versions);incoming(versions);enable(strm_delay_seconds=60)
    execute(workflow.plan(versions.id,'1','2'))
    incoming(versions,'Movie.2024.2160p.SDR.B.mkv','7',300)
    plan=workflow.plan(versions.id,'1','2')
    assert not plan['items'] and '延时发布任务' in plan['quality_held'][0]['reason'] and not versions.deleted


def test_publication_recovery_refuses_changed_new_source_and_preserves_old_strm(versions,client):
    old=seed(versions);incoming(versions);enable();versions.proof_fail.add('4')
    result=execute(workflow.plan(versions.id,'1','2'));jid=result['id'];versions.proof_fail.clear()
    versions.remote.files['6']['size']+=1
    response=client.post(f'/api/cloud/jobs/{jid}/upgrades/continue',json={'confirmed':True})
    assert response.status_code==409 and Path(old['local_path']).exists() and '5' in versions.remote.files


def test_local_prune_failure_can_resume_after_verified_sources_without_redelete(versions,client,monkeypatch):
    old=seed(versions);incoming(versions);enable();prune=replacement.prune
    monkeypatch.setattr(replacement,'prune',lambda ref: (_ for _ in ()).throw(OSError('test mount')))
    result=execute(workflow.plan(versions.id,'1','2'));jid=result['id']
    assert result['state']=='needs_review' and Path(old['local_path']).exists()
    assert replacement.summary(jid)[0]['remote_verified']
    monkeypatch.setattr(replacement,'prune',prune)
    assert client.post(f'/api/cloud/jobs/{jid}/upgrades/continue',json={'confirmed':True}).status_code==200
    assert len([w for w in versions.remote.writes if w[0]=='delete'])==2
    assert not Path(old['local_path']).exists()


@pytest.mark.parametrize('patch',[{'quality8_mode':'other'},{'quality8_mode':'upgrade','quality8_enabled':False}])
def test_invalid_execution_mode_rejected(versions,patch):
    with pytest.raises(CloudError):
        settings.save(patch)
