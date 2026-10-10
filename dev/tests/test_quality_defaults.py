# -*- coding: utf-8 -*-
"""New defaults, opt-in source preference, and preservation of saved rules."""
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault('AGENT_DATA', tempfile.mkdtemp(prefix='strimkeep-quality-defaults-'))
os.environ.setdefault('WEB_PASSWORD', 'quality-defaults-test-password')
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app import config, core
from app.routers import settings


EXPECTED_ORDER = ['release_group', 'source', 'resolution', 'dolby',
                  'fps', 'bitdepth', 'audio']


def assert_defaults(strategy):
    assert [r['key'] for r in strategy['rules']] == EXPECTED_ORDER
    assert [r['enabled'] for r in strategy['rules']] == [False, False, True, True, True, True, True]


def test_new_config_and_missing_rules_use_new_defaults(monkeypatch):
    monkeypatch.setattr(config, 'load_config', lambda: dict(config.DEFAULTS))
    assert_defaults(settings.api_get_cover()['strategy'])
    assert_defaults(core.normalize_cover({'rules': []}))
    assert_defaults(core.normalize_cover({'rules': [{'key': 'release_group'}, {'key': 'source'}]}))


def test_saved_order_and_enabled_flags_survive_upgrade(monkeypatch):
    strategy = core.cover_default_strategy()
    strategy['rules'][0]['enabled'] = True
    strategy['rules'][0]['groups'] = ['GroupA', 'GroupB']
    strategy['rules'][1]['enabled'] = True
    strategy['rules'].sort(key=lambda r: r['key'] != 'audio')
    monkeypatch.setattr(config, 'load_config', lambda: {'strategy_cover': json.dumps(strategy)})
    loaded = settings.api_get_cover()['strategy']
    assert loaded == strategy


def test_reset_persists_new_defaults_and_opt_in_survives_reload(monkeypatch):
    cfg = {'strategy_cover': json.dumps({'rules': [{'key': 'source', 'enabled': True}]})}
    monkeypatch.setattr(config, 'load_config', lambda: dict(cfg))
    monkeypatch.setattr(config, 'save_config', lambda value: cfg.update(value))
    reset = settings.api_set_cover({'strategy': {'rules': []}})['strategy']
    assert_defaults(reset)
    assert_defaults(config.get_cover_strategy())
    reset['rules'][1]['enabled'] = True
    settings.api_set_cover({'strategy': reset})
    reloaded = config.get_cover_strategy()
    assert reloaded['rules'][1]['enabled'] is True
    assert core.compare_cover('Film.1080p.BluRay.strm', 'Film.2160p.WEB-DL.strm', reloaded) == 1
