"""Real ingest/mapping/journal handoff with isolated SQLite and fake APIs."""
import copy
import time

import pytest

from app import engine, morning, subscribe, state_store
from dev.tests.test_subscription_events_p2 import event_env, facts
from dev.tests.test_library_updates_p1 import isolated, ep


@pytest.fixture
def handoff(event_env, isolated, monkeypatch):
    state_store.save(engine.EMBY_LIB_CACHE_FILE, {'ts': 1, 'series': [{
        'id': 's', 'series_ids': ['s'], 'name': '测试剧', 'tmdb_id': '100',
        'year': 2026, 'tmdb_info': {'match_status': 'pending'},
        'have_eps': 1, 'seasons': [{'season': 1, 'max_ep': 1, 'missing': []}],
    }], 'movies': []})
    monkeypatch.setattr(engine, 'INGEST_CACHE_FILE', isolated / 'ingest_cache.json')
    monkeypatch.setattr(engine, '_fetch_ingest', lambda **kw: {
        'ts': time.time(), 'ok': True, 'stats': {'movies': 0, 'series': 1, 'episodes': 2},
        'tree': {}, 'episodes_raw': [{'series_id': 's'}], 'movies_raw': [],
    })
    monkeypatch.setattr(morning, '_live_series_episodes', lambda sid: [ep(1, sid), ep(2, sid)])
    subscribe.check_subscriptions(False, only_tmdb_ids={'100'},
                                  facts_by_tmdb={'100': facts([1])}, reason='ingest')
    return isolated


def test_ingest_saves_mapping_before_queue_and_never_sends_network(handoff, monkeypatch):
    original = engine.enqueue_ingest_notifications
    observed = []

    def enqueue(result):
        saved = state_store.read(engine.EMBY_LIB_CACHE_FILE)['series'][0]
        assert saved['have_eps'] == 2
        assert saved['seasons'][0]['episode_numbers'] == [1, 2]
        observed.append(True)
        return original(result)

    monkeypatch.setattr(engine, 'enqueue_ingest_notifications', enqueue)
    monkeypatch.setattr(engine, 'notify_telegram', lambda *_: pytest.fail('ingest cannot send Telegram'))
    result = engine.refresh_ingest_cache()
    assert result['ok'] and result['notification']['status'] == 'queued' and observed == [True]
    assert subscribe._load_sub_state()['a']['notified_episodes'] == ['S01E01']
    monkeypatch.setattr(engine, 'notify_telegram', lambda *_: True)
    assert subscribe.drain_subscription_notifications()['sent'] == 1
    assert subscribe._load_sub_state()['a']['latest_ep'] == 'S01E02'


def test_journal_failure_after_mapping_commit_is_repaired_without_losing_ingest(handoff, monkeypatch):
    save = subscribe._save_notification_queue
    monkeypatch.setattr(subscribe, '_save_notification_queue',
                        lambda *_: (_ for _ in ()).throw(OSError('journal unavailable')))
    result = engine.refresh_ingest_cache()
    assert result['ok'] and result['notification']['status'] == 'error'
    assert state_store.read(engine.EMBY_LIB_CACHE_FILE)['series'][0]['have_eps'] == 2
    assert subscribe._load_sub_state()['a']['latest_ep'] == 'S01E01'
    assert subscribe.load_subscription_notification_status()['reconcile_required']
    monkeypatch.setattr(subscribe, '_save_notification_queue', save)
    assert subscribe.recover_subscription_notifications()['status'] == 'success'
    queued = subscribe._load_notification_queue()['events']
    assert len(queued) == 1 and queued[0]['updates'][0]['new_eps'] == ['S01E02']


def test_failed_mapping_persistence_never_enqueues_uncommitted_facts(handoff, monkeypatch):
    before = copy.deepcopy(state_store.read(engine.EMBY_LIB_CACHE_FILE))
    monkeypatch.setattr(morning, 'save_emby_lib_cache',
                        lambda *a, **kw: (_ for _ in ()).throw(OSError('mapping write failed')))
    monkeypatch.setattr(engine, 'enqueue_ingest_notifications', lambda *_: pytest.fail('uncommitted facts cannot notify'))
    result = engine.refresh_ingest_cache()
    assert result['ok'] and result['notification']['status'] == 'skipped'
    assert state_store.read(engine.EMBY_LIB_CACHE_FILE) == before
    assert subscribe._load_notification_queue()['events'] == []


def test_verified_unchanged_mapping_can_still_handoff_without_rewriting_cache(handoff):
    payload = {'ok': True, 'episodes_raw': [{'series_id': 's'}], 'movies_raw': []}
    assert engine.refresh_mapping_cache_after_ingest(payload)['updated'] == 1
    before = copy.deepcopy(state_store.read(engine.EMBY_LIB_CACHE_FILE))
    result = engine.refresh_mapping_cache_after_ingest(payload)
    assert result['updated'] == 0 and result['series_facts'][0]['seasons'][0]['episode_numbers'] == [1, 2]
    assert state_store.read(engine.EMBY_LIB_CACHE_FILE) == before


def test_invalid_huge_episode_range_preserves_old_mapping(handoff, monkeypatch):
    before = copy.deepcopy(state_store.read(engine.EMBY_LIB_CACHE_FILE))
    monkeypatch.setattr(morning, '_live_series_episodes', lambda sid: [dict(ep(1, sid), IndexNumberEnd=100000000)])
    result = engine.refresh_mapping_cache_after_ingest({'ok': True, 'episodes_raw': [{'series_id': 's'}]})
    assert result['updated'] == 0 and result['series_facts'] == []
    assert state_store.read(engine.EMBY_LIB_CACHE_FILE) == before
