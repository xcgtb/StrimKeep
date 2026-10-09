"""Direct directory residue cleanup, media preservation and failure reporting."""
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app import engine, wash


@pytest.fixture
def roots(tmp_path, monkeypatch):
    for name, rel in [('L_ROOT', 'local'), ('S_ROOT', 'share'), ('DATA_DIR', 'data'),
                      ('STATE_DIR', 'data/state')]:
        p = tmp_path / rel; p.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(engine, name, p)
    monkeypatch.setattr(engine, 'LOCK_FILE', engine.DATA_DIR / 'agent.lock')
    monkeypatch.setattr(engine, 'notify_emby_deleted', lambda *args, **kwargs: None)


def media(root=None, title='样本 {tmdb-123}'):
    p = (root or engine.L_ROOT) / '电影' / '华语电影' / title
    p.mkdir(parents=True, exist_ok=True)
    return p


def scan(path): return engine.scan_empty_dirs(str(path))
def clean(*paths): return engine.clean_empty_dirs([str(p) for p in paths])
def hits(result): return {p['path'] for p in result.get('preview', [])}


def test_input_scope_is_not_library_root(roots):
    a = media(title='A {tmdb-1}'); b = media(title='B {tmdb-2}')
    (b / 'B.strm').write_text('fake')
    c = media(title='C {tmdb-3}')
    result = scan(a)
    assert result['scope'] == str(a) and hits(result) == {str(a)}
    assert str(c) not in hits(result)


def test_all_empty_category_is_still_searched(roots):
    d = media()
    assert hits(scan(engine.L_ROOT)) == {str(d)}


@pytest.mark.parametrize('filename', ['video.mkv', 'video.MP4', 'video.iso', 'video.avi',
                                     'media.STRM', 'unknown.txt', '.DS_Store'])
def test_media_and_unknown_files_are_not_candidates_or_moved(roots, filename):
    d = media(); f = d / filename; f.write_text('valuable')
    assert not hits(scan(d))
    result = clean(d)
    assert result['count'] == 0 and result['errors'] and f.read_text() == 'valuable'
    assert result['backup_root'] == ''


def test_known_metadata_directly_deleted_without_backup(roots):
    d = media(); sub = d/'Season 01'; sub.mkdir()
    for rel in ['movie.nfo','poster.JPG','Season 01/subtitle.srt']: (d/rel).write_text('temporary')
    (sub/'empty-child').mkdir()
    result = clean(d)
    assert result['count']==1 and result['files_removed']==3 and not result['errors'] and not d.exists()
    assert result['backup_root']=='' and not (engine.STATE_DIR/'residue_backup').exists()
    assert d.parent.exists()



def test_root_category_and_non_media_directory_never_moved(roots):
    d = media()
    plain = d.parent / '普通目录'; plain.mkdir()
    result = clean(engine.L_ROOT, d.parent, plain)
    assert result['count'] == 0 and len(result['errors']) == 3
    assert engine.L_ROOT.exists() and d.exists() and plain.exists()


def test_topmost_candidate_only_and_preview_limit(roots):
    parent = media(title='合集 {tmdb-10}'); child = parent/'片名 {tmdb-11}'; child.mkdir()
    other = media(title='其他 {tmdb-12}')
    result = engine.scan_empty_dirs(str(engine.L_ROOT), limit=1)
    assert result['hits'] == 2 and len(result['preview']) == 1
    assert str(child) not in hits(scan(engine.L_ROOT))


def test_invalid_missing_and_relative_scope_fail(roots, tmp_path):
    for path in [tmp_path/'outside', engine.L_ROOT/'missing', Path('relative'),
                 Path(str(engine.L_ROOT)+'/../local')]:
        result = scan(path)
        assert result['status'] == 'error' and not result.get('preview')


def test_symlink_scopes_and_contents_are_protected(roots, tmp_path):
    d = media(); outside = tmp_path/'outside'; outside.mkdir()
    f = outside/'valuable.nfo'; f.write_text('do not touch')
    link = d/'link.nfo'; link.symlink_to(f)
    assert not hits(scan(d)) and clean(d)['count'] == 0
    link.unlink(); linked_dir = d/'linked'; linked_dir.symlink_to(outside, target_is_directory=True)
    assert not hits(scan(d)) and scan(linked_dir)['status'] == 'error'
    assert clean(linked_dir)['count'] == 0 and f.read_text() == 'do not touch'


def test_special_file_is_protected(roots):
    d = media(); os.mkfifo(d/'pipe.nfo')
    assert not hits(scan(d)) and clean(d)['count'] == 0


def test_directory_permission_failure_returns_no_partial_preview(roots):
    first = media(title='A {tmdb-1}'); second = media(title='B {tmdb-2}')
    real_scandir = wash.os.scandir
    def denied(path):
        if Path(path) == second: raise PermissionError('injected scandir failure')
        return real_scandir(path)
    with patch.object(wash.os, 'scandir', denied):
        result = scan(engine.L_ROOT)
        assert result['status'] == 'error' and not result.get('preview')
        result = clean(second)
        assert result['count'] == 0 and result['errors'] and second.exists()


def test_new_media_after_preview_is_preserved(roots):
    d = media(); (d/'old.nfo').write_text('old')
    assert hits(scan(d)) == {str(d)}
    video = d/'new.mkv'; video.write_text('new media')
    assert clean(d)['count'] == 0 and video.read_text() == 'new media'


def test_local_share_identical_paths_are_cleaned_without_backups(roots):
    local,share=media(),media(engine.S_ROOT)
    (local/'movie.nfo').write_text('local'); (share/'movie.nfo').write_text('share')
    result=clean(local,share)
    assert result['count']==2 and result['files_removed']==2 and not result['errors']
    assert {x['lib'] for x in result['moved']}=={'local','share'}
    assert not local.exists() and not share.exists() and not (engine.STATE_DIR/'residue_backup').exists()



def test_repeated_success_does_not_accumulate_backups(roots):
    old=engine.STATE_DIR/'residue_backup'/'old'; old.mkdir(parents=True)
    marker=old/'old.nfo'; marker.write_text('historical')
    for text in ['first','second','third']:
        d=media(); (d/'movie.nfo').write_text(text)
        assert clean(d)['count']==1 and not d.exists()
    assert list((engine.STATE_DIR/'residue_backup').iterdir())==[old]
    assert marker.read_text()=='historical'



def test_duplicate_input_does_not_move_twice(roots):
    d = media(); assert clean(d, d)['count'] == 1


def test_new_media_before_removal_preserves_all_files(roots):
    d=media(); (d/'movie.nfo').write_text('metadata'); real=wash._remove_residue
    def arrive(*args,**kwargs):
        (d/'new.STRM').write_text('new media'); return real(*args,**kwargs)
    with patch.object(wash,'_remove_residue',arrive): result=clean(d)
    assert result['count']==0 and result['errors'] and (d/'movie.nfo').exists() and (d/'new.STRM').exists()
    assert not (engine.STATE_DIR/'residue_backup').exists()



def test_failed_file_is_skipped_and_other_files_still_cleaned(roots):
    d=media(); (d/'failed.nfo').write_text('keep'); (d/'okay.ass').write_text('delete')
    real=wash.os.unlink
    def denied(path,*args,**kwargs):
        if path=='failed.nfo': raise PermissionError('injected denied')
        return real(path,*args,**kwargs)
    with patch.object(wash.os,'unlink',denied): result=clean(d)
    assert result['count']==0 and result['files_removed']==1
    assert any('failed.nfo' in e and 'injected denied' in e for e in result['errors'])
    assert (d/'failed.nfo').exists() and not (d/'okay.ass').exists()



def test_cleanup_does_not_copy_or_fsync_backup_files(roots):
    d=media(); (d/'movie.nfo').write_text('metadata')
    with patch.object(wash.shutil,'copyfileobj',side_effect=AssertionError('no copy')):
        with patch.object(wash.os,'fsync',side_effect=AssertionError('no fsync')): result=clean(d)
    assert result['count']==1 and not d.exists() and not (engine.STATE_DIR/'residue_backup').exists()



def test_fifo_replacement_does_not_block_or_get_removed(roots):
    d=media(); nfo=d/'movie.nfo'; nfo.write_text('metadata'); real=wash.os.stat
    def replace(path,*args,**kwargs):
        if path=='movie.nfo': nfo.unlink(); os.mkfifo(nfo)
        return real(path,*args,**kwargs)
    with patch.object(wash.os,'stat',replace): result=clean(d)
    assert result['count']==0 and result['errors'] and nfo.exists()



def test_changed_source_before_unlink_is_skipped(roots):
    d=media(); nfo=d/'movie.nfo'; nfo.write_text('metadata'); real=wash.os.stat
    def changed(path,*args,**kwargs):
        if path=='movie.nfo': nfo.write_text('replacement')
        return real(path,*args,**kwargs)
    with patch.object(wash.os,'stat',changed): result=clean(d)
    assert result['count']==0 and result['errors'] and nfo.read_text()=='replacement'



def test_last_moment_arrival_is_not_recursively_deleted(roots):
    d = media(); (d/'movie.nfo').write_text('metadata'); real_unlink = wash.os.unlink
    def arrive(path, *args, **kwargs):
        if path == 'movie.nfo': (d/'new.mkv').write_text('new media')
        return real_unlink(path, *args, **kwargs)
    with patch.object(wash.os, 'unlink', arrive): result = clean(d)
    assert result['count'] == 0 and result['errors'] and (d/'new.mkv').read_text() == 'new media'
    assert result['files_removed']==1 and not (engine.STATE_DIR/'residue_backup').exists()


def test_parent_symlink_replacement_does_not_touch_external_files(roots, tmp_path):
    d = media(); (d/'movie.nfo').write_text('metadata')
    outside = tmp_path/'outside'; outside.mkdir(); (outside/'movie.nfo').write_text('external')
    old = d.with_name('moved-source'); real_unlink = wash.os.unlink
    def replace(path, *args, **kwargs):
        if path == 'movie.nfo': d.rename(old); d.symlink_to(outside, target_is_directory=True)
        return real_unlink(path, *args, **kwargs)
    with patch.object(wash.os, 'unlink', replace): result = clean(d)
    assert result['count'] == 0 and result['errors']
    assert (outside/'movie.nfo').read_text() == 'external'


def test_no_backup_state_is_created_inside_source(roots, monkeypatch):
    d=media(); monkeypatch.setattr(engine,'STATE_DIR',d/'data'/'state')
    result=clean(d)
    assert result['count']==1 and not d.exists() and result['backup_root']==''



def test_mutation_busy_does_not_move(roots):
    d = media()
    with engine.mutation_lock(): result = clean(d)
    assert result['status'] == 'busy' and d.exists()
