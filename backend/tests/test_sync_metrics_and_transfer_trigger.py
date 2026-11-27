import asyncio
import json

import pytest

from test_organize import CONFIG, new_job, setup
from test_phase5 import sync_config
from test_transfer import mock_share


def test_transfer_receipt_sync_skips_unrelated_folders_and_existing_videos(tmp_path):
    async def run():
        store, cloud, http, engine, _ = setup(tmp_path)
        profile = {'id': 'receipt', 'name': '增量', 'mode': 'incremental', 'source_cids': ['200'],
                   'after_transfer': True, 'enabled': True, 'metadata_mode': 'technical'}
        store.set_many({'organize_config': CONFIG, 'sync_profiles': [profile]})
        child = (await cloud.mkdir('200', '留待定时全扫'))['cid']
        added = cloud.add_video('8001', 'New.mkv', '200')
        added['sha1'] = 'A' * 40
        cloud.add_video('8002', 'Old.mkv', '200')['sha1'] = 'B' * 40
        cloud.add_video('8003', 'Nested.mkv', child)['sha1'] = 'C' * 40
        try:
            engine.tasks.enqueue_transfer_sync('1', ['0', '200'], '归档',
                                               [{**added, 'id': '8001', 'is_dir': False}])
            row = store.db.execute("SELECT * FROM organize_jobs WHERE json_extract(config,'$.sync_trigger')='transfer'").fetchone()
            await engine.run_job(row['id'])
            metrics = json.loads(engine.job(row['id'])['config'])['sync_metrics']
            assert metrics['scan']['listing'] == 'receipt'
            assert metrics['videos'] == 1
            assert [file.name for file in engine.root.rglob('*.strm')] == ['New.strm']
        finally:
            await http.aclose()
            store.close()
    asyncio.run(run())


def test_full_and_incremental_sync_report_scan_and_actual_writes(tmp_path):
    async def run():
        store, cloud, http, engine, _ = setup(tmp_path)
        cloud.add_video(name='Movie.mkv')['sha1'] = 'A' * 40
        children = cloud.children
        calls = []

        async def list_children(cid):
            calls.append(cid)
            await asyncio.sleep(.03)
            return await children(cid)

        cloud.children = list_children
        cloud.ledger_summary = lambda: {'read': len(calls) + 1, 'write': 0, 'blocked': 0, 'text': 'test'}
        try:
            for mode, created, reused in (('full', 1, 0), ('incremental', 0, 1), ('full', 0, 1)):
                calls.clear()
                config = {**sync_config(), 'notify_emby': False, 'mode': mode, '_force_full': mode == 'full'}
                jid = new_job(store, config)
                with store.db:
                    store.db.execute('UPDATE organize_jobs SET created=created-2 WHERE id=?', (jid,))
                await engine.run_job(jid)
                row = engine.job(jid)
                report = json.loads(row['config'])['sync_metrics']
                assert row['state'] == 'completed'
                assert report['queue'] >= 2 and report['attempt'] == 1
                assert report['execution'] >= report['stages']['scan'] >= .01
                assert report['stages']['scan_wait'] >= 0 and report['stages']['verify'] >= 0
                assert report['scan'] == {'listing': 'directory', 'directories': 1, 'directories_exact': True, 'files': 1}
                assert report['requests'] == {'read': len(calls) + 1, 'write': 0, 'blocked': 0}
                assert (report['videos'], report['created'], report['updated'], report['reused']) == (1, created, 0, reused)
                if not created:
                    assert report['stages']['publish'] == 0
                await engine.notifications.job(row)
                message = store.db.execute('SELECT message FROM notifications WHERE event_key=?',
                                           (f'strm:{jid}:sync-summary',)).fetchone()[0]
                for label in ('排队', '115 枚举', '文件核对', '本地写入', '115 请求'):
                    assert label in message
        finally:
            await http.aclose()
            store.close()

    asyncio.run(run())


@pytest.mark.parametrize('moved', [False, True])
def test_confirmed_transfer_limits_followup_to_matched_destination(tmp_path, moved):
    async def run():
        store, cloud, http, engine, _ = setup(tmp_path)
        dest = (await cloud.mkdir('200', '补扫目录'))['cid']
        sibling = (await cloud.mkdir('200', '其他目录'))['cid']
        cloud.add_video('8000', 'Other.mkv', sibling)['sha1'] = 'B' * 40
        profile = {'id': 'scope', 'name': '归档增量', 'mode': 'incremental', 'source_cids': ['200'],
                   'source_labels': {'200': '归档'}, 'after_transfer': True, 'enabled': True}
        store.set_many({'organize_config': CONFIG, 'sync_profiles': [
            profile, {**profile, 'id': 'unrelated', 'source_cids': ['300']},
            {**profile, 'id': 'legacy', 'after_transfer': False},
        ], 'sync_full_due': 1})
        store.set_meta('sync_watermark', {'at': 123, 'roots': {'200': 123}})
        mock_share(cloud)
        try:
            sweep = engine.tasks.sync_profile_config({**profile, 'source_cids': [dest]})
            regular, _ = engine.tasks.enqueue(sweep, scheduled=True)
            jid, = await engine.transfer.enqueue('https://115.com/s/abc?password=Z9q8', dest)
            await engine.run_job(jid)
            assert engine.job(jid)['state'] == 'completed'
            rows = store.db.execute(
                "SELECT * FROM organize_jobs WHERE json_extract(config,'$.sync_trigger')='transfer'").fetchall()
            supplemental, = rows
            config = json.loads(supplemental['config'])
            assert config['source_cids'] == [dest]
            assert config['sync_profile_id'] == 'scope'
            assert config['cleanup_missing'] is False and config['stale_detect'] is False
            assert engine.job(regular)['state'] == 'queued'
            if moved:
                cloud.nodes[dest]['parent_id'] = '300'
            visited = []
            children = cloud.children

            async def record(cid):
                visited.append(cid)
                return await children(cid)

            cloud.children = record
            await engine.run_job(supplemental['id'])
            result = engine.job(supplemental['id'])
            files = list(engine.root.rglob('*.strm'))
            if moved:
                assert result['state'] == 'needs_review' and not files and not visited
                assert '不在启用变更补扫的同步范围' in result['message']
            else:
                assert result['state'] == 'completed'
                file, = files
                assert file.relative_to(engine.root).parts == ('归档', '补扫目录', 'Movie', '测试电影 (2024).strm')
                assert '200' not in visited and sibling not in visited
            assert store.meta_json('sync_watermark', {}) == {'at': 123, 'roots': {'200': 123}}
            assert store.get('sync_full_due') == 1
        finally:
            await http.aclose()
            store.close()

    asyncio.run(run())
