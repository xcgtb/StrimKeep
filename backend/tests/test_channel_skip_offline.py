import asyncio

from test_channel_monitor import FakeResponse, make, page, generated


def test_strm_channel_skips_magnet_and_ed2k_but_processes_115_share(tmp_path):
    async def run():
        config = {'enabled': True, 'notify': False, 'channels': [
            {'name': 'testchan', 'share_mode': 'strm', 'offline_cid': '12345'}]}
        pages = {('testchan', None): FakeResponse(200, page([(100, 'old', [])]))}
        store, monitor, http, _ = make(tmp_path, pages, config)
        await monitor.refresh('testchan')
        text = 'magnet:?xt=urn:btih:abcdef ed2k://|file|x.mkv|100|ABCDEF|/'
        share = 'https://115.com/s/swabc123?password=abcd'
        http.pages[('testchan', None)] = FakeResponse(200, page([(101, text, [share])]))
        assert await monitor.refresh('testchan') == 1
        assert generated(store) == 1
        assert monitor.engine.offline.calls == []
        assert monitor.sources()[0]['last_message_id'] == 101
        assert store.db.execute("SELECT 1 FROM events WHERE kind='channel' AND message LIKE '%已跳过 2 个链接%'").fetchone()
        store.close()
    asyncio.run(run())


def test_strm_channel_skips_magnet_only_without_stalling(tmp_path):
    async def run():
        config = {'enabled': True, 'notify': False, 'channels': [
            {'name': 'testchan', 'share_mode': 'strm', 'offline_cid': '12345'}]}
        pages = {('testchan', None): FakeResponse(200, page([(100, 'old', [])]))}
        store, monitor, http, _ = make(tmp_path, pages, config)
        await monitor.refresh('testchan')
        http.pages[('testchan', None)] = FakeResponse(200, page([(101, 'magnet:?xt=urn:btih:abcdef', [])]))
        assert await monitor.refresh('testchan') == 0
        assert monitor.sources()[0]['last_message_id'] == 101
        assert monitor.engine.offline.calls == []
        store.close()
    asyncio.run(run())


def test_strm_mode_never_submits_offline_with_saved_directories_or_routes(tmp_path):
    async def run():
        config = {'enabled': True, 'notify': False, 'channels': [{
            'name':'testchan', 'share_mode':'strm', 'magnet_cid':'111', 'ed2k_cid':'222',
            'routes':[{'keywords':'4K','target_cid':'333','target_label':'saved'}]}]}
        pages = {('testchan', None): FakeResponse(200, page([(100, 'old', [])]))}
        store, monitor, http, _ = make(tmp_path, pages, config)
        await monitor.refresh('testchan')
        text = '4K magnet:?xt=urn:btih:abcdef ed2k://|file|x.mkv|100|ABCDEF|/'
        share = 'https://115.com/s/swabc123?password=abcd'
        http.pages[('testchan', None)] = FakeResponse(200, page([(101, text, [share])]))
        assert await monitor.refresh('testchan') == 1
        assert generated(store) == 1 and monitor.engine.offline.calls == []
        assert monitor.rule('testchan')['magnet_cid'] == '111'
        store.close()
    asyncio.run(run())
