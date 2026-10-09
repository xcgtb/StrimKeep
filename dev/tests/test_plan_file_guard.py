"""Execute plans against controlled file changes, without production media."""
from app import state_store as _state
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault('WEB_PASSWORD', 'test-pass-123')
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app import engine, governance, plan_guard
from app.routers import governance as router


def setup(tmp_path, monkeypatch):
    for key, sub in [('L_ROOT', 'local'), ('S_ROOT', 'share'), ('CLOUD_L_ROOT', 'cloud'),
                     ('DATA_DIR', 'data'), ('STATE_DIR', 'data/state')]:
        p = tmp_path / sub; p.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(engine, key, p)
    for key, sub in [('LOCK_FILE', 'data/agent.lock'), ('GOV_LATEST_FILE', 'data/state/gov_latest.json')]:
        monkeypatch.setattr(engine, key, tmp_path / sub)
    monkeypatch.setenv('INGEST_QUIET_MINUTES', '0')
    monkeypatch.setattr(engine, '_get_lib', lambda root: engine.Lib(root))
    monkeypatch.setattr(engine, '_strategy', lambda: {
        'decision': 'quality_first', 'match_strategy': 'title_year',
        'multi_season_protect': 'compare', 'tie_keep_local': False,
        'exempt_keywords': [], 'special_action': 'compare'})
    for key in ['notify_emby_refresh', 'notify_telegram', 'purge_old']:
        monkeypatch.setattr(engine, key, lambda *a, **kw: False)
    monkeypatch.setattr(governance, 'write_audit_log', lambda *a, **kw: None)


def touch(root, rel, text='http://test.invalid/fake'):
    p = root / rel; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(text)
    return p


def duplicate(title='样本 (2020)'):
    low = touch(engine.S_ROOT, f'电影/华语电影/{title}/样本.720p.strm')
    high = touch(engine.S_ROOT, f'电影/华语电影/{title}/样本.1080p.strm')
    return low, high


def execute(pid, dry=False):
    return engine.action_inter_clean(SimpleNamespace(plan=pid, dry_run=dry, notify=False))


class TestPlanFileGuard:
    def test_missing_retained_copy_keeps_last_copy(self, tmp_path, monkeypatch):
        setup(tmp_path, monkeypatch); low, high = duplicate()
        pid = engine.save_plan(engine.build_plan()); high.unlink()
        result = execute(pid)
        assert low.exists() and result['skipped'] == 1
        assert any('保留依据' in line for line in result['detail'])

    def test_content_replaced_with_original_size_and_mtime_is_skipped(self, tmp_path, monkeypatch):
        setup(tmp_path, monkeypatch); low, high = duplicate()
        pid = engine.save_plan(engine.build_plan()); st = low.stat()
        low.write_text('X' * st.st_size); os.utime(low, ns=(st.st_atime_ns, st.st_mtime_ns))
        result = execute(pid)
        assert low.exists() and high.exists() and result['skipped'] == 1

    def test_same_content_new_inode_is_skipped(self, tmp_path, monkeypatch):
        setup(tmp_path, monkeypatch); low, high = duplicate()
        pid = engine.save_plan(engine.build_plan())
        replacement = low.with_suffix('.new'); replacement.write_bytes(low.read_bytes()); replacement.replace(low)
        assert execute(pid)['skipped'] == 1 and low.exists()

    def test_changed_retained_content_keeps_target(self, tmp_path, monkeypatch):
        setup(tmp_path, monkeypatch); low, high = duplicate()
        pid = engine.save_plan(engine.build_plan()); high.write_text('http://test.invalid/other')
        assert execute(pid)['skipped'] == 1 and low.exists()

    def test_unchanged_plan_deletes_only_confirmed_low_copy(self, tmp_path, monkeypatch):
        setup(tmp_path, monkeypatch); low, high = duplicate()
        pid = engine.save_plan(engine.build_plan())
        result = execute(pid)
        assert not low.exists() and high.exists() and result['skipped'] == 0

    def test_new_file_after_scan_is_not_deleted(self, tmp_path, monkeypatch):
        setup(tmp_path, monkeypatch); low, high = duplicate()
        pid = engine.save_plan(engine.build_plan()); extra = touch(low.parent, '样本.2160p.strm')
        execute(pid)
        assert extra.exists() and high.exists()

    def test_symlink_replacement_is_skipped(self, tmp_path, monkeypatch):
        setup(tmp_path, monkeypatch); low, high = duplicate()
        pid = engine.save_plan(engine.build_plan()); low.unlink(); low.symlink_to(high)
        assert execute(pid)['skipped'] == 1 and low.is_symlink() and high.exists()

    def test_unreadable_retained_file_is_skipped(self, tmp_path, monkeypatch):
        setup(tmp_path, monkeypatch); low, high = duplicate()
        pid = engine.save_plan(engine.build_plan()); real_open = Path.open
        def deny(self, *a, **kw):
            if self == high: raise PermissionError('unreadable')
            return real_open(self, *a, **kw)
        with patch.object(Path, 'open', deny):
            assert execute(pid)['skipped'] == 1
        assert low.exists()

    def test_dry_run_changed_plan_preserves_files_and_plan_bytes(self, tmp_path, monkeypatch):
        setup(tmp_path, monkeypatch); low, high = duplicate()
        pid = engine.save_plan(engine.build_plan()); pf = engine.STATE_DIR / f'plan_{pid}.json'
        before = json.dumps(_state.read(pf), sort_keys=True).encode(); high.unlink()
        assert execute(pid, True)['skipped'] == 1
        assert json.dumps(_state.read(pf), sort_keys=True).encode() == before and low.exists()

    def test_legacy_plan_requires_rescan(self, tmp_path, monkeypatch):
        setup(tmp_path, monkeypatch); low, high = duplicate()
        pid = engine.save_plan(engine.build_plan()); pf = engine.STATE_DIR / f'plan_{pid}.json'
        data = _state.read(pf); data.pop('guard_version', None); data.pop('file_snapshots', None)
        _state.save(pf, data)
        result = execute(pid)
        assert result['code'] == 'plan_snapshot_missing' and low.exists() and high.exists()

    def test_cross_library_keeper_disappears(self, tmp_path, monkeypatch):
        setup(tmp_path, monkeypatch)
        share = touch(engine.S_ROOT, '电影/欧美电影/同片 (2020)/同片.720p.strm')
        local = touch(engine.L_ROOT, '电影/欧美电影/同片 (2020)/同片.1080p.strm')
        pid = engine.save_plan(engine.build_plan()); local.unlink()
        assert execute(pid)['skipped'] == 1 and share.exists()

    def test_tmdb_alias_keeper_is_captured_by_matched_identity(self, tmp_path, monkeypatch):
        setup(tmp_path, monkeypatch)
        strategy = engine._strategy(); strategy['match_strategy'] = 'tmdb_first'
        monkeypatch.setattr(engine, '_strategy', lambda: strategy)
        share = touch(engine.S_ROOT, '电影/欧美电影/译名甲 (2020) {tmdb-123}/译名甲.720p.strm')
        local = touch(engine.L_ROOT, '电影/欧美电影/译名乙 (2020) {tmdb-123}/译名乙.1080p.strm')
        pid = engine.save_plan(engine.build_plan()); local.unlink()
        assert execute(pid)['skipped'] == 1 and share.exists()

    def test_later_action_changed_during_execution_is_skipped(self, tmp_path, monkeypatch):
        setup(tmp_path, monkeypatch); low1, high1 = duplicate('甲 (2020)'); low2, high2 = duplicate('乙 (2020)')
        pid = engine.save_plan(engine.build_plan()); plan = engine.load_plan(pid)
        later = plan['actions'][1]
        later_keeper = Path(later['file_guard']['retained'][0])
        later_target = Path(later['files'][0])
        original = engine.safe_delete_files; calls = []
        def intercept(*a, **kw):
            result = original(*a, **kw); calls.append(a[0])
            if len(calls) == 1: later_keeper.unlink()
            return result
        monkeypatch.setattr(engine, 'safe_delete_files', intercept)
        result = execute(pid)
        assert len(calls) == 1 and result['skipped'] == 1 and later_target.exists()

    def test_large_evidence_is_not_truncated_to_display_limit(self, tmp_path, monkeypatch):
        setup(tmp_path, monkeypatch)
        for ep in range(1, 502):
            touch(engine.S_ROOT, f'剧集/某剧 (2020)/Season 1/某剧.S01E{ep:03}.720p.strm')
            touch(engine.L_ROOT, f'剧集/某剧 (2020)/Season 1/某剧.S01E{ep:03}.1080p.strm')
        pid = engine.save_plan(engine.build_plan()); plan = engine.load_plan(pid)
        assert len(plan['actions'][0]['file_guard']['retained']) == 501
        touch(engine.L_ROOT, '剧集/某剧 (2020)/Season 1/某剧.S01E501.1080p.strm').unlink()
        assert execute(pid)['skipped'] == 1

    def test_intentional_special_cleanup_has_no_keeper_requirement(self, tmp_path, monkeypatch):
        setup(tmp_path, monkeypatch)
        strategy = engine._strategy(); strategy['special_action'] = 'delete'
        monkeypatch.setattr(engine, '_strategy', lambda: strategy)
        residual = touch(engine.S_ROOT, '剧集/某剧 (2020)/Season 0/某剧.S00E01.720p.strm')
        pid = engine.save_plan(engine.build_plan()); result = execute(pid)
        assert result['skipped'] == 0 and not residual.exists()

    def test_execute_does_not_rebuild_library(self, tmp_path, monkeypatch):
        setup(tmp_path, monkeypatch); low, high = duplicate()
        pid = engine.save_plan(engine.build_plan())
        def no_scan(*a): raise AssertionError('unexpected full library scan')
        monkeypatch.setattr(engine, 'build_plan', no_scan)
        monkeypatch.setattr(engine, '_get_lib', no_scan)
        assert execute(pid)['skipped'] == 0 and high.exists()

    def test_new_episode_repairing_gap_skips_incomplete_cleanup(self, tmp_path, monkeypatch):
        setup(tmp_path, monkeypatch)
        share = touch(engine.S_ROOT, '剧集/某剧 (2020)/Season 1/某剧.S01E02.720p.strm')
        for ep in (1, 2):
            touch(engine.L_ROOT, f'剧集/某剧 (2020)/Season 1/某剧.S01E{ep:02}.1080p.strm')
        pid = engine.save_plan(engine.build_plan())
        added = touch(share.parent, '某剧.S01E01.720p.strm')
        result = execute(pid)
        assert result['skipped'] == 1 and share.exists() and added.exists()

    def test_two_intentionally_incomplete_sides_do_not_block_each_other(self, tmp_path, monkeypatch):
        setup(tmp_path, monkeypatch)
        rel = '剧集/某剧 (2020)/Season 1/某剧.S01E02.720p.strm'
        local = touch(engine.L_ROOT, rel); share = touch(engine.S_ROOT, rel)
        cloud = touch(engine.CLOUD_L_ROOT, rel.replace('.strm', '.mkv'))
        pid = engine.save_plan(engine.build_plan()); result = execute(pid)
        assert result['skipped'] == 0
        assert not local.exists() and not share.exists() and not cloud.exists()


class TestGuardApiStatus:
    def test_legacy_pending_plan_is_not_usable(self):
        import time
        latest = {'ts': time.time(), 'plan_id': 'abc123', 'rule_sig': 'same', 'result': {}}
        plan = {'state': 'pending', 'rule_sig': 'same'}
        with patch.object(engine, 'load_latest_scan', return_value=latest), \
             patch.object(engine, 'load_plan', return_value=plan), \
             patch.object(engine, '_current_rule_snapshot', return_value={'sig': 'same'}):
            result = router.api_gov_latest()
            assert not result['usable'] and result['reason'] == 'snapshot_missing'

    def test_summary_agrees_with_legacy_plan_rejection(self):
        import time
        latest = {'ts': time.time(), 'plan_id': 'abc123', 'rule_sig': 'same', 'result': {}}
        with patch.object(engine, 'load_latest_scan', return_value=latest), \
             patch.object(engine, 'load_plan', return_value={'state': 'pending', 'rule_sig': 'same'}), \
             patch.object(engine, 'daily_consistency_snapshot', return_value={'rule_sig': 'same'}):
            result = router.api_governance_summary()
            assert not result['scan']['usable'] and not result['scan']['file_guard_ready']

    def test_zero_action_scan_does_not_require_plan_guard(self):
        import time
        latest = {'ts': time.time(), 'plan_id': None, 'rule_sig': 'same', 'result': {}}
        with patch.object(engine, 'load_latest_scan', return_value=latest), \
             patch.object(engine, 'daily_consistency_snapshot', return_value={'rule_sig': 'same'}):
            assert router.api_governance_summary()['scan']['usable']
