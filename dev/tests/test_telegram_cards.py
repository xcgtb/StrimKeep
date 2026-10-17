"""Offline transport and durable per-card delivery regressions; no real TG calls."""
import io
import json
import time
import urllib.error

import pytest

from app import engine, morning, posters, state_store, subscribe, tg
from dev.tests.test_subscription_events_p2 import event_env


class Response(io.BytesIO):
    status = 200


def transport(monkeypatch, result=None):
    calls = []
    monkeypatch.setattr(tg, '_runtime_cfg', lambda: {
        'telegram_bot_token': 'fixture-token', 'telegram_chat_id': 'fixture-chat'})
    monkeypatch.setattr(tg, '_notification_poster', lambda value: (b'fixture-photo', 'image/jpeg'))
    def send(req, **kw):
        calls.append(req)
        return Response(json.dumps(result or {'ok': True}).encode())
    monkeypatch.setattr(tg._network, 'open_external', send)
    return calls


def test_local_poster_is_uploaded_with_caption_and_api_ack(monkeypatch):
    calls = transport(monkeypatch)
    assert tg.notify_telegram_card('📺 <b>标题</b>\nS01E15', '/api/tmdb/poster/example.jpg')
    assert len(calls) == 1 and calls[0].full_url.endswith('/sendPhoto')
    assert b'fixture-photo' in calls[0].data
    assert b'name="caption"' in calls[0].data
    assert b'parse_mode' in calls[0].data
    assert b'/api/tmdb/poster/' not in calls[0].data


def test_unknown_send_outcome_never_immediately_duplicates_text(monkeypatch):
    transport(monkeypatch)
    monkeypatch.setattr(tg._network, 'open_external', lambda *a, **kw: (_ for _ in ()).throw(TimeoutError()))
    monkeypatch.setattr(tg, 'notify_telegram', lambda *a: pytest.fail('timeout cannot fallback'))
    assert tg.notify_telegram_card('caption', '/api/tmdb/poster/example.jpg') is False


@pytest.mark.parametrize('code', [400, 413])
def test_definite_photo_rejection_falls_back_to_text(monkeypatch, code):
    transport(monkeypatch)
    monkeypatch.setattr(tg._network, 'open_external', lambda *a, **kw: (_ for _ in ()).throw(
        urllib.error.HTTPError('https://api.telegram.org/fixture', code, 'rejected', {}, None)))
    messages = []
    monkeypatch.setattr(tg, 'notify_telegram', lambda text: messages.append(text) or True)
    assert tg.notify_telegram_card('details', '/api/tmdb/poster/example.jpg')
    assert messages == ['details']


@pytest.mark.parametrize('code', [401, 429, 500])
def test_auth_rate_limit_or_server_failure_preserves_retry(monkeypatch, code):
    transport(monkeypatch)
    monkeypatch.setattr(tg._network, 'open_external', lambda *a, **kw: (_ for _ in ()).throw(
        urllib.error.HTTPError('https://api.telegram.org/fixture', code, 'failed', {}, None)))
    monkeypatch.setattr(tg, 'notify_telegram', lambda *a: pytest.fail('must retain retry'))
    assert tg.notify_telegram_card('details', '/api/tmdb/poster/example.jpg') is False


def test_missing_image_falls_back_before_send(monkeypatch):
    calls = transport(monkeypatch)
    monkeypatch.setattr(tg, '_notification_poster', lambda value: (_ for _ in ()).throw(ValueError()))
    messages = []
    monkeypatch.setattr(tg, 'notify_telegram', lambda text: messages.append(text) or True)
    assert tg.notify_telegram_card('details', '/api/tmdb/poster/missing.jpg')
    assert not calls and messages == ['details']


def test_long_caption_keeps_complete_text(monkeypatch):
    monkeypatch.setattr(tg, '_notification_poster', lambda *a: pytest.fail('too long for photo'))
    messages = []
    monkeypatch.setattr(tg, 'notify_telegram', lambda text: messages.append(text) or True)
    text = '📺' * 600
    assert tg.notify_telegram_card(text, '/api/tmdb/poster/example.jpg')
    assert messages == [text]


@pytest.mark.parametrize('value', ['https://evil.test/a.jpg', 'http://127.0.0.1/a.jpg',
    'https://image.tmdb.org@evil.test/t/p/w500/a.jpg', '//evil.test/a.jpg',
    '/api/tmdb/poster/../../private', '/api/emby/poster/../private'])
def test_poster_source_rejects_arbitrary_hosts_and_paths(monkeypatch, value):
    monkeypatch.setattr(tg._network, 'open_external', lambda *a, **kw: pytest.fail('no network'))
    with pytest.raises(ValueError):
        tg._notification_poster(value)


def test_tmdb_poster_route_uses_existing_cache(monkeypatch):
    calls = []
    monkeypatch.setattr(posters, 'get_tmdb_poster', lambda name, size: calls.append((name, size)) or (b'photo', 'image/jpeg'))
    assert tg._notification_poster('/api/tmdb/poster/abc.jpg?size=w342') == (b'photo', 'image/jpeg')
    assert tg._notification_poster('https://image.tmdb.org/t/p/original/abc.jpg')[0] == b'photo'
    assert calls == [('abc.jpg', 'w500')] * 2


def test_http_success_without_telegram_ack_is_failure(monkeypatch):
    calls = transport(monkeypatch, {'ok': False, 'error_code': 429})
    assert tg.notify_telegram('details') is False
    assert tg.notify_telegram_card('details', '/api/tmdb/poster/example.jpg') is False
    assert len(calls) == 2


def queue_two(monkeypatch):
    monkeypatch.setattr(subscribe._cfg, 'get_subscriptions', lambda: [
        {'id': 'a', 'enabled': True}, {'id': 'b', 'enabled': True}])
    updates = [{'sid': sid, 'name': sid, 'poster': '/api/tmdb/poster/example.jpg',
                'new_eps': ['S01E02']} for sid in ('a', 'b')]
    return subscribe._queue_update_batch(updates, {
        sid: {'notified_episodes': ['S01E01', 'S01E02'], 'latest_ep': 'S01E02'} for sid in ('a', 'b')})


def test_retry_skips_the_card_already_accepted(event_env, monkeypatch):
    queue_two(monkeypatch)
    captions = []
    def send(text, poster, **kw):
        captions.append(text)
        return '《a》' in text
    monkeypatch.setattr(engine, 'notify_telegram_card', send)
    result = subscribe.drain_subscription_notifications(force=True)
    assert result['failed'] == 1 and result['pending'] == 1
    assert subscribe._load_notification_queue()['events'][0]['delivered_sids'] == ['a']
    assert not subscribe._load_sub_state()  # event state committed only after all acknowledgments
    monkeypatch.setattr(engine, 'notify_telegram_card', lambda text, poster, **kw: captions.append(text) or True)
    result = subscribe.drain_subscription_notifications(force=True)
    assert result['sent'] == 1 and result['pending'] == 0
    assert sum('《a》' in text for text in captions) == 1
    assert sum('《b》' in text for text in captions) == 2
    assert subscribe._load_sub_state()['b']['latest_ep'] == 'S01E02'


def test_shutdown_between_cards_preserves_receipt(event_env, monkeypatch):
    queue_two(monkeypatch)
    sent = []
    monkeypatch.setattr(engine, 'notify_telegram_card', lambda text, poster, **kw: sent.append(text) or True)
    result = subscribe.drain_subscription_notifications(should_stop=lambda: bool(sent))
    assert result['failed'] == 0 and result['pending'] == 1 and len(sent) == 1
    assert subscribe._load_notification_queue()['events'][0]['delivered_sids'] == ['a']
    subscribe.drain_subscription_notifications(force=True)
    assert len(sent) == 2 and '《b》' in sent[1]


def test_pause_between_cards_prevents_second_show_delivery(event_env, monkeypatch):
    queue_two(monkeypatch)
    sent = []
    def send(text, poster, **kw):
        sent.append(text)
        monkeypatch.setattr(subscribe._cfg, 'get_subscriptions', lambda: [{'id': 'a', 'enabled': True}])
        return True
    monkeypatch.setattr(engine, 'notify_telegram_card', send)
    subscribe.drain_subscription_notifications()
    subscribe.drain_subscription_notifications()
    assert len(sent) == 1 and 'b' not in subscribe._load_sub_state()


def test_caption_uses_real_counts_and_escapes_metadata():
    text = subscribe._render_notification_card({'name': '甲<&', 'year': 2026,
        'new_eps': ['S01E15', 'S01E16'], 'refilled': ['S01E15'], 'missing_eps': ['S01E02'],
        'have': 15, 'tmdb_total': 16, 'tmdb_source': 'tmdb', 'facts_source': 'library_facts',
        'local_eps': 0, 'share_eps': 15, 'tmdb_id': '100', 'season_counts': [{'season': 1, 'have': 15}]})
    assert '甲&lt;&amp;' in text and 'S01E15–E16' in text and '已补齐' in text
    assert '本地库 0 集' in text and '分享库 15 集' in text and 'TMDB 已播 16 集' in text
    assert len(text.encode('utf-16-le')) // 2 <= 1024
    unknown = subscribe._render_notification_card({'name': '甲', 'have': 2})
    assert '分享库' not in unknown and 'TMDB 已播' not in unknown


def test_duplicate_identity_does_not_sum_overlapping_library_counts():
    row = {'id': 'a', 'tmdb_id': '100', 'local_eps': 1, 'share_eps': 1,
           'seasons': [{'season': 1, 'episode_numbers': [1]}]}
    out = subscribe._mapping_facts_by_tmdb({'series': [row, dict(row, id='b')]})['100']
    assert len(out['episodes']) == 1 and out['local_eps'] is None and out['share_eps'] is None


def test_morning_readable_timestamps_keep_stale_and_pending_warnings(monkeypatch):
    monkeypatch.setattr(morning, 'daily_consistency_snapshot', lambda **kw: {'rule_sig': 'secret-rule-hash'})
    monkeypatch.setattr(morning, 'gap_report', lambda **kw: {'status': 'success', 'missing': [], 'stats': {},
        'facts_version': 'secret-facts-hash', 'facts_ts': time.time(), 'stale': True})
    monkeypatch.setattr(engine, '_load_subscription_report', lambda **kw: None)
    monkeypatch.setattr(engine, 'load_subscription_notification_status', lambda: {'pending': 2})
    monkeypatch.setattr(engine, 'load_subscription_check_status', lambda: {'status': 'partial', 'error': 'TMDB 暂不可用'})
    text = morning.build_morning_report(['subscriptions', 'emby_gap'], cache_only=True)
    assert 'secret-' not in text and '片库更新于' in text
    assert '对照已过期' in text and '2 批通知等待发送' in text and 'TMDB 暂不可用' in text


def test_pausing_accepted_card_still_recovers_its_state(event_env, monkeypatch):
    queue_two(monkeypatch)
    monkeypatch.setattr(engine, 'notify_telegram_card', lambda text, poster, **kw: '《a》' in text)
    subscribe.drain_subscription_notifications(force=True)
    monkeypatch.setattr(subscribe._cfg, 'get_subscriptions', lambda: [])
    monkeypatch.setattr(engine, 'notify_telegram_card', lambda *a, **kw: pytest.fail('paused must not send'))
    result = subscribe.drain_subscription_notifications(force=True)
    assert result['sent'] == 1 and subscribe._load_sub_state()['a']['latest_ep'] == 'S01E02'
    assert 'b' not in subscribe._load_sub_state()
