import asyncio
from types import SimpleNamespace

from strimkeep.channel_monitor import ChannelMonitor, normalize_rule
from strimkeep.store import Store


def test_channel_rule_has_inbox_mode():
    rule = normalize_rule({'name': 'demo', 'share_mode': 'transfer', 'inbox_mode': True})
    assert rule['inbox_mode'] is True


def test_channel_inbox_queues_each_link_with_channel_target(tmp_path):
    store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
    calls = []

    class Resources:
        def add_many(self, text, source, external_id, title, target_cid, auto_organize):
            calls.append((text, source, external_id, title, target_cid, auto_organize))
            return {'accepted': 1, 'requeued': 0}

    engine = SimpleNamespace(store=store, resources=Resources())
    monitor = ChannelMonitor(engine)
    rule = {'share_mode': 'transfer', 'routes': [], 'share_cid': '111',
            'share_label': '', 'magnet_cid': '222', 'magnet_label': '',
            'ed2k_cid': '333', 'ed2k_label': '', 'offline_cid': '444',
            'offline_label': '', 'organize_after': True}
    message = {'id': 9, 'text': '', 'links': []}
    text = ('https://115.com/s/abcd1234?password=efgh\n'
            'magnet:?xt=urn:btih:ABCDEF123456&dn=Movie')
    try:
        count = asyncio.run(monitor._inbox('demo', 'Demo', message, rule, text))
        assert count == 2
        assert [call[1] for call in calls] == ['telegram', 'telegram']
        assert [call[4] for call in calls] == ['111', '222']
        assert all(call[5] is True for call in calls)
    finally:
        store.close()
