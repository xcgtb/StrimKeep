"""115 的 405（webapi WAF）必须按「整段封锁」处理，而不是逐请求阶梯冷却。

背景：405 是阿里云 WAF 按时间窗的拦截，不是 429。阶梯冷却对它无效，
每次重试只会把窗口往后推——19:55 被拦到 21:04 就是这么来的。
"""
import asyncio

import httpx
import pytest

from strimkeep.cloud115 import Cloud115, CloudError, playback_request
from strimkeep.store import Store


def make(tmp_path):
    store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
    store.set_many({'cookie': 'UID=1_A1; CID=test; SEID=test'})
    return store, Cloud115(store)


def test_http_405_blocks_the_whole_window(tmp_path):
    async def run():
        store, cloud = make(tmp_path)

        async def boom(*args, **kwargs):
            request = httpx.Request('GET', 'https://webapi.115.com/files')
            response = httpx.Response(405, request=request)
            raise httpx.HTTPStatusError('405 Not Allowed', request=request, response=response)

        with pytest.raises(CloudError) as info:
            await cloud.call(boom, _kind='read')
        assert info.value.code == 'RATE_LIMITED'
        assert '拦截' in str(info.value)
        assert '分钟' in str(info.value)
        assert cloud.waf_until > 0, '405 之后必须进入整段封锁'
        # 405 不走阶梯：间隔不该被抬高，抬高也拦不住 WAF。
        assert cloud.throttle_scale == 1.0

        # 封锁期内再调用：一个请求都不许发出去。
        async def never(*args, **kwargs):
            raise AssertionError('封锁期内不应发起任何请求')

        with pytest.raises(CloudError) as blocked:
            await cloud.call(never, _kind='read')
        assert blocked.value.code == 'RATE_LIMITED'
        assert '封锁' in str(blocked.value)

        # 播放取链同一扇门：否则播放器重连就是在给封锁续期。
        token = playback_request.set(True)
        try:
            with pytest.raises(CloudError) as during_play:
                await cloud.call(never, _kind='read')
            assert '封锁' in str(during_play.value)
        finally:
            playback_request.reset(token)

        await cloud.close()
        store.close()

    asyncio.run(run())


def test_429_keeps_the_ladder(tmp_path):
    """429/503 仍是逐请求限流，保留原来的阶梯冷却。"""

    async def run():
        store, cloud = make(tmp_path)

        async def boom(*args, **kwargs):
            request = httpx.Request('GET', 'https://webapi.115.com/files')
            response = httpx.Response(429, request=request)
            raise httpx.HTTPStatusError('429 Too Many', request=request, response=response)

        with pytest.raises(CloudError) as info:
            await cloud.call(boom, _kind='read')
        assert info.value.code == 'RATE_LIMITED'
        assert cloud.rate_limit_until > 0, '429 仍进阶梯冷却'
        assert cloud.waf_until == 0, '429 不该触发整段封锁'
        assert cloud.throttle_scale == 2.0, '429 要抬高间隔'
        await cloud.close()
        store.close()

    asyncio.run(run())
