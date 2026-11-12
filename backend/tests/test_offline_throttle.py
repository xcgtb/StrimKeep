"""离线提交也受统一写入节奏约束，失败后不自动重复提交。"""
import asyncio

import pytest
from p115client.client import P115OpenClient
from strimkeep.cloud115 import CloudError, playback_request
from test_rate_limits import accepted, make_cloud


def test_offline_submission_shares_write_gate_and_proapi_arguments(tmp_path, monkeypatch):
    async def run():
        store, cloud, clock, sleeps = make_cloud(tmp_path)
        calls = []
        async def send(client, payload, *, async_):
            calls.append((payload, async_))
            return {'state': True, 'data': {'task_id': '1'}}
        monkeypatch.setattr(P115OpenClient, 'clouddownload_task_add_urls', staticmethod(send))
        await cloud.call(accepted, _kind='write')
        # 即使处在播放上下文也必须进入后台写限流。
        token = playback_request.set(True)
        try:
            result = await cloud.offline_add(['magnet:?xt=test', 'ed2k://test'], '123')
            assert playback_request.get() is True
        finally:
            playback_request.reset(token)
        assert result['data']['task_id'] == '1'
        assert calls == [({'urls': 'magnet:?xt=test\ned2k://test', 'wp_path_id': 123}, True)]
        assert sleeps[-1] == pytest.approx(3.0)
        assert cloud.next_request - clock() == pytest.approx(3.0)
        assert cloud.ledger['write'] == 2 and cloud.ledger['scope']['offline'] == 1
        await cloud.close()
        store.close()
    asyncio.run(run())


def test_offline_rate_limit_is_penalized_without_retry(tmp_path, monkeypatch):
    async def run():
        store, cloud, clock, _ = make_cloud(tmp_path)
        calls = []
        async def send(client, payload, *, async_):
            calls.append(payload)
            return {'state': False, 'message': '操作过于频繁，请稍后再试'}
        monkeypatch.setattr(P115OpenClient, 'clouddownload_task_add_urls', staticmethod(send))
        with pytest.raises(CloudError) as exc:
            await cloud.offline_add(['magnet:?xt=test'])
        assert exc.value.code == 'RATE_LIMITED'
        assert cloud.rate_limit_until > clock()
        with pytest.raises(CloudError) as exc:
            await cloud.offline_add(['magnet:?xt=test'])
        assert exc.value.code == 'RATE_LIMITED' and len(calls) == 1
        assert cloud.ledger['blocked'] == 1
        await cloud.close()
        store.close()
    asyncio.run(run())


def test_offline_unknown_transport_result_is_not_retried(tmp_path, monkeypatch):
    async def run():
        store, cloud, clock, _ = make_cloud(tmp_path)
        calls = []
        async def send(client, payload, *, async_):
            calls.append(payload)
            raise TimeoutError('response lost')
        monkeypatch.setattr(P115OpenClient, 'clouddownload_task_add_urls', staticmethod(send))
        with pytest.raises(CloudError) as exc:
            await cloud.offline_add(['magnet:?xt=test'])
        assert exc.value.code == 'CLOUD_UNAVAILABLE' and len(calls) == 1
        assert cloud.next_request - clock() == pytest.approx(3.0)
        await cloud.close()
        store.close()
    asyncio.run(run())


def test_offline_duplicate_keeps_business_message(tmp_path, monkeypatch):
    async def run():
        store, cloud, _, _ = make_cloud(tmp_path)
        async def send(client, payload, *, async_):
            return {'state': False, 'errno': 20018, 'message': '任务已存在，请勿输入重复的链接地址'}
        monkeypatch.setattr(P115OpenClient, 'clouddownload_task_add_urls', staticmethod(send))
        with pytest.raises(CloudError) as exc:
            await cloud.offline_add(['magnet:?xt=test'])
        assert exc.value.code == 'CLOUD_REJECTED'
        assert '任务已存在' in str(exc.value)
        await cloud.close()
        store.close()
    asyncio.run(run())
