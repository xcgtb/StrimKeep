import asyncio
import json
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from strimkeep.phase5_api import install_phase5
from test_organize import new_job, setup


def test_profile_path_filters_save_and_preview_before_running(tmp_path):
    store, cloud, http, engine, _ = setup(tmp_path)
    cloud.add_video('1000', 'Movie.PREVIEW.mkv')
    cloud.add_video('1001', 'Movie.PREVIEW.nfo')
    app = FastAPI()
    app.state.organizer, app.state.cloud = engine, cloud
    install_phase5(app, store, lambda: True, lambda row: {'id': row['id']})
    url = '/api/v1/sync/profiles'
    body = {'name': '电影', 'source_cids': ['100'], 'mode': 'incremental', 'metadata_mode': 'source'}
    try:
        with TestClient(app) as client:
            response = client.post(url, json={**body, 'exclude_keywords': ['   ']})
            assert response.status_code == 422
            profile = client.post(url, json={**body, 'exclude_keywords': [' preview ']}).json()
            assert profile['exclude_keywords'] == ['preview']
            path = f'{url}/{profile["id"]}'
            assert client.put(path, json=body).json()['exclude_keywords'] == ['preview']
            job = client.post(path + '/run').json()
            assert json.loads(engine.job(job['id'])['config'])['sync_exclude_keywords'] == ['preview']
            assert client.put(path, json={**body, 'exclude_keywords': []}).json()['exclude_keywords'] == []
            for fid, suffix in [('1000', '.mkv'), ('1001', '.nfo')]:
                sample = {'source_cid': '100', 'file_id': fid, 'metadata_mode': 'source'}
                preview = client.post(url + '/preview-path', json={**sample, 'exclude_keywords': ['preview']}).json()
                assert preview == {'cloud_path': '/待整理/Movie.PREVIEW' + suffix,
                                   'skip_reason': '命中同步路径排除词「preview」'}
                allowed = client.post(url + '/preview-path', json=sample).json()
                assert allowed.get('sidecar') if fid == '1001' else allowed.get('local_path')
    finally:
        asyncio.run(http.aclose())
        store.close()


def test_path_filter_preserves_existing_strm_and_applies_to_sidecars(tmp_path):
    async def run():
        store, cloud, http, engine, _ = setup(tmp_path)
        extras = (await cloud.mkdir('100', 'EXTRAS'))['cid']
        cloud.add_video('1000', 'Old.mkv', extras)['sha1'] = 'A' * 40
        profile = {'id': 'movies', 'name': '电影', 'source_cid': '100', 'mode': 'full',
                   'metadata_mode': 'source', 'emby_target': 'off', 'after_transfer': True}
        try:
            first = new_job(store, engine.tasks.sync_profile_config(profile))
            await engine.run_job(first)
            previous = engine.root / '待整理' / 'EXTRAS' / 'Old.strm'
            assert previous.is_file()
            original = previous.read_bytes()
            cloud.add_video('1001', 'New.mkv', extras)['sha1'] = 'B' * 40
            cloud.add_video('1002', 'poster.jpg', extras)
            cloud.add_video('1003', 'Main.mkv')['sha1'] = 'C' * 40
            cloud.add_video('1004', 'cover.jpg')
            cloud.live_file_exists = AsyncMock(wraps=cloud.live_file_exists)
            filtered_profile = {**profile, 'exclude_keywords': ['/待整理/extras/']}
            for mode in ('full', 'incremental'):
                config = engine.tasks.sync_profile_config({**filtered_profile, 'mode': mode})
                jid = new_job(store, config)
                await engine.run_job(jid)
                row = engine.job(jid)
                state = json.loads(row['config'])
                assert row['state'] == 'completed', row['message']
                assert state['sync_path_excluded_count'] == 3
                assert state['sync_removed'] == 0
                assert previous.read_bytes() == original
                assert not previous.with_name('New.strm').exists()
                assert not previous.with_name('poster.jpg').exists()
                assert (engine.root / '待整理' / 'Main.strm').is_file()
                assert (engine.root / '待整理' / 'cover.jpg').is_file()
                assert '命中同步路径排除词' in row['message']
            cloud.live_file_exists.assert_not_awaited()
            await engine.notifications.job(row)
            notice = store.db.execute('SELECT message FROM notifications WHERE event_key=?',
                                      (f'strm:{jid}:sync-summary',)).fetchone()
            assert '按路径排除词跳过 3 个文件' in notice['message']

            incremental = {**filtered_profile, 'mode': 'incremental'}
            store.set_many({'sync_profiles': [incremental]})
            engine.tasks.enqueue_transfer_sync('1', ['0', '100', extras], '附录')
            row = store.db.execute(
                "SELECT config FROM organize_jobs WHERE json_extract(config,'$.sync_trigger')='transfer'").fetchone()
            assert json.loads(row['config'])['sync_exclude_keywords'] == ['/待整理/extras/']

            allowed = engine.tasks.sync_profile_config({**incremental, 'exclude_keywords': []})
            assert engine.sync.scope(allowed) != engine.sync.scope({**allowed, 'sync_exclude_keywords': ['/extras/']})
            jid = new_job(store, allowed)
            await engine.run_job(jid)
            assert engine.job(jid)['state'] == 'completed'
            assert previous.with_name('New.strm').is_file()
            assert previous.with_name('poster.jpg').is_file()
        finally:
            await http.aclose()
            store.close()

    asyncio.run(run())
