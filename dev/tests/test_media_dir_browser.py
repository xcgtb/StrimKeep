"""Read-only media-root browsing for directory selection."""
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

os.environ.setdefault('WEB_PASSWORD', 'test-pass-123')
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app import engine, wash
from app.routers import governance as router


@pytest.fixture
def roots(tmp_path, monkeypatch):
    for key, sub in [('L_ROOT', 'actual-local'), ('S_ROOT', 'actual-share')]:
        path = tmp_path / sub; path.mkdir()
        monkeypatch.setattr(engine, key, path)
    return tmp_path


def browse(path='', **kwargs): return wash.browse_media_dirs(str(path), **kwargs)


def test_lists_only_configured_roots(roots):
    result = browse()
    assert result['status'] == 'success' and result['path'] == ''
    assert [p['path'] for p in result['roots']] == [str(engine.L_ROOT), str(engine.S_ROOT)]
    assert [p['name'] for p in result['roots']] == ['本地库', '分享库']
    assert all(p['available'] for p in result['roots'])


def test_browses_only_immediate_dirs_not_files_or_symlinks(roots):
    (engine.L_ROOT/'电影'/'片名').mkdir(parents=True)
    (engine.L_ROOT/'剧集').mkdir(); (engine.L_ROOT/'media.strm').write_text('fake')
    outside = roots/'external'; outside.mkdir()
    (engine.L_ROOT/'link').symlink_to(outside, target_is_directory=True)
    result = browse(engine.L_ROOT)
    assert {p['name'] for p in result['items']} == {'电影', '剧集'}
    assert result['parent'] == '' and len(result['breadcrumbs']) == 1


def test_directory_breadcrumbs_never_go_above_root(roots):
    child = engine.S_ROOT/'剧集'/'日韩剧集'; child.mkdir(parents=True)
    result = browse(child)
    assert result['parent'] == str(child.parent) and result['lib'] == 'share'
    assert [p['path'] for p in result['breadcrumbs']] == [str(engine.S_ROOT), str(child.parent), str(child)]


def test_empty_directory_can_be_selected(roots):
    child = engine.L_ROOT/'空目录'; child.mkdir()
    result = browse(child)
    assert result['status'] == 'success' and result['path'] == str(child) and result['items'] == []


@pytest.mark.parametrize('kind', ['outside', 'relative', 'traversal', 'missing'])
def test_invalid_or_missing_paths_rejected(roots, kind):
    raw = {'outside': roots, 'relative': 'relative',
           'traversal': str(engine.L_ROOT)+'/../actual-share', 'missing': engine.L_ROOT/'missing'}[kind]
    assert browse(raw)['status'] == 'error'


def test_symlink_path_and_ancestor_rejected(roots):
    real = engine.L_ROOT/'real'; (real/'child').mkdir(parents=True)
    link = engine.L_ROOT/'link'; link.symlink_to(real, target_is_directory=True)
    assert browse(link)['status'] == 'error'
    assert browse(link/'child')['status'] == 'error'


def test_unavailable_root_visible_but_not_selectable(roots):
    engine.S_ROOT.rmdir()
    result = browse()
    assert result['roots'][0]['available'] and not result['roots'][1]['available']
    assert result['roots'][1]['message']


def test_permission_error_returns_no_partial_items(roots):
    d = engine.L_ROOT/'denied'; d.mkdir(); real_scandir = wash.os.scandir
    def denied(path):
        if Path(path) == d: raise PermissionError('denied')
        return real_scandir(path)
    with patch.object(wash.os, 'scandir', denied): result = browse(d)
    assert result['status'] == 'error' and 'denied' in result['message'] and not result.get('items')


def test_pagination_does_not_hide_directories(roots):
    for i in range(205): (engine.L_ROOT/f'folder-{i:03}').mkdir()
    first, second = browse(engine.L_ROOT), browse(engine.L_ROOT, offset=200)
    assert len(first['items']) == 200 and first['has_more'] and first['total'] == 205
    assert len(second['items']) == 5 and not second['has_more']
    assert len({p['path'] for p in first['items']+second['items']}) == 205


def test_search_unicode_and_quoted_directory_names(roots):
    name = '请回答1988 "字幕" <test> {tmdb-64010}'
    (engine.L_ROOT/name).mkdir(); (engine.L_ROOT/'OTHER').mkdir()
    result = browse(engine.L_ROOT, search='1988')
    assert result['items'] == [{'name': name, 'path': str(engine.L_ROOT/name)}]
    assert browse(engine.L_ROOT, search='other')['items'][0]['name'] == 'OTHER'


def test_browse_does_not_scan_recursive_or_write(roots):
    child = engine.L_ROOT/'电影'; child.mkdir(); (child/'movie.nfo').write_text('metadata')
    before = sorted(str(p) for p in roots.rglob('*'))
    with patch.object(wash.os, 'walk', side_effect=AssertionError('recursive walk')):
        assert browse(engine.L_ROOT)['status'] == 'success'
    assert sorted(str(p) for p in roots.rglob('*')) == before


def test_route_auth_and_live_configured_root_browsing(roots):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI(); app.include_router(router.router)
    client = TestClient(app)
    assert client.get('/api/wash/empty-dirs/browse').status_code == 401
    app.dependency_overrides[router.auth] = lambda: True
    response = client.get('/api/wash/empty-dirs/browse', params={'path': str(engine.L_ROOT)})
    assert response.status_code == 200 and response.json()['path'] == str(engine.L_ROOT)


def test_subtitle_only_preview_is_not_called_an_empty_directory(roots):
    d = engine.L_ROOT/'剧集'/'请回答1988 (2015) {tmdb-64010}'/'Season 1'; d.mkdir(parents=True)
    for i in range(1, 21): (d/f'S01E{i:02}.zh-cn.ass').write_text('fake subtitle')
    result = engine.scan_empty_dirs(str(d.parent))
    item = result['preview'][0]
    assert item['type'] == '附属文件残留' and item['file_count'] == 20 and item['extensions'] == {'.ass':20}


def test_empty_preview_and_mixed_metadata_counts(roots):
    first = engine.L_ROOT/'空 {tmdb-1}'; first.mkdir()
    second = engine.L_ROOT/'附属 {tmdb-2}'; second.mkdir()
    for name in ['poster.JPG','movie.nfo','subtitle.srt']: (second/name).write_text('fake')
    items = {p['path']:p for p in engine.scan_empty_dirs(str(engine.L_ROOT))['preview']}
    assert items[str(first)]['type'] == '空目录' and items[str(first)]['file_count'] == 0
    assert items[str(second)]['file_count'] == 3 and items[str(second)]['extensions'] == {'.jpg':1,'.nfo':1,'.srt':1}
