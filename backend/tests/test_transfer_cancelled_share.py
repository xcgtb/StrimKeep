import asyncio
import pytest

from strimkeep.cloud115 import CloudError
from test_organize import setup


@pytest.mark.parametrize('origin,code,expected', [
    ('channel', 'CLOUD_REJECTED', 'partial'),
    ('channel', 'SHARE_NOT_FOUND', 'partial'),
    ('web', 'CLOUD_REJECTED', 'needs_review'),
    ('channel', 'LOGIN_INVALID', 'waiting_login'),
])
def test_cancelled_channel_share_is_skipped_before_any_cloud_write(tmp_path, origin, code, expected):
    async def run():
        store, cloud, http, engine, _ = setup(tmp_path)
        jid = (await engine.transfer.enqueue('https://115.com/s/abc', '100', False, origin))[0]
        calls = []
        async def items(secret, cid=0):
            raise CloudError('分享已取消', code, 422)
        async def receive(*args):
            calls.append(args)
        cloud.share_items = items
        cloud.share_receive = receive
        await engine.run_job(jid)
        job = engine.job(jid)
        assert job['state'] == expected
        if expected == 'partial':
            assert job['stage'] == '分享已取消，已跳过'
            assert '不会自动重试' in job['message']
        assert not calls
        await http.aclose(); store.close()
    asyncio.run(run())


def test_cancelled_share_during_first_receive_is_skipped_without_retry(tmp_path):
    async def run():
        store, cloud, http, engine, _ = setup(tmp_path)
        async def items(secret, cid=0):
            return [{'id': '9001', 'name': 'file.mkv', 'is_dir': False, 'size': 1000}]
        calls = []
        async def receive(*args):
            calls.append(args)
            raise CloudError('分享已取消', 'CLOUD_REJECTED', 422)
        cloud.share_items = items; cloud.share_receive = receive
        jid = (await engine.transfer.enqueue('https://115.com/s/abc', '100', False, 'channel'))[0]
        await engine.run_job(jid)
        job = engine.job(jid)
        assert job['state'] == 'partial' and job['stage'] == '分享已取消，已跳过'
        assert len(calls) == 1
        import json
        assert 'phase' not in json.loads(job['config'])['receipt']
        await http.aclose(); store.close()
    asyncio.run(run())



def test_cancelled_response_after_uncertain_previous_submit_requires_review(tmp_path):
    async def run():
        import json
        store, cloud, http, engine, _ = setup(tmp_path)
        jid = (await engine.transfer.enqueue('https://115.com/s/abc', '100', False, 'channel'))[0]
        config = json.loads(engine.job(jid)['config'])
        config['verify_attempts'] = 1
        config['receipt'] = {'items': [{'id': '9001', 'name': 'file.mkv', 'is_dir': False, 'size': 1000}],
                             'tree': [], 'before_ids': [], 'phase': 'intent'}
        engine.update_job(jid, config=json.dumps(config))
        calls = []
        async def receive(*args):
            calls.append(args)
            raise CloudError('分享已取消', 'CLOUD_REJECTED', 422)
        cloud.share_receive = receive
        await engine.run_job(jid)
        assert engine.job(jid)['state'] == 'needs_review'
        assert len(calls) == 1
        await http.aclose(); store.close()
    asyncio.run(run())
