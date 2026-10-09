"""Incomplete disk scans must preserve facts and block confirmed cleanup."""
from app import state_store as _state
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

os.environ.setdefault('WEB_PASSWORD', 'test-pass-123')
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app import engine, governance, lib, storage
from app.tasks import TaskManager
from app.routers import governance as router


@pytest.fixture
def media(tmp_path, monkeypatch):
    for key, sub in [('L_ROOT', 'local'), ('S_ROOT', 'share'), ('CLOUD_L_ROOT', 'cloud'),
                     ('DATA_DIR', 'data'), ('STATE_DIR', 'data/state')]:
        path = tmp_path / sub
        path.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(engine, key, path)
    monkeypatch.setattr(engine, 'GOV_LATEST_FILE', engine.STATE_DIR / 'gov_latest.json')
    monkeypatch.setattr(engine, 'LOCK_FILE', engine.DATA_DIR / 'agent.lock')
    monkeypatch.setattr(engine, '_strm_count_cache', {'ts': 1, 'local': 9, 'share': 8})
    monkeypatch.setenv('INGEST_QUIET_MINUTES', '0')
    monkeypatch.setattr(engine, '_strategy', lambda: {
        'decision': 'quality_first', 'match_strategy': 'title_year',
        'multi_season_protect': 'compare', 'tie_keep_local': False,
        'exempt_keywords': [], 'special_action': 'compare'})
    for name in ('notify_telegram', 'notify_emby_refresh', 'purge_old', '_save_strm_count_disk'):
        monkeypatch.setattr(engine, name, lambda *a, **kw: None)
    monkeypatch.setattr(governance, 'write_audit_log', lambda *a, **kw: None)
    engine._invalidate_lib_cache()
    yield
    engine._invalidate_lib_cache()


def touch(root, name='样本.1080p.strm'):
    path = root / '电影' / '样本 (2020)' / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('http://test.invalid/fictional-media')
    return path


def rows(root):
    # 比较真实 SQLite 行，包括 updated_at，证明失败扫描没有部分写入。
    return storage._execute('SELECT * FROM media_index WHERE root=? ORDER BY path',
                            (str(root),)).fetchall()


def seed():
    path = touch(engine.S_ROOT)
    engine.Lib(engine.S_ROOT)
    return path, rows(engine.S_ROOT)


def failed_walk(root, onerror=None):
    yield str(root), ['电影'], []
    yield str(Path(root) / '电影' / '新片 (2021)'), [], ['新增.strm']
    if onerror:
        onerror(PermissionError(13, 'Permission denied', str(Path(root) / '电影' / '样本 (2020)')))


def test_missing_root_is_error_and_preserves_index(media):
    path, before = seed()
    moved = engine.S_ROOT.with_name('offline'); engine.S_ROOT.rename(moved)
    with pytest.raises(lib.LibraryScanError, match='旧索引已保留'):
        engine.Lib(engine.S_ROOT)
    assert rows(engine.S_ROOT) == before


def test_root_is_file_is_error(media):
    engine.S_ROOT.rmdir(); engine.S_ROOT.write_text('not a directory')
    with pytest.raises(lib.LibraryScanError, match='不是目录'):
        engine.Lib(engine.S_ROOT)


def test_root_stat_permission_error(media):
    path, before = seed(); real_stat = Path.stat
    def denied(self, *args, **kwargs):
        if self == engine.S_ROOT: raise PermissionError(13, 'denied', str(self))
        return real_stat(self, *args, **kwargs)
    with patch.object(Path, 'stat', denied), pytest.raises(lib.LibraryScanError):
        engine.Lib(engine.S_ROOT)
    assert rows(engine.S_ROOT) == before


def test_walk_partial_error_does_not_upsert_or_delete(media):
    path, before = seed()
    new = engine.S_ROOT / '电影' / '新片 (2021)' / '新增.strm'
    new.parent.mkdir(); new.write_text('fake')
    with patch.object(lib.os, 'walk', failed_walk), pytest.raises(lib.LibraryScanError, match='Permission denied'):
        engine.Lib(engine.S_ROOT)
    assert rows(engine.S_ROOT) == before


@pytest.mark.parametrize('exception', [FileNotFoundError, PermissionError])
def test_file_stat_failure_does_not_publish_partial_index(media, exception):
    path, before = seed(); real_stat = Path.stat
    touch(engine.S_ROOT, '新版本.2160p.strm')
    def denied(self, *args, **kwargs):
        if self == path: raise exception(5, 'injected file failure', str(self))
        return real_stat(self, *args, **kwargs)
    with patch.object(Path, 'stat', denied), pytest.raises(lib.LibraryScanError):
        engine.Lib(engine.S_ROOT)
    assert rows(engine.S_ROOT) == before


def test_root_replaced_during_walk_does_not_commit(media):
    path, before = seed(); real_walk = lib.os.walk
    def replaced(root, **kwargs):
        yield from real_walk(root, **kwargs)
        moved = Path(root).with_name('old-root'); Path(root).rename(moved); Path(root).mkdir()
    with patch.object(lib.os, 'walk', replaced), pytest.raises(lib.LibraryScanError, match='根目录已更换'):
        engine.Lib(engine.S_ROOT)
    assert rows(engine.S_ROOT) == before


def test_empty_walk_without_root_visit_is_failure(media):
    path, before = seed()
    with patch.object(lib.os, 'walk', return_value=iter(())), pytest.raises(lib.LibraryScanError):
        engine.Lib(engine.S_ROOT)
    assert rows(engine.S_ROOT) == before


def test_symlink_directory_is_not_silently_skipped(media, tmp_path):
    path, before = seed(); outside = tmp_path / 'external'; outside.mkdir()
    (engine.S_ROOT / 'linked').symlink_to(outside, target_is_directory=True)
    with pytest.raises(lib.LibraryScanError, match='目录软链接'):
        engine.Lib(engine.S_ROOT)
    assert rows(engine.S_ROOT) == before


def test_symlink_strm_is_failure(media):
    path, before = seed(); link = path.with_name('linked.strm'); link.symlink_to(path)
    with pytest.raises(lib.LibraryScanError, match='软链接'):
        engine.Lib(engine.S_ROOT)
    assert rows(engine.S_ROOT) == before


def test_readable_empty_root_removes_genuinely_deleted_index(media):
    path, before = seed(); path.unlink()
    assert engine.Lib(engine.S_ROOT).strm_count == 0
    assert rows(engine.S_ROOT) == []


def test_failed_build_is_not_cached_and_retry_succeeds(media):
    path, before = seed(); moved = engine.S_ROOT.with_name('offline'); engine.S_ROOT.rename(moved)
    with pytest.raises(lib.LibraryScanError): engine._get_lib(engine.S_ROOT)
    assert str(engine.S_ROOT) not in lib._lib_cache
    assert str(engine.S_ROOT) not in lib._lib_building
    moved.rename(engine.S_ROOT)
    assert engine._get_lib(engine.S_ROOT).strm_count == 1


def check():
    return engine.action_inter_check(SimpleNamespace(silent=True))


def test_failed_scan_preserves_success_snapshot_counts_and_plan(media):
    low = touch(engine.S_ROOT, '样本.720p.strm'); high = touch(engine.S_ROOT)
    assert check()['status'] == 'success'
    latest_before = json.dumps(_state.read(engine.GOV_LATEST_FILE), sort_keys=True).encode()
    pid = engine.load_latest_scan()['plan_id']
    plan_path = engine.STATE_DIR / f'plan_{pid}.json'; plan_before = json.dumps(_state.read(plan_path), sort_keys=True).encode()
    counts = dict(engine._strm_count_cache); index_before = rows(engine.S_ROOT)
    # 缓存尚在 TTL 内，明确重扫也必须发现此故障。
    engine._get_lib(engine.S_ROOT)
    engine.L_ROOT.rmdir()
    result = check()
    assert result['status'] == 'error' and result['code'] == 'library_scan_failed'
    assert json.dumps(_state.read(engine.GOV_LATEST_FILE), sort_keys=True).encode() == latest_before
    assert engine._strm_count_cache == counts and rows(engine.S_ROOT) == index_before
    assert json.dumps(_state.read(plan_path), sort_keys=True).encode() == plan_before
    assert governance.load_scan_status()['status'] == 'failed'
    assert lib._lib_cache == {}
    for dry in (True, False):
        result = engine.action_inter_clean(SimpleNamespace(plan=pid, dry_run=dry, notify=False))
        assert result['code'] == 'library_scan_failed'
    assert low.exists() and high.exists() and json.dumps(_state.read(plan_path), sort_keys=True).encode() == plan_before


def test_failed_scan_api_and_task_error_then_recovery(media, monkeypatch):
    touch(engine.S_ROOT)
    assert check()['status'] == 'success'
    before = json.dumps(_state.read(engine.GOV_LATEST_FILE), sort_keys=True).encode()
    engine.L_ROOT.rmdir()
    manager = TaskManager(); task = manager.spawn('inter_check', check)
    assert task.done.wait(5) and task.status == 'error'
    assert str(engine.L_ROOT) in task.error
    monkeypatch.setattr(engine, 'daily_consistency_snapshot', lambda **kw: {
        'rule_sig': engine._current_rule_snapshot()['sig']})
    assert router.api_gov_latest()['reason'] == 'scan_failed'
    summary = router.api_governance_summary()['scan']
    assert not summary['usable'] and summary['result']['strm_counts']['share'] == 1
    engine.L_ROOT.mkdir()
    assert check()['status'] == 'success'
    assert json.dumps(_state.read(engine.GOV_LATEST_FILE), sort_keys=True).encode() != before
    assert governance.load_scan_status()['status'] == 'success'
    assert router.api_gov_latest()['usable'] and router.api_governance_summary()['scan']['usable']


def test_unreadable_or_corrupt_scan_status_blocks_cleanup(media):
    path = engine.STATE_DIR / 'gov_scan_status.json'; _state.save(path, {'status': 'failed', 'message': '状态无法读取'})
    result = engine.action_inter_clean(SimpleNamespace(plan='abc', dry_run=True, notify=False))
    assert result['code'] == 'library_scan_failed'
    _state.save(path, [])
    assert governance.load_scan_status()['status'] == 'failed'
