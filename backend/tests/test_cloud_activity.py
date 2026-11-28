import asyncio
import json

import pytest

from strimkeep.cloud115 import Cloud115, CloudError
from strimkeep.cloud_activity import STATE
from test_organize import setup


def event(event_id, kind, parent, *, file_id='900'):
    return {'id': str(event_id), 'type': kind, 'parent_id': parent, 'file_id': file_id}


def test_activity_queues_only_verified_subtree_and_publishes_strm(tmp_path):
    async def run():
        store, cloud, http, engine, _ = setup(tmp_path)
        try:
            cloud.nodes['101'] = {'id': '101', 'parent_id': '100', 'name': '新目录', 'is_dir': True}
            store.set_many({'sync_profiles': [
                {'id': 'tracked', 'name': '变更', 'mode': 'full', 'source_cid': '100',
                 'watch_changes': True, 'metadata_mode': 'source', 'emby_target': 'off'},
                {'id': 'off', 'name': '定时', 'mode': 'incremental', 'source_cid': '200'},
            ]})
            cloud.use_scope = lambda *_: None
            cloud.reset_ledger = lambda: None
            rows = [event(10, 2, '300')]

            async def activity_page():
                return rows.copy()

            cloud.activity_page = activity_page
            await engine.cloud_activity.tick()
            assert store.db.execute('SELECT count(*) FROM organize_jobs').fetchone()[0] == 0
            cloud.add_video('901', '新片.mkv', parent='101')
            rows[:0] = [event(14, 22, '100'), event(13, 8, '200'), event(12, 2, '200'), event(11, 24, '101')]
            await engine.cloud_activity.tick()
            await engine.cloud_activity.tick()
            jobs = store.db.execute('SELECT id,config FROM organize_jobs').fetchall()
            assert len(jobs) == 1
            config = json.loads(jobs[0]['config'])
            assert config['source_cids'] == ['101'] and config['sync_trigger'] == 'cloud_activity'
            assert config['mode'] == 'incremental' and config['metadata_mode'] == 'source'
            assert config['emby_target'] == 'off' and not config['cleanup_missing'] and not config['stale_detect']
            await engine.run_job(jobs[0]['id'])
            assert (engine.root / '待整理' / '新目录' / '新片.strm').is_file()
            assert not cloud.writes
            rows.insert(0, event(15, 6, '101'))
            await engine.cloud_activity.tick()
            pending = store.db.execute("SELECT id FROM organize_jobs WHERE state='queued'").fetchone()
            assert pending
            cloud.nodes['101']['parent_id'] = '300'
            await engine.run_job(pending['id'])
            assert engine.job(pending['id'])['state'] == 'needs_review'
            assert not (engine.root / '待处理' / '新目录' / '新片.strm').exists()
        finally:
            await http.aclose()
            store.close()
    asyncio.run(run())


def test_activity_never_advances_on_limit_and_recovers_cursor_gap(tmp_path):
    async def run():
        store, cloud, http, engine, _ = setup(tmp_path)
        try:
            store.set_many({'sync_profiles': [
                {'id': 'tracked', 'name': '变更', 'mode': 'incremental', 'source_cid': '100',
                 'watch_changes': True},
            ]})
            cloud.use_scope = lambda *_: None
            cloud.reset_ledger = lambda: None

            async def page():
                return [event(11, 2, '100'), event(10, 8, '200')]

            async def limited(_):
                raise CloudError('限流', 'RATE_LIMITED')

            store.set_meta(STATE, {'account': engine.tasks.account(), 'cursor': '10', 'initialized': True})
            cloud.activity_page = page
            cloud.directory = limited
            with pytest.raises(CloudError, match='限流'):
                await engine.cloud_activity.tick()
            assert store.meta_json(STATE)['cursor'] == '10'
            assert not store.db.execute('SELECT 1 FROM organize_jobs').fetchone()

            async def new_page():
                return [event(2000, 6, '300')]

            cloud.activity_page = new_page
            await engine.cloud_activity.tick()
            assert store.meta_json(STATE)['cursor'] == '2000'
            config = json.loads(store.db.execute('SELECT config FROM organize_jobs').fetchone()[0])
            assert config['source_cids'] == ['100'] and not config['cleanup_missing']
        finally:
            await http.aclose()
            store.close()
    asyncio.run(run())


def test_sdk_routes_use_shared_transport_and_validate_shape(tmp_path, monkeypatch):
    async def run():
        store, _, http, _, _ = setup(tmp_path)
        cloud = Cloud115(store)
        try:
            output = []

            async def call(method, payload, **options):
                output.append((method.__name__, payload, options))
                if method.__name__ == 'fs_files_app':
                    return {'data': [{'fid': '20', 'fn': '目录', 'pid': '10', 'fc': '0'}], 'count': 1}
                return {'data': {'list': [event(100, 24, '10')]}, 'state': True}

            monkeypatch.setattr(cloud, 'call', call)
            listing = await cloud.list_files_android(10, 0, 50)
            assert listing['items'][0]['id'] == '20' and listing['items'][0]['is_dir']
            assert await cloud.activity_page() == [event(100, 24, '10')]
            assert output[0][0] == 'fs_files_app' and output[0][1]['record_open_time'] == 0
            assert output[1] == ('life_behavior_detail_app', {'offset': 0, 'limit': 200}, {'_scope': 'life'})
        finally:
            await cloud.close()
            await http.aclose()
            store.close()
    asyncio.run(run())
