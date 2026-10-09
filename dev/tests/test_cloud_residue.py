"""Cloud residue cleanup against temporary roots only."""
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

os.environ.setdefault('WEB_PASSWORD', 'test-pass-123')
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app import engine, wash, cloud_residue as cloud


@pytest.fixture
def roots(tmp_path, monkeypatch):
    for key, name in [('L_ROOT','local'), ('S_ROOT','share'), ('CLOUD_L_ROOT','cloud'),
                      ('DATA_DIR','data'), ('STATE_DIR','data/state')]:
        path = tmp_path/name; path.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(engine,key,path)
    monkeypatch.setattr(engine,'LOCK_FILE',engine.DATA_DIR/'agent.lock')
    cloud._PREVIEWS.clear()
    rel = Path('剧集/日韩剧集/请回答1988 (2015) {tmdb-64010}')
    local, source = engine.L_ROOT/rel, engine.CLOUD_L_ROOT/rel
    for directory in [local,source]:
        (directory/'Season 1').mkdir(parents=True)
        (directory/'Season 1/episode.ass').write_bytes(b'original subtitle')
    return local,source


def preview(local): return cloud.preview_cloud_residue(str(local))
def clean(p): return cloud.clean_cloud_residue(p['token'],confirmed=True)


def test_exact_preview_direct_deletion_preserves_local_files(roots):
    local,source=roots; p=preview(local)
    assert p['status']=='success' and p['file_count']==1 and p['extensions']=={'.ass':1}
    assert p['cloud_path']==str(source)
    result=clean(p)
    assert result['status']=='success' and result['files_removed']==1 and not source.exists()
    assert (local/'Season 1/episode.ass').read_bytes()==b'original subtitle'
    assert not (engine.STATE_DIR/'residue_backup').exists()



@pytest.mark.parametrize('target,filename', [('local','episode.strm'),('local','new.mkv'),
    ('cloud','new.mkv'),('cloud','new.mp4'),('cloud','important.txt'),('cloud','archive.zip')])
def test_media_and_unknown_files_block_preview(roots,target,filename):
    local,source=roots; d=local if target=='local' else source
    (d/filename).write_text('keep')
    result=preview(local)
    assert result['status']=='error' and not result.get('token')
    assert (d/filename).read_text()=='keep'


@pytest.mark.parametrize('kind',['share','outside','category','traversal','relative','season'])
def test_invalid_local_targets_cannot_map_to_cloud(roots,kind):
    local,_=roots
    raw={'share':engine.S_ROOT,'outside':local.parents[4],
         'category':engine.L_ROOT/'剧集','traversal':str(engine.L_ROOT)+'/../cloud',
         'relative':'剧集','season':local/'Season 1'}[kind]
    assert preview(raw)['status']=='error'


def test_no_fuzzy_mapping(roots):
    local,source=roots
    renamed=source.with_name('请回答1988 (2015) {tmdb-64010} another')
    source.rename(renamed)
    assert preview(local)['status']=='error' and renamed.exists()


def test_cloud_symlink_is_rejected(roots):
    local,source=roots
    moved=source.with_name('moved'); source.rename(moved)
    source.symlink_to(moved,target_is_directory=True)
    assert preview(local)['status']=='error'


def test_cloud_root_overlap_is_rejected(roots,monkeypatch):
    local,_=roots; monkeypatch.setattr(engine,'CLOUD_L_ROOT',engine.L_ROOT)
    assert preview(local)['status']=='error'


def test_read_failure_does_not_publish_partial_preview(roots):
    local,source=roots; real=wash.os.scandir
    def fail(path):
        if Path(path)==source/'Season 1': raise PermissionError('cloud denied')
        return real(path)
    with patch.object(wash.os,'scandir',fail): result=preview(local)
    assert result['status']=='error' and 'cloud denied' in result['message'] and not result.get('token')


def test_requires_explicit_confirmation_and_single_use_token(roots):
    local,source=roots; p=preview(local)
    assert cloud.clean_cloud_residue(p['token'])['status']=='error' and source.exists()
    assert cloud.clean_cloud_residue('not-issued',confirmed=True)['status']=='error'
    assert clean(p)['status']=='success'
    assert clean(p)['status']=='error'


def test_expired_token_preserves_sources(roots):
    local,source=roots; p=preview(local)
    cloud._PREVIEWS[p['token']]['expires']=0
    assert clean(p)['status']=='error' and source.exists()


@pytest.mark.parametrize('target',['local','cloud'])
def test_arriving_media_after_preview_blocks_execution(roots,target):
    local,source=roots; p=preview(local); d=local if target=='local' else source
    (d/'new.strm').write_text('new media')
    result=clean(p)
    assert result['status']=='error' and source.exists() and (source/'Season 1/episode.ass').exists()
    assert not list((engine.STATE_DIR).glob('residue_backup/*'))


def test_changed_file_after_preview_requires_new_preview(roots):
    local,source=roots; p=preview(local)
    (source/'Season 1/episode.ass').write_bytes(b'changed subtitle contents')
    assert clean(p)['status']=='error' and source.exists()



def test_cleanup_never_uses_backup_or_creates_state_files(roots):
    local,source=roots; p=preview(local)
    with patch.object(wash.shutil,'copyfileobj',side_effect=AssertionError('no backup')):
        with patch.object(wash.os,'fsync',side_effect=AssertionError('no backup')): result=clean(p)
    assert result['status']=='success' and not source.exists() and not list(engine.STATE_DIR.glob('*.json')) and not list(engine.STATE_DIR.glob('*backup*'))



def test_new_local_strm_before_execution_preserves_cloud_subtitles(roots):
    local,source=roots; p=preview(local); (local/'new.strm').write_text('new')
    assert clean(p)['status']=='error' and (source/'Season 1/episode.ass').exists()



def test_last_moment_cloud_video_is_never_recursively_deleted(roots):
    local,source=roots; p=preview(local); real=wash.os.unlink
    def arrive(path,*args,**kwargs):
        if path=='episode.ass': (source/'Season 1/new.mkv').write_text('new video')
        return real(path,*args,**kwargs)
    with patch.object(wash.os,'unlink',arrive): result=clean(p)
    assert result['status']=='partial' and result['files_removed']==1 and result['errors']
    assert (source/'Season 1/new.mkv').read_text()=='new video'
    assert not (engine.STATE_DIR/'residue_backup').exists()


def test_busy_operation_keeps_preview_usable(roots):
    local,source=roots; p=preview(local)
    with wash.mutation_lock(): assert clean(p)['status']=='busy'
    assert source.exists() and clean(p)['status']=='success'


def test_configuration_changed_after_preview_blocks_cleanup(roots,monkeypatch,tmp_path):
    local,source=roots; p=preview(local)
    different=tmp_path/'other-cloud'; different.mkdir()
    monkeypatch.setattr(engine,'CLOUD_L_ROOT',different)
    assert clean(p)['status']=='error' and source.exists()


def test_empty_cloud_directory_can_be_directly_cleaned(roots):
    local,source=roots; (source/'Season 1/episode.ass').unlink()
    result=clean(preview(local))
    assert result['status']=='success' and result['files_removed']==0 and not source.exists()
    assert not list(engine.STATE_DIR.glob('*.json')) and not list(engine.STATE_DIR.glob('*backup*'))



def test_existing_backup_folder_is_neither_used_nor_deleted(roots,tmp_path):
    local,source=roots; old=engine.STATE_DIR/'residue_backup'; old.mkdir()
    marker=old/'historical.nfo'; marker.write_text('old')
    result=clean(preview(local))
    assert result['status']=='success' and marker.read_text()=='old'



def test_bounded_preview_cache(roots):
    local,source=roots
    for _ in range(51): assert preview(local)['status']=='success'
    assert len(cloud._PREVIEWS)==50



def test_authenticated_routes_and_confirmation(roots):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.routers import governance as router
    app=FastAPI(); app.include_router(router.router); client=TestClient(app)
    assert client.post('/api/wash/cloud-residue/preview',json={}).status_code==401
    assert client.post('/api/wash/cloud-residue/clean',json={}).status_code==401
    app.dependency_overrides[router.auth]=lambda:True
    local,source=roots
    p=client.post('/api/wash/cloud-residue/preview',json={'local_path':str(local)}).json()
    assert p['status']=='success'
    blocked=client.post('/api/wash/cloud-residue/clean',json={'token':p['token'],'confirmed':'true'}).json()
    assert blocked['status']=='error' and source.exists()
    result=client.post('/api/wash/cloud-residue/clean',json={'token':p['token'],'confirmed':True}).json()
    assert result['status']=='success'



def test_cloud_failed_file_is_skipped_and_others_are_deleted(roots):
    local,source=roots; (source/'Season 1/second.ass').write_text('second')
    p=preview(local); real=wash.os.unlink
    def fail(path,*args,**kwargs):
        if path=='episode.ass': raise PermissionError('cloud denied')
        return real(path,*args,**kwargs)
    with patch.object(wash.os,'unlink',fail): result=clean(p)
    assert result['status']=='partial' and result['files_removed']==1
    assert any('episode.ass' in e and 'cloud denied' in e for e in result['errors'])
    assert (source/'Season 1/episode.ass').exists() and not (source/'Season 1/second.ass').exists()
    assert not list(engine.STATE_DIR.glob('*.json')) and not list(engine.STATE_DIR.glob('*backup*'))
