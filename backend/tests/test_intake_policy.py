import asyncio
import base64
import json

import pytest

from strimkeep.cloud115 import CloudError
from strimkeep.share_service import enqueue_organize
from test_organize import setup


def test_transfer_reuses_cross_entry_receipts_and_preserves_constraints(tmp_path):
    async def run():
        store, _, http, engine, _ = setup(tmp_path)
        try:
            link = 'https://115.com/s/Abc?password=abcd'
            first, second = await asyncio.gather(
                engine.transfer.enqueue(link, '100', origin='channel'),
                engine.transfer.enqueue(link, '100', origin='web'),
            )
            assert first == second
            expected = {'path': ['episode.mkv'], 'size': 42, 'extensions': ['.mkv']}
            assert await engine.transfer.enqueue(link, '100', origin='inbox', expected_video=expected) == first
            config = json.loads(engine.job(first[0])['config'])
            assert config['expected_subscription_video'] == expected
            different_target = await engine.transfer.enqueue(link, '200', origin='web')
            assert different_target != first
            with pytest.raises(CloudError, match='入库方式'):
                await engine.transfer.enqueue(link, '100', auto=True, origin='web')
            with pytest.raises(CloudError, match='提取码不同'):
                await engine.transfer.enqueue(link.replace('abcd', 'xyzw'), '100', origin='web')
            assert store.db.execute('SELECT count(*) FROM organize_jobs').fetchone()[0] == 2
        finally:
            await http.aclose()
            store.close()
    asyncio.run(run())


def test_share_queue_merges_across_entries(tmp_path):
    async def run():
        store, _, http, engine, _ = setup(tmp_path)
        try:
            secret = {'share_code': 'abc', 'receive_code': 'abcd'}
            job = enqueue_organize(engine, origin='telegram', secret=secret)
            assert enqueue_organize(engine, origin='subscription', secret=secret) == job
            with pytest.raises(CloudError, match='入库方式'):
                enqueue_organize(engine, origin='telegram', secret=secret, generate_only=True)
            assert store.db.execute('SELECT count(*) FROM organize_jobs').fetchone()[0] == 1
        finally:
            await http.aclose()
            store.close()
    asyncio.run(run())


def test_confirmed_revocation_ends_transfer_and_share_jobs_without_a_write(tmp_path):
    async def run():
        store, cloud, http, engine, _ = setup(tmp_path)
        try:
            async def expired(*_):
                raise CloudError('分享已取消', 'SHARE_NOT_FOUND', 404)

            cloud.share_items = expired
            expected = {'path': ['episode.mkv'], 'size': 42, 'extensions': ['.mkv']}
            transfer, = await engine.transfer.enqueue('https://115.com/s/first', '100', expected_video=expected)
            share = enqueue_organize(engine, origin='subscription',
                                     secret={'share_code': 'second', 'receive_code': ''},
                                     expected_video=expected)
            for jid in (transfer, share):
                await engine.run_job(jid)
                job = engine.job(jid)
                assert job['state'] == 'partial'
                assert json.loads(job['config'])['subscription_source_rejected']
            assert not cloud.writes
        finally:
            await http.aclose()
            store.close()
    asyncio.run(run())


def test_offline_claim_is_atomic_after_an_await_and_canonicalizes_hash(tmp_path):
    async def run():
        store, cloud, http, engine, _ = setup(tmp_path)
        try:
            arrivals = asyncio.Barrier(2)
            submissions = []

            async def children(_):
                await arrivals.wait()
                return []

            async def submit(urls, target):
                submissions.append((urls, target))

            cloud.children, cloud.offline_add = children, submit
            btih = bytes.fromhex('ab' * 20)
            prefix = 'magnet:?xt=urn:btih:'
            base32 = prefix + base64.b32encode(btih).decode()
            hex_url = prefix + btih.hex() + '&dn=episode'
            results = await asyncio.gather(
                engine.offline.submit([base32], '100', '下载', origin='channel'),
                engine.offline.submit([hex_url], '100', '下载', origin='web'),
                return_exceptions=True)
            assert len(submissions) == 1
            assert sum(isinstance(result, int) for result in results) == 1
            duplicate, = [result for result in results if isinstance(result, CloudError)]
            assert duplicate.code == 'OFFLINE_DUPLICATE'
        finally:
            await http.aclose()
            store.close()
    asyncio.run(run())
