import os
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from strimkeep.inventory_orphans import clean_one, inventory, fingerprint, scan
from strimkeep.inventory_delete_adapter import cleanup_empty_work


def setup_tree(tmp_path):
    root = tmp_path / 'strm'; (root / 'cloud').mkdir(parents=True); (root / '分享影视库').mkdir()
    work = root / 'cloud' / '剧集' / 'Example {tmdb-42}'
    season = work / 'Season 1'; season.mkdir(parents=True)
    (work / 'poster.jpg').write_bytes(b'poster')
    (work / 'tvshow.nfo').write_text('show')
    (season / 'S01E01.zh.srt').write_text('subtitle')
    return root, work, season


def test_scan_and_clean_orphan_backups_and_keeps_roots(tmp_path):
    root, work, season = setup_tree(tmp_path)
    result = scan(root, ['cloud', '分享影视库'])
    assert len(result['items']) == 1
    item = result['items'][0]
    result = clean_one(root, ['cloud', '分享影视库'], item['path'], item['fingerprint'], tmp_path / 'backups', backup=True)
    assert (result['files_removed'], result['dirs_removed']) == (3, 2)
    assert not work.exists() and (root / 'cloud').is_dir() and not result['errors']
    with tarfile.open(result['backup']) as archive:
        assert archive.extractfile(item['path'] + '/poster.jpg').read() == b'poster'


def test_live_video_unknown_and_changed_directory_are_never_removed(tmp_path):
    root, work, season = setup_tree(tmp_path)
    item = scan(root, ['cloud', '分享影视库'])['items'][0]
    (season / 'new.strm').write_text('http://example')
    assert not scan(root, ['cloud', '分享影视库'])['items']
    with pytest.raises(ValueError):
        clean_one(root, ['cloud', '分享影视库'], item['path'], item['fingerprint'], tmp_path / 'backups')
    (season / 'new.strm').unlink(); (work / 'user.bin').write_bytes(b'user')
    assert not scan(root, ['cloud', '分享影视库'])['items']
    assert (work / 'poster.jpg').exists() and (work / 'user.bin').exists()


def test_scan_only_reports_unusual_protected_content_for_review(tmp_path):
    root, work, season = setup_tree(tmp_path)
    video = season / 'new.strm'; video.write_text('link')
    result = scan(root, ['cloud', '分享影视库'])
    assert result['skipped'] == 1 and result['attention_count'] == 0
    video.unlink()
    (season / 'unknown.bin').write_bytes(b'other')
    result = scan(root, ['cloud', '分享影视库'])
    assert result['attention_count'] == 1 and result['attention_items'][0]['path'].endswith('Example {tmdb-42}')


def test_symlink_and_library_escape_are_rejected(tmp_path):
    root, work, season = setup_tree(tmp_path)
    (season / 'link.nfo').symlink_to(tmp_path / 'outside')
    assert not scan(root, ['cloud', '分享影视库'])['items']
    with pytest.raises(ValueError): inventory(root, '../outside {tmdb-1}', ['cloud'])
    with pytest.raises(ValueError): inventory(root, 'cloud', ['cloud'])


def test_post_delete_removes_show_sidecars_but_preserves_other_episode(tmp_path):
    import sqlite3
    root, work, season = setup_tree(tmp_path)
    path = season / 'S01E02.strm'; path.write_text('video')
    db = sqlite3.connect(':memory:'); db.row_factory = sqlite3.Row
    db.executescript('create table organized_files(file_id text,relative_path text); create table share_strms(id text,strm_path text,revoked integer);')
    store = SimpleNamespace(root=tmp_path / 'data', db=db, get=lambda k, default=None: {'library_dir_name':'cloud'})
    engine = SimpleNamespace(root=root, store=store)
    cleanup_empty_work(engine, (season / 'S01E01.strm').relative_to(root).as_posix())
    assert path.exists() and (work / 'poster.jpg').exists()
    path.unlink()
    cleanup_empty_work(engine, (season / 'S01E01.strm').relative_to(root).as_posix())
    assert not work.exists() and not (store.root / 'cleanup' / 'orphans').exists()


def test_post_delete_ignores_stale_registration_and_cleans_empty_work(tmp_path):
    import sqlite3
    root, work, season = setup_tree(tmp_path)
    db = sqlite3.connect(':memory:'); db.row_factory = sqlite3.Row
    db.executescript('create table organized_files(file_id text,relative_path text); create table share_strms(id text,strm_path text,revoked integer);')
    relative = (season / 'missing.strm').relative_to(root).as_posix()
    db.execute('insert into organized_files values (?,?)', ('keep', relative))
    store = SimpleNamespace(root=tmp_path / 'data', db=db, get=lambda k, default=None: {'library_dir_name':'cloud'})
    cleanup_empty_work(SimpleNamespace(root=root, store=store), relative)
    # 目录是事实：文件已经不在磁盘上，残留的登记行不该把空作品目录留住。
    assert not work.exists()


def test_http_scan_and_clean_recheck_registered_inventory(tmp_path):
    import asyncio
    import sqlite3
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient
    from strimkeep.inventory_api import install_inventory
    root, work, season = setup_tree(tmp_path)
    db = sqlite3.connect(':memory:', check_same_thread=False); db.row_factory = sqlite3.Row
    db.executescript('create table meta(key text primary key,value text); create table organized_files(file_id text,relative_path text); create table share_strms(id text,strm_path text,revoked integer); create table organize_jobs(state text); create table share_wash_intents(id text);')
    store = SimpleNamespace(root=tmp_path / 'data', db=db, get=lambda k, default=None: {'library_dir_name':'cloud'} if k == 'organize_config' else default)
    app = FastAPI(); app.state.strms = root; app.state.organizer = SimpleNamespace(publication_gate=asyncio.Lock())
    def admin(): raise HTTPException(401)
    install_inventory(app, store, admin)
    with TestClient(app) as client:
        assert client.post('/api/v1/inventory/orphans/scan').status_code == 401
        app.dependency_overrides[admin] = lambda: None
        found = client.post('/api/v1/inventory/orphans/scan').json()['items']
        assert len(found) == 1
        selection = {'items':[{'path':found[0]['path'], 'fingerprint':found[0]['fingerprint']}]}
        db.execute('insert into organized_files values (?,?)', ('1', found[0]['path']+'/Season 1/missing.strm')); db.commit()
        assert not client.post('/api/v1/inventory/orphans/scan').json()['items']
        rejected = client.post('/api/v1/inventory/orphans/clean', json=selection).json()['items'][0]
        assert rejected['errors'] and work.exists()
        db.execute('delete from organized_files'); db.commit()
        cleaned = client.post('/api/v1/inventory/orphans/clean', json=selection).json()['items'][0]
        assert not cleaned['errors'] and cleaned['files_removed'] == 3 and not work.exists()
    db.close()


def test_deleted_episode_sidecar_scan_and_backup_preserves_remaining_episode(tmp_path):
    from strimkeep.inventory_orphans import sidecars_for_deleted, clean_sidecar
    root, work, season = setup_tree(tmp_path)
    (season / 'S01E02.strm').write_text('other video')
    source = (season / 'S01E01.strm').relative_to(root).as_posix()
    candidates = sidecars_for_deleted(root, ['cloud','分享影视库'], source)
    assert len(candidates) == 1 and candidates[0]['path'].endswith('S01E01.zh.srt')
    item = candidates[0]
    outcome = clean_sidecar(root, ['cloud','分享影视库'], source, item['path'], item['fingerprint'], tmp_path/'backup', backup=True)
    assert outcome['files_removed'] == 1 and (season/'S01E02.strm').exists() and (work/'poster.jpg').exists()
    with tarfile.open(outcome['backup']) as archive:
        assert archive.extractfile(item['path']).read() == b'subtitle'


def test_deleted_episode_reappearance_or_overlapping_video_blocks_sidecar(tmp_path):
    from strimkeep.inventory_orphans import sidecars_for_deleted, clean_sidecar
    root, work, season = setup_tree(tmp_path)
    source = (season / 'S01E01.strm').relative_to(root).as_posix()
    item = sidecars_for_deleted(root, ['cloud','分享影视库'], source)[0]
    (season / 'S01E01.strm').write_text('restored')
    with pytest.raises(ValueError): clean_sidecar(root, ['cloud','分享影视库'], source, item['path'], item['fingerprint'], tmp_path/'backup')
    (season / 'S01E01.strm').unlink()
    (season / 'S01E01.zh.strm').write_text('other version')
    assert not sidecars_for_deleted(root, ['cloud','分享影视库'], source)
    assert (season / 'S01E01.zh.srt').exists()


def test_http_recent_deleted_episode_sidecars_are_cleaned_independently(tmp_path):
    import asyncio, sqlite3
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient
    from strimkeep.inventory_api import install_inventory
    from strimkeep.internal_delete import remember
    root, work, season = setup_tree(tmp_path)
    (season/'S01E02.strm').write_text('surviving')
    source=(season/'S01E01.strm').relative_to(root).as_posix()
    db=sqlite3.connect(':memory:',check_same_thread=False);db.row_factory=sqlite3.Row
    db.executescript('create table meta(key text primary key,value text); create table organized_files(file_id text,relative_path text); create table share_strms(id text,strm_path text,revoked integer); create table organize_jobs(state text); create table share_wash_intents(id text);')
    store=SimpleNamespace(root=tmp_path/'data',db=db,get=lambda k,default=None: {'library_dir_name':'cloud'} if k=='organize_config' else default)
    remember(store,[source])
    app=FastAPI();app.state.strms=root;app.state.organizer=SimpleNamespace(publication_gate=asyncio.Lock())
    def admin(): raise HTTPException(401)
    install_inventory(app,store,admin);app.dependency_overrides[admin]=lambda:None
    with TestClient(app) as client:
        found=client.post('/api/v1/inventory/orphans/scan').json()['items']
        assert len(found)==1 and found[0]['kind']=='file'
        row=found[0]
        result=client.post('/api/v1/inventory/orphans/clean',json={'items':[{k:row[k] for k in ('path','source','kind','fingerprint')}]}).json()['items'][0]
        assert result['files_removed']==1 and not result['errors'] and (season/'S01E02.strm').exists()
    db.close()


def test_scan_reports_why_directories_were_skipped(tmp_path):
    """跳过必须给出原因，否则用户没法判断下一步（现场 229 个目录只给了一个数字）。"""
    from strimkeep.inventory_orphans import scan

    library = tmp_path / '媒体库'
    (library / '作品A (2020) {tmdb-1}').mkdir(parents=True)
    (library / '作品A (2020) {tmdb-1}' / 'poster.jpg').write_bytes(b'x')
    (library / '作品B (2020) {tmdb-2}').mkdir(parents=True)
    (library / '作品B (2020) {tmdb-2}' / 'S01E01.strm').write_text('http://x', encoding='utf-8')
    (library / '作品C (2020) {tmdb-3}').symlink_to(library / '作品A (2020) {tmdb-1}')

    result = scan(tmp_path, ['媒体库'])
    assert [item['path'] for item in result['items']] == ['媒体库/作品A (2020) {tmdb-1}']
    reasons = {item['path']: item['reason'] for item in result['skipped_items']}
    assert reasons['媒体库/作品B (2020) {tmdb-2}'] == 'has_media_or_unknown'
    assert reasons['媒体库/作品C (2020) {tmdb-3}'] == 'symlink'
    assert result['reasons'] == {'has_media_or_unknown': 1, 'symlink': 1}
    assert result['skipped'] == 2


def test_clean_without_backup_removes_entries_and_reports_no_archive(tmp_path):
    """备份是可选的：不备份时仍复核指纹，但不再生成 tar。"""
    from strimkeep.inventory_orphans import clean_one, fingerprint, inventory

    library = tmp_path / '媒体库'
    target = library / '作品A (2020) {tmdb-1}'
    (target / 'extras').mkdir(parents=True)
    (target / 'poster.jpg').write_bytes(b'x')
    (target / 'extras' / 'fanart.jpg').write_bytes(b'y')
    entries = inventory(tmp_path, '媒体库/作品A (2020) {tmdb-1}', ['媒体库'])

    row = clean_one(tmp_path, ['媒体库'], '媒体库/作品A (2020) {tmdb-1}',
                    fingerprint(entries), tmp_path / 'cleanup' / 'orphans', backup=False)
    assert row['files_removed'] == 2 and row['dirs_removed'] == 2
    assert row['backup'] == '' and not (tmp_path / 'cleanup').exists()
    assert not target.exists()
