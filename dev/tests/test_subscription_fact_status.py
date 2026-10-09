"""Subscription observation failures never masquerade as unchanged or reset baselines."""
from app import state_store as _state
import copy
import json
from types import SimpleNamespace
import pytest
from dev.tests.test_library_fact_counts import cached, series, write_cache
from app import engine, morning, subscribe


@pytest.fixture
def subenv(cached, monkeypatch):
    state = {'a': {'name': '临时剧', 'latest_ep': 'S01E01',
                  'notified_episodes': ['S01E01'], 'missing_episodes': ['S01E02'], 'tmdb_total': 3}}
    saved = []; audit = []
    monkeypatch.setattr(subscribe._cfg, 'load_config', lambda: {'subscribe_enabled': '1', 'subscribe_check_tmdb': '1'})
    monkeypatch.setattr(subscribe._cfg, 'get_subscriptions', lambda: [{'id': 'a', 'name': '临时剧', 'tmdb_id': '100', 'enabled': True}])
    monkeypatch.setattr(subscribe, '_load_sub_state', lambda: copy.deepcopy(state))
    monkeypatch.setattr(subscribe, '_save_sub_state', lambda data: (saved.append(copy.deepcopy(data)), state.update(data)))
    monkeypatch.setattr(subscribe.logger, 'write', lambda *a, **kw: audit.append(kw.get('extra')))
    monkeypatch.setattr(subscribe, '_disk_series_eps', lambda tid: (_ for _ in ()).throw(RuntimeError('share mount unavailable')))
    monkeypatch.setattr(engine, '_tmdb_series_info', lambda tid: {'name': '临时剧', 'aired': {(1, 1), (1, 2), (1, 3)}, 'total_episodes': 3})
    monkeypatch.setattr(engine, 'notify_telegram', lambda *a, **kw: True)
    return SimpleNamespace(state=state, saved=saved, audit=audit)


def test_failed_observation_preserves_state_and_reports_error(subenv, monkeypatch):
    before = copy.deepcopy(subenv.state)
    monkeypatch.setattr(subscribe, '_emby_series_latest_ep', lambda tid: (_ for _ in ()).throw(RuntimeError('Emby unavailable')))
    result = subscribe.check_subscriptions(False)
    assert result['failed'] == 1 and result['checked'] == 0 and result['status'] == 'error'
    assert subenv.state == before
    assert result['rows'][0]['status'] == 'error' and 'unavailable' in result['rows'][0]['error']


def test_tmdb_failure_does_not_clear_known_gaps_or_block_real_new_episodes(subenv, monkeypatch):
    monkeypatch.setattr(subscribe, '_emby_series_latest_ep', lambda tid: {'episodes': {(1, 1), (1, 3)}, 'series_name': '临时剧'})
    monkeypatch.setattr(engine, '_tmdb_series_info', lambda tid: None)
    result = subscribe.check_subscriptions(False)
    assert result['status'] == 'partial' and result['failed'] == 1
    assert result['updates'][0]['new_eps'] == ['S01E03']
    assert subenv.state['a']['missing_episodes'] == ['S01E02']
    assert subenv.state['a']['tmdb_total'] == 3


def test_telegram_failure_restores_missing_baseline_for_refill_retry(subenv, monkeypatch):
    monkeypatch.setattr(subscribe, '_emby_series_latest_ep', lambda tid: {'episodes': {(1, 1), (1, 2)}, 'series_name': '临时剧'})
    monkeypatch.setattr(engine, 'notify_telegram', lambda *a, **kw: False)
    result = subscribe.check_subscriptions(True)
    assert result['sent'] is False
    assert subenv.state['a']['missing_episodes'] == ['S01E02']
    assert subenv.state['a']['notified_episodes'] == ['S01E01']


def test_partial_emby_episode_query_never_returns_partial_success(cached, monkeypatch):
    def fake(path, params):
        if params['IncludeItemTypes'] == 'Series': return {'Items': [{'Id': 'local'}, {'Id': 'share'}]}
        if params['ParentId'] == 'share': raise RuntimeError('share episode query failed')
        return {'Items': [{'ParentIndexNumber': 1, 'IndexNumber': 1}]}
    monkeypatch.setattr(engine, 'emby_request', fake)
    with pytest.raises(RuntimeError): subscribe._emby_series_latest_ep('100')


def test_disk_failure_never_returns_partial_success(cached, monkeypatch):
    lib = SimpleNamespace(tmdb_refs={'tv:100': ['key']}, meta={'key': ('临时剧', '', None)},
                          tv={'key': {1: [cached / 'S01E01.strm']}})
    def get(root):
        if root == engine.S_ROOT: raise RuntimeError('share mount unavailable')
        return lib
    monkeypatch.setattr(engine, '_get_lib', get)
    with pytest.raises(RuntimeError): subscribe._disk_series_eps('100')


def test_successful_empty_query_is_a_valid_zero(cached, monkeypatch):
    monkeypatch.setattr(engine, 'emby_request', lambda path, params: {'Items': [{'Id': 's1'}]} if params['IncludeItemTypes'] == 'Series' else {'Items': []})
    result = subscribe._emby_series_latest_ep('100')
    assert result is not None and result['episodes'] == set()


def test_stale_gap_cache_matches_latest_shared_facts_without_network(cached):
    write_cache([series(12)], facts_ts=100); morning.save_library_snapshot(morning.build_library_health_snapshot())
    write_cache([series(13)], facts_ts=200)
    result = morning.gap_report(cache_only=True)
    assert result['status'] == 'success' and result['missing'][0]['have_eps'] == 13
    assert result['facts_version'] == engine.unified_health()['facts_version']
    assert result['stale'] is True and result['facts_ts'] == 200


def test_subscription_display_uses_shared_current_count_not_notification_history(subenv, monkeypatch):
    from app.routers import subscribe as route
    monkeypatch.setattr(route, 'get_subscriptions', lambda: [{'id': 'a', 'tmdb_id': '100', 'name': '临时剧'}])
    monkeypatch.setattr(route, 'load_config', lambda: {})
    monkeypatch.setattr(engine, '_load_sub_state', lambda: subenv.state)
    write_cache([series(13)], facts_ts=200)
    result = route.api_get_subs()['subscriptions'][0]
    assert result['have_eps'] == 13 and result['latest_ep'] == 'S01E01'
    assert result['facts_version'] == engine.unified_health()['facts_version']


def test_partial_status_and_per_show_reason_are_persisted(subenv, monkeypatch):
    monkeypatch.setattr(subscribe, '_emby_series_latest_ep', lambda tid: {'episodes': {(1, 1)}})
    monkeypatch.setattr(engine, '_tmdb_series_info', lambda tid: None)
    subscribe.check_subscriptions(False)
    status = subscribe.load_subscription_check_status()
    assert status['status'] == 'partial' and status['failed'] == 1
    assert status['rows'][0]['tmdb_source'] == 'unavailable'
    assert 'TMDB' in status['rows'][0]['error']
    assert subenv.audit[-1]['failed'] == 1


def test_successful_disk_fallback_is_identified(subenv, monkeypatch):
    monkeypatch.setattr(subscribe, '_emby_series_latest_ep', lambda tid: (_ for _ in ()).throw(RuntimeError('Emby down')))
    monkeypatch.setattr(subscribe, '_disk_series_eps', lambda tid: {'episodes': {(1, 1)}, 'source': 'disk'})
    result = subscribe.check_subscriptions(False)
    assert result['status'] == 'success' and result['checked'] == 1
    assert result['rows'][0]['facts_source'] == 'disk'
    assert result['rows'][0]['fallback_reason'] == 'Emby down'


def test_incomplete_emby_page_is_a_failure(cached, monkeypatch):
    monkeypatch.setattr(engine, 'emby_request', lambda *a: {'Items': [{'Id': 's1'}], 'TotalRecordCount': 2})
    with pytest.raises(RuntimeError, match='不完整'):
        subscribe._emby_series_latest_ep('100')


def test_morning_report_never_claims_no_change_after_check_failure(subenv, monkeypatch):
    monkeypatch.setattr(engine, '_load_subscription_report', lambda **kw: None)
    monkeypatch.setattr(engine, 'load_subscription_check_status', lambda: {'status': 'partial', 'error': 'TMDB down'})
    text = morning.build_morning_report(['subscriptions'], cache_only=True)
    assert 'TMDB down' in text and '无变化' not in text


def test_unavailable_comparisons_are_not_a_zero_gap_success(cached):
    row = series(13); row['tmdb_info'] = {'match_status': 'pending'}
    write_cache([row])
    result = morning.gap_report(max_age=None, cache_only=True)
    assert result['stats']['unavailable'] == 1 and result['stale'] is True
    text = morning.build_morning_report(['emby_gap'], cache_only=True)
    assert '不能判定' in text and '已过期' in text and '全部对齐' not in text


def test_subscription_display_conflict_never_selects_an_arbitrary_count(subenv, monkeypatch):
    from app.routers import subscribe as route
    monkeypatch.setattr(route, 'get_subscriptions', lambda: [{'id': 'a', 'tmdb_id': '100', 'name': '临时剧'}])
    monkeypatch.setattr(route, 'load_config', lambda: {})
    monkeypatch.setattr(engine, '_load_sub_state', lambda: subenv.state)
    write_cache([series(13), dict(series(20), id='different')])
    row = route.api_get_subs()['subscriptions'][0]
    assert row['have_eps'] is None and row['facts_status'] == 'ambiguous'


def test_tmdb_error_payload_is_not_a_known_empty_season_list(cached, monkeypatch):
    from app import tmdb
    class FakeTmdb:
        def get(self, *a, **kw): return {'success': False, 'status_code': 7}
        def save(self): pass
    monkeypatch.setattr(engine, 'Tmdb', FakeTmdb)
    assert tmdb._tmdb_series_info('100') is None


def test_subscription_missing_cache_does_not_restore_historical_tmdb_count(subenv, monkeypatch):
    from app.routers import subscribe as route
    monkeypatch.setattr(route, 'get_subscriptions', lambda: [{'id': 'a', 'tmdb_id': '100', 'name': '临时剧'}])
    monkeypatch.setattr(route, 'load_config', lambda: {})
    monkeypatch.setattr(engine, '_load_sub_state', lambda: subenv.state)
    row = route.api_get_subs()['subscriptions'][0]
    assert row['have_eps'] is None and row['tmdb_total'] is None
    assert row['latest_ep'] == 'S01E01'
