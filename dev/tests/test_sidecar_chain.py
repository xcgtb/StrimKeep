"""Automatic STRM/video sidecar deletion chain, temporary roots only."""
import os
import sys
from pathlib import Path
from unittest.mock import patch
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from app import engine,wash

@pytest.fixture
def roots(tmp_path,monkeypatch):
    for key,name in [('L_ROOT','local'),('S_ROOT','share'),('CLOUD_L_ROOT','cloud'),('DATA_DIR','data'),('STATE_DIR','data/state')]:
        p=tmp_path/name;p.mkdir(parents=True,exist_ok=True);monkeypatch.setattr(engine,key,p)
    monkeypatch.setattr(engine,'LOCK_FILE',engine.DATA_DIR/'agent.lock')
    monkeypatch.setattr(engine,'WASH_RESIDUAL_FILE',engine.STATE_DIR/'wash_residuals.json')
    monkeypatch.setattr(engine,'db_doc_set',lambda *a,**k:None)
    monkeypatch.setattr(engine,'PRUNE_MIN_DEPTH',3)
    return tmp_path

def put(root,rel):
    p=root/rel;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('temporary');return p

def delete(files,root,cloud=False,dry=False):
    return wash.safe_delete_files(files,root,engine.CLOUD_L_ROOT if cloud else None,dry_run=dry)

@pytest.mark.parametrize('lib',['local','share'])
def test_strm_deletion_removes_ass_json_and_whole_empty_media_tree(roots,lib):
    root=engine.L_ROOT if lib=='local' else engine.S_ROOT
    rel=Path('剧集/日韩剧集/样本 {tmdb-1}/Season 1')
    f=put(root,rel/'Show.S01E01.strm')
    for name in ['Show.S01E01.json','Show.S01E01.zh-cn.ass','poster.jpg','unrelated-old.srt']:
        put(root,rel/name)
    put(root,rel.parent/'tvshow.nfo')
    result=delete([f],root)
    assert result['strm_removed']==1 and result['sidecars_removed']==5 and not (root/rel.parent).exists()
    assert (root/'剧集/日韩剧集').exists() and not (engine.STATE_DIR/'residue_backup').exists()

def test_cloud_and_nas_20_episodes_all_sidecars_and_empty_dirs_removed(roots):
    rel=Path('剧集/日韩剧集/请回答1988 (2015) {tmdb-64010}/Season 1')
    files=[]
    for episode in range(1,21):
        stem=f'Show.S01E{episode:02}.1080p'
        files.append(put(engine.L_ROOT,rel/(stem+'.strm')))
        put(engine.L_ROOT,rel/(stem+'.json'));put(engine.L_ROOT,rel/(stem+'.zh-cn.ass'))
        put(engine.CLOUD_L_ROOT,rel/(stem+'.mkv'))
        put(engine.CLOUD_L_ROOT,rel/(stem+'.json'));put(engine.CLOUD_L_ROOT,rel/(stem+'.zh-cn.ass'))
    result=delete(files,engine.L_ROOT,cloud=True)
    assert result['strm_removed']==result['cloud_removed']==20
    assert result['sidecars_removed']==result['cloud_sidecars_removed']==40
    for root in [engine.L_ROOT,engine.CLOUD_L_ROOT]:
        assert not (root/rel.parent).exists() and (root/'剧集/日韩剧集').exists()
    assert not (engine.STATE_DIR/'residue_backup').exists()

def test_partial_episode_deletion_keeps_remaining_cloud_and_local_episode(roots):
    rel=Path('剧集/日韩剧集/剧 {tmdb-2}/Season 1')
    first=put(engine.L_ROOT,rel/'Show.S01E01.strm')
    for root,ext in [(engine.L_ROOT,'.strm'),(engine.CLOUD_L_ROOT,'.mkv')]:
        for n in [1,10]:
            put(root,rel/(f'Show.S01E{n:02}'+ext))
            put(root,rel/(f'Show.S01E{n:02}.zh.ass'))
            put(root,rel/(f'Show.S01E{n:02}.json'))
    result=delete([first],engine.L_ROOT,cloud=True)
    assert result['cloud_sidecars_removed']==result['sidecars_removed']==2
    for root in [engine.L_ROOT,engine.CLOUD_L_ROOT]:
        assert (root/rel/'Show.S01E10.zh.ass').exists() and (root/rel/'Show.S01E10.json').exists()

def test_cloud_multiversion_ownership_and_normalized_paths(roots,monkeypatch):
    f=put(engine.L_ROOT,'电影/华语电影/电影 {tmdb-3}/Movie.strm')
    v=put(engine.CLOUD_L_ROOT,'电影/华语电影/电影另一个写法 {tmdb-3}/Movie.mkv')
    side=put(engine.CLOUD_L_ROOT,str(v.relative_to(engine.CLOUD_L_ROOT).with_suffix('.ass')))
    second=put(engine.CLOUD_L_ROOT,str(v.parent.relative_to(engine.CLOUD_L_ROOT)/'Movie - 2160p.mkv'))
    attached=put(engine.CLOUD_L_ROOT,str(v.parent.relative_to(engine.CLOUD_L_ROOT)/'Movie - 2160p.json'))
    monkeypatch.setattr(engine,'cloud_videos',lambda *a:([v],'normalized'))
    result=delete([f],engine.L_ROOT,cloud=True)
    assert result['cloud_sidecars_removed']==1 and not side.exists()
    assert second.exists() and attached.exists()

def test_last_video_cleans_mismatched_old_subtitles_and_artwork(roots):
    rel=Path('剧集/日韩剧集/剧 {tmdb-4}/Season 1')
    f=put(engine.L_ROOT,rel/'New.strm');put(engine.CLOUD_L_ROOT,rel/'New.mkv')
    put(engine.CLOUD_L_ROOT,rel/'Old.zh-cn.ass');put(engine.CLOUD_L_ROOT,rel/'Old.json')
    put(engine.CLOUD_L_ROOT,rel.parent/'poster.jpg')
    r=delete([f],engine.L_ROOT,cloud=True)
    assert r['cloud_sidecars_removed']==3 and not (engine.CLOUD_L_ROOT/rel.parent).exists()

def test_cloud_failed_sidecar_is_reported_and_not_retried_by_directory_cleanup(roots,monkeypatch):
    rel=Path('剧集/日韩剧集/剧 {tmdb-5}/Season 1')
    f=put(engine.L_ROOT,rel/'E01.strm');put(engine.CLOUD_L_ROOT,rel/'E01.mkv')
    side=put(engine.CLOUD_L_ROOT,rel/'E01.ass');put(engine.CLOUD_L_ROOT,rel/'E01.json')
    real=wash._unlink_with_timeout; calls=[]
    def fail(p,*a,**k):
        calls.append(p)
        return False if p==side else real(p,*a,**k)
    monkeypatch.setattr(wash,'_unlink_with_timeout',fail)
    result=delete([f],engine.L_ROOT,cloud=True)
    assert result['strm_removed']==1 and result['cloud_sidecars_removed']==1 and side.exists()
    assert result['cloud_sidecar_residuals']==[str(side)] and calls.count(side)==1
    assert any(str(side) in e for e in result['errors'])

def test_dry_run_preserves_video_json_subtitles_and_directories(roots):
    rel=Path('电影/华语电影/电影 {tmdb-6}')
    f=put(engine.L_ROOT,rel/'Movie.strm')
    v=put(engine.CLOUD_L_ROOT,rel/'Movie.mkv');side=put(engine.CLOUD_L_ROOT,rel/'Movie.ass')
    put(engine.L_ROOT,rel/'Movie.json')
    r=delete([f],engine.L_ROOT,cloud=True,dry=True)
    assert r['cloud_removed']==1 and f.exists() and v.exists() and side.exists()
    assert (f.parent/'Movie.json').exists()

def test_unknown_file_keeps_cloud_directory_but_known_ass_is_deleted(roots):
    rel=Path('电影/华语电影/电影 {tmdb-7}')
    f=put(engine.L_ROOT,rel/'Movie.strm');put(engine.CLOUD_L_ROOT,rel/'Movie.mkv')
    side=put(engine.CLOUD_L_ROOT,rel/'Movie.ass');unknown=put(engine.CLOUD_L_ROOT,rel/'important.txt')
    delete([f],engine.L_ROOT,cloud=True)
    assert not side.exists() and unknown.exists()

def test_local_actual_video_is_not_a_sidecar(roots):
    rel=Path('电影/华语电影/电影 {tmdb-8}')
    f=put(engine.S_ROOT,rel/'Movie.strm');v=put(engine.S_ROOT,rel/'Movie.mkv')
    delete([f],engine.S_ROOT)
    assert v.exists()

def test_new_video_during_metadata_cleanup_is_preserved(roots,monkeypatch):
    rel=Path('电影/华语电影/电影 {tmdb-9}')
    f=put(engine.S_ROOT,rel/'Movie.strm');put(engine.S_ROOT,rel/'poster.jpg')
    real=wash.os.unlink
    def arrive(name,*a,**k):
        if name=='poster.jpg':put(engine.S_ROOT,rel/'new.mkv')
        return real(name,*a,**k)
    monkeypatch.setattr(wash.os,'unlink',arrive)
    r=delete([f],engine.S_ROOT)
    assert (engine.S_ROOT/rel/'new.mkv').exists() and r['errors']

def test_cloud_video_failure_preserves_strm_and_its_sidecars(roots,monkeypatch):
    rel=Path('电影/华语电影/电影 {tmdb-10}')
    f=put(engine.L_ROOT,rel/'Movie.strm');v=put(engine.CLOUD_L_ROOT,rel/'Movie.mkv')
    side=put(engine.CLOUD_L_ROOT,rel/'Movie.json')
    monkeypatch.setattr(wash,'_unlink_with_timeout',lambda *a,**k:False)
    r=delete([f],engine.L_ROOT,cloud=True)
    assert r['strm_removed']==0 and f.exists() and v.exists() and side.exists()

@pytest.mark.parametrize('lib',['local','share'])
@pytest.mark.parametrize('remaining',['empty','subtitle_only','media'])
def test_season_cleanup_never_removes_a_different_season(roots,lib,remaining):
    root=engine.L_ROOT if lib=='local' else engine.S_ROOT
    rel=Path('剧集/日韩剧集/多季剧 {tmdb-11}')
    f=put(root,rel/'Season 1/E01.strm');put(root,rel/'Season 1/E01.json')
    other=root/rel/'Season 2';other.mkdir()
    if remaining=='subtitle_only':put(root,rel/'Season 2/E01.ass')
    if remaining=='media':put(root,rel/'Season 2/E01.strm')
    if lib=='local':
        put(engine.CLOUD_L_ROOT,rel/'Season 1/E01.mkv')
        put(engine.CLOUD_L_ROOT,rel/'Season 1/E01.ass')
        (engine.CLOUD_L_ROOT/rel/'Season 2').mkdir()
        if remaining=='subtitle_only':put(engine.CLOUD_L_ROOT,rel/'Season 2/E01.ass')
        if remaining=='media':put(engine.CLOUD_L_ROOT,rel/'Season 2/E01.mkv')
    delete([f],root,cloud=lib=='local')
    assert not (root/rel/'Season 1').exists() and other.exists() and (root/rel).exists()
    if remaining!='empty':assert list(other.iterdir())
    if lib=='local':
        assert not (engine.CLOUD_L_ROOT/rel/'Season 1').exists()
        other_cloud=engine.CLOUD_L_ROOT/rel/'Season 2';assert other_cloud.exists()
        if remaining!='empty':assert list(other_cloud.iterdir())

