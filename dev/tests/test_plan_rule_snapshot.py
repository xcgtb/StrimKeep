# -*- coding: utf-8 -*-
"""Changing effective quality rules must invalidate previously confirmed plans."""
import copy
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_TMP = Path(tempfile.mkdtemp(prefix='strimkeep_rule_snapshot_'))
os.environ.setdefault('AGENT_DATA', str(_TMP / 'data'))
os.environ.setdefault('WEB_PASSWORD', 'test-pass-123')
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app import config, core, engine, governance
from app.routers import governance as router


def _cover_change(change):
    cover = core.cover_default_strategy()
    if change == 'order':
        cover['rules'][0], cover['rules'][1] = cover['rules'][1], cover['rules'][0]
    elif change == 'enabled':
        cover['rules'][1]['enabled'] = not cover['rules'][1]['enabled']
    elif change == 'tiers':
        tiers = cover['rules'][2]['tiers']
        tiers[0], tiers[1] = tiers[1], tiers[0]
    elif change == 'groups':
        cover['rules'][0]['enabled'] = True
        cover['rules'][0]['groups'] = ['GroupA', 'GroupB']
    return cover


class TestQualityRuleFingerprint:
    def _assert_changed(self, change):
        cfg = dict(config.DEFAULTS)
        with patch.object(config, 'load_config', lambda: dict(cfg)):
            before = governance._current_rule_snapshot()['sig']
            cfg['strategy_cover'] = json.dumps(_cover_change(change))
            assert governance._current_rule_snapshot()['sig'] != before

    def test_rule_order(self):
        self._assert_changed('order')

    def test_rule_enable(self):
        self._assert_changed('enabled')

    def test_tier_order(self):
        self._assert_changed('tiers')

    def test_release_groups(self):
        self._assert_changed('groups')

    def test_equivalent_normalized_rules_keep_same_signature(self):
        cfg = dict(config.DEFAULTS)
        with patch.object(config, 'load_config', lambda: dict(cfg)):
            before = governance._current_rule_snapshot()['sig']
            cover = core.cover_default_strategy()
            for rule in cover['rules']:
                rule.pop('label'); rule.pop('desc')
            cfg['strategy_cover'] = json.dumps(cover, indent=2)
            assert governance._current_rule_snapshot()['sig'] == before


class TestChangedQualityPlan:
    def _reject_plan(self, dry_run):
        cfg = dict(config.DEFAULTS)
        with patch.object(config, 'load_config', lambda: dict(cfg)):
            old_sig = governance._current_rule_snapshot()['sig']
            cfg['strategy_cover'] = json.dumps(_cover_change('enabled'))
            plan = {'schema_version': 2, 'id': 'abc123', 'state': 'pending',
                    'rule_sig': old_sig, 'ts': time.time(), 'actions': []}
            with patch.object(governance, 'load_plan', return_value=plan), \
                    patch.object(governance, 'save_plan_state') as state, \
                    patch.object(engine, 'safe_delete_files') as delete:
                result = governance._run_inter_clean(
                    SimpleNamespace(plan='abc123', dry_run=dry_run, notify=False))
                assert result['code'] == 'plan_rule_changed'
                delete.assert_not_called()
                if dry_run:
                    state.assert_not_called()
                else:
                    assert state.call_args.args[:2] == ('abc123', 'stale')

    def test_actual_execution_rejects_changed_rules(self):
        self._reject_plan(False)

    def test_preview_rejects_changed_rules_without_writes(self):
        self._reject_plan(True)

    def test_legacy_qualityless_signature_requires_rescan(self):
        import hashlib
        current = governance._current_rule_snapshot()
        legacy_rules = copy.deepcopy(current['rules'])
        legacy_rules.pop('cover_strategy', None)
        legacy_sig = hashlib.sha256(json.dumps(legacy_rules, ensure_ascii=False,
            sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()[:16]
        assert current['sig'] != legacy_sig


class TestLatestPlanRuleStatus:
    def _latest(self, scan_sig, plan_sig):
        latest = {'ts': time.time(), 'plan_id': 'abc123', 'rule_sig': scan_sig,
                  'result': {'del_local_cnt': 1}}
        with patch.object(engine, 'load_latest_scan', return_value=latest), \
                patch.object(engine, 'load_plan', return_value={'state': 'pending', 'rule_sig': plan_sig, 'guard_version': 1}), \
                patch.object(engine, '_current_rule_snapshot', return_value={'sig': 'current'}):
            return router.api_gov_latest()

    def test_latest_hides_scan_after_rules_change(self):
        result = self._latest('old', 'old')
        assert result['usable'] is False
        assert result['reason'] == 'rule_changed'
        assert result['result'] is None

    def test_latest_checks_plan_even_when_scan_signature_missing(self):
        assert self._latest('', 'old')['usable'] is False

    def test_unchanged_plan_remains_usable(self):
        assert self._latest('current', 'current')['usable'] is True
