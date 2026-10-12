"""P2 reshape: bounded retries, live calibration and nonblocking ingest.

No real media/cloud deletes, credentials or external service requests.
"""
import copy
import threading
import time

import pytest

from app import engine, morning, scheduler, state_store, subscribe
from dev.tests.test_subscription_events_p2 import event_env, facts


def seed():
    return subscribe.check_subscriptions(False, only_tmdb_ids={'100'},
                                         facts_by_tmdb={'100': facts([1])}, reason='ingest')


def enqueue(eps):
    return subscribe.enqueue_ingest_notifications({'series_facts': [{
        'id': 's', 'series_ids': ['s'], 'name': '测试剧', 'tmdb_id': '100',
        'seasons': [{'season': 1, 'episode_numbers': list(eps)}],
    }]})


def cache(monkeypatch, eps=(1,), ts=1):
    monkeypatch.setattr(morning, 'read_emby_lib_cache', lambda **kw: {
        'ts': ts, 'series': [{'id': 's', 'series_ids': ['s'], 'tmdb_id': '100',
                             'seasons': [{'season': 1, 'episode_numbers': list(eps)}]}]})


def test_ingest_journals_while_telegram_call_is_inflight(event_env, monkeypatch):
    seed(); enqueue([1, 2])
    sending, release, completed = threading.Event(), threading.Event(), threading.Event()
    sent = []

    def telegram(text):
        sending.set()
        assert release.wait(3)
        sent.append(text)
        return True

    monkeypatch.setattr(engine, 'notify_telegram', telegram)
    worker = threading.Thread(target=lambda: subscribe.drain_subscription_notifications(limit=1), daemon=True)
    worker.start()
    assert sending.wait(2)
    results = []

    def ingest():
        try:
            results.append(enqueue([1, 2, 3]))
        finally:
            completed.set()

    ingest_worker = threading.Thread(target=ingest, daemon=True)
    ingest_worker.start()
    try:
        assert completed.wait(1), 'ingest must not wait for the network sender lock'
        assert results[0]['status'] == 'queued'
        queued = subscribe._load_notification_queue()['events']
        assert len(queued) == 2
        assert queued[0]['updates'][0]['new_eps'] == ['S01E02']
        assert queued[1]['updates'][0]['new_eps'] == ['S01E03']
    finally:
        release.set(); worker.join(2); ingest_worker.join(2)
    monkeypatch.setattr(engine, 'notify_telegram', lambda text: sent.append(text) or True)
    subscribe.drain_subscription_notifications(force=True)
    assert len(sent) == 2
    assert subscribe._load_sub_state()['a']['notified_episodes'] == ['S01E01', 'S01E02', 'S01E03']


def test_slow_tmdb_preparation_does_not_hold_ingest_state_lock(event_env, monkeypatch):
    seed()
    monkeypatch.setattr(subscribe._cfg, 'load_config', lambda: {
        'subscribe_enabled': '1', 'subscribe_check_tmdb': '1'})
    observing, release = threading.Event(), threading.Event()
    results = []
    monkeypatch.setattr(subscribe, '_emby_series_latest_ep', lambda *_: facts([1]))

    def tmdb(*_):
        observing.set()
        assert release.wait(3)
        return {'name': '测试剧', 'aired': {(1, 1), (1, 2)}, 'total_episodes': 2}

    monkeypatch.setattr(engine, '_tmdb_series_info', tmdb)
    worker = threading.Thread(target=lambda: results.append(subscribe.check_subscriptions(dispatch=False)), daemon=True)
    worker.start()
    assert observing.wait(2)
    try:
        assert enqueue([1, 2])['status'] == 'queued'
    finally:
        release.set(); worker.join(2)
    assert not worker.is_alive()
    assert results[0]['rows'][0]['status'] == 'skipped'
    queue = subscribe._load_notification_queue()['events']
    assert len(queue) == 1 and queue[0]['updates'][0]['newly_missing'] == []


def test_pending_count_backpressure_never_drops_old_events_or_acknowledges_new(event_env, monkeypatch):
    seed(); monkeypatch.setattr(subscribe, '_NOTIFICATION_PENDING_LIMIT', 1)
    assert enqueue([1, 2])['status'] == 'queued'
    old = copy.deepcopy(subscribe._load_notification_queue())
    result = enqueue([1, 2, 3])
    assert result['status'] == 'error' and '已满' in result['error']
    assert subscribe._load_notification_queue() == old
    assert subscribe._load_sub_state()['a']['notified_episodes'] == ['S01E01']
    assert subscribe.load_subscription_notification_status()['reconcile_required'] is True


def test_pending_byte_backpressure_is_visible_and_preserves_state(event_env, monkeypatch):
    seed(); monkeypatch.setattr(subscribe, '_NOTIFICATION_BYTES_LIMIT', 128)
    result = enqueue([1, 2])
    assert result['status'] == 'error'
    assert subscribe._load_notification_queue()['events'] == []
    assert subscribe._load_sub_state()['a']['notified_episodes'] == ['S01E01']


def test_sent_history_is_compact_and_bounded_without_discarding_pending(event_env):
    subscribe._save_notification_queue({'events': [
        {'id': 'p', 'status': 'pending', 'updates': [{'new_eps': ['S01E02']}]}] + [
        {'id': str(i), 'status': 'sent', 'updates': ['large old payload'], 'state_after': {'a': {}}}
        for i in range(300)]})
    rows = subscribe._load_notification_queue()['events']
    assert len(rows) == 257 and rows[0]['updates'][0]['new_eps'] == ['S01E02']
    assert all('updates' not in row and 'state_after' not in row for row in rows[1:])


def test_backpressure_repair_requeues_only_unconfirmed_changes(event_env, monkeypatch):
    seed(); monkeypatch.setattr(subscribe, '_NOTIFICATION_PENDING_LIMIT', 1)
    enqueue([1, 2]); assert enqueue([1, 2, 3])['status'] == 'error'
    sent = []
    monkeypatch.setattr(engine, 'notify_telegram', lambda text: sent.append(text) or True)
    subscribe.drain_subscription_notifications(force=True)
    cache(monkeypatch, [1, 2, 3], ts=time.time())
    assert subscribe.recover_subscription_notifications()['status'] == 'success'
    assert subscribe.load_subscription_notification_status()['reconcile_required'] is False
    rows = subscribe._load_notification_queue()['events']
    pending = [row for row in rows if row['status'] == 'pending']
    assert len(pending) == 1 and pending[0]['updates'][0]['new_eps'] == ['S01E03']
    subscribe.drain_subscription_notifications(force=True)
    assert len(sent) == 2 and 'S01E02' not in sent[-1]
    assert subscribe._load_sub_state()['a']['latest_ep'] == 'S01E03'


def test_repair_failure_keeps_marker_and_retries_no_more_than_every_five_minutes(event_env, monkeypatch):
    subscribe._mark_notification_reconcile()
    calls = []
    monkeypatch.setattr(subscribe, 'process_subscription_tmdb_calibration',
                        lambda **kw: calls.append(kw) or {'status': 'error'})
    assert subscribe.recover_subscription_notifications()['status'] == 'error'
    assert subscribe.recover_subscription_notifications()['status'] == 'skipped'
    assert len(calls) == 1 and subscribe.load_subscription_notification_status()['reconcile_required']


def test_new_repair_generation_is_not_cleared_by_an_older_success(event_env, monkeypatch):
    subscribe._mark_notification_reconcile()

    def repair(**kw):
        subscribe._mark_notification_reconcile()
        return {'status': 'success'}

    monkeypatch.setattr(subscribe, 'process_subscription_tmdb_calibration', repair)
    subscribe.recover_subscription_notifications()
    assert subscribe.load_subscription_notification_status()['reconcile_required']


def test_dispatch_budget_stops_between_batches(event_env, monkeypatch):
    seed(); enqueue([1, 2]); enqueue([1, 2, 3])
    monkeypatch.setattr(engine, 'notify_telegram', lambda *_: True)
    clock = iter([0, 0, 0, 16])  # deadline, batch, card, next batch
    monkeypatch.setattr(subscribe.time, 'monotonic', lambda: next(clock))
    result = subscribe.drain_subscription_notifications(time_budget=15)
    assert result['sent'] == 1 and result['pending'] == 1


def test_unreadable_pause_config_cannot_send_pending_event(event_env, monkeypatch):
    seed(); enqueue([1, 2])
    monkeypatch.setattr(subscribe._cfg, 'get_subscriptions',
                        lambda: (_ for _ in ()).throw(OSError('config unavailable')))
    monkeypatch.setattr(engine, 'notify_telegram', lambda *_: pytest.fail('must not send without pause verification'))
    result = subscribe.drain_subscription_notifications(force=True)
    assert result['status'] == 'error' and result['failed'] == 1
    assert subscribe._load_notification_queue()['events'][0]['status'] == 'pending'


def test_stale_calibration_uses_targeted_live_queries_not_stale_absence(event_env, monkeypatch):
    seed(); cache(monkeypatch)
    calls = []

    def query(path, params):
        calls.append(params)
        if params['IncludeItemTypes'] == 'Series':
            assert params['Ids'] == 's' and params['Limit'] == 64
            return {'Items': [{'Id': 's', 'Type': 'Series', 'Name': '测试剧', 'ProviderIds': {'Tmdb': '100'}}]}
        assert params['ParentId'] == 's' and params['Limit'] == 5000
        return {'Items': [{'ParentIndexNumber': 1, 'IndexNumber': 1, 'IndexNumberEnd': 2}]}

    monkeypatch.setattr(engine, 'emby_request', query)
    monkeypatch.setattr(subscribe, '_disk_series_eps', lambda *_: pytest.fail('automatic calibration must not disk scan'))
    result = subscribe.process_subscription_tmdb_calibration(dispatch=False)
    assert result['status'] == 'success' and result['updates'][0]['new_eps'] == ['S01E02']
    assert result['rows'][0]['facts_source'] == 'emby_live' and len(calls) == 2


@pytest.mark.parametrize('bad', ['network', 'wrong_id', 'wrong_tmdb', 'truncated_series', 'truncated_episodes'])
def test_failed_precise_calibration_preserves_state_and_never_falls_back_to_full_scan(event_env, monkeypatch, bad):
    seed(); cache(monkeypatch)
    before = copy.deepcopy(subscribe._load_sub_state())

    def query(path, params):
        assert 'Ids' in params or 'ParentId' in params
        if bad == 'network':
            raise OSError('Emby timeout')
        if params['IncludeItemTypes'] == 'Series':
            return {'Items': [{'Id': 'wrong' if bad == 'wrong_id' else 's', 'Type': 'Series',
                               'ProviderIds': {'Tmdb': '200' if bad == 'wrong_tmdb' else '100'}}],
                    'TotalRecordCount': 2 if bad == 'truncated_series' else 1}
        return {'Items': [{'ParentIndexNumber': 1, 'IndexNumber': 1}], 'TotalRecordCount': 2}

    monkeypatch.setattr(engine, 'emby_request', query)
    monkeypatch.setattr(subscribe, '_disk_series_eps', lambda *_: pytest.fail('no disk fallback'))
    result = subscribe.process_subscription_tmdb_calibration(dispatch=False)
    assert result['status'] == 'error' and result['updates'] == []
    assert subscribe._load_sub_state() == before


def test_fresh_calibration_reuses_verified_cache_without_emby_request(event_env, monkeypatch):
    seed(); cache(monkeypatch, [1, 2], time.time())
    monkeypatch.setattr(engine, 'emby_request', lambda *_: pytest.fail('fresh cache must be reused'))
    result = subscribe.process_subscription_tmdb_calibration(dispatch=False)
    assert result['status'] == 'success' and result['updates'][0]['new_eps'] == ['S01E02']


def test_unrelated_mapping_entries_are_not_expanded_into_episode_sets(event_env, monkeypatch):
    calls = []
    monkeypatch.setattr(subscribe, '_mapping_entry_facts',
                        lambda row, ts: calls.append(row['tmdb_id']) or facts([1]))
    result = subscribe._mapping_facts_by_tmdb({'series': [
        {'tmdb_id': '100'}, {'tmdb_id': '200'}, {'tmdb_id': '300'}]}, {'100'})
    assert set(result) == {'100'} and calls == ['100']


def test_sender_single_flight_and_scheduler_never_wait_for_telegram(event_env, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls, repairs = [], []
    monkeypatch.setattr(scheduler, '_stop', threading.Event())
    monkeypatch.setattr(scheduler, '_notification_worker', {'t': None})

    def drain(**kw):
        calls.append(kw); entered.set(); assert release.wait(3)

    monkeypatch.setattr(scheduler.engine, 'drain_subscription_notifications', drain)
    monkeypatch.setattr(scheduler.engine, 'recover_subscription_notifications', lambda: repairs.append(True))
    try:
        assert scheduler._request_notification_delivery()
        assert entered.wait(2)
        assert not scheduler._request_notification_delivery()
        assert len(calls) == 1 and calls[0]['limit'] == 2 and calls[0]['time_budget'] == 15
        assert callable(calls[0]['should_stop'])
        scheduler._stop.set()
    finally:
        release.set(); scheduler._notification_worker['t'].join(2)
    assert repairs == []
    assert not scheduler._request_notification_delivery()


def test_tick_schedules_sender_before_continuing_other_work(event_env, monkeypatch):
    requested, governed = [], []
    monkeypatch.setattr(scheduler._cfg, 'load_config', lambda: {
        'ingest_enabled': '0', 'tmdb_scan_enabled': '0', 'subscribe_enabled': '1',
        'subscribe_calibration_last_ts': '1000'})
    monkeypatch.setattr(scheduler.overview, 'request_refresh', lambda: None)
    monkeypatch.setattr(scheduler, '_request_notification_delivery', lambda: requested.append(True))
    monkeypatch.setattr(scheduler.engine, 'drain_subscription_notifications', lambda **kw: pytest.fail('tick cannot call sender directly'))
    monkeypatch.setattr(scheduler, '_gov_auto_tick', lambda now: governed.append(now))
    monkeypatch.setattr(scheduler._cfg, 'get_morning_report', lambda: {'enabled': False})
    scheduler._tick(1001)
    assert requested == [True] and governed == [1001]


def test_missing_cache_timestamp_is_not_fabricated_as_fresh(event_env, monkeypatch):
    seed(); cache(monkeypatch, ts=0)
    called = []
    monkeypatch.setattr(subscribe, '_precise_calibration_facts',
                        lambda tid, row: called.append(tid) or facts([1, 2]))
    result = subscribe.process_subscription_tmdb_calibration(dispatch=False)
    assert called == ['100'] and result['updates'][0]['new_eps'] == ['S01E02']


def test_oldest_member_controls_union_freshness():
    base = {'tmdb_id': '100', 'seasons': [{'season': 1, 'episode_numbers': [1]}]}
    union = subscribe._mapping_facts_by_tmdb({'ts': time.time(), 'series': [
        dict(base, id='local', subscription_facts_ts=1),
        dict(base, id='share', subscription_facts_ts=time.time()),
    ]})
    assert union['100']['facts_ts'] == 1


def test_stop_callback_retains_the_next_batch_without_sending(event_env, monkeypatch):
    seed(); enqueue([1, 2]); enqueue([1, 2, 3])
    stopped = threading.Event()
    monkeypatch.setattr(engine, 'notify_telegram', lambda *_: stopped.set() or True)
    result = subscribe.drain_subscription_notifications(should_stop=stopped.is_set)
    assert result['sent'] == 1 and result['pending'] == 1


def test_unknown_queue_event_status_fails_closed_without_reset(event_env, monkeypatch):
    value = {'schema': 1, 'events': [{'id': 'unknown', 'status': 'invalid'}]}
    state_store.save(engine.SUB_NOTIFICATION_FILE, value)
    with pytest.raises(RuntimeError, match='损坏'):
        subscribe._load_notification_queue()
    assert state_store.read(engine.SUB_NOTIFICATION_FILE) == value


def test_malformed_calibration_setting_is_clamped_without_failure():
    from app.routers.subscribe import _calibration_hours
    assert _calibration_hours({'subscribe_calibration_interval_hours': 'invalid'}) == 6
    assert _calibration_hours({'subscribe_calibration_interval_hours': '-2'}) == 1
    assert _calibration_hours({'subscribe_calibration_interval_hours': '999'}) == 168
