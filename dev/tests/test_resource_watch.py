"""No external messages: authoritative cache events, durable retry and dedupe."""
import pytest
from app import config, morning, resource_watch as w, tg


@pytest.fixture
def cache(tmp_path, monkeypatch):
    data = {'series': [], 'movies': []}
    monkeypatch.setattr(config, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(config, 'load_config', lambda: {'subscribe_enabled': '1'})
    monkeypatch.setattr(config, 'get_subscriptions', lambda: [])
    monkeypatch.setattr(morning, 'read_emby_lib_cache', lambda: data)
    return data


def test_movie_import_waits_for_emby_and_retries_without_rechecking_media(cache, monkeypatch):
    w.register({'media_type': 'movie', 'tmdb_id': '7', 'title': '样本'})
    w.changed()
    assert w.status()['pending'] == 0
    cache['movies'] = [{'id': 'a', 'ids': ['a'], 'tmdb_id': '7', 'in_local': True}]
    w.changed(); w.changed()
    assert w.status()['pending'] == 1
    monkeypatch.setattr(tg, 'notify_telegram', lambda text: False)
    w.drain(); assert w.status()['pending'] == 1
    data = w._load(); data['pending'][0]['retry'] = 0
    w.state_store.save(w._path(), data)
    monkeypatch.setattr(morning, 'read_emby_lib_cache', lambda: pytest.fail('delivery must not query media'))
    messages = []
    monkeypatch.setattr(tg, 'notify_telegram', lambda text: messages.append(text) or True)
    w.drain(); w.drain()
    assert len(messages) == 1 and w.status()['pending'] == 0


def test_existing_import_is_baseline_and_cross_library_add_is_new(cache):
    cache['movies'] = [{'id': 'a', 'ids': ['a'], 'tmdb_id': '7', 'in_local': True}]
    w.register({'media_type': 'movie', 'tmdb_id': '7', 'title': '样本'})
    w.changed(); assert w.status()['pending'] == 0
    cache['movies'][0].update(ids=['a', 'b'], in_share=True)
    w.changed(); assert w.status()['pending'] == 1


def test_tv_existing_notification_subscription_prevents_duplicate(cache, monkeypatch):
    w.register({'media_type': 'tv', 'tmdb_id': '7', 'title': '剧'})
    monkeypatch.setattr(config, 'get_subscriptions', lambda: [{'tmdb_id': '7', 'enabled': True}])
    cache['series'] = [{'id': 'a', 'tmdb_id': '7', 'have_eps': 3, 'in_local': True}]
    w.changed(); assert w.status()['pending'] == 0


def test_ambiguous_movie_identity_never_sends(cache):
    w.register({'media_type': 'movie', 'tmdb_id': '7', 'title': '样本'})
    cache['movies'] = [{'id': 'a', 'tmdb_id': '7'}, {'id': 'b', 'tmdb_id': '7'}]
    w.changed(); assert w.status()['pending'] == 0


def test_no_resource_watches_adds_no_library_cache_read(cache, monkeypatch):
    monkeypatch.setattr(morning, 'read_emby_lib_cache', lambda: pytest.fail('existing installations must not read an extra snapshot'))
    w.changed()
