"""One-button NAS/115 residue cleanup; temporary filesystem roots only."""
import os
import sys
from pathlib import Path
from unittest.mock import patch
import pytest
os.environ.setdefault('WEB_PASSWORD','test-pass-123')
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from app import engine,wash,cloud_residue as cloud

@pytest.fixture
def roots(tmp_path,monkeypatch):
    for key,name in [('L_ROOT','local'),('S_ROOT','share'),('CLOUD_L_ROOT','cloud'),('DATA_DIR','data'),('STATE_DIR','data/state')]:
        p=tmp_path/name;p.mkdir(parents=True,exist_ok=True);monkeypatch.setattr(engine,key,p)
    monkeypatch.setattr(engine,'LOCK_FILE',engine.DATA_DIR/'agent.lock')
    monkeypatch.setattr(engine,'notify_emby_deleted',lambda *a,**k:None)
    return tmp_path

def put(root,title='show',name='E01.ass',category='日韩剧集'):
    p=root/'剧集'/category/f'{title} {{tmdb-1}}'/'Season 1'/name
    p.parent.mkdir(parents=True,exist_ok=True);p.write_text('temporary');return p

def preview(root=None,limit=100):
    return cloud.scan_directory_cleanup(str(root or engine.L_ROOT),limit=limit)

def execute(r):return cloud.clean_directory_cleanup(r['preview'])

def test_pair_20_episodes_and_json_deleted_together_without_backups(roots):
    for root in [engine.L_ROOT,engine.CLOUD_L_ROOT]:
        for n in range(1,21):
            put(root,name=f'E{n:02}.ass');put(root,name=f'E{n:02}.json')
    p=preview();item=p['preview'][0]
    assert p['hits']==1 and item['nas_file_count']==40 and item['cloud_file_count'] is None
    assert item['cloud_checked'] is False
    r=execute(p)
    assert r['nas_count']==r['cloud_count']==r['count']==1
    assert r['files_removed']==r['cloud_files_removed']==40 and not r['errors']
    for root in [engine.L_ROOT,engine.CLOUD_L_ROOT]:
        assert not (root/'剧集/日韩剧集/show {tmdb-1}').exists()
        assert (root/'剧集/日韩剧集').exists()
    assert not (engine.STATE_DIR/'residue_backup').exists()

def test_known_preview_still_cleans_counterpart_after_nas_title_deleted(roots):
    local=put(engine.L_ROOT);source=put(engine.CLOUD_L_ROOT)
    p=preview();item=p['preview'][0]
    local.unlink();local.parent.rmdir();local.parent.parent.rmdir()
    assert not Path(item['path']).exists()
    r=execute(p)
    assert r['nas_count']==0 and r['cloud_count']==1 and r['cloud_files_removed']==1
    assert not source.exists() and not r['errors']

def test_cloud_without_nas_reference_is_not_searched(roots):
    source=put(engine.CLOUD_L_ROOT);share=put(engine.S_ROOT,name='E01.strm')
    r=execute(preview())
    assert r['cloud_count']==0 and source.exists() and share.exists()

def test_share_scope_only_deletes_nas_and_does_not_require_cloud_mount(roots,monkeypatch):
    share=put(engine.S_ROOT);source=put(engine.CLOUD_L_ROOT)
    monkeypatch.setattr(engine,'CLOUD_L_ROOT',engine.CLOUD_L_ROOT/'unavailable')
    p=preview(engine.S_ROOT);r=execute(p)
    assert r['nas_count']==1 and r['cloud_count']==0 and not share.exists() and source.exists()

@pytest.mark.parametrize('filename',['E01.mkv','E01.strm','unknown.bin'])
def test_cloud_media_or_unknown_file_blocks_linked_item_and_reports_reason(roots,filename):
    local=put(engine.L_ROOT);source=put(engine.CLOUD_L_ROOT)
    keep=put(engine.CLOUD_L_ROOT,name=filename)
    p=preview();assert p['preview'][0]['cloud_checked'] is False
    r=execute(p)
    assert r['nas_count']==r['cloud_count']==0 and r['errors']
    assert local.exists() and source.exists() and keep.exists()

@pytest.mark.parametrize('rootname',['L_ROOT','CLOUD_L_ROOT'])
def test_media_arriving_after_preview_blocks_both_sides(roots,rootname):
    local=put(engine.L_ROOT);source=put(engine.CLOUD_L_ROOT);p=preview()
    new=put(getattr(engine,rootname),name='new.strm')
    r=execute(p)
    assert r['errors'] and local.exists() and source.exists() and new.exists()

def test_cloud_failure_keeps_nas_and_continues_other_preview_items(roots):
    local=put(engine.L_ROOT,title='failed');source=put(engine.CLOUD_L_ROOT,title='failed')
    put(engine.CLOUD_L_ROOT,title='failed',name='second.json')
    good=put(engine.L_ROOT,title='good');goodsource=put(engine.CLOUD_L_ROOT,title='good')
    p=preview();real=wash.os.unlink
    def fail(name,*args,**kwargs):
        if name=='E01.ass' and Path('/proc/self/fd/'+str(kwargs.get('dir_fd'))).resolve()==source.parent:
            raise PermissionError('cloud denied')
        return real(name,*args,**kwargs)
    with patch.object(wash.os,'unlink',fail):r=execute(p)
    assert r['nas_count']==r['cloud_count']==1 and r['cloud_files_removed']==2
    assert local.exists() and source.exists() and not good.exists() and not goodsource.exists()
    assert any(str(source) in e and 'cloud denied' in e for e in r['errors'])

def test_missing_cloud_counterpart_still_cleans_nas(roots):
    local=put(engine.L_ROOT);r=execute(preview())
    assert r['nas_count']==1 and r['cloud_count']==0 and not local.exists() and not r['errors']

def test_missing_mount_does_not_block_nas_scan_and_cleanup_reports_reason(roots,monkeypatch):
    local=put(engine.L_ROOT);monkeypatch.setattr(engine,'CLOUD_L_ROOT',engine.CLOUD_L_ROOT/'missing')
    p=preview();assert p['status']=='success' and len(p['preview'])==1
    r=execute(p);assert r['errors'] and local.exists()

def test_unknown_nas_file_blocks_cloud_only_candidate(roots):
    local=put(engine.L_ROOT,name='unknown.bin');source=put(engine.CLOUD_L_ROOT)
    assert preview()['hits']==0 and local.exists() and source.exists()

def test_all_scope_deduplicates_paired_rows_and_keeps_both_libraries_in_limit(roots):
    for title in ['one','two']:
        put(engine.L_ROOT,title);put(engine.CLOUD_L_ROOT,title);put(engine.S_ROOT,title)
    p=cloud.scan_directory_cleanup(scope='all',limit=2)
    assert p['hits']==4 and p['cloud_checked'] is False and len(p['preview'])==2
    assert {i['lib'] for i in p['preview']}=={'local','share'}
    assert [s['hits'] for s in p['scopes']]==[2,2]

def test_selected_category_does_not_include_another_cloud_category(roots):
    source=put(engine.CLOUD_L_ROOT);outside=put(engine.CLOUD_L_ROOT,category='欧美剧集')
    selected=engine.L_ROOT/'剧集/日韩剧集';selected.mkdir(parents=True)
    (selected/'show {tmdb-1}').mkdir()
    r=execute(preview(selected))
    assert r['cloud_count']==1 and not source.exists() and outside.exists()

@pytest.mark.parametrize('kind',['cloud_path','lib','outside','category','traversal'])
def test_forged_target_scope_preserves_media(roots,kind):
    local=put(engine.L_ROOT);source=put(engine.CLOUD_L_ROOT);item=preview()['preview'][0]
    if kind=='cloud_path':item['cloud_path']=str(source.parent)
    if kind=='lib':item['lib']='share'
    if kind=='outside':item['path']=str(roots/'outside {tmdb-1}')
    if kind=='category':item['path']=str(engine.L_ROOT/'剧集')
    if kind=='traversal':item['path']=str(engine.L_ROOT)+'/../cloud/show {tmdb-1}'
    r=cloud.clean_directory_cleanup([item])
    assert r['errors'] and source.exists() and local.exists()

def test_cloud_root_changed_after_preview_is_not_used_for_deletion(roots,monkeypatch):
    local=put(engine.L_ROOT);source=put(engine.CLOUD_L_ROOT);p=preview()
    new=roots/'new-cloud';new.mkdir();other=put(new)
    monkeypatch.setattr(engine,'CLOUD_L_ROOT',new)
    r=execute(p);assert r['errors'] and other.exists() and local.exists() and source.exists()

def test_symlink_nas_ancestor_blocks_cloud_only_cleanup(roots):
    local=put(engine.L_ROOT);source=put(engine.CLOUD_L_ROOT);p=preview();outside=roots/'outside';outside.mkdir()
    local.unlink();local.parent.rmdir();local.parent.parent.rmdir();local.parent.parent.parent.rmdir();(engine.L_ROOT/'剧集').rmdir()
    (engine.L_ROOT/'剧集').symlink_to(outside,target_is_directory=True)
    assert execute(p)['errors'] and source.exists()

def test_last_moment_cloud_video_is_preserved_and_nas_kept(roots):
    local=put(engine.L_ROOT);source=put(engine.CLOUD_L_ROOT);p=preview();real=wash.os.unlink
    def arrive(name,*args,**kwargs):
        if name=='E01.ass':(source.parent/'new.mkv').write_text('new media')
        return real(name,*args,**kwargs)
    with patch.object(wash.os,'unlink',arrive):r=execute(p)
    assert r['errors'] and local.exists() and (source.parent/'new.mkv').exists()

def test_busy_cleanup_does_not_consume_or_change_preview(roots):
    local=put(engine.L_ROOT);source=put(engine.CLOUD_L_ROOT);p=preview()
    with wash.mutation_lock():assert execute(p)['status']=='busy'
    assert local.exists() and source.exists() and execute(p)['count']==1

def test_empty_cloud_only_directory_is_removed(roots):
    local=put(engine.L_ROOT);local.unlink()
    source=put(engine.CLOUD_L_ROOT);source.unlink()
    p=preview();assert p['preview'][0]['file_count']==0
    r=execute(p);assert r['cloud_count']==1 and r['cloud_files_removed']==0

def test_linked_route_uses_one_clean_request_and_requires_auth(roots):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.routers import governance as router
    app=FastAPI();app.include_router(router.router);client=TestClient(app)
    assert client.post('/api/wash/empty-dirs/clean',json={'items':[]}).status_code==401
    app.dependency_overrides[router.auth]=lambda:True
    local=put(engine.L_ROOT);source=put(engine.CLOUD_L_ROOT)
    p=client.post('/api/wash/empty-dirs/scan',json={'scope':'all'}).json()
    r=client.post('/api/wash/empty-dirs/clean',json={'items':p['preview']}).json()
    assert r['nas_count']==r['cloud_count']==1 and not local.exists() and not source.exists()
