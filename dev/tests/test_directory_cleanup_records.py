"""Directory cleanup writes lasting records with actual NAS/115 results."""
import os
import sys
from pathlib import Path
from unittest.mock import patch
import pytest

os.environ.setdefault('WEB_PASSWORD', 'test-pass-123')
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app import engine, wash, cloud_residue as cloud, logger


@pytest.fixture
def roots(tmp_path, monkeypatch):
    for key, name in [('L_ROOT', 'local'), ('S_ROOT', 'share'), ('CLOUD_L_ROOT', 'cloud'),
                      ('DATA_DIR', 'data'), ('STATE_DIR', 'data/state')]:
        path = tmp_path / name; path.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(engine, key, path)
    monkeypatch.setattr(engine, 'LOCK_FILE', engine.DATA_DIR / 'agent.lock')
    monkeypatch.setattr(engine, 'notify_emby_deleted', lambda *a, **k: None)
    monkeypatch.setattr(logger, 'RECORDS_DIR', engine.DATA_DIR / 'records')
    monkeypatch.setattr(logger, 'RECORDS_FILE', engine.DATA_DIR / 'records/audit.jsonl')


def put(root, title='show', name='E01.ass'):
    path = root / '剧集/日韩剧集' / (title + ' {tmdb-1}') / 'Season 1' / name
    path.parent.mkdir(parents=True, exist_ok=True); path.write_text('temporary')
    return path


def preview(): return cloud.scan_directory_cleanup(scope='all')['preview']


def test_cleanup_counts_and_paths_persist_and_are_visible_in_records_api(roots, monkeypatch):
    from app.routers import governance, library
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI(); app.include_router(governance.router); app.include_router(library.router)
    app.dependency_overrides[governance.auth] = lambda: True
    monkeypatch.setattr(engine, 'daily_consistency_snapshot', lambda *a: {})
    client = TestClient(app)
    local = put(engine.L_ROOT); source = put(engine.CLOUD_L_ROOT); share = put(engine.S_ROOT)
    assert client.get('/api/records').json()['records'] == []
    items = client.post('/api/wash/empty-dirs/scan', json={'scope': 'all'}).json()['preview']
    assert client.get('/api/records').json()['records'] == []  # read-only preview creates no execution record
    result = client.post('/api/wash/empty-dirs/clean', json={'items': items}).json()
    assert result['audit_recorded'] and result['requested_count'] == 2 and result['failed_count'] == 0
    record = client.get('/api/records').json()['records'][0]
    assert record['category'] == '目录清理' and '清理完成' in record['title']
    summary = record['extra']['summary']
    assert summary['nas_count'] == summary['files_removed'] == 2
    assert summary['cloud_count'] == summary['cloud_files_removed'] == 1
    rows = record['extra']['cleanup_items']
    assert {x['lib'] for x in rows} == {'local', 'share'}
    assert all(x['status'] == 'success' for x in rows)
    assert any(x['cloud_path'] == str(source.parent.parent) for x in rows)
    # Fresh API client reads the persisted entry; deletion of source directories doesn't erase it.
    fresh = TestClient(app); persisted = fresh.get('/api/records/' + str(record['id'])).json()['record']
    assert persisted['extra'] == record['extra']
    assert not local.exists() and not source.exists() and not share.exists()


def test_skipped_item_records_reason_without_claiming_success(roots):
    local = put(engine.L_ROOT); source = put(engine.CLOUD_L_ROOT, name='E01.mkv')
    result = cloud.clean_directory_cleanup(preview())
    assert result['count'] == 0 and result['failed_count'] == 1 and result['audit_recorded']
    record = logger.read_recent(1)[0]; row = record['extra']['cleanup_items'][0]
    assert row['status'] == 'skipped' and any('存在媒体文件' in x for x in row['errors'])
    assert '未完成' in record['title'] and local.exists() and source.exists()


def test_partial_cloud_cleanup_records_actual_file_count_and_one_failed_item(roots):
    local = put(engine.L_ROOT); source = put(engine.CLOUD_L_ROOT)
    extra = put(engine.CLOUD_L_ROOT, name='E01.json'); items = preview()
    real = wash.os.unlink
    def unlink(name, *a, **k):
        if name == 'E01.ass': raise PermissionError('temporary denied')
        return real(name, *a, **k)
    with patch.object(wash.os, 'unlink', unlink): result = cloud.clean_directory_cleanup(items)
    assert result['cloud_files_removed'] == 1 and result['failed_count'] == 1
    assert len(result['errors']) > result['failed_count']
    row = logger.read_recent(1)[0]['extra']['cleanup_items'][0]
    assert row['status'] == 'partial' and row['cloud_files_removed'] == 1
    assert row['nas_count'] == row['files_removed'] == 0
    assert local.exists() and source.exists() and not extra.exists()


def test_duplicate_targets_produce_one_entry_and_one_record(roots):
    put(engine.S_ROOT); items = preview()
    result = cloud.clean_directory_cleanup(items + items)
    assert result['requested_count'] == 1 and len(logger.read_recent(10)) == 1
    assert len(logger.read_recent(1)[0]['extra']['cleanup_items']) == 1


def test_busy_attempt_does_not_create_a_completed_execution_record(roots):
    put(engine.S_ROOT); items = preview()
    with wash.mutation_lock(): result = cloud.clean_directory_cleanup(items)
    assert result['status'] == 'busy' and logger.read_recent(10) == []


def test_record_write_failure_does_not_hide_successful_media_cleanup(roots, monkeypatch):
    share = put(engine.S_ROOT)
    monkeypatch.setattr(logger._storage, 'db_add_audit', lambda *a: False)
    result = cloud.clean_directory_cleanup(preview())
    assert result['nas_count'] == 1 and not share.exists()
    assert not result['audit_recorded'] and result['audit_error']


def test_logger_exception_preserves_cleanup_result_and_returns_reason(roots, monkeypatch):
    share = put(engine.S_ROOT); items = preview()
    def fail(*a, **k): raise OSError('temporary record denied')
    monkeypatch.setattr(logger, 'write', fail)
    result = cloud.clean_directory_cleanup(items)
    assert result['status'] == 'success' and result['nas_count'] == 1 and not share.exists()
    assert 'temporary record denied' in result['audit_error']
