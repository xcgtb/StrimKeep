"""Regression: NAS previews never traverse CD2; execution visits the mapped target only."""
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
    for key, name in [('L_ROOT', 'local'), ('S_ROOT', 'share'), ('CLOUD_L_ROOT', 'cloud'),
                      ('DATA_DIR', 'data'), ('STATE_DIR', 'data/state')]:
        root = tmp_path / name; root.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(engine, key, root)
    monkeypatch.setattr(engine, 'LOCK_FILE', engine.DATA_DIR / 'agent.lock')
    monkeypatch.setattr(engine, 'notify_emby_deleted', lambda *a, **k: None)


def put(root, title='show', filename='E01.ass'):
    path = root / '剧集/日韩剧集' / (title + ' {tmdb-1}') / 'Season 1' / filename
    path.parent.mkdir(parents=True, exist_ok=True); path.write_text('temporary')
    return path


def test_preview_performs_zero_cloud_filesystem_reads(roots, monkeypatch):
    local = put(engine.L_ROOT); put(engine.S_ROOT); source = put(engine.CLOUD_L_ROOT)
    def guarded(fn):
        def call(path, *a, **k):
            target = Path(path)
            if target == engine.CLOUD_L_ROOT or engine.CLOUD_L_ROOT in target.parents:
                raise AssertionError('preview must never read CD2: ' + str(target))
            return fn(path, *a, **k)
        return call
    with patch.object(Path, 'stat', guarded(Path.stat)), patch.object(Path, 'lstat', guarded(Path.lstat)), \
         patch.object(wash.os, 'scandir', guarded(wash.os.scandir)):
        result = cloud.scan_directory_cleanup(scope='all')
        assert result['status'] == 'success' and result['hits'] == 2
        item = next(x for x in result['preview'] if x['lib'] == 'local')
        assert item['cloud_path'] == str(source.parent.parent)
        assert item['cloud_file_count'] is None and not item['cloud_checked']
    assert local.exists() and source.exists()


def test_cleanup_reads_only_selected_cloud_title_not_library_or_sibling(roots):
    local = put(engine.L_ROOT); source = put(engine.CLOUD_L_ROOT)
    other = put(engine.CLOUD_L_ROOT, title='other', filename='E01.mkv')
    result = cloud.scan_directory_cleanup(str(local.parent.parent))
    seen = []; real = wash.os.scandir
    def scan(path):
        target = Path(path)
        if target == engine.CLOUD_L_ROOT or engine.CLOUD_L_ROOT in target.parents:
            seen.append(target)
            assert target == source.parent.parent or source.parent.parent in target.parents
        return real(path)
    with patch.object(wash.os, 'scandir', scan):
        outcome = cloud.clean_directory_cleanup(result['preview'])
    assert outcome['nas_count'] == outcome['cloud_count'] == 1 and seen
    assert not local.exists() and not source.exists() and other.exists()


def test_empty_nas_title_maps_and_deletes_cloud_subtitles_without_strm(roots):
    local = put(engine.L_ROOT); local.unlink()
    source = put(engine.CLOUD_L_ROOT); extra = put(engine.CLOUD_L_ROOT, filename='E01.json')
    outcome = cloud.clean_directory_cleanup(cloud.scan_directory_cleanup(str(engine.L_ROOT))['preview'])
    assert outcome['cloud_files_removed'] == 2 and not outcome['errors']
    assert not source.exists() and not extra.exists() and not local.parent.parent.exists()


def test_share_preview_and_cleanup_do_not_resolve_cloud_root(roots, monkeypatch):
    share = put(engine.S_ROOT); source = put(engine.CLOUD_L_ROOT)
    monkeypatch.setattr(cloud, '_configured_cloud_root', lambda: pytest.fail('share must not map cloud'))
    outcome = cloud.clean_directory_cleanup(cloud.scan_directory_cleanup(str(engine.S_ROOT))['preview'])
    assert outcome['nas_count'] == 1 and outcome['cloud_count'] == 0
    assert not share.exists() and source.exists()


def test_scanning_resumes_after_busy_response(roots):
    from app.routers import governance as router
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI(); app.include_router(router.router)
    app.dependency_overrides[router.auth] = lambda: True
    client = TestClient(app); put(engine.L_ROOT)
    with router._orphan_lock:
        assert client.post('/api/wash/empty-dirs/scan', json={'scope': 'all'}).status_code == 409
    result = client.post('/api/wash/empty-dirs/scan', json={'scope': 'all'})
    assert result.status_code == 200 and result.json()['hits'] == 1


def test_cloud_directory_without_nas_reference_is_not_enumerated(roots):
    source = put(engine.CLOUD_L_ROOT)
    result = cloud.scan_directory_cleanup(scope='all')
    assert result['hits'] == 0 and not result['preview'] and source.exists()
