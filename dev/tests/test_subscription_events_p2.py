# -*- coding: utf-8 -*-
"""P2 event-driven追更回归：只消费持久化片库事实，队列先落盘后发送。"""
import copy
import time
from pathlib import Path

import pytest

from app import engine, state_store, subscribe


@pytest.fixture
def event_env(monkeypatch, tmp_path):
    state_file = tmp_path / 'subscriptions_state.json'
    notify_file = tmp_path / 'subscription_notifications.json'
    monkeypatch.setattr(engine, 'STATE_DIR', tmp_path)
    monkeypatch.setattr(engine, 'SUB_STATE_FILE', state_file)
    monkeypatch.setattr(engine, 'SUB_NOTIFICATION_FILE', notify_file)
    monkeypatch.setattr(subscribe._cfg, 'load_config', lambda: {
        'subscribe_enabled': '1', 'subscribe_check_tmdb': '0',
        'ingest_enabled': '1',
    })
    subs = [{'id': 'a', 'name': '测试剧', 'tmdb_id': '100', 'enabled': True}]
    monkeypatch.setattr(subscribe._cfg, 'get_subscriptions', lambda: copy.deepcopy(subs))
    monkeypatch.setattr(subscribe, '_save_subscription_check_status', lambda *a, **kw: None)
    monkeypatch.setattr(subscribe.logger, 'write', lambda *a, **kw: None)
    state_store.save(state_file, {})
    state_store.save(notify_file, {'schema': 1, 'events': []})
    return tmp_path


def facts(eps):
    return {'series_name': '测试剧', 'source': 'library_facts', 'facts_ts': time.time(),
            'episodes': {(1, int(e)) for e in eps}}


def test_event_uses_mapping_facts_without_emby_query(event_env, monkeypatch):
    sent = []
    monkeypatch.setattr(engine, 'notify_telegram', lambda text: sent.append(text) or True)
    monkeypatch.setattr(subscribe, '_emby_series_latest_ep',
                        lambda *_: (_ for _ in ()).throw(AssertionError('event queried Emby')))
    subscribe.check_subscriptions(send_notify=False, only_tmdb_ids={'100'},
                                  facts_by_tmdb={'100': facts([1])}, reason='ingest')
    result = subscribe.check_subscriptions(
        send_notify=True, only_tmdb_ids={'100'},
        facts_by_tmdb={'100': facts([1, 16, 17, 18])}, reason='ingest')
    assert result['updates'][0]['new_eps'] == ['S01E16', 'S01E17', 'S01E18']
    assert len(sent) == 1 and 'S01E16–E18' in sent[0]
    assert subscribe._load_sub_state()['a']['latest_ep'] == 'S01E18'


def test_ingest_event_is_persisted_before_telegram_delivery(event_env, monkeypatch):
    subscribe.check_subscriptions(send_notify=False, only_tmdb_ids={'100'},
                                  facts_by_tmdb={'100': facts([1])}, reason='ingest')
    calls = []
    monkeypatch.setattr(engine, 'notify_telegram', lambda text: calls.append(text) or True)
    mapping = {'series_facts': [{
        'id': 'series-1', 'name': '测试剧', 'tmdb_id': '100',
        'seasons': [{'season': 1, 'episode_numbers': [1, 2]}],
    }]}
    result = subscribe.enqueue_ingest_notifications(mapping)
    queue = state_store.read(engine.SUB_NOTIFICATION_FILE)
    assert result['status'] == 'queued' and result['queued'] == 1
    assert queue['events'][0]['status'] == 'pending'
    assert calls == []
    assert subscribe._load_sub_state()['a']['notified_episodes'] == ['S01E01']


def test_failed_send_persists_pending_event_and_retries(event_env, monkeypatch):
    calls = []
    subscribe.check_subscriptions(send_notify=False, only_tmdb_ids={'100'},
                                  facts_by_tmdb={'100': facts([1])}, reason='ingest')
    monkeypatch.setattr(engine, 'notify_telegram', lambda text: calls.append(text) or False)
    first = subscribe.check_subscriptions(
        send_notify=True, only_tmdb_ids={'100'},
        facts_by_tmdb={'100': facts([1, 2])}, reason='ingest')
    assert first['sent'] is False
    assert subscribe._load_sub_state()['a']['notified_episodes'] == ['S01E01']
    queue = state_store.read(engine.SUB_NOTIFICATION_FILE)
    assert queue['events'][0]['status'] == 'pending'
    monkeypatch.setattr(engine, 'notify_telegram', lambda text: calls.append(text) or True)
    second = subscribe.check_subscriptions(
        send_notify=True, only_tmdb_ids={'100'},
        facts_by_tmdb={'100': facts([1, 2])}, reason='ingest')
    assert second['sent'] is True
    assert subscribe._load_sub_state()['a']['notified_episodes'] == ['S01E01', 'S01E02']
    assert len(calls) == 2


def test_overlapping_fact_observation_does_not_repeat_sent_episode(event_env, monkeypatch):
    calls = []
    subscribe.check_subscriptions(send_notify=False, only_tmdb_ids={'100'},
                                  facts_by_tmdb={'100': facts([1])}, reason='ingest')
    monkeypatch.setattr(engine, 'notify_telegram', lambda text: calls.append(text) or True)
    first = subscribe.check_subscriptions(
        send_notify=True, only_tmdb_ids={'100'},
        facts_by_tmdb={'100': facts([1, 2])}, reason='ingest')
    second = subscribe.check_subscriptions(
        send_notify=True, only_tmdb_ids={'100'},
        facts_by_tmdb={'100': facts([1, 2])}, reason='ingest')
    assert first['updates'][0]['new_eps'] == ['S01E02']
    assert second['updates'] == []
    assert len(calls) == 1


def test_delivered_event_recovers_state_without_resending(event_env, monkeypatch):
    subscribe.check_subscriptions(send_notify=False, only_tmdb_ids={'100'},
                                  facts_by_tmdb={'100': facts([1])}, reason='ingest')
    event = subscribe._queue_update_batch([
        {'sid': 'a', 'name': '测试剧', 'new_eps': ['S01E02'],
         'newly_missing': [], 'refilled': [], 'missing_eps': []}],
        {'a': {'tmdb_id': '100', 'notified_episodes': ['S01E01', 'S01E02'],
                'missing_episodes': [], 'latest_ep': 'S01E02'}})
    queue = state_store.read(engine.SUB_NOTIFICATION_FILE)
    queue['events'][0]['status'] = 'delivered'
    state_store.save(engine.SUB_NOTIFICATION_FILE, queue)
    monkeypatch.setattr(engine, 'notify_telegram',
                        lambda *_: pytest.fail('delivered event must not resend'))
    result = subscribe.drain_subscription_notifications(force=True)
    assert result['sent'] == 0
    assert state_store.read(engine.SUB_NOTIFICATION_FILE)['events'][0]['status'] == 'sent'
    assert subscribe._load_sub_state()['a']['latest_ep'] == 'S01E02'
    assert subscribe._load_sub_state()['a']['notified_episodes'] == ['S01E01', 'S01E02']


def test_paused_subscription_cancels_pending_delivery(event_env, monkeypatch):
    subs = [{'id': 'a', 'name': '测试剧', 'tmdb_id': '100', 'enabled': True}]
    monkeypatch.setattr(subscribe._cfg, 'get_subscriptions', lambda: copy.deepcopy(subs))
    subscribe.check_subscriptions(send_notify=False, only_tmdb_ids={'100'},
                                  facts_by_tmdb={'100': facts([1])}, reason='ingest')
    calls = []
    monkeypatch.setattr(engine, 'notify_telegram', lambda text: calls.append(text) or False)
    subscribe.check_subscriptions(send_notify=True, only_tmdb_ids={'100'},
                                  facts_by_tmdb={'100': facts([1, 2])}, reason='ingest')
    subs[0]['enabled'] = False
    monkeypatch.setattr(engine, 'notify_telegram',
                        lambda *_: pytest.fail('paused event must be canceled before send'))
    subscribe.drain_subscription_notifications(force=True)
    queue = state_store.read(engine.SUB_NOTIFICATION_FILE)
    assert queue['events'][0]['status'] == 'sent'
    assert len(calls) == 1


def test_two_changed_subscriptions_get_separate_detail_cards(event_env, monkeypatch):
    monkeypatch.setattr(subscribe._cfg, 'get_subscriptions', lambda: [
        {'id': 'a', 'name': '甲', 'tmdb_id': '100', 'enabled': True},
        {'id': 'b', 'name': '乙', 'tmdb_id': '200', 'enabled': True},
    ])
    sent = []
    monkeypatch.setattr(engine, 'notify_telegram', lambda text: sent.append(text) or True)
    facts_by_tmdb = {'100': facts([1, 2]), '200': dict(facts([3]), series_name='乙')}
    subscribe.check_subscriptions(send_notify=False, only_tmdb_ids={'100'},
                                  facts_by_tmdb={'100': facts([1])}, reason='ingest')
    subscribe.check_subscriptions(send_notify=False, only_tmdb_ids={'200'},
                                  facts_by_tmdb={'200': facts([1])}, reason='ingest')
    # The second seeded call updated the same state document, preserving both
    # quiet baselines before the event batch.
    result = subscribe.check_subscriptions(send_notify=True,
                                           only_tmdb_ids={'100', '200'},
                                           facts_by_tmdb=facts_by_tmdb,
                                           reason='ingest')
    assert result['checked'] == 2 and len(result['updates']) == 2
    assert len(sent) == 2 and '《测试剧》' in sent[0] and '《乙》' in sent[1]


def test_mapping_facts_reconstruct_sparse_episodes():
    row = {'id': 's', 'name': '补集剧', 'tmdb_id': '100',
           'seasons': [{'season': 1, 'max_ep': 18, 'missing': [8]}]}
    out = subscribe._mapping_entry_facts(row)
    assert (1, 8) not in out['episodes'] and (1, 7) in out['episodes'] and (1, 18) in out['episodes']


def test_tmdb_calibration_can_report_new_aired_gap_without_ingest(event_env, monkeypatch):
    cfg = {'subscribe_enabled': '1', 'subscribe_check_tmdb': '1', 'ingest_enabled': '1'}
    monkeypatch.setattr(subscribe._cfg, 'load_config', lambda: cfg)
    monkeypatch.setattr(engine, '_tmdb_series_info',
                        lambda tid: {'name': '测试剧', 'aired': {(1, 1)},
                                     'total_episodes': 1, 'source': 'tmdb'})
    subscribe.check_subscriptions(send_notify=False, only_tmdb_ids={'100'},
                                  facts_by_tmdb={'100': facts([1])},
                                  reason='tmdb_calibration')
    monkeypatch.setattr(engine, '_tmdb_series_info',
                        lambda tid: {'name': '测试剧', 'aired': {(1, 1), (1, 2)},
                                     'total_episodes': 2, 'source': 'tmdb'})
    sent = []
    monkeypatch.setattr(engine, 'notify_telegram', lambda text: sent.append(text) or True)
    result = subscribe.check_subscriptions(send_notify=True, only_tmdb_ids={'100'},
                                           facts_by_tmdb={'100': facts([1])},
                                           reason='tmdb_calibration')
    assert result['updates'][0]['newly_missing'] == ['S01E02']
    assert sent and 'S01E02' in sent[0]


def test_delivered_old_batch_must_not_absorb_next_ingest_batch(event_env, monkeypatch):
    """The first Telegram delivery was accepted but state journal needs recovery."""
    subscribe.check_subscriptions(send_notify=False, only_tmdb_ids={'100'},
                                  facts_by_tmdb={'100': facts([1])}, reason='ingest')
    subscribe.check_subscriptions(send_notify=True, dispatch=False,
                                  only_tmdb_ids={'100'}, facts_by_tmdb={'100': facts([1, 2])}, reason='ingest')
    queue = state_store.read(engine.SUB_NOTIFICATION_FILE)
    queue['events'][0]['status'] = 'delivered'
    state_store.save(engine.SUB_NOTIFICATION_FILE, queue)
    subscribe.check_subscriptions(send_notify=True, dispatch=False,
                                  only_tmdb_ids={'100'}, facts_by_tmdb={'100': facts([1, 2, 3])}, reason='ingest')
    queued = state_store.read(engine.SUB_NOTIFICATION_FILE)['events']
    assert len(queued) == 2
    assert queued[0]['status'] == 'delivered'
    assert queued[0]['updates'][0]['new_eps'] == ['S01E02']
    assert queued[1]['status'] == 'pending'
    assert queued[1]['updates'][0]['new_eps'] == ['S01E03']
    sent = []
    monkeypatch.setattr(engine, 'notify_telegram', lambda msg: sent.append(msg) or True)
    subscribe.drain_subscription_notifications(force=True)
    assert len(sent) == 1 and 'S01E03' in sent[0]
    assert subscribe._load_sub_state()['a']['notified_episodes'] == ['S01E01', 'S01E02', 'S01E03']


def test_two_pending_ingest_cycles_stay_separate_without_duplicate_eps(event_env, monkeypatch):
    subscribe.check_subscriptions(send_notify=False, only_tmdb_ids={'100'},
                                  facts_by_tmdb={'100': facts([1])}, reason='ingest')
    subscribe.check_subscriptions(send_notify=True, dispatch=False,
                                  only_tmdb_ids={'100'}, facts_by_tmdb={'100': facts([1, 2])}, reason='ingest')
    subscribe.check_subscriptions(send_notify=True, dispatch=False,
                                  only_tmdb_ids={'100'}, facts_by_tmdb={'100': facts([1, 2, 3, 4])}, reason='ingest')
    queue = state_store.read(engine.SUB_NOTIFICATION_FILE)['events']
    assert len(queue) == 2
    assert queue[0]['updates'][0]['new_eps'] == ['S01E02']
    assert queue[1]['updates'][0]['new_eps'] == ['S01E03', 'S01E04']
    sent = []
    monkeypatch.setattr(engine, 'notify_telegram', lambda msg: sent.append(msg) or True)
    subscribe.drain_subscription_notifications(force=True)
    assert len(sent) == 2
    assert 'S01E02' in sent[0] and 'S01E03' not in sent[0]
    assert 'S01E03–E04' in sent[1] and 'S01E02' not in sent[1]
    assert subscribe._load_sub_state()['a']['notified_episodes'] == [
        'S01E01', 'S01E02', 'S01E03', 'S01E04']


def test_notification_state_recovery_never_rewinds_newer_accepted_eps(event_env):
    state_store.save(engine.SUB_STATE_FILE, {'a': {'notified_episodes': ['S01E01', 'S01E03'],
                                                 'latest_ep': 'S01E03', 'name': '更新后'}})
    subscribe._apply_notification_state({'state_after': {'a': {
        'notified_episodes': ['S01E01', 'S01E02'], 'latest_ep': 'S01E02', 'name': '旧名称',
    }}})
    state = subscribe._load_sub_state()['a']
    assert state['notified_episodes'] == ['S01E01', 'S01E02', 'S01E03']
    assert state['latest_ep'] == 'S01E03'



def test_noop_ingest_must_not_dispatch_waiting_telegram(event_env, monkeypatch):
    subscribe.check_subscriptions(send_notify=False, only_tmdb_ids={'100'},
                                  facts_by_tmdb={'100': facts([1])}, reason='ingest')
    subscribe.check_subscriptions(send_notify=True, dispatch=False,
                                  only_tmdb_ids={'100'}, facts_by_tmdb={'100': facts([1, 2])}, reason='ingest')
    monkeypatch.setattr(engine, 'notify_telegram',
                        lambda *_: pytest.fail('no-op ingest must not send Telegram'))
    r = subscribe.check_subscriptions(send_notify=True, dispatch=False,
                                      only_tmdb_ids={'100'}, facts_by_tmdb={'100': facts([1, 2])}, reason='ingest')
    assert r['updates'] == [] and r['notification_pending'] is True
    assert state_store.read(engine.SUB_NOTIFICATION_FILE)['events'][0]['status'] == 'pending'
