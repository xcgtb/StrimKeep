"""公开频道监控：解析游客预览页、按游标增量、去重、退避。"""
import asyncio

import pytest

import strimkeep.channel_monitor as channel_monitor
from strimkeep.channel_monitor import ChannelMonitor, normalize_channel, parse_channel
from strimkeep.channels_api import ChannelOptions
from strimkeep.cloud115 import CloudError
from strimkeep.store import Store

SHARE = 'https://115.com/s/swabc123?password=abcd'


class FakeNotifications:
    def __init__(self):
        self.sent = []

    async def publish(self, *args, **kwargs):
        self.sent.append((args, kwargs))
        return 'ok'


class FakeResponse:
    def __init__(self, status_code, text=''):
        self.status_code, self.text = status_code, text


class FakeHTTP:
    def __init__(self, pages):
        self.pages = pages          # {(name, before): FakeResponse}
        self.urls = []

    async def get(self, url, headers=None):
        self.urls.append(url)
        name = url.split('/s/')[1].split('?')[0]
        before = int(url.split('?before=')[1]) if '?before=' in url else None
        return self.pages[(name, before)]


class FakeTransfer:
    def __init__(self):
        self.calls, self.error = [], None

    async def enqueue(self, text, dest, auto=False, origin='web', update=None):
        self.calls.append({'text': text, 'dest': dest, 'auto': auto, 'origin': origin, 'update': update})
        if self.error:
            raise self.error
        return [len(self.calls)]


class FakeOffline:
    def __init__(self):
        self.calls, self.error = [], None

    async def submit(self, urls, cid, label='', organize=True):
        self.calls.append({'urls': list(urls), 'cid': cid, 'label': label, 'organize': organize})
        if self.error:
            raise self.error
        return 1


class FakeCloud:
    def __init__(self):
        self.videos = [{'id': 'v1', 'name': 'Test.Show.S01E01.2160p.mkv',
                        'size': 300 * 1024 * 1024, 'is_dir': False, 'sha1': 'a' * 40}]

    async def share_items(self, secret, cid):
        return list(self.videos) if str(cid) == '0' else []


class FakeEngine:
    def __init__(self, store, http, root=None):
        self.store, self.http = store, http
        self.root = root or store.path.parent
        self.cloud = FakeCloud()
        self.notifications = FakeNotifications()
        self.notice_tasks = set()
        self.transfer = FakeTransfer()
        self.offline = FakeOffline()


def generated(store):
    """已生成的待整理 STRM 条数（频道免转存模式不再直接建整理任务）。"""
    return store.db.execute('SELECT count(*) FROM share_strms').fetchone()[0]


def make(tmp_path, pages, config=None, monkeypatch=None):
    store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
    if config:
        store.set_many({'channel_monitor': config})
    http = FakeHTTP(pages)
    monitor = ChannelMonitor(FakeEngine(store, http, tmp_path / 'strm'))
    clock = {'now': 1000.0}
    monitor.clock = lambda: clock['now']
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)
        clock['now'] += seconds

    monitor.sleep = fake_sleep
    monitor.slept = slept
    queued = []
    if monkeypatch is not None:
        def fake_enqueue(engine_arg, origin='web', secret=None, channel_claim=None):
            queued.append((origin, secret))
            return len(queued)
        monkeypatch.setattr(channel_monitor, 'enqueue_organize', fake_enqueue)
    return store, monitor, http, queued


def page(messages, title='测试频道', has_more=False, button='https://example.com/x'):
    blocks = []
    for message_id, text, links in messages:
        anchors = ''.join('<a href="%s">x</a>' % url for url in links)
        blocks.append(
            '<div class="tgme_widget_message text_not_supported_wrap" data-post="testchan/%d">'
            '<div class="tgme_widget_message_text js-message_text" dir="auto">%s%s</div>'
            '<div class="tgme_widget_message_footer"><time datetime="2026-10-27T00:00:00+00:00">x</time>'
            '<a class="tgme_widget_message_inline_button" href="%s">按钮</a></div>'
            '</div>' % (message_id, text, anchors, button))
    more = '<a class="tme_messages_more js-messages_more" data-before="1">more</a>' if has_more else ''
    return ('<html><head><meta property="og:title" content="%s"></head><body>%s%s</body></html>'
            % (title, more, ''.join(blocks)))


CONFIG = {'enabled': True, 'channels': ['testchan'], 'notify': True, 'interval': 300}


def seed_orphan(store, name, title, cursor, posts=0):
    """造一条"已从配置移除、channel_state 还留着"的旧条目。"""
    with store.db:
        store.db.execute(
            'INSERT INTO channel_state(name,title,last_message_id,state,posts,added) '
            'VALUES(?,?,?,?,?,?)', (name, title, cursor, 'ready', posts, 1.0))
        store.db.execute('INSERT INTO channel_posts(name,message_id,created) VALUES(?,?,?)',
                         (name, cursor - 1, 1.0))


def test_removed_then_readded_channel_keeps_its_cursor(tmp_path, monkeypatch):
    """删掉旧条目再从会话列表重加数字 ID，不该重扫一页。

    账本按频道名字记，名字从用户名换成数字 ID 就对不上了：新条目当"首次接入"回溯
    一页，磁力/电驴又提交一遍。抓到标题后认回旧条目，游标和账本一起接过来。
    """
    html = page([(101, '没内容的帖子', []), (102, '没内容的帖子', [])], title='测试频道')
    pages = {('testchan', None): FakeResponse(200, html)}
    config = {'enabled': True, 'notify': True,
              'channels': [{'name': 'testchan', 'enabled': True, 'source': 'preview'}]}
    store, monitor, http, queued = make(tmp_path, pages, config, monkeypatch)
    seed_orphan(store, 'oldname', '测试频道', cursor=100, posts=7)

    asyncio.run(monitor.refresh('testchan'))

    row = store.db.execute("SELECT * FROM channel_state").fetchall()
    assert [r['name'] for r in row] == ['testchan']        # 旧行搬完就删，不留残留
    assert row[0]['last_message_id'] == 102                # 从 100 接着走，不是从 0 重扫
    assert row[0]['posts'] == 7                            # 累计条数也接过来
    assert [r['name'] for r in store.db.execute(
        'SELECT DISTINCT name FROM channel_posts')] == ['testchan']   # 旧账本搬到新名字下
    assert [r['message_id'] for r in store.db.execute(
        'SELECT message_id FROM channel_posts ORDER BY message_id')] == [99, 101, 102]


def test_a_channel_still_in_the_list_is_not_adopted(tmp_path, monkeypatch):
    """旧条目还在配置里时不抢账本 —— 那是两份配置，交给判重逻辑处理。"""
    html = page([(101, '没内容的帖子', [])], title='测试频道')
    pages = {('testchan', None): FakeResponse(200, html)}
    config = {'enabled': True, 'notify': True,
              'channels': [{'name': 'testchan', 'enabled': True, 'source': 'preview'},
                           {'name': 'oldname', 'enabled': True, 'source': 'preview'}]}
    store, monitor, http, queued = make(tmp_path, pages, config, monkeypatch)
    seed_orphan(store, 'oldname', '测试频道', cursor=100)

    asyncio.run(monitor.refresh('testchan'))

    assert [r['name'] for r in store.db.execute(
        'SELECT name FROM channel_state ORDER BY name')] == ['oldname', 'testchan']
    assert store.db.execute("SELECT last_message_id FROM channel_state WHERE name='testchan'").fetchone()[
        'last_message_id'] == 101



def test_normalize_channel_accepts_common_forms_and_rejects_invites():
    assert normalize_channel('@Test_Chan') == 'Test_Chan'
    assert normalize_channel('testchan') == 'testchan'
    assert normalize_channel('https://t.me/s/testchan?before=3') == 'testchan'
    assert normalize_channel('t.me/Test_Chan') == 'Test_Chan'
    assert normalize_channel('https://t.me/+AbCdEf') == ''
    with pytest.raises(ValueError):
        normalize_channel('https://t.me/+AbCdEf', strict=True)
    with pytest.raises(ValueError):
        ChannelOptions(channels=[{'name': 'https://t.me/+AbCdEf'}])


def test_parse_channel_reads_text_links_and_time():
    html = page([(7, '第七集来了', ['https://115.com/s/swabc123?password=abcd']),
                 (8, '纯文字没有链接', [])], title='资源频道',
                button='https://115.com/s/swbtn999')
    parsed = parse_channel(html)
    assert parsed['title'] == '资源频道'
    assert [m['id'] for m in parsed['messages']] == [7, 8]
    assert parsed['messages'][0]['text'].startswith('第七集来了')
    assert 'https://115.com/s/swabc123?password=abcd' in parsed['messages'][0]['links']
    assert 'https://115.com/s/swbtn999' in parsed['messages'][0]['links']   # 内联按钮里的链接
    assert parsed['messages'][0]['at'].startswith('2026-10-27')


def test_first_poll_backfills_the_visible_page(tmp_path, monkeypatch):
    async def run():
        html = page([(100, '老帖', [SHARE]), (101, '公告', [])])
        store, monitor, http, queued = make(
            tmp_path, {('testchan', None): FakeResponse(200, html)}, CONFIG, monkeypatch)
        assert await monitor.refresh('testchan') == 1        # 首轮回溯当前页并处理
        assert generated(store) == 1
        state = monitor.sources()[0]
        assert state['last_message_id'] == 101 and state['state'] == 'ready'
        store.close()
    asyncio.run(run())


def test_first_poll_does_not_walk_further_back(tmp_path, monkeypatch):
    async def run():
        html = page([(100, '老帖', [SHARE])], has_more=True)
        store, monitor, http, queued = make(
            tmp_path, {('testchan', None): FakeResponse(200, html)}, CONFIG, monkeypatch)
        assert await monitor.refresh('testchan') == 1
        assert http.urls == ['https://t.me/s/testchan']       # 首轮只读一页
        store.close()
    asyncio.run(run())


def test_new_post_with_share_is_queued_once(tmp_path, monkeypatch):
    async def run():
        first = page([(100, '老帖', [])])
        second = page([(100, '老帖', []), (101, '新帖', [SHARE])])
        pages = {('testchan', None): FakeResponse(200, first)}
        store, monitor, http, queued = make(tmp_path, pages, CONFIG, monkeypatch)
        await monitor.refresh('testchan')          # 种子
        http.pages[('testchan', None)] = FakeResponse(200, second)
        assert await monitor.refresh('testchan') == 1
        assert generated(store) == 1
        assert store.db.execute('SELECT share_code FROM share_strms').fetchone()[0] == 'swabc123'
        assert monitor.sources()[0]['last_message_id'] == 101
        assert monitor.sources()[0]['posts'] == 1
        store.close()
    asyncio.run(run())


def test_post_without_links_advances_the_cursor_without_queueing(tmp_path, monkeypatch):
    async def run():
        first = page([(100, '老帖', [])])
        second = page([(100, '老帖', []), (101, '公告', [])])
        pages = {('testchan', None): FakeResponse(200, first)}
        store, monitor, http, queued = make(tmp_path, pages, CONFIG, monkeypatch)
        await monitor.refresh('testchan')
        http.pages[('testchan', None)] = FakeResponse(200, second)
        assert await monitor.refresh('testchan') == 0
        assert queued == []
        assert monitor.sources()[0]['last_message_id'] == 101
        store.close()
    asyncio.run(run())


def test_same_share_in_a_later_post_is_not_queued_again(tmp_path, monkeypatch):
    async def run():
        first = page([(100, 'x', [])])
        second = page([(100, 'x', []), (101, '第一次', [SHARE])])
        third = page([(101, '第一次', [SHARE]), (102, '转来转去', [SHARE])])
        pages = {('testchan', None): FakeResponse(200, first)}
        store, monitor, http, queued = make(tmp_path, pages, CONFIG, monkeypatch)
        await monitor.refresh('testchan')
        http.pages[('testchan', None)] = FakeResponse(200, second)
        assert await monitor.refresh('testchan') == 1
        http.pages[('testchan', None)] = FakeResponse(200, third)
        assert await monitor.refresh('testchan') == 0
        assert generated(store) == 1
        store.close()
    asyncio.run(run())


def test_busy_channel_walks_back_a_page_to_reach_the_cursor(tmp_path, monkeypatch):
    async def run():
        pages = {('testchan', None): FakeResponse(200, page([(i, 'x', []) for i in range(100, 111)]))}
        store, monitor, http, queued = make(tmp_path, pages, CONFIG, monkeypatch)
        await monitor.refresh('testchan')
        assert monitor.sources()[0]['last_message_id'] == 110

        # 游标 110，新一页只有 121-140（其中 125 带链接）：必须再翻一页拿到 111-120
        http.pages[('testchan', None)] = FakeResponse(
            200, page([(i, '带链接' if i == 125 else 'x', [SHARE] if i == 125 else [])
                       for i in range(121, 141)], has_more=True))
        http.pages[('testchan', 121)] = FakeResponse(
            200, page([(i, 'x', []) for i in range(111, 121)], has_more=True))
        http.pages[('testchan', 111)] = FakeResponse(200, page([(110, 'old', [])]))
        assert await monitor.refresh('testchan') == 0
        assert monitor.sources()[0]['last_message_id'] == 110
        assert await monitor.refresh('testchan') == 1
        assert generated(store) == 1
        assert monitor.sources()[0]['last_message_id'] == 140
        store.close()
    asyncio.run(run())


def test_rate_limit_marks_backoff_and_keeps_state(tmp_path, monkeypatch):
    async def run():
        pages = {('testchan', None): FakeResponse(429, '')}
        store, monitor, http, queued = make(tmp_path, pages, CONFIG, monkeypatch)
        assert await monitor.refresh('testchan') == 0
        state = monitor.sources()[0]
        assert state['state'] == 'backoff' and '限流' in state['error']
        store.close()
    asyncio.run(run())


def test_empty_preview_is_reported_as_unreadable(tmp_path, monkeypatch):
    async def run():
        pages = {('testchan', None): FakeResponse(200, '<html><body>nothing</body></html>')}
        store, monitor, http, queued = make(tmp_path, pages, CONFIG, monkeypatch)
        assert await monitor.refresh('testchan') == 0
        state = monitor.sources()[0]
        assert state['state'] == 'error' and '公开预览' in state['error']
        store.close()
    asyncio.run(run())


def test_requests_to_t_me_are_spaced_out(tmp_path, monkeypatch):
    async def run():
        pages = {('onechan', None): FakeResponse(200, page([(1, 'x', [])])),
                 ('twochan', None): FakeResponse(200, page([(1, 'x', [])]))}
        store, monitor, http, queued = make(
            tmp_path, pages, {'enabled': True, 'channels': ['onechan', 'twochan'], 'notify': False}, monkeypatch)
        await monitor.tick()
        assert len(http.urls) == 2
        assert monitor.slept and min(monitor.slept) >= monitor.GAP - 1e-6
        store.close()
    asyncio.run(run())


def test_legacy_string_channels_get_rule_defaults(tmp_path, monkeypatch):
    async def run():
        store, monitor, http, queued = make(
            tmp_path, {}, {'enabled': True, 'channels': ['testchan'], 'notify': True}, monkeypatch)
        rule = monitor.config()['channels'][0]
        assert rule == {'name': 'testchan', 'enabled': True, 'share_mode': 'strm', 'routes': [],
                        'share_cid': '', 'share_label': '', 'offline_cid': '', 'offline_label': '',
                        'magnet_cid': '', 'magnet_label': '', 'ed2k_cid': '', 'ed2k_label': '',
                        'include': '', 'exclude': '', 'organize_after': False, 'source': 'preview',
                        'history_pages': 1}
        store.close()
    asyncio.run(run())


def test_include_regex_only_takes_matching_posts(tmp_path, monkeypatch):
    async def run():
        pages = {('testchan', None): FakeResponse(200, page([(100, 'x', [])]))}
        store, monitor, http, queued = make(tmp_path, pages, {
            'enabled': True, 'channels': [{'name': 'testchan', 'include': '2160p'}], 'notify': False}, monkeypatch)
        await monitor.refresh('testchan')
        http.pages[('testchan', None)] = FakeResponse(200, page([(101, '1080p 版本', [SHARE])]))
        assert await monitor.refresh('testchan') == 0          # 不命中 include
        http.pages[('testchan', None)] = FakeResponse(200, page([(102, '2160p 版本', [SHARE])]))
        assert await monitor.refresh('testchan') == 1
        store.close()
    asyncio.run(run())


def test_exclude_regex_skips_posts(tmp_path, monkeypatch):
    async def run():
        pages = {('testchan', None): FakeResponse(200, page([(100, 'x', [])]))}
        store, monitor, http, queued = make(tmp_path, pages, {
            'enabled': True, 'channels': [{'name': 'testchan', 'exclude': '预告'}], 'notify': False}, monkeypatch)
        await monitor.refresh('testchan')
        http.pages[('testchan', None)] = FakeResponse(200, page([(101, '求片 预告 2160p', [SHARE])]))
        assert await monitor.refresh('testchan') == 0
        http.pages[('testchan', None)] = FakeResponse(200, page([(102, '正片 2160p', [SHARE])]))
        assert await monitor.refresh('testchan') == 1
        store.close()
    asyncio.run(run())


def test_broken_regex_is_treated_as_no_filter(tmp_path, monkeypatch):
    async def run():
        pages = {('testchan', None): FakeResponse(200, page([(100, 'x', [])]))}
        store, monitor, http, queued = make(tmp_path, pages, {
            'enabled': True, 'channels': [{'name': 'testchan', 'include': '(['}], 'notify': False}, monkeypatch)
        await monitor.refresh('testchan')
        http.pages[('testchan', None)] = FakeResponse(200, page([(101, '随便', [SHARE])]))
        assert await monitor.refresh('testchan') == 1
        store.close()
    asyncio.run(run())


def test_transfer_mode_saves_into_the_selected_115_folder(tmp_path, monkeypatch):
    async def run():
        pages = {('testchan', None): FakeResponse(200, page([(100, 'x', [])]))}
        store, monitor, http, queued = make(tmp_path, pages, {
            'enabled': True, 'notify': False,
            'channels': [{'name': 'testchan', 'mode': 'transfer',
                          'target_cid': '12345', 'target_label': '115 网盘 / 转存'}]}, monkeypatch)
        await monitor.refresh('testchan')
        http.pages[('testchan', None)] = FakeResponse(200, page([(101, '正片', [SHARE])]))
        assert await monitor.refresh('testchan') == 1
        call = monitor.engine.transfer.calls[0]
        assert call['dest'] == '12345' and call['auto'] is False and call['origin'] == 'channel'
        assert 'https://115.com/s/swabc123?password=abcd' in call['text']
        store.close()
    asyncio.run(run())


def test_transfer_failure_is_reported_not_silently_dropped(tmp_path, monkeypatch):
    async def run():
        pages = {('testchan', None): FakeResponse(200, page([(100, 'x', [])]))}
        store, monitor, http, queued = make(tmp_path, pages, {
            'enabled': True, 'notify': False,
            'channels': [{'name': 'testchan', 'mode': 'transfer', 'target_cid': '12345'}]}, monkeypatch)
        await monitor.refresh('testchan')
        monitor.engine.transfer.error = ValueError('自动整理的目标必须是已保存的待整理目录之一。')
        http.pages[('testchan', None)] = FakeResponse(200, page([(101, '正片', [SHARE])]))
        assert await monitor.refresh('testchan') == 0
        state = monitor.sources()[0]
        assert state['state'] == 'error' and '转存失败' in state['error']
        store.close()
    asyncio.run(run())


def test_cron_schedule_drives_the_next_delay(tmp_path, monkeypatch):
    async def run():
        store, monitor, http, queued = make(tmp_path, {}, {
            'enabled': True, 'schedule_mode': 'cron', 'cron': '*/10 * * * *', 'channels': []}, monkeypatch)
        delay = monitor.next_delay()
        assert 30 <= delay <= 600
        store.close()
    asyncio.run(run())


def test_api_rejects_incomplete_transfer_rule_and_bad_cron():
    with pytest.raises(ValueError):
        ChannelOptions(channels=[{'name': 'testchan', 'mode': 'transfer'}])
    with pytest.raises(ValueError):
        ChannelOptions(schedule_mode='cron', cron='not a cron')
    with pytest.raises(ValueError):
        ChannelOptions(channels=[{'name': 'testchan', 'include': '(['}])
    ok = ChannelOptions(channels=[{'name': '@Test_Chan', 'mode': 'transfer', 'target_cid': '12345'}],
                        schedule_mode='cron', cron='*/10 * * * *')
    assert ok.channels[0].name == 'Test_Chan' and ok.cron == '*/10 * * * *'


def test_channel_never_rewrites_the_organize_sources(tmp_path, monkeypatch):
    """目录不锁死：频道转存不该替用户改「待整理目录」。"""
    async def run():
        store, monitor, http, queued = make(
            tmp_path, {('testchan', None): FakeResponse(200, page([(100, 'x', [])]))},
            {'enabled': True, 'notify': False,
             'channels': [{'name': 'testchan', 'mode': 'transfer', 'target_cid': '12345'}]}, monkeypatch)
        store.set_many({'organize_config': {'source_cids': ['100'], 'archive_cid': '200'}})
        await monitor.refresh('testchan')
        http.pages[('testchan', None)] = FakeResponse(200, page([(101, '正片', [SHARE])]))
        await monitor.refresh('testchan')
        config = store.get('organize_config')
        assert config['source_cids'] == ['100']      # 没被偷偷加进去
        assert config['archive_cid'] == '200'
        store.close()
    asyncio.run(run())

def test_strm_mode_skips_magnet_in_mixed_post(tmp_path, monkeypatch):
    # 免转存模式处理分享 STRM，跳过同一消息里的磁力链接。
    async def run():
        pages = {('testchan', None): FakeResponse(200, page([(100, 'x', [])]))}
        store, monitor, http, queued = make(tmp_path, pages, {
            'enabled': True, 'notify': False,
            'channels': [{'name': 'testchan', 'share_mode': 'strm',
                          'offline_cid': '777', 'offline_label': '离线目录'}]}, monkeypatch)
        await monitor.refresh('testchan')
        magnet = 'magnet:?xt=urn:btih:abcdef&dn=test'
        http.pages[('testchan', None)] = FakeResponse(200, page([(101, SHARE + ' 磁力 ' + magnet, [])]))
        await monitor.refresh('testchan')
        assert generated(store) == 1
        assert monitor.engine.offline.calls == []
        store.close()
    asyncio.run(run())


def test_offline_mode_submits_magnet_and_ed2k(tmp_path, monkeypatch):
    async def run():
        pages = {('testchan', None): FakeResponse(200, page([(100, 'x', [])]))}
        store, monitor, http, queued = make(tmp_path, pages, {
            'enabled': True, 'notify': False,
            'channels': [{'name': 'testchan', 'mode': 'offline',
                          'target_cid': '12345', 'target_label': '115 网盘 / 云下载'}]}, monkeypatch)
        await monitor.refresh('testchan')
        magnet = 'magnet:?xt=urn:btih:abcdef&dn=test'
        ed2k = 'ed2k://|file|x.mkv|1000|ABCDEF|/'
        http.pages[('testchan', None)] = FakeResponse(200, page([(101, '磁力 ' + magnet + ' 还有 ' + ed2k, [])]))
        assert await monitor.refresh('testchan') == 2
        calls = monitor.engine.offline.calls
        assert [call['urls'] for call in calls] == [[magnet], [ed2k]]
        assert all(call['cid'] == '12345' for call in calls)
        assert all(call['organize'] is False for call in calls)  # 频道只落盘，不自动整理
        assert queued == []                       # 离线模式不生成分享 STRM 任务
        store.close()
    asyncio.run(run())


def test_offline_mode_ignores_posts_without_magnet(tmp_path, monkeypatch):
    async def run():
        pages = {('testchan', None): FakeResponse(200, page([(100, 'x', [])]))}
        store, monitor, http, queued = make(tmp_path, pages, {
            'enabled': True, 'notify': False,
            'channels': [{'name': 'testchan', 'mode': 'offline', 'target_cid': '12345'}]}, monkeypatch)
        await monitor.refresh('testchan')
        http.pages[('testchan', None)] = FakeResponse(200, page([(101, '普通帖子 ' + SHARE, [])]))
        assert await monitor.refresh('testchan') == 0
        assert monitor.engine.offline.calls == []
        store.close()
    asyncio.run(run())


def test_api_requires_a_folder_for_offline_mode():
    with pytest.raises(ValueError):
        ChannelOptions(channels=[{'name': 'testchan', 'mode': 'offline'}])
    ok = ChannelOptions(channels=[{'name': 'testchan', 'mode': 'offline', 'target_cid': '12345'}])
    assert ok.channels[0].mode == 'offline'


def test_transfer_failure_retries_same_post_without_losing_cursor(tmp_path, monkeypatch):
    async def run():
        cfg = {'enabled': True, 'notify': False, 'channels': [{'name':'testchan','mode':'transfer','target_cid':'12345'}]}
        pages = {('testchan', None): FakeResponse(200, page([(100, 'old', [])]))}
        store, monitor, http, _ = make(tmp_path, pages, cfg, monkeypatch)
        await monitor.refresh('testchan')
        http.pages[('testchan', None)] = FakeResponse(200, page([(101, 'new', [SHARE])]))
        monitor.engine.transfer.error = ValueError('temporary')
        await monitor.refresh('testchan')
        assert monitor.sources()[0]['last_message_id'] == 100
        monitor.engine.transfer.error = None
        assert await monitor.refresh('testchan') == 1
        assert monitor.sources()[0]['last_message_id'] == 101
        assert store.db.execute('SELECT job_id FROM channel_shares').fetchone()[0] is not None
        store.close()
    asyncio.run(run())


def test_two_shares_track_their_own_jobs(tmp_path, monkeypatch):
    async def run():
        cfg = {'enabled':True, 'notify':False, 'channels':[{'name':'testchan','mode':'transfer','target_cid':'12345'}]}
        pages = {('testchan', None):FakeResponse(200,page([(100,'x',[])]))}
        store, monitor, http, _ = make(tmp_path,pages,cfg,monkeypatch)
        await monitor.refresh('testchan')
        async def enqueue(*args, **kwargs): return [501, 502]
        monitor.engine.transfer.enqueue = enqueue
        http.pages[('testchan', None)] = FakeResponse(200,page([(101,'x',['https://115.com/s/aaa1','https://115.com/s/bbb2'])]))
        assert await monitor.refresh('testchan') == 2
        rows = store.db.execute('SELECT share_code,job_id FROM channel_shares ORDER BY share_code').fetchall()
        assert [tuple(row) for row in rows] == [('aaa1',501),('bbb2',502)]
        store.close()
    asyncio.run(run())


def test_backfill_continues_after_monitor_restart(tmp_path, monkeypatch):
    async def run():
        pages = {('testchan', None): FakeResponse(200, page([(10,'old',[])]))}
        store, monitor, http, queued = make(tmp_path,pages,CONFIG,monkeypatch)
        await monitor.refresh('testchan')
        http.pages[('testchan', None)] = FakeResponse(200,page([(31,'new',[SHARE])],has_more=True))
        http.pages[('testchan', 31)] = FakeResponse(200,page([(21,'middle',[])],has_more=True))
        http.pages[('testchan', 21)] = FakeResponse(200,page([(10,'old',[])]))
        assert await monitor.refresh('testchan') == 0
        assert monitor.sources()[0]['last_message_id'] == 10
        replacement = ChannelMonitor(monitor.engine)
        replacement.sleep = monitor.sleep
        replacement.clock = monitor.clock
        assert await replacement.refresh('testchan') == 1
        assert replacement.sources()[0]['last_message_id'] == 31
        assert generated(store) == 1
        store.close()
    asyncio.run(run())


def test_share_listing_failure_retries_pending_claim(tmp_path, monkeypatch):
    async def run():
        pages = {('testchan', None): FakeResponse(200, page([(100, 'old', [])]))}
        store, monitor, http, queued = make(tmp_path, pages,
            {'enabled': True, 'notify': False, 'channels': ['testchan']}, monkeypatch)
        await monitor.refresh('testchan')
        http.pages[('testchan', None)] = FakeResponse(200, page([(101, 'new', [SHARE])]))
        original = monitor.engine.cloud.share_items

        async def fail(secret, cid):
            raise CloudError('temporary', 'NETWORK', 500)

        monitor.engine.cloud.share_items = fail
        assert await monitor.refresh('testchan') == 0
        assert monitor.sources()[0]['last_message_id'] == 100      # 不认领，下一轮再试
        monitor.engine.cloud.share_items = original
        assert await monitor.refresh('testchan') == 1
        assert generated(store) == 1
        store.close()
    asyncio.run(run())


def test_restart_replays_claimed_strm_post_after_cursor(tmp_path, monkeypatch):
    async def run():
        pages = {('testchan', None): FakeResponse(200, page([(100, 'old', [])]))}
        store, monitor, http, queued = make(tmp_path, pages,
            {'enabled': True, 'notify': False, 'channels': ['testchan']}, monkeypatch)
        await monitor.refresh('testchan')
        with store.db:
            store.db.execute('INSERT INTO channel_posts VALUES(?,?,?)', ('testchan', 101, 1))
        http.pages[('testchan', None)] = FakeResponse(200, page([(101, 'new', [SHARE])]))
        restarted = ChannelMonitor(monitor.engine)
        restarted.sleep, restarted.clock = monitor.sleep, monitor.clock
        assert await restarted.refresh('testchan') == 1
        assert generated(store) == 1
        store.close()
    asyncio.run(run())


def test_notices_for_different_resource_types_do_not_share_event_keys(tmp_path):
    async def run():
        store, monitor, _, _ = make(tmp_path, {}, CONFIG)
        post = {'id': 88, 'text': '资源标题'}
        monitor._notify('testchan', '测试频道', post, [], 'strm', count=4)
        monitor._notify('testchan', '测试频道', post, [], 'offline', '磁力目录', count=2, resource_kind='磁力')
        monitor._notify('testchan', '测试频道', post, [], 'offline', '电驴目录', count=1, resource_kind='ED2K')
        await asyncio.gather(*list(monitor.engine.notice_tasks))
        notices = monitor.engine.notifications.sent
        assert len({args[0] for args, _ in notices}) == 3
        assert any('4 个 STRM' in args[2] and '分享 STRM 待整理' in args[2] for args, _ in notices)
        assert any('2 个链接' in args[2] and '磁力' in args[2] for args, _ in notices)
        assert any('1 个链接' in args[2] and 'ED2K' in args[2] for args, _ in notices)
        store.close()
    asyncio.run(run())


def test_offline_magnet_follows_keyword_route(tmp_path, monkeypatch):
    """磁力命中关键词分流：落盘目录用路由目录，而不是频道默认目录。"""
    async def run():
        pages = {('testchan', None): FakeResponse(200, page([(100, 'x', [])]))}
        store, monitor, http, queued = make(tmp_path, pages, {
            'enabled': True, 'notify': False,
            'channels': [{'name': 'testchan', 'mode': 'offline',
                          'target_cid': '12345', 'target_label': '默认目录',
                          'routes': [{'keywords': '4k,原盘', 'target_cid': '999',
                                      'target_label': '4K 目录'}]}]}, monkeypatch)
        await monitor.refresh('testchan')
        magnet = 'magnet:?xt=urn:btih:abcdef&dn=test'
        http.pages[('testchan', None)] = FakeResponse(200, page([(101, '4K 原盘 ' + magnet, [])]))
        assert await monitor.refresh('testchan') == 1
        calls = monitor.engine.offline.calls
        assert [call['cid'] for call in calls] == ['999']
        assert calls[0]['organize'] is False      # 频道只落盘
        store.close()
    asyncio.run(run())


def test_offline_ed2k_uses_route_too(tmp_path, monkeypatch):
    """ED2K 与磁力共用同一条关键词分流规则。"""
    async def run():
        pages = {('testchan', None): FakeResponse(200, page([(100, 'x', [])]))}
        store, monitor, http, queued = make(tmp_path, pages, {
            'enabled': True, 'notify': False,
            'channels': [{'name': 'testchan', 'mode': 'offline',
                          'target_cid': '12345', 'target_label': '默认目录',
                          'routes': [{'keywords': '纪录片', 'target_cid': '888',
                                      'target_label': '纪录片目录'}]}]}, monkeypatch)
        await monitor.refresh('testchan')
        ed2k = 'ed2k://|file|x.mkv|1000|ABCDEF|/'
        http.pages[('testchan', None)] = FakeResponse(200, page([(101, '纪录片 ' + ed2k, [])]))
        assert await monitor.refresh('testchan') == 1
        assert monitor.engine.offline.calls[0]['cid'] == '888'
        store.close()
    asyncio.run(run())


TWO_CHANNELS = {'enabled': True, 'notify': True, 'interval': 300,
                'channels': [{'name': 'chanA'}, {'name': 'chanB'}]}


def test_duplicate_channel_is_skipped_after_titles_are_known(tmp_path, monkeypatch):
    """同一个频道的两种写法只抓一个；正本被禁用时由另一条接管，不能两条都不抓。

    title 只有抓完才知道，所以首轮两条各抓一次，第二轮起才认得出重复。
    """
    async def run():
        html = page([(100, '公告', [])], title='同一个频道')
        pages = {('chanA', None): FakeResponse(200, html), ('chanB', None): FakeResponse(200, html)}
        store, monitor, http, queued = make(tmp_path, pages, TWO_CHANNELS, monkeypatch)
        assert [name for name, _ in await monitor.tick()] == ['chanA', 'chanB']
        assert dict(store.db.execute('SELECT name,duplicate_of FROM channel_state')) == {
            'chanA': None, 'chanB': 'chanA'}
        http.urls.clear()
        assert [name for name, _ in await monitor.tick()] == ['chanA']
        assert http.urls == ['https://t.me/s/chanA']
        store.set_many({'channel_monitor': {'enabled': True, 'notify': True, 'interval': 300,
                                            'channels': [{'name': 'chanA', 'enabled': False},
                                                         {'name': 'chanB'}]}})
        assert [name for name, _ in await monitor.tick()] == ['chanB']
        store.close()
    asyncio.run(run())
