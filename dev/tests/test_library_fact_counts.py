"""One cached library fact set supplies all health aggregates and versions."""
from app import state_store as _state
import copy
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
import pytest

os.environ.setdefault('WEB_PASSWORD', 'test-pass-123')
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app import engine, morning


@pytest.fixture
def cached(tmp_path, monkeypatch):
    for name, leaf in [('STATE_DIR', ''), ('LIBRARY_SNAPSHOT_FILE', 'health.json'),
                       ('EMBY_LIB_CACHE_FILE', 'mapping.json'), ('MANUAL_DONE_FILE', 'manual.json'),
                       ('GOV_LATEST_FILE', 'governance.json'), ('INGEST_CACHE_FILE', 'ingest.json'),
                       ('_EMBY_OVERVIEW_CACHE_FILE', 'overview.json')]:
        monkeypatch.setattr(engine, name, tmp_path / leaf)
    monkeypatch.setattr(engine, '_emby_lib_cache', {'ts': 0, 'data': None})
    monkeypatch.setattr(engine, 'load_library_snapshot', lambda **kw: morning.load_library_snapshot(max_age=None, background_refresh=False))
    monkeypatch.setattr(engine, 'emby_request', lambda *a, **kw: (_ for _ in ()).throw(AssertionError('network is forbidden')))
    monkeypatch.setattr(morning, 'emby_library_overview', lambda **kw: (_ for _ in ()).throw(AssertionError('full scan is forbidden')))
    monkeypatch.setattr(morning, 'read_manual_done', lambda: {})
    monkeypatch.setattr(engine, '_current_rule_snapshot', lambda: {'sig': 'test-rule', 'rules': {}})
    monkeypatch.setattr(engine, 'read_ingest_cache', lambda: {})
    monkeypatch.setattr(engine, 'load_latest_scan', lambda: {})
    return tmp_path


def series(have=13):
    return {'id': 's1', 'series_ids': ['s1'], 'tmdb_id': '100', 'name': '测试剧集',
            'have_eps': have, 'total_episodes': have, 'local_eps': have, 'share_eps': 0,
            'tmdb_info': {'match_status': 'missing', 'tmdb_total': 20, 'diff': have - 20}}


def write_cache(rows, ts=100, facts_ts=None):
    data = {'ts': ts, 'series': rows, 'movies': [], 'status': 'success'}
    if facts_ts is not None: data['facts_ts'] = facts_ts
    _state.save(engine.EMBY_LIB_CACHE_FILE, data)
    return data


def test_thirteen_episodes_agree_in_health_and_consistency(cached):
    write_cache([series()])
    health = engine.unified_health(); consistency = engine.daily_consistency_snapshot(False)['library']
    assert health['episodes'] == health['episode_total'] == consistency['episode_total'] == 13
    assert consistency['facts_version'] == health['facts_version']
    assert health['top_missing'][0]['have'] == 13 and health['top_missing'][0]['diff'] == 7


def test_ingest_refresh_recomputes_snapshot_aggregate_and_not_only_rows(cached, monkeypatch):
    write_cache([series(12)])
    morning.save_library_snapshot(morning.build_library_health_snapshot())
    before = _state.read(engine.LIBRARY_SNAPSHOT_FILE)
    monkeypatch.setattr(morning, '_live_series_episodes', lambda sid: [dict(Id='e13')])
    def resync(entry, live): entry.update(series(13))
    monkeypatch.setattr(morning, '_resync_series_entry', resync)
    result = engine.refresh_mapping_cache_after_ingest({'ok': True, 'episodes_raw': [{'series_id': 's1', 'episode': 13}]})
    snap = _state.read(engine.LIBRARY_SNAPSHOT_FILE)
    assert result['updated'] == 1 and snap['series'][0]['have_eps'] == snap['episodes'] == snap['episode_total'] == 13
    assert snap['facts_version'] != before['facts_version']
    assert snap['ts'] == before['ts']  # adding an episode does not claim a full TMDB rescan
    assert snap['facts_ts'] > before['facts_ts']


def test_fresher_mapping_facts_override_older_saved_health(cached):
    write_cache([series(12)], facts_ts=100)
    morning.save_library_snapshot(morning.build_library_health_snapshot())
    write_cache([series(13)], facts_ts=200)
    health = engine.unified_health()
    assert health['episodes'] == 13 and health['facts_ts'] == 200
    assert health['ts'] == 100 and health['stale'] is True


def test_explicit_zero_does_not_fall_back_to_old_total(cached):
    row = series(0); row['total_episodes'] = 20
    write_cache([row])
    health = engine.unified_health()
    assert health['episodes'] == health['top_missing'][0]['have'] == 0


def test_empty_mapping_is_authoritative_and_never_triggers_full_scan(cached):
    write_cache([series()]); morning.save_library_snapshot(morning.build_library_health_snapshot())
    write_cache([], facts_ts=200)
    assert morning.build_library_health_snapshot()['episodes'] == 0
    assert engine.unified_health()['episodes'] == 0
    assert engine.unified_health()['stats']['total_series'] == 0


def test_manual_done_changes_stats_and_version_without_mutating_cache(cached, monkeypatch):
    original = write_cache([series()]); before = copy.deepcopy(original)
    normal = engine.unified_health()
    monkeypatch.setattr(morning, 'read_manual_done', lambda: {'s1': {'name': '测试剧集'}})
    manual = engine.unified_health()
    assert manual['stats']['missing'] == 0 and manual['stats']['aligned'] == manual['stats']['manual_done'] == 1
    assert manual['episodes'] == 13 and manual['facts_version'] != normal['facts_version']
    assert _state.read(engine.EMBY_LIB_CACHE_FILE) == before


def test_legacy_flat_episode_total_and_snapshot_only_are_compatible(cached):
    _state.save(engine.LIBRARY_SNAPSHOT_FILE, {'ts': 100, 'episode_total': 12, 'stats': {},
                                                      'top_missing': [{'name': '旧汇总', 'diff': 3}]})
    health = engine.unified_health()
    assert health['episodes'] == health['episode_total'] == 12
    assert engine.daily_consistency_snapshot(False)['library']['episode_total'] == 12
    assert health['top_missing'][0]['name'] == '旧汇总'


def test_health_and_dashboard_api_share_counts_and_facts_version(cached, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.routers import system
    write_cache([series()])
    monkeypatch.setattr(engine, '_get_strm_counts', lambda: (13, 0))
    monkeypatch.setattr(engine, 'emby_request', lambda *a, **kw: {})
    monkeypatch.setattr(system.bot, 'status', lambda: {})
    app = FastAPI(); app.include_router(system.router); app.dependency_overrides[system.auth] = lambda: True
    client = TestClient(app)
    health = client.get('/api/library/health').json()
    dashboard = client.get('/api/dashboard').json()['libraryHealth']
    consistency = client.get('/api/consistency').json()['snapshot']['library']
    assert health['episodes'] == dashboard['episodes'] == consistency['episode_total'] == 13
    assert health['facts_version'] == dashboard['facts_version'] == consistency['facts_version']


def test_corrupt_health_falls_back_to_mapping_and_corrupt_mapping_keeps_good_health(cached):
    write_cache([series()])
    engine.LIBRARY_SNAPSHOT_FILE.write_text('[]')
    assert engine.unified_health()['episodes'] == 13
    morning.save_library_snapshot(morning.build_library_health_snapshot())
    engine.EMBY_LIB_CACHE_FILE.write_text('[]')
    assert engine.unified_health()['episodes'] == 13
