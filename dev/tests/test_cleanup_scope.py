"""Entire-library and scoped directory previews; isolated roots only."""
import os
import sys
from pathlib import Path
from unittest.mock import patch
import pytest
os.environ.setdefault('WEB_PASSWORD','test-pass-123')
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from app import engine,wash
from app.routers import governance as router

@pytest.fixture
def roots(tmp_path,monkeypatch):
    for key,name in [('L_ROOT','local'),('S_ROOT','share'),('CLOUD_L_ROOT','cloud')]:
        root=tmp_path/name;root.mkdir();monkeypatch.setattr(engine,key,root)
    return tmp_path

def residue(root,title):
    p=root/'剧集/日韩剧集'/f'{title} {{tmdb-1}}'/'Season 1'/'E01.ass'
    p.parent.mkdir(parents=True);p.write_text('subtitle');return p

def test_all_combines_roots_and_reports_library_per_item_without_writes(roots):
    files=[residue(engine.L_ROOT,'local'),residue(engine.S_ROOT,'share')]
    before={str(p):p.read_bytes() for p in files}
    r=wash.scan_all_empty_dirs()
    assert r['status']=='success' and r['lib']=='all' and r['hits']==2
    assert {p['lib'] for p in r['preview']}=={'local','share'}
    assert [s['hits'] for s in r['scopes']]==[1,1]
    assert r['folders']==sum(s['folders'] for s in r['scopes'])
    assert before=={str(p):p.read_bytes() for p in files}

@pytest.mark.parametrize('tag',['local','share'])
def test_single_library_or_title_does_not_include_other_scope(roots,tag):
    root=engine.L_ROOT if tag=='local' else engine.S_ROOT
    chosen=residue(root,'chosen');residue(root,'other')
    residue(engine.S_ROOT if tag=='local' else engine.L_ROOT,'opposite')
    r=wash.scan_empty_dirs(str(chosen.parent.parent))
    assert r['hits']==1 and r['preview'][0]['lib']==tag
    assert r['preview'][0]['path']==str(chosen.parent.parent)
    assert wash.scan_empty_dirs(str(root))['hits']==2

def test_global_preview_limit_and_counts(roots):
    for n in range(3):residue(engine.L_ROOT,f'local{n}');residue(engine.S_ROOT,f'share{n}')
    r=wash.scan_all_empty_dirs(limit=2)
    assert r['hits']==6 and len(r['preview'])==r['preview_limit']==2
    assert {p['lib'] for p in r['preview']}=={'local','share'}
    assert [s['hits'] for s in r['scopes']]==[3,3]

def test_failed_second_root_does_not_publish_partial_preview(roots):
    residue(engine.L_ROOT,'local');engine.S_ROOT.rmdir()
    r=wash.scan_all_empty_dirs()
    assert r['status']=='error' and '分享库' in r['message'] and 'preview' not in r

@pytest.mark.parametrize('nested',[False,True])
def test_overlapping_roots_not_double_scanned(roots,monkeypatch,nested):
    monkeypatch.setattr(engine,'S_ROOT',engine.L_ROOT/'sub' if nested else engine.L_ROOT)
    assert wash.scan_all_empty_dirs()['status']=='error'

def test_media_and_unknown_files_still_protect_directories(roots):
    for root in [engine.L_ROOT,engine.S_ROOT]:
        p=residue(root,'media');(p.parent/'E01.strm').write_text('url')
        p=residue(root,'unknown');(p.parent/'unknown.bin').write_text('unknown')
    assert wash.scan_all_empty_dirs()['hits']==0

def test_api_requires_auth_explicit_scope_and_no_competing_path(roots):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app=FastAPI();app.include_router(router.router);client=TestClient(app)
    assert client.post('/api/wash/empty-dirs/scan',json={'scope':'all'}).status_code==401
    app.dependency_overrides[router.auth]=lambda:True
    residue(engine.L_ROOT,'local');residue(engine.S_ROOT,'share')
    assert client.post('/api/wash/empty-dirs/scan',json={'scope':'all'}).json()['hits']==2
    assert client.post('/api/wash/empty-dirs/scan',json={'path':str(engine.S_ROOT)}).json()['lib']=='share'
    for body in [{},{'scope':'bad'},{'scope':'all','path':str(engine.L_ROOT)}]:
        assert client.post('/api/wash/empty-dirs/scan',json=body).status_code==400
