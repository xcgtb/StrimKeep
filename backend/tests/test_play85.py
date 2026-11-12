"""播放链路 0.8.5：直链缓存分层、并发合并、UA 日志标签。"""
import asyncio
import time

from strimkeep.cloud115 import Cloud115, _probe_user_agent, playback_request
from strimkeep.playaddr import ua_label
from strimkeep.store import Store


class FakeStore:
    def get(self, key, default=''):
        return 'UID=1_A1; CID=2; SEID=3'


def engine():
    cloud = Cloud115.__new__(Cloud115)
    cloud.store = FakeStore()
    cloud.play_urls = {}
    cloud.play_locks = {}
    cloud.play_hits = 0
    cloud.play_misses = 0
    cloud.calls = []

    async def download(pickcode, user_agent):
        cloud.calls.append((pickcode, user_agent))
        await asyncio.sleep(0.05)
        return 'https://cdn.example.com/v.mkv?t=1793839752'

    cloud._download_url = download
    return cloud


def test_probe_user_agent_recognizes_media_servers():
    """Emby/Plex/Jellyfin 的探测 UA 必须被识别，否则扫描放大照旧。"""
    assert _probe_user_agent('Lavf/57.28.100')
    assert _probe_user_agent('ffprobe version 5.1')
    assert _probe_user_agent('Emby/4.8.0.0')
    assert _probe_user_agent('PlexMediaServer/1.40')
    assert not _probe_user_agent('Infuse/7.8')
    assert not _probe_user_agent('Kodi/21.0')
    assert not _probe_user_agent('VLC/3.0.20 LibVLC/3.0.20')
    assert not _probe_user_agent('')


def test_ua_label_keeps_logs_readable():
    assert ua_label('') == '无 UA'
    assert ua_label('Emby/4.8.0.0') == 'Emby/4.8.0.0'
    long_ua = 'x' * 100
    assert len(ua_label(long_ua)) == 33
    assert ua_label(long_ua).endswith('…')


def test_concurrent_requests_for_same_pickcode_hit_115_once():
    """Emby 扫一季时探测与播放会同时撞上来，必须合并成一次取链。"""
    async def run():
        cloud = engine()
        token = playback_request.set(True)
        try:
            results = await asyncio.gather(
                *[cloud.url('pick-1', 'Emby/4.8.0.0') for _ in range(5)])
        finally:
            playback_request.reset(token)
        assert len(cloud.calls) == 1, cloud.calls
        assert {item[0] for item in results} == {'https://cdn.example.com/v.mkv?t=1793839752'}
    asyncio.run(run())



def test_cached_link_is_reused_before_calling_115_again():
    async def run():
        cloud = engine()
        token = playback_request.set(True)
        try:
            await cloud.url('pc-1', 'VLC/3.0.20')
            await cloud.url('pc-1', 'VLC/3.0.20')
        finally:
            playback_request.reset(token)
        assert len(cloud.calls) == 1
    asyncio.run(run())



def playback_cloud(tmp_path, delay=.2):
    """真 __init__ 的 Cloud115：让 call()/play_gate 真的参与，只把 115 SDK 换成假的。

    本文件其他用例直接替换 `_download_url`，绕过了 call() 里的闸门；而串行化恰恰
    发生在闸门里，所以要验证「同时取链」必须走真调用链。
    """
    store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
    store.set_many({'cookie': 'UID=1; CID=t; SEID=t'})
    cloud = Cloud115(store)
    stats = {'calls': [], 'peak': 0, 'in_flight': 0}

    class Stub:
        async def download_url(self, pickcode, **kwargs):
            stats['calls'].append(pickcode)
            stats['in_flight'] += 1
            stats['peak'] = max(stats['peak'], stats['in_flight'])
            try:
                await asyncio.sleep(delay)
            finally:
                stats['in_flight'] -= 1
            return 'https://cdn.example.com/%s?v=1' % pickcode

    cloud.client = lambda: Stub()
    return store, cloud, stats


def test_play_links_for_different_files_are_not_serialized(tmp_path):
    """0.9.57：play_gate 曾是全局 Lock，播放器开播的并发请求只能一个个排队。

    现场实测：单个 302 只要 0.21 秒，但 6 个并发要 1.37 秒（≈ 6 ×（50ms 间隔 +
    单次往返））。Emby 开播时探测、正片、断线重连会同时撞上来，观感就是「加载慢」。
    """
    async def run():
        store, cloud, stats = playback_cloud(tmp_path, delay=.2)
        assert isinstance(cloud.play_gate, asyncio.Semaphore)
        token = playback_request.set(True)
        try:
            started = time.monotonic()
            await asyncio.gather(*[cloud.url('pc-%d' % i, 'Infuse/7.8') for i in range(4)])
            elapsed = time.monotonic() - started
        finally:
            playback_request.reset(token)
            await cloud.http.aclose()
            await cloud.play_http.aclose()
            store.close()
        assert stats['peak'] >= 2, stats        # 确实同时在飞，不是排完一个再一个
        assert elapsed < .6, elapsed            # 串行要 4 × (.2 + .05) ≈ 1.0 秒
    asyncio.run(run())


def test_play_slots_still_keep_the_minimum_gap(tmp_path):
    """并发放开后 50ms 间隔不能跟着失效：它是 115 风控的唯一闸门。"""
    async def run():
        store, cloud, _ = playback_cloud(tmp_path, delay=0)
        cloud.play_next_request = 0.0
        await cloud.reserve_play_slot()          # 第一个不排队
        first = cloud.play_next_request
        started = time.monotonic()
        await cloud.reserve_play_slot()          # 第二个必须等满一个间隔
        waited = time.monotonic() - started
        await cloud.http.aclose()
        await cloud.play_http.aclose()
        store.close()
        assert first > 0
        assert cloud.play_next_request > first
        assert waited >= Cloud115.PLAY_SPACING * .8, waited
    asyncio.run(run())


def test_link_cache_ttl_comes_from_the_link_itself():
    """0.9.59：缓存时长按直链里的 `t`（过期时间戳）算，不再写死 15/60 秒。

    现场实测 115 返回的直链有效期是 92~297 小时；以前固定 15 秒，等于把一个能用
    四天的地址每 15 秒重取一次。
    """
    async def run():
        cloud = engine()
        stamp = int(time.time())
        links = {
            'far': 'https://cdn.example/a.mkv?t=%d' % (stamp + 10 * 3600),   # 远超上限
            'near': 'https://cdn.example/b.mkv?t=%d' % (stamp + 900),        # 15 分钟
            'none': 'https://cdn.example/c.mkv',                             # 读不到 t
        }

        async def download(pickcode, user_agent):
            cloud.calls.append((pickcode, user_agent))
            return links[pickcode]

        cloud._download_url = download
        token = playback_request.set(True)
        try:
            for name in links:
                await cloud.url(name, 'VLC/3.0.20')
        finally:
            playback_request.reset(token)
        now = time.monotonic()
        expiry = {key[1]: value[0] - now for key, value in cloud.play_urls.items()}
        assert Cloud115.PLAY_LINK_TTL_CAP - 5 < expiry['far'] <= Cloud115.PLAY_LINK_TTL_CAP + 1
        assert 580 < expiry['near'] <= 600          # 900 - 300 安全边距
        assert 14 < expiry['none'] <= 16            # 退回保守的 15 秒
        assert cloud.play_misses == 3 and cloud.play_hits == 0
    asyncio.run(run())


def test_link_cache_is_isolated_by_user_agent():
    """0.9.61：直链按 UA 分开缓存 —— 115 的 CDN 地址绑定签发时的 UA。

    0.9.59 一度只按「账号 + 文件」缓存，依据是 115driver 对**分享**下载的注释。
    现场证伪：同一条链接用 Infuse/7.8 取 206，用 Emby/4.8、Lavf、空 UA 取全是 403；
    后果是 Emby 探测（Lavf）拿到的链接被 Emby 播放复用 → CDN 403 → Emby 回 500。
    """
    async def run():
        cloud = engine()
        token = playback_request.set(True)
        try:
            probe = await cloud.url('same-file', 'Lavf/57.28.100')
            play = await cloud.url('same-file', 'Infuse/7.8')
            again = await cloud.url('same-file', 'Infuse/7.8')
        finally:
            playback_request.reset(token)
        assert [call[1] for call in cloud.calls] == ['Lavf/57.28.100', 'Infuse/7.8']
        assert probe and play                                # 两个 UA 各取一次链
        assert again[0] == play[0]                           # 同一 UA 仍然复用
        assert cloud.play_hits == 1 and cloud.play_misses == 2
    asyncio.run(run())


def test_links_without_expiry_fall_back_to_the_old_short_ttl():
    """读不到 `t` 就退回 15 秒：宁可多取一次，也不要递出一条看不懂的链接。"""
    assert Cloud115.link_expiry('https://cdn.example/a.mkv') == 0
    assert Cloud115.link_expiry('https://cdn.example/a.mkv?t=abc') == 0
    assert Cloud115.link_expiry('https://cdn.example/a.mkv?t=1794590257') == 1794590257
    assert Cloud115.link_expiry('https://cdn.example/a.mkv?t=1794590257000') == 1794590257


def test_share_playback_cache_is_bounded_and_isolated_by_identity_and_ua():
    async def run():
        cloud = engine()
        async def download(file_id, code, receive, ua):
            cloud.calls.append((file_id,code,receive,ua))
            await asyncio.sleep(.03)
            return 'https://cdn.example/v?t=%d' % int(time.time()+3600), {'test':'header'}
        cloud._share_download_url = download
        token = playback_request.set(True)
        try:
            values = await asyncio.gather(*[cloud.share_url_download('1','share','pwd','SenPlayer') for _ in range(4)])
            assert len(cloud.calls) == 1 and all(x == values[0] for x in values)
            await cloud.share_url_download('1','share','pwd','Infuse')
            await cloud.share_url_download('1','share','other','SenPlayer')
            await cloud.share_url_download('2','share','pwd','SenPlayer')
            assert len(cloud.calls) == 4
            assert all(0 < entry[0] - time.monotonic() <= 60 for entry in cloud.play_urls.values())
        finally:
            playback_request.reset(token)
    asyncio.run(run())


def test_share_non_playback_does_not_reuse_playback_cache():
    async def run():
        cloud = engine()
        async def download(*args):
            cloud.calls.append(args)
            return 'https://cdn.example/v', {}
        cloud._share_download_url = download
        await cloud.share_url_download('1','s','','ua')
        await cloud.share_url_download('1','s','','ua')
        assert len(cloud.calls) == 2 and not cloud.play_urls
    asyncio.run(run())
