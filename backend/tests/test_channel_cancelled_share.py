import asyncio

from strimkeep.channel_monitor import ChannelMonitor
from strimkeep.cloud115 import CloudError
from test_channel_monitor import FakeResponse, make, page, generated


def test_cancelled_share_is_skipped_and_other_links_and_posts_continue(tmp_path):
    async def run():
        pages = {('testchan', None): FakeResponse(200, page([(100, 'old', [])]))}
        config = {'enabled': True, 'notify': False, 'channels': ['testchan']}
        store, monitor, http, _ = make(tmp_path, pages, config)
        await monitor.refresh('testchan')
        original = monitor.engine.cloud.share_items
        calls = []
        async def listing(secret, cid):
            calls.append(secret['share_code'])
            if secret['share_code'] == 'swcancel':
                raise CloudError('分享已取消', 'CLOUD_REJECTED', 422)
            return await original(secret, cid)
        monitor.engine.cloud.share_items = listing
        cancelled = 'https://115.com/s/swcancel'
        good = 'https://115.com/s/swvalid1'
        http.pages[('testchan', None)] = FakeResponse(200, page([
            (101, 'mixed', [cancelled, good]), (102, 'later', [cancelled])]))
        assert await monitor.refresh('testchan') == 1
        assert monitor.sources()[0]['last_message_id'] == 102
        assert generated(store) == 1
        assert calls.count('swcancel') == 1
        assert store.db.execute('select count(*) from channel_cancelled_shares').fetchone()[0] == 1
        restarted = ChannelMonitor(monitor.engine)
        restarted.sleep, restarted.clock = monitor.sleep, monitor.clock
        http.pages[('testchan', None)] = FakeResponse(200, page([(103, 'repost', [cancelled])]))
        assert await restarted.refresh('testchan') == 0
        assert restarted.sources()[0]['last_message_id'] == 103
        assert calls.count('swcancel') == 1
        store.close()
    asyncio.run(run())


def test_cancellation_words_in_network_error_are_not_cached(tmp_path):
    async def run():
        pages = {('testchan', None): FakeResponse(200, page([(100, 'old', [])]))}
        store, monitor, http, _ = make(tmp_path, pages, {'enabled': True, 'notify': False, 'channels': ['testchan']})
        await monitor.refresh('testchan')
        async def failure(secret, cid):
            raise CloudError('分享已取消', 'CLOUD_UNAVAILABLE', 502)
        monitor.engine.cloud.share_items = failure
        http.pages[('testchan', None)] = FakeResponse(200, page([(101, 'new', ['https://115.com/s/swcancel'])]))
        assert await monitor.refresh('testchan') == 0
        assert monitor.sources()[0]['last_message_id'] == 100
        assert store.db.execute('select count(*) from channel_cancelled_shares').fetchone()[0] == 0
        store.close()
    asyncio.run(run())



def test_share_retries_three_times_then_skips_and_advances(tmp_path):
    async def run():
        pages = {('testchan', None): FakeResponse(200, page([(100, 'old', [])]))}
        store, monitor, http, _ = make(tmp_path, pages, {'enabled': True, 'notify': False, 'channels': ['testchan']})
        await monitor.refresh('testchan')
        calls = []
        original = monitor.engine.cloud.share_items
        async def listing(secret, cid):
            calls.append(secret['share_code'])
            if secret['share_code'] == 'swbad123':
                raise CloudError('temporary network error', 'CLOUD_UNAVAILABLE', 502)
            return await original(secret, cid)
        monitor.engine.cloud.share_items = listing
        http.pages[('testchan', None)] = FakeResponse(200, page([
            (101, 'mixed', ['https://115.com/s/swbad123', 'https://115.com/s/swgood12']),
            (102, 'later post', [])]))
        for attempt in range(1, 5):
            if attempt == 3:
                restarted = ChannelMonitor(monitor.engine)
                restarted.clock, restarted.sleep = monitor.clock, monitor.sleep
                monitor = restarted
            await monitor.refresh('testchan')
            row = store.db.execute('select attempts,skipped from channel_share_attempts where share_code=?', ('swbad123',)).fetchone()
            assert row['attempts'] == attempt and bool(row['skipped']) == (attempt == 4)
            assert monitor.sources()[0]['last_message_id'] == (102 if attempt == 4 else 100)
        assert calls.count('swbad123') == 4
        assert generated(store) == 1
        await monitor.refresh('testchan')
        assert calls.count('swbad123') == 4
        store.close()
    asyncio.run(run())
