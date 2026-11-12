"""Emby 反代：直链注入契约（对齐上游 2026-11-09 黑盒结论）。

上游反代与它的上游 Emby 相比，PlaybackInfo 只在 MediaSources[*] 上多出
DirectStreamUrl 一个字段；本文件锁死这条契约，防止以后把别的字段也改了。
"""
import asyncio
import json
import socket
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest

from strimkeep.cloud115 import CloudError
from strimkeep.emby_proxy import (Config, EmbyProxyManager, ProxyServer, inject_direct_links,
                                  play_identity, resolve_direct)

PLAYCODE_PATH = 'http://192.0.2.10:9000/play115/abcdefg/3556939847706871370/11d1a0a8b2/52428800/%E7%89%87.mkv'


class FakeStore:
    def __init__(self, values=None, shares=None):
        self.values = dict(values or {})
        self.events = []
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.execute('CREATE TABLE share_strms (id TEXT PRIMARY KEY, file_id TEXT, '
                        'share_code TEXT, receive_code TEXT, revoked INTEGER DEFAULT 0)')
        for row in shares or []:
            self.db.execute('INSERT INTO share_strms VALUES (?,?,?,?,?)', row)
        self.db.commit()

    def get(self, key, default=''):
        return self.values.get(key, default)

    def set_many(self, values):
        self.values.update(values)

    def forget(self, keys):
        removed = [key for key in keys if key in self.values]
        for key in removed:
            self.values.pop(key, None)
        return removed

    def event(self, kind, message):
        self.events.append((kind, message))


class FakeCloud:
    def __init__(self, link='https://cdnfhnfile.115cdn.net/x/y.mkv', error=None):
        self.link = link
        self.error = error
        self.calls = []

    async def url(self, pickcode, user_agent):
        self.calls.append(('pickcode', pickcode, user_agent))
        if self.error:
            raise self.error
        return self.link, {}

    async def share_url_download(self, file_id, share_code, receive_code, user_agent):
        self.calls.append(('share', file_id, user_agent))
        if self.error:
            raise self.error
        return self.link, {}


def test_play_identity_recognises_our_two_address_forms():
    assert play_identity(PLAYCODE_PATH) == ('pickcode', 'abcdefg')
    assert play_identity('http://192.0.2.10:9000/play115/share/8d6f5fd85f03/%E7%89%87.mkv') \
        == ('share', '8d6f5fd85f03')
    # 别人的地址、相对地址、坏地址都不碰，让客户端照常走 Emby 转发。
    assert play_identity('https://example.com/video.mkv') is None
    assert play_identity('/play115/abcdefg/x') is None
    assert play_identity('') is None


def test_inject_only_adds_direct_stream_url():
    payload = {'MediaSources': [
        {'Id': 'mediasource_1', 'Path': PLAYCODE_PATH, 'Protocol': 'Http',
         'SupportsDirectPlay': True, 'Container': 'mkv'},
        {'Id': 'mediasource_2', 'Path': 'https://example.com/other.mkv'}]}

    async def run():
        filled = await inject_direct_links(payload, 'SenPlayer/6.3.0', FakeCloud(), FakeStore())
        assert filled == 1
    asyncio.run(run())

    ours, other = payload['MediaSources']
    assert ours['DirectStreamUrl'] == 'https://cdnfhnfile.115cdn.net/x/y.mkv'
    # 其余字段一个都不许动：上游只加这一个键。
    assert ours['Path'] == PLAYCODE_PATH
    assert ours['Protocol'] == 'Http' and ours['SupportsDirectPlay'] is True
    assert ours['Container'] == 'mkv'
    assert 'DirectStreamUrl' not in other



def test_inject_keeps_response_intact_when_link_fails():
    """取链失败不能让 PlaybackInfo 变成错误：客户端还要能回落到 Emby 转发。"""
    payload = {'MediaSources': [{'Id': 'a', 'Path': PLAYCODE_PATH}]}

    async def run():
        cloud = FakeCloud(error=CloudError('取链失败', 'PLAY_FAILED'))
        assert await inject_direct_links(payload, 'Infuse/7.8', cloud, FakeStore()) == 0
    asyncio.run(run())

    assert payload['MediaSources'][0] == {'Id': 'a', 'Path': PLAYCODE_PATH}


def test_inject_skips_source_that_already_has_direct_stream_url():
    payload = {'MediaSources': [{'Path': PLAYCODE_PATH,
                                 'DirectStreamUrl': 'https://keep.example/v.mkv'}]}

    async def run():
        cloud = FakeCloud()
        assert await inject_direct_links(payload, 'Infuse/7.8', cloud, FakeStore()) == 0
        assert cloud.calls == []
    asyncio.run(run())

    assert payload['MediaSources'][0]['DirectStreamUrl'] == 'https://keep.example/v.mkv'


def test_share_identity_resolves_through_share_strms_row():
    shares = [('8d6f5fd85f03', '35569398', 'swhnk1j3zh9', 'sae6', 0)]
    payload = {'MediaSources': [{'Path': 'http://192.0.2.10:9000/play115/share/'
                                          '8d6f5fd85f03/%E7%89%87.mkv'}]}

    async def run():
        cloud = FakeCloud()
        store = FakeStore(shares=shares)
        assert await inject_direct_links(payload, 'SenPlayer/6.3.0', cloud, store) == 1
        assert cloud.calls == [('share', '35569398', 'SenPlayer/6.3.0')]
    asyncio.run(run())


def test_revoked_share_gets_no_link():
    shares = [('8d6f5fd85f03', '35569398', 'swhnk1j3zh9', 'sae6', 1)]
    payload = {'MediaSources': [{'Path': 'http://192.0.2.10:9000/play115/share/8d6f5fd85f03/x.mkv'}]}

    async def run():
        cloud = FakeCloud()
        assert await inject_direct_links(payload, 'SenPlayer/6.3.0', cloud, FakeStore(shares=shares)) == 0
        assert cloud.calls == []
    asyncio.run(run())


def test_config_reuses_the_emby_service_connection(monkeypatch):
    # 上游地址与 API Key 沿用「Emby 服务连接」，不再单独配置；端口取默认值。
    config = Config(FakeStore({'emby_url': 'http://127.0.0.1:8100/', 'emby_key': 'k-123'}))
    assert config.ready is True and config.upstream == 'http://127.0.0.1:8100'
    assert config.api_key == 'k-123' and config.port == 9001

    # 规则字段（页面里能改的那份）仍然优先于服务连接。
    config = Config(FakeStore({'emby_url': 'http://a:1', 'emby_key': 'k1'}),
                    {'id': 'r9', 'upstream': 'http://b:2', 'api_key': 'k2', 'port': 9300})
    assert (config.upstream, config.api_key, config.port) == ('http://b:2', 'k2', 9300)

    # 老的 emby_proxy_* 键**不再**被读取：它们只在迁移时用一次，随后删除。
    config = Config(FakeStore({'emby_url': 'http://a:1', 'emby_key': 'k1',
                               'emby_proxy_upstream': 'http://stale:9',
                               'emby_proxy_api_key': 'stale-key',
                               'emby_proxy_port': 9301}))
    assert (config.upstream, config.api_key, config.port) == ('http://a:1', 'k1', 9001)

    # 环境变量优先于一切，开关也认 on/1/true。
    monkeypatch.setenv('STRIMKEEP_EMBY_PROXY', 'on')
    monkeypatch.setenv('STRIMKEEP_EMBY_UPSTREAM', 'http://10.0.0.2:8096')
    monkeypatch.setenv('STRIMKEEP_EMBY_PROXY_PORT', '9100')
    config = Config(FakeStore({'emby_url': 'http://127.0.0.1:8100', 'emby_proxy_enabled': False}))
    assert (config.upstream, config.port, config.ready) == ('http://10.0.0.2:8096', 9100, True)

    # Emby 服务连接也没配：不监听端口。
    monkeypatch.delenv('STRIMKEEP_EMBY_UPSTREAM')
    monkeypatch.delenv('STRIMKEEP_EMBY_PROXY')
    assert Config(FakeStore()).ready is False



def test_restart_moves_listener_to_the_new_port():
    """端口任意改：保存后旧端口立刻关闭，新端口开始监听（不需要重启容器）。"""
    rule = {'id': 'r1', 'name': 'Emby-1', 'upstream': 'http://127.0.0.1:9',
            'api_key': '', 'port': 19001, 'enabled': True}
    store = FakeStore({'emby_url': 'http://127.0.0.1:9'})
    server = ProxyServer(rule, store, FakeCloud())

    def listening(port):
        try:
            socket.create_connection(('127.0.0.1', port), timeout=1).close()
            return True
        except OSError:
            return False

    async def run():
        assert await server.start() is not None and listening(19001)
        server.rule = {**rule, 'port': 19002}
        assert await server.restart() is not None
        assert server.config().port == 19002
        assert listening(19002) and not listening(19001)
        await server.stop()
        assert not listening(19002)
    asyncio.run(run())



def test_post_body_reaches_upstream_with_content_length():
    """实测教训：POST 体按分块转发时 Emby 的 AuthenticateByName 会 400，必须带上长度。"""
    seen = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get('content-length') or 0)
            seen['length'] = self.headers.get('content-length')
            seen['body'] = self.rfile.read(length)
            payload = json.dumps({'ok': True}).encode()
            self.send_response(200)
            self.send_header('content-type', 'application/json')
            self.send_header('content-length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    upstream = HTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    rule = {'id': 'r1', 'name': 'Emby-1',
            'upstream': 'http://127.0.0.1:%d' % upstream.server_address[1],
            'api_key': '', 'port': 19003, 'enabled': True}
    store = FakeStore({'emby_url': 'http://127.0.0.1:9'})
    server = ProxyServer(rule, store, FakeCloud())

    async def run():
        assert await server.start() is not None
        async with httpx.AsyncClient() as client:
            response = await client.post('http://127.0.0.1:19003/emby/Users/AuthenticateByName',
                                         json={'Username': 'x', 'Pw': 'y'})
            assert response.status_code == 200
        await server.stop()

    try:
        asyncio.run(run())
    finally:
        upstream.shutdown()
    assert seen['body'] == b'{"Username":"x","Pw":"y"}'
    assert seen['length'] == str(len(seen['body']))



def test_manager_migrates_legacy_single_config_into_one_rule():
    """老库只有单台配置：迁移成一条规则，然后把这些老键删掉（不再有读取方）。"""
    store = FakeStore({'emby_url': 'http://192.0.2.10:8100', 'emby_key': 'k',
                       'emby_proxy_enabled': True, 'emby_proxy_port': 9005,
                       'emby_proxy_upstream': 'http://192.0.2.10:8100',  # 与服务连接相同 → 不带过来
                       'emby_proxy_api_key': 'other-key'})                 # 与之不同 → 带过来
    manager = EmbyProxyManager(store, FakeCloud())
    rules = manager.rules()
    assert len(rules) == 1
    assert rules[0]['port'] == 9005 and rules[0]['enabled'] is True
    assert rules[0]['upstream'] == '' and rules[0]['api_key'] == 'other-key'
    # 迁移完，老键就没了：库里不会再有"改了没效果"的幽灵配置
    for key in ('emby_proxy_enabled', 'emby_proxy_port',
                'emby_proxy_upstream', 'emby_proxy_api_key'):
        assert key not in store.values
    assert any('已删除旧版单台反代遗留设置项' in message for _, message in store.events)
    # 迁移结果落库，重复读取不会再建一条
    assert manager.rules()[0]['id'] == rules[0]['id']
    status = manager.status()[0]
    assert status['upstream'] == 'http://192.0.2.10:8100'
    assert status['api_key_configured'] is True and status['running'] is False


def test_legacy_keys_are_cleaned_even_when_rules_already_exist():
    """规则已经在库里（本项目现场就是这种）：老键没有读取方，必须照样清掉。"""
    store = FakeStore({'emby_url': 'http://a:1', 'emby_key': 'k',
                       'emby_proxy_port': 9001,
                       'emby_proxy_servers': [{'id': 'r1', 'name': 'Emby-1', 'upstream': '',
                                               'api_key': '', 'port': 9001, 'enabled': True}]})
    manager = EmbyProxyManager(store, FakeCloud())
    rules = manager.rules()
    assert len(rules) == 1 and rules[0]['id'] == 'r1' and rules[0]['port'] == 9001
    assert 'emby_proxy_port' not in store.values
    assert rules[0]['enabled'] is True                    # 原有规则不受影响
    assert manager.rules()[0]['id'] == 'r1'               # 幂等，不重建规则


def test_manager_rule_level_config_overrides_service_connection():
    store = FakeStore({'emby_url': 'http://192.0.2.10:8100', 'emby_key': 'k1'})
    manager = EmbyProxyManager(store, FakeCloud())
    rule = manager.add_rule()
    assert rule['enabled'] is False          # 新增默认不开启，和上游一致
    assert Config(store, rule).upstream == 'http://192.0.2.10:8100'
    rules = manager.rules()
    for item in rules:
        if item['id'] == rule['id']:
            item.update({'upstream': 'http://10.0.0.9:8096', 'api_key': 'k2',
                         'port': 9010, 'enabled': True})
    manager.save_rules(rules)
    config = Config(store, manager.find(rule['id']))
    assert (config.upstream, config.api_key, config.port, config.ready) == \
        ('http://10.0.0.9:8096', 'k2', 9010, True)



def test_port_conflict_only_fails_that_one_server():
    """端口被占用时只能这一台起不来：uvicorn 的 SystemExit 以前会把整个事件循环带崩。"""
    rule = {'id': 'r1', 'name': 'Emby-1', 'upstream': 'http://127.0.0.1:9',
            'api_key': '', 'port': 19004, 'enabled': True}
    store = FakeStore({'emby_url': 'http://127.0.0.1:9'})
    first = ProxyServer(dict(rule), store, FakeCloud())
    second = ProxyServer({**rule, 'id': 'r2', 'name': 'Emby-2'}, store, FakeCloud())

    async def run():
        assert await first.start() is not None
        assert await second.start() is None          # 这一台失败，但不影响别人
        await asyncio.sleep(0.1)                     # 事件循环还活着才会走到这里
        assert first.running()
        socket.create_connection(('127.0.0.1', 19004), timeout=1).close()
        await second.stop()
        await first.stop()

    asyncio.run(run())



def test_proxy_never_substitutes_admin_credentials():
    """所有请求都必须使用客户端自己的身份；匿名请求不能被提升为管理员。"""
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(('GET', self.path, self.headers.get('x-emby-token')))
            payload = json.dumps({'Items': [], 'TotalRecordCount': 0}).encode()
            self.send_response(200)
            self.send_header('content-type', 'application/json')
            self.send_header('content-length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self):
            length = int(self.headers.get('content-length') or 0)
            self.rfile.read(length)
            seen.append(('POST', self.path, self.headers.get('x-emby-token')))
            payload = json.dumps({'MediaSources': []}).encode()
            self.send_response(200)
            self.send_header('content-type', 'application/json')
            self.send_header('content-length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    upstream = HTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    rule = {'id': 'r1', 'name': 'Emby-1',
            'upstream': 'http://127.0.0.1:%d' % upstream.server_address[1],
            'api_key': 'ADMIN-KEY', 'port': 19005, 'enabled': True}
    store = FakeStore({'emby_url': 'http://127.0.0.1:9'})
    server = ProxyServer(rule, store, FakeCloud())

    async def run():
        assert await server.start() is not None
        async with httpx.AsyncClient() as client:
            await client.get('http://127.0.0.1:19005/emby/Items?Recursive=true')
            await client.post('http://127.0.0.1:19005/emby/Items/1/PlaybackInfo',
                              json={'EnableDirectPlay': True})
            await client.post('http://127.0.0.1:19005/emby/Items/1/PlaybackInfo?api_key=USER-TOKEN',
                              headers={'X-Emby-Token':'USER-TOKEN'}, json={})
        await server.stop()

    try:
        asyncio.run(run())
    finally:
        upstream.shutdown()

    # 无论普通列表还是 PlaybackInfo，都不能给匿名请求补管理员 Key。
    assert seen[0][0] == 'GET' and seen[0][2] is None
    assert 'api_key=' not in seen[0][1]
    assert seen[1][0] == 'POST' and seen[1][2] is None
    assert 'api_key=' not in seen[1][1]
    assert seen[2][2] == 'USER-TOKEN' and 'api_key=USER-TOKEN' in seen[2][1]
    assert 'ADMIN-KEY' not in seen[2][1]



class FlakyCloud(FakeCloud):
    """前 N 次分享取链失败，之后成功：模拟 115 的瞬时错误。"""

    def __init__(self, fail_times):
        super().__init__()
        self.fail_times = fail_times

    async def share_url_download(self, *args):
        if self.fail_times:
            self.fail_times -= 1
            raise CloudError('busy')
        return self.link, {}


def test_resolve_direct_says_why_it_failed():
    """失败原因要能区分：没有身份 / 分享记录缺失 / 已撤销。"""
    store = FakeStore(shares=[('sid1', 'f1', 'code', '', 1)])
    assert asyncio.run(resolve_direct(('share', 'sid1'), 'ua', FakeCloud(), store)) == \
        (None, 'share_revoked')
    assert asyncio.run(resolve_direct(('share', 'nope'), 'ua', FakeCloud(), store)) == \
        (None, 'share_missing')
    assert asyncio.run(resolve_direct(None, 'ua', FakeCloud(), store)) == (None, 'no_identity')


def test_transient_cloud_error_is_retried_once():
    """115 抖一下不该让播放器退回走隧道（4 MB/s 对 20 MB/s）。"""
    store = FakeStore(shares=[('sid2', 'f2', 'code', '', 0)])
    url, reason = asyncio.run(resolve_direct(('share', 'sid2'), 'ua', FlakyCloud(1), store))
    assert url and reason == ''

    url, reason = asyncio.run(resolve_direct(('share', 'sid2'), 'ua', FlakyCloud(5), store))
    assert url is None and reason.startswith('cloud:')


def test_inject_detail_reports_reasons():
    payload = {'MediaSources': [{'Path': 'http://127.0.0.1:9000/play115/share/sid1'}]}
    store = FakeStore(shares=[('sid1', 'f1', 'code', '', 1)])
    filled, reasons = asyncio.run(
        inject_direct_links(payload, 'ua', FakeCloud(), store, detail=True))
    assert filled == 0 and reasons == ['share_revoked']
    assert 'DirectStreamUrl' not in payload['MediaSources'][0]


def test_play_record_keeps_reason_and_migrates_old_table():
    """老库没有 reason 列：启动时补列，写入能带原因。"""
    store = FakeStore({'emby_url': 'http://127.0.0.1:9'})
    store.db.execute('CREATE TABLE emby_proxy_plays (id INTEGER PRIMARY KEY AUTOINCREMENT, '
                     'rule_id TEXT, name TEXT, port INTEGER, target TEXT, user_agent TEXT, '
                     'filled INTEGER, outcome TEXT, at REAL)')
    store.db.commit()
    manager = EmbyProxyManager(store, FakeCloud())
    manager.ensure_table()
    columns = {row[1] for row in store.db.execute('PRAGMA table_info(emby_proxy_plays)')}
    assert 'reason' in columns

    class Request:
        class url:                       # noqa: N801 - 模拟 Starlette Request
            path = '/emby/Items/1/PlaybackInfo'
        headers = {'user-agent': 'SenPlayer/6.3.0'}

    rule = {'id': 'r1', 'name': 'Emb', 'upstream': '', 'api_key': '', 'port': 9001,
            'enabled': True}
    manager.record_play(ProxyServer(rule, store, FakeCloud()), Request(), 0, 'error',
                        'cloud:CloudError')
    rows = manager.plays('r1')
    assert rows[0]['outcome'] == 'error' and rows[0]['reason'] == 'cloud:CloudError'



def test_multi_source_resolution_is_concurrent_and_deduplicated():
    class SlowCloud(FakeCloud):
        async def url(self, pickcode, user_agent):
            self.calls.append(pickcode)
            await asyncio.sleep(.12)
            return 'https://cdn.example/'+pickcode, {}
    async def run():
        import time
        cloud = SlowCloud(); store = FakeStore()
        payload = {'MediaSources':[{'Path':'http://localhost/play115/'+p+'/x'} for p in ('a','b','a','c')]}
        start = time.monotonic()
        count = await inject_direct_links(payload,'SenPlayer',cloud,store)
        assert count == 4 and sorted(cloud.calls) == ['a','b','c']
        assert time.monotonic()-start < .32
    asyncio.run(run())


def test_resolution_deadline_returns_partial_results_without_hanging(monkeypatch):
    import strimkeep.emby_proxy as proxy
    monkeypatch.setattr(proxy, 'RESOLVE_TOTAL_TIMEOUT', .08)
    class MixedCloud(FakeCloud):
        async def url(self, pickcode, user_agent):
            await asyncio.sleep(.01 if pickcode == 'fast' else 1)
            return 'https://cdn.example/'+pickcode, {}
    async def run():
        payload = {'MediaSources':[{'Path':'http://localhost/play115/fast/x'}, {'Path':'http://localhost/play115/slow/x'}]}
        count,reasons = await inject_direct_links(payload,'SenPlayer',MixedCloud(),FakeStore(),detail=True)
        assert count == 1 and reasons == ['timeout']
        assert payload['MediaSources'][0]['DirectStreamUrl'].endswith('/fast')
        assert 'DirectStreamUrl' not in payload['MediaSources'][1]
    asyncio.run(run())


def test_fatal_cloud_error_is_not_retried():
    class RateLimited(FakeCloud):
        async def url(self, *args):
            self.calls.append(args)
            raise CloudError('too many requests','RATE_LIMITED',503)
    cloud = RateLimited()
    result = asyncio.run(resolve_direct(('pickcode','p'),'ua',cloud,FakeStore()))
    assert result == (None,'cloud:RATE_LIMITED') and len(cloud.calls) == 1


def test_revoked_share_is_checked_even_after_a_successful_lookup():
    store = FakeStore(shares=[('sid1','f1','code','',0)]); cloud=FakeCloud()
    async def run():
        assert (await resolve_direct(('share','sid1'),'ua',cloud,store))[0]
        store.db.execute("update share_strms set revoked=1 where id='sid1'")
        assert await resolve_direct(('share','sid1'),'ua',cloud,store) == (None,'share_revoked')
        assert len(cloud.calls) == 1
    asyncio.run(run())


def test_empty_source_list_is_reported_as_no_media_source():
    result = asyncio.run(inject_direct_links({'MediaSources':[]},'ua',FakeCloud(),FakeStore(),detail=True))
    assert result == (0,['no_sources'])


def test_get_playbackinfo_is_injected_with_client_authentication():
    async def run():
        seen = []
        class Stream(httpx.AsyncByteStream):
            def __init__(self, data): self.data = data
            async def __aiter__(self): yield json.dumps(self.data).encode()
        def upstream(request):
            seen.append((request.headers.get('x-emby-token'), str(request.url)))
            if request.headers.get('x-emby-token') != 'user-token':
                return httpx.Response(401, stream=Stream({'error':'unauthorized'}), headers={'content-type':'application/json'})
            return httpx.Response(200, stream=Stream({'MediaSources':[{'Path':PLAYCODE_PATH}]}), headers={'content-type':'application/json'})
        rule = {'id':'r1','upstream':'http://emby','api_key':'ADMIN-KEY','port':9001,'enabled':True}
        server = ProxyServer(rule, FakeStore(), FakeCloud())
        server.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        app = server.build()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://proxy') as client:
            anonymous = await client.get('/emby/Items/123/PlaybackInfo')
            assert anonymous.status_code == 401
            result = await client.get('/emby/Items/123/PlaybackInfo', headers={'X-Emby-Token':'user-token','User-Agent':'SenPlayer'})
            assert result.status_code == 200
            assert result.json()['MediaSources'][0]['DirectStreamUrl'].startswith('https://cdn')
            assert result.headers['cache-control'] == 'private, no-store'
        await server.client.aclose()
        assert seen[0][0] is None and seen[1][0] == 'user-token'
        assert all('ADMIN-KEY' not in url for _,url in seen)
    asyncio.run(run())


def test_websocket_forwards_auth_and_bidirectional_messages():
    from websockets.asyncio.server import serve
    from websockets.asyncio.client import connect
    from websockets.exceptions import InvalidStatus
    async def run():
        seen = []
        async def auth(connection, request):
            seen.append((request.path, request.headers.get('x-emby-token'), request.headers.get('origin')))
            if request.headers.get('x-emby-token') != 'user-token' and 'api_key=user-token' not in request.path:
                return connection.respond(401, 'unauthorized')
        async def echo(ws):
            async for message in ws:
                await ws.send(message)
        async with serve(echo,'127.0.0.1',0,process_request=auth,subprotocols=['emby-test']) as upstream:
            upstream_port = upstream.sockets[0].getsockname()[1]
            with socket.socket() as temp:
                temp.bind(('127.0.0.1',0)); listen_port=temp.getsockname()[1]
            rule = {'id':'ws','name':'ws','upstream':'http://127.0.0.1:%s' % upstream_port,
                    'api_key':'ADMIN-KEY','port':listen_port,'enabled':True}
            proxy = ProxyServer(rule,FakeStore(),FakeCloud())
            assert await proxy.start() is not None
            try:
                async with connect('ws://127.0.0.1:%s/embywebsocket?DeviceId=abc' % listen_port,
                                   additional_headers={'X-Emby-Token':'user-token'},
                                   origin='https://client.example', subprotocols=['emby-test'], proxy=None) as client:
                    assert client.subprotocol == 'emby-test'
                    await client.send('play-state')
                    assert await asyncio.wait_for(client.recv(),2) == 'play-state'
                    await client.send(b'\x01\x02')
                    assert await asyncio.wait_for(client.recv(),2) == b'\x01\x02'
                assert seen[0] == ('/embywebsocket?DeviceId=abc','user-token','https://client.example')
                async with connect('ws://127.0.0.1:%s/embywebsocket?api_key=user-token' % listen_port, subprotocols=['emby-test'], proxy=None) as client:
                    await client.send('query-auth')
                    assert await asyncio.wait_for(client.recv(),2) == 'query-auth'
                assert seen[1][0] == '/embywebsocket?api_key=user-token' and seen[1][1] is None
                with pytest.raises(InvalidStatus):
                    async with connect('ws://127.0.0.1:%s/embywebsocket' % listen_port, proxy=None): pass
                assert seen[2][1] is None and 'ADMIN-KEY' not in seen[2][0]
                with pytest.raises(InvalidStatus):
                    async with connect('ws://127.0.0.1:%s/unrelated' % listen_port, proxy=None): pass
            finally:
                await proxy.stop()
    asyncio.run(run())


def test_video_stream_is_redirected_to_cdn_instead_of_relayed():
    """播放器请求 stream.mkv 时，反代应 302 到 115 直链，而不是替它搬字节（走 frp）。"""
    async def run():
        seen = []

        def upstream(request):
            seen.append(str(request.url))
            if request.url.path == '/emby/Items/123/PlaybackInfo':
                return httpx.Response(200, json={'MediaSources': [{'Path': PLAYCODE_PATH}]})
            return httpx.Response(206, content=b'x' * 8)

        rule = {'id': 'r1', 'upstream': 'http://emby', 'api_key': 'ADMIN-KEY',
                'port': 9001, 'enabled': True}
        server = ProxyServer(rule, FakeStore(), FakeCloud())
        server.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        app = server.build()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url='http://proxy') as client:
            first = await client.get('/emby/videos/123/stream.mkv',
                                     params={'static': 'true', 'api_key': 'user-token'},
                                     headers={'User-Agent': 'SenPlayer'}, follow_redirects=False)
            assert first.status_code == 302
            assert first.headers['location'].startswith('https://cdn')
            # 播放器每几秒发一次 Range：每次都应是 302，且不再回源问 Path
            again = await client.get('/emby/videos/123/stream.mkv',
                                     headers={'User-Agent': 'SenPlayer',
                                              'Range': 'bytes=1048576-'},
                                     follow_redirects=False)
            assert again.status_code == 302
        assert sum(1 for url in seen if '/emby/Items/123' in url) == 1
        await server.client.aclose()
    asyncio.run(run())


def test_video_stream_falls_back_when_path_is_not_115():
    """不是 115 的文件（本地/其它来源）不应被 302：交给原来的转发逻辑。"""
    from types import SimpleNamespace

    class FakeRequest:
        method = 'GET'
        headers = {'user-agent': 'SenPlayer'}
        query_params = {}

        def __init__(self, path):
            self.url = SimpleNamespace(path=path, query='')

    async def run():
        def upstream(request):
            assert request.url.path == '/emby/Items/9/PlaybackInfo'
            return httpx.Response(200, json={'MediaSources': [{'Path': '/media/local/movie.mkv'}]})

        rule = {'id': 'r1', 'upstream': 'http://emby', 'api_key': '', 'port': 9001,
                'enabled': True}
        server = ProxyServer(rule, FakeStore(), FakeCloud())
        server.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        assert await server._stream_redirect(FakeRequest('/emby/videos/9/stream.mkv'),
                                             server.config()) is None
        await server.client.aclose()
    asyncio.run(run())


def test_video_stream_refuses_to_relay_through_tunnel_when_link_fails():
    from types import SimpleNamespace

    class FakeRequest:
        method = 'GET'
        headers = {'user-agent': 'SenPlayer'}
        query_params = {}

        def __init__(self, path):
            self.url = SimpleNamespace(path=path, query='')

    async def run():
        def upstream(request):
            assert request.url.path == '/emby/Items/5/PlaybackInfo'
            return httpx.Response(200, json={'MediaSources': [{'Path': PLAYCODE_PATH}]})

        rule = {'id': 'r1', 'upstream': 'http://emby', 'api_key': '', 'port': 9001,
                'enabled': True}
        server = ProxyServer(rule, FakeStore(), FakeCloud(link=None))
        server.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        response = await server._stream_redirect(FakeRequest('/emby/videos/5/stream.mkv'),
                                                 server.config())
        # 根因收口：我们的片取不到直链时必须 503，绝不回退到"经 frp 转发视频"。
        assert response is not None and response.status_code == 503
        await server.client.aclose()
    asyncio.run(run())


def test_video_stream_reads_strm_content_to_build_the_redirect(monkeypatch, tmp_path):
    """Emby 的 Path 是 .strm 文件；真正的 play115 地址在文件内容里，必须读出来才能 302。"""
    import os
    from types import SimpleNamespace

    relative = '电影/测试 (2026) {tmdb-1}/测试.strm'
    target = tmp_path / relative
    target.parent.mkdir(parents=True)
    target.write_text(PLAYCODE_PATH + '\n', encoding='utf-8')
    monkeypatch.setenv('STRIMKEEP_STRM_DIR', str(tmp_path))

    store = FakeStore({'console_config': {'emby_strm_path': '/media'}})

    class FakeRequest:
        method = 'GET'
        headers = {'user-agent': 'SenPlayer'}
        query_params = {}

        def __init__(self, path):
            self.url = SimpleNamespace(path=path, query='')

    async def run():
        def upstream(request):
            assert request.url.path == '/emby/Items/77/PlaybackInfo'
            return httpx.Response(200, json={'MediaSources': [{'Path': '/media/' + relative}]})

        rule = {'id': 'r1', 'upstream': 'http://emby', 'api_key': '', 'port': 9001,
                'enabled': True}
        server = ProxyServer(rule, store, FakeCloud())
        server.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        response = await server._stream_redirect(FakeRequest('/emby/videos/77/stream.mkv'),
                                                 server.config())
        assert response is not None and response.status_code == 302
        assert response.headers['location'].startswith('https://cdn')
        await server.client.aclose()
    asyncio.run(run())
