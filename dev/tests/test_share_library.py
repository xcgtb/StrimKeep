"""Read-only remote fixture, real SQLite, signed references and NAS ownership."""

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from p115client.type import P115URL
from dev.tests.test_cloud_workbench import cloud
from dev.tests.test_my_drive import client
from app.cloud import share_library as shares, workflow, jobs, store, settings, strm, playback, governance, provider115, emby_proxy, media_probe
from app.cloud.errors import CloudError


@pytest.fixture
def shared(cloud, monkeypatch, tmp_path):
    root = tmp_path / "share"
    root.mkdir()
    settings.save({"share_output_root": str(root), "share_emby_output_root": "/share"})
    cloud.share_root = root
    cloud.shared = {"4": {**cloud.remote.info("4"), "parent_id": "0"}}
    cloud.share_requests, cloud.share_downloads = [], []
    def page(code, password, parent="0", offset=0):
        cloud.share_requests.append((code, password, parent, offset))
        rows = [copy.deepcopy(r) for r in cloud.shared.values() if r["parent_id"] == parent]
        return {"items": rows[offset:offset+100], "total": len(rows), "offset": offset, "has_more": offset+100 < len(rows)}
    def download(code, password, fid, ua):
        cloud.share_downloads.append((code, password, fid, ua))
        return {"url": "https://download.115cdn.com/shared?t=9999999999", "headers": {"User-Agent": ua}, "size": cloud.shared[fid]["size"]}
    monkeypatch.setattr(cloud.remote, "share_page", page)
    monkeypatch.setattr(cloud.remote, "share_download", download, raising=False)
    return cloud


def preview(cloud, code="secretshare", password="abcd", ids=None, overrides=None):
    first = workflow.preview_share(cloud.id, code, password)
    return shares.preview(first["preview_id"], ids or ["4"], overrides)


def execute(plan):
    jid = shares.apply(plan["id"], True)
    jobs.execute(jid)
    return store.job(jid)


def generated(cloud):
    result = execute(preview(cloud))
    assert result["state"] == "success", result
    return store.asset(result["result"]["generated"][0]["asset_id"])


def test_share_generates_signed_strm_nfo_without_any_personal_write_or_transfer(shared, client):
    original = copy.deepcopy(shared.remote.files)
    plan = preview(shared, password="fixturePass9")
    assert plan["items"] and not plan["items"][0]["media"]["directory"].startswith("分享-")
    result = execute(plan)
    assert result["state"] == "success"
    asset = store.asset(result["result"]["generated"][0]["asset_id"])
    assert asset["file_id"].startswith("share:") and asset["payload"]["source_kind"] == "share"
    assert Path(asset["local_path"]).is_relative_to(shared.share_root)
    assert not list(shared.root.rglob("*.strm"))
    assert Path(asset["local_path"]).with_suffix(".nfo").exists()
    for exposed in (str(plan), str(result), Path(asset["local_path"]).read_text(), client.get('/api/cloud/assets/'+asset['id']+'/source').text):
        assert "secretshare" not in exposed and "fixturePass9" not in exposed
    with store.db() as con:
        row = con.execute("SELECT sealed FROM cloud_share_sources").fetchone()
        job = con.execute("SELECT payload FROM cloud_jobs WHERE id=?", (result["id"],)).fetchone()[0]
    assert "secretshare" not in row[0]+job and "fixturePass9" not in row[0]+job
    assert shared.remote.files == original and shared.remote.writes == []
    source = client.get('/api/cloud/assets/'+asset['id']+'/source').json()
    assert source['source']['source_kind'] == 'share' and source['deletions'] == []


def test_play_uses_share_api_not_same_id_in_own_drive_and_does_not_cache_cancellation(shared, monkeypatch):
    asset = generated(shared)
    monkeypatch.setattr(shared.remote, "info", Mock(side_effect=AssertionError("personal info used")))
    monkeypatch.setattr(shared.remote, "download", Mock(side_effect=AssertionError("personal download used")))
    sig = strm.signature(asset["id"], asset["nonce"])
    link = playback.resolve(asset["id"], sig, "Player/1")
    assert link["url"].startswith("https://download.115cdn.com/shared")
    assert shared.share_downloads[-1] == ('secretshare', 'abcd', '4', 'Player/1')
    shared.shared.clear()
    with pytest.raises(CloudError):
        playback.resolve(asset["id"], sig, "Player/1")
    assert len(shared.share_downloads) == 1


def test_share_url_cookie_uses_existing_proxy_and_bad_cdn_is_rejected(shared, monkeypatch):
    asset=generated(shared);sig=strm.signature(asset['id'],asset['nonce'])
    original=shared.remote.share_download
    monkeypatch.setattr(shared.remote, 'share_download', lambda *a: {**original(*a), 'headers': {'Cookie':'temporary-secret'}})
    assert playback.resolve(asset['id'],sig,'UA')['requires_proxy']
    monkeypatch.setattr(shared.remote,'share_download',lambda *a:{**original(*a),'url':'http://127.0.0.1/x'})
    with pytest.raises(CloudError):playback.resolve(asset['id'],sig,'UA')


def test_personal_and_share_with_identical_remote_id_remain_separate(shared):
    own = workflow.plan(shared.id, '1', '2')
    jid=workflow.apply(own['id'],True,True);jobs.execute(jid)
    aid=store.job(jid)['result']['generated'][0]['asset_id']
    a=generated(shared);other=preview(shared,code='anothercode')
    assert not other['items'] and '同名 STRM' in other['skipped'][0]['reason']
    assert aid != a['id']
    assert strm.reconcile(shared.id,'0',set())=={'removed':[],'deferred':[]}
    assert store.asset(a['id'])['state']=='active'


def test_retry_same_share_reuses_asset_and_reference(shared):
    first=generated(shared);again=generated(shared)
    assert first['id']==again['id'] and first['local_path']==again['local_path']
    assert len(list(shared.share_root.rglob('*.strm')))==1


def test_share_cleanup_is_nas_only_and_local_source_delete_is_blocked(shared, client):
    a=generated(shared)
    with pytest.raises(CloudError,match='分享免转存'):
        governance.lookup(a['local_path'],shared.share_root)
    p=governance.preview(a['local_path'],shared.share_root,local=False)
    assert p['enabled'] and '仅清理 NAS' in p['effect']
    assert client.post('/api/cloud/assets/'+a['id']+'/deletion/verify',json={}).status_code==409
    assert client.post('/api/cloud/assets/'+a['id']+'/deletion/reset',json={'confirmed':True}).status_code==409
    assert not shared.remote.writes


@pytest.mark.parametrize('patch',[{'size':101},{'sha1':'B'*40},{'name':'Changed.mkv'},{'is_dir':True},{'parent_id':'9'}])
def test_changed_share_source_blocks_generation_and_playback(shared,patch):
    p=preview(shared);shared.shared['4'].update(patch)
    r=execute(p);assert r['state']=='needs_review' and not r['result']['generated']
    assert not list(shared.share_root.rglob('*.strm')) and not shared.remote.writes


def test_account_change_and_expired_or_double_applied_plan_are_rejected(shared):
    p=preview(shared)
    with pytest.raises(CloudError):shares.apply(p['id'],False)
    with store.db() as con:con.execute('UPDATE cloud_accounts SET revision=revision+1 WHERE id=?',(shared.id,))
    assert execute(p)['state']=='failed'
    with pytest.raises(CloudError):shares.apply(p['id'],True)
    p=preview(shared)
    with store.db() as con:con.execute('UPDATE cloud_plans SET expires=0 WHERE id=?',(p['id'],))
    with pytest.raises(CloudError):shares.apply(p['id'],True)


def test_changed_password_requires_regeneration_before_playback(shared):
    a=generated(shared)
    preview(shared,password='newpass')
    with pytest.raises(CloudError,match='凭据已变化'):playback.resolve(a['id'],strm.signature(a['id'],a['nonce']),'UA')
    assert not shared.share_downloads


def test_cancel_revoke_and_changed_generation_settings(shared):
    a=generated(shared);strm.revoke(a['id'])
    with pytest.raises(CloudError):playback.resolve(a['id'],strm.signature(a['id'],a['nonce']),'UA')
    p=preview(shared);settings.save({'naming_style':'compact'})
    assert execute(p)['state']=='failed'


def test_settings_reject_overlapping_libraries_and_symlink_root(shared,tmp_path):
    for patch in ({'share_output_root':str(shared.root)}, {'share_output_root':str(shared.root/'sub')}, {'share_output_root':str(shared.root.parent)}, {'share_emby_output_root':'/strm/sub'}, {'share_output_root':'/'}):
        with pytest.raises(CloudError):settings.save(patch)
    link=tmp_path/'linked';link.symlink_to(shared.share_root,target_is_directory=True)
    settings.save({'share_output_root':str(link)})
    with pytest.raises(CloudError):preview(shared)


def test_foreign_file_preserved_and_mount_swap_does_not_publish(shared):
    p=preview(shared);i=p['items'][0]
    path=shared.share_root/i['media']['directory']/Path(i['media']['name']).with_suffix('.strm')
    path.parent.mkdir(parents=True);path.write_text('foreign')
    assert execute(p)['state']=='needs_review' and path.read_text()=='foreign'
    shared.shared['4']['name']='Movie.2024.2160p.mkv'
    p=preview(shared,code='othercode');shared.share_root.rename(shared.share_root.with_name('old-share'));shared.share_root.mkdir()
    assert execute(p)['state']=='failed' and not list(shared.share_root.rglob('*.strm'))


def test_folder_recursive_scan_and_filters_use_share_contents_only(shared):
    shared.shared={'9':{**shared.remote.info('1'),'id':'9','parent_id':'0'},'4':{**shared.shared['4'],'parent_id':'9'},'8':{**shared.shared['4'],'id':'8','parent_id':'9','name':'sample.strm'}}
    p=preview(shared,ids=['9'])
    assert len(p['items'])==1 and p['items'][0]['source']['share_parent']=='9'
    assert p['filtered'][0]['file_id']=='8'
    assert execute(p)['state']=='success' and not shared.remote.writes


@pytest.mark.parametrize('mode',['truncated','duplicate','changing','too_large'])
def test_bad_share_pagination_fails_closed(shared,monkeypatch,mode):
    p=workflow.preview_share(shared.id,'secretshare','abcd')
    row=shared.shared['4'];count=[0]
    def page(*args):
        count[0]+=1
        if mode=='duplicate':return {'items':[row,row],'total':2}
        if mode=='too_large':return {'items':[row],'total':shares.LIMIT+1}
        if mode=='changing':return {'items':[row] if count[0]==1 else [],'total':2 if count[0]==1 else 3}
        return {'items':[],'total':1}
    monkeypatch.setattr(shared.remote,'share_page',page)
    with pytest.raises(CloudError):shares.preview(p['preview_id'],['4'])


def test_manual_tmdb_and_ffprobe_use_share_adapter_and_namespace(shared,monkeypatch):
    settings.save({'ffprobe_enabled':True})
    fields={'resolution':'2160p','codec':'H265'}
    def extract(account_id,view,row):
        assert account_id.startswith('share:'+shared.id+':')
        assert view.info('4')['id']=='4'
        assert view.download('4','','Probe')['url'].startswith('https://download.115cdn.com/shared')
        return {'status':'success','fields':fields}
    monkeypatch.setattr(media_probe,'extract',extract)
    p=preview(shared,overrides={'4':{'kind':'movie','tmdb_id':'44'}})
    assert p['items'] and '2160p' in p['items'][0]['media']['name']
    assert shared.remote.downloads==0


def test_api_preview_apply_account_auth_and_source_summary(shared,client):
    r=client.post('/api/cloud/shares/preview',json={'account_id':shared.id,'link':'secretshare','password':'abcd'})
    p=client.post('/api/cloud/shares/strm/preview',json={'preview_id':r.json()['preview_id'],'file_ids':['4']})
    assert p.status_code==200
    pid=p.json()['plan']['id']
    assert client.post('/api/cloud/shares/strm/apply',json={'plan_id':pid,'confirmed':False}).status_code==400
    r=client.post('/api/cloud/shares/strm/apply',json={'plan_id':pid,'confirmed':True})
    assert r.status_code==202
    jobs.execute(r.json()['job_id'])
    assert store.job(r.json()['job_id'])['state']=='success'


def test_emby_share_path_mapping_and_stop_before_generation(shared):
    a=generated(shared)
    path='/share/'+str(Path(a['local_path']).relative_to(shared.share_root))
    assert emby_proxy.asset_for_path(path)['id']==a['id']
    p=preview(shared);jid=shares.apply(p['id'],True);jobs.request_stop(jid);jobs.execute(jid)
    assert store.job(jid)['state']=='stopped' and len(list(shared.share_root.rglob('*.strm')))==1


def test_share_sdk_adapter_matches_pinned_native_url_signature():
    p=object.__new__(provider115.Provider115)
    p.call=Mock(return_value=P115URL('https://download.115cdn.com/x',id=4,size=123,is_dir=False))
    result=p.share_download('secretshare','abcd','4','UA')
    p.call.assert_called_once_with('share_download_url',{'share_code':'secretshare','receive_code':'abcd','file_id':'4'},headers={'User-Agent':'UA'})
    assert result['size']==123 and result['headers']['User-Agent']=='UA'
    p.call.return_value=P115URL('https://download.115cdn.com/x',id=5,size=123)
    with pytest.raises(CloudError):p.share_download('secretshare','abcd','4','UA')


def test_share_nas_cleanup_removes_reference_and_never_contacts_provider(shared,monkeypatch):
    from app import wash
    a=generated(shared)
    monkeypatch.setattr(provider115,'for_account',Mock(side_effect=AssertionError('no remote call')))
    result=wash.safe_delete_files([Path(a['local_path'])],shared.share_root,None,False)
    assert result['strm_removed']==1 and result['cloud_removed']==0 and not result['errors']
    assert not Path(a['local_path']).exists() and store.asset(a['id'])['state']=='revoked'
    assert not shared.remote.writes and '4' in shared.remote.files


def test_new_share_preview_and_transfer_job_encrypt_access_and_old_shape_still_reads(shared):
    first=workflow.preview_share(shared.id,'secretshare','passSECRET')
    with store.db() as con:raw=con.execute('SELECT payload FROM cloud_plans WHERE id=?',(first['preview_id'],)).fetchone()[0]
    assert 'secretshare' not in raw and 'passSECRET' not in raw
    assert workflow.share_access(json.loads(raw))=={'code':'secretshare','password':'passSECRET'}
    jid=workflow.transfer_submit(first['preview_id'],['4'],'3',True)
    with store.db() as con:raw=con.execute('SELECT payload FROM cloud_jobs WHERE id=?',(jid,)).fetchone()[0]
    assert 'secretshare' not in raw and 'passSECRET' not in raw
    jobs.execute(jid);assert store.job(jid)['state']=='success'
    assert workflow.share_access({'code':'oldcode','password':'oldpass'})=={'code':'oldcode','password':'oldpass'}


def test_selected_folder_copies_unique_subtitle_using_share_view(shared,monkeypatch):
    shared.shared['9']={**shared.remote.info('1'),'id':'9','parent_id':'0'}
    shared.shared['4']['parent_id']='9'
    shared.shared['5']={**shared.remote.info('5'),'parent_id':'9'}
    def sidecar(view,row):
        assert view.info(row['id'])['share_parent']=='9'
        view.download(row['id'],'','Subtitle')
        return b'shared subtitle'
    monkeypatch.setattr(strm,'fetch_sidecar',sidecar)
    p=preview(shared,ids=['9']);assert len(p['items'][0]['followers'])==1
    r=execute(p);assert r['state']=='success'
    a=store.asset(r['result']['generated'][0]['asset_id'])
    assert any(Path(o['path']).read_bytes()==b'shared subtitle' for o in a['payload']['owned'])
    assert shared.remote.downloads==0 and not shared.remote.writes


def test_share_ffprobe_cache_is_separate_and_failure_policy_is_honored(shared,monkeypatch):
    settings.save({'ffprobe_enabled':True})
    probe=Mock(return_value={'resolution':'2160p','codec':'H265'})
    monkeypatch.setattr(media_probe,'probe_url',probe)
    assert preview(shared)['items'] and preview(shared)['items']
    assert probe.call_count==1
    with store.db() as con:
        rows=con.execute('SELECT account_id FROM cloud_media_probes').fetchall()
    assert rows and all(r[0].startswith('share:'+shared.id+':') for r in rows)
    probe.side_effect=CloudError('网络提取失败',502)
    p=preview(shared,code='newshare');assert not p['items'] and p['skipped']
    settings.save({'ffprobe_failure':'filename'})
    p=preview(shared,code='newshare');assert p['items'] and p['items'][0]['media']['media_info']['status']=='fallback'


def test_zero_fingerprint_or_oversized_walk_never_publishes(shared,monkeypatch):
    shared.shared['4']['sha1']=''
    p=preview(shared);assert not p['items'] and p['skipped']
    with pytest.raises(CloudError):shares.apply(p['id'],True)
    shared.shared['4']['sha1']='A'*40
    shared.shared['9']={**shared.remote.info('1'),'id':'9','parent_id':'0'}
    shared.shared['4']['parent_id']='9'
    monkeypatch.setattr(shares,'LIMIT',1)
    with pytest.raises(CloudError):preview(shared,ids=['9'])
    assert not list(shared.share_root.rglob('*.strm'))


def test_same_share_scope_id_cannot_enter_personal_quality_preview_or_execution(shared):
    shared.shared['4']['parent_id']='2'
    first=workflow.preview_share(shared.id,'secretshare','abcd',cid='2')
    r=execute(shares.preview(first['preview_id'],['4']))
    assert r['state']=='success'
    aid=r['result']['generated'][0]['asset_id']
    assert store.asset(aid)['payload']['scope_cid']=='2'
    settings.save({'quality8_enabled':True})
    p=workflow.plan(shared.id,'1','2')
    assert not p['quality8']['comparisons']
    settings.save({'quality8_mode':'upgrade'})
    p=workflow.plan(shared.id,'1','2')
    assert p['items'] and not p['items'][0]['upgrades'] and not p['quality_held']
    assert store.asset(aid)['state']=='active' and not shared.remote.writes


def test_share_flat_output_keeps_movie_and_tv_classification(shared):
    plan = preview(shared)
    assert not any(part.startswith('分享-') for part in Path(plan['items'][0]['media']['directory']).parts)
    asset = generated(shared)
    assert not any(p.name.startswith('分享-') for p in shared.share_root.iterdir())
    assert Path(asset['local_path']).exists()


@pytest.mark.parametrize('keep_foreign',[False,True])
def test_regenerating_legacy_share_migrates_bound_files_and_prunes_only_empty_wrapper(shared,keep_foreign):
    plan=preview(shared)
    with store.db() as con:
        row=con.execute('SELECT payload FROM cloud_plans WHERE id=?',(plan['id'],)).fetchone()
        payload=json.loads(row[0]);wrapper='分享-'+payload['share_id'][:12]
        payload['items'][0]['media']['directory']=wrapper+'/'+payload['items'][0]['media']['directory']
        con.execute('UPDATE cloud_plans SET payload=? WHERE id=?',(store.encode(payload),plan['id']))
    legacy=execute(plan)
    old=store.asset(legacy['result']['generated'][0]['asset_id'])
    old_path=Path(old['local_path']);content=old_path.read_bytes()
    if keep_foreign:
        (old_path.parent/'my-note.txt').write_text('keep me')
    fresh=generated(shared)
    assert fresh['id']==old['id'] and fresh['nonce']==old['nonce']
    assert Path(fresh['local_path']).read_bytes()==content and not old_path.exists()
    assert not Path(fresh['local_path']).relative_to(shared.share_root).parts[0].startswith('分享-')
    assert (shared.share_root/wrapper).exists()==keep_foreign
    if keep_foreign: assert (old_path.parent/'my-note.txt').read_text()=='keep me'
    assert shared.remote.writes==[]


def test_flat_share_preview_preserves_foreign_target_and_reports_conflict(shared):
    plan=preview(shared);item=plan['items'][0]
    target=shared.share_root/item['media']['directory']/Path(item['media']['name']).with_suffix('.strm')
    target.parent.mkdir(parents=True);target.write_text('foreign')
    again=preview(shared)
    assert not again['items'] and '同名 STRM' in again['skipped'][0]['reason']
    assert target.read_text()=='foreign' and not shared.remote.writes


def test_queued_legacy_layout_requires_new_preview(shared):
    plan=preview(shared)
    with store.db() as con:
        payload=json.loads(con.execute('SELECT payload FROM cloud_plans WHERE id=?',(plan['id'],)).fetchone()[0])
        payload['config'].pop('share_layout_version')
        con.execute('UPDATE cloud_plans SET payload=? WHERE id=?',(store.encode(payload),plan['id']))
    result=execute(plan)
    assert result['state']=='failed' and '重新预览' in result['message']
    assert not list(shared.share_root.rglob('*.strm')) and not shared.remote.writes
