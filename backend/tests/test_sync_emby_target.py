import asyncio
import json
import time

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from strimkeep.phase5_api import install_phase5
from test_organize import setup


SERVER = {'id': 'secondary', 'name': '独立 Emby', 'upstream': 'http://secondary.test/edge/emby',
          'api_key': 'secondary-key', 'emby_strm_path': '/secondary', 'enabled': False}
PROFILE = {'id': 'sync-profile', 'name': '子库同步', 'mode': 'incremental', 'source_cids': ['100'],
           'source_labels': {'100': '待整理'}}
EXPECTED_PATH = '/secondary/待整理/Movie.strm'


@pytest.mark.parametrize('change', ['', 'before_submit', 'after_notify'])
def test_selected_emby_uses_its_own_mount_and_pinned_connection(tmp_path, change):
    async def run():
        store, cloud, http, engine, _ = setup(tmp_path)
        store.set_many({'emby_proxy_servers': [SERVER],
                        'console_config': {'emby_strm_path': '/primary'},
                        'sync_config': {'notify_emby': False, 'emby_media_info': True}})
        cloud.add_video(name='Movie.mkv')['sha1'] = 'A' * 40
        calls = []
        item = {'Id': '1', 'Path': '/primary/待整理/Movie.strm'}

        def report(request):
            calls.append((request.method, request.url.path))
            assert request.url.host == 'secondary.test'
            if request.url.path.endswith('/System/Info/Public'):
                return httpx.Response(200, json={'ServerName': 'secondary'})
            assert request.headers['X-Emby-Token'] == 'secondary-key'
            if request.url.path == '/edge/emby/Library/Media/Updated':
                assert json.loads(request.content) == {'Updates': [{'Path': EXPECTED_PATH, 'UpdateType': 'Created'}]}
                return httpx.Response(204)
            if request.url.path.endswith('/Library/VirtualFolders'):
                return httpx.Response(200, json=[{'Name': 'Second library', 'Locations': ['/secondary'], 'ItemId': '7'}])
            if request.url.path.endswith('/Items'):
                if request.url.params.get('ParentId'):
                    return httpx.Response(200, json={'Items': [], 'TotalRecordCount': 1})
                assert request.url.params.get('Path') == EXPECTED_PATH
                assert request.url.params.get('Limit') == '8'
                assert 'SearchTerm' not in request.url.params
                return httpx.Response(200, json={'Items': [item], 'TotalRecordCount': 1})
            if request.url.path == '/edge/emby/Items/7/Refresh':
                return httpx.Response(204)
            raise AssertionError(str(request.url))

        def change_server():
            store.set_many({'emby_proxy_servers': [{**SERVER, 'emby_strm_path': '/moved'}]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(report)) as upstream:
            engine.emby.http = upstream
            try:
                config = engine.tasks.sync_profile_config({**PROFILE, 'emby_target': SERVER['id']})
                assert config['notify_emby'] and not config['emby_media_info']
                assert 'secondary-key' not in json.dumps(config)
                jid, _ = engine.tasks.enqueue(config)
                await engine.run_job(jid)
                assert engine.job(jid)['state'] == 'completed'
                if change == 'before_submit':
                    change_server()
                await engine.emby.drain_outbox()
                row = engine.emby.row(jid)
                assert row['state'] == 'pending'
                payload = json.loads(row['payload'])
                assert payload['target_id'] == 'secondary' and payload['targets'][0]['emby_path'] == EXPECTED_PATH
                assert 'secondary-key' not in row['payload']
                now = time.time()
                await engine.emby.tick(now)
                if change == 'before_submit':
                    assert engine.emby.row(jid)['state'] == 'unconfirmed' and not calls
                elif change == 'after_notify':
                    assert engine.emby.row(jid)['state'] == 'accepted'
                    change_server()
                    previous = list(calls)
                    await engine.emby.tick(now + 15)
                    assert engine.emby.row(jid)['state'] == 'unconfirmed' and calls == previous
                else:
                    await engine.emby.tick(now + 150)
                    assert engine.emby.row(jid)['state'] == 'checking'
                    assert ('POST', '/edge/emby/Items/7/Refresh') in calls
                    item['Path'] = EXPECTED_PATH
                    await engine.emby.tick(now + 190)
                    assert engine.emby.row(jid)['state'] == 'verified'
                assert '独立 Emby' in engine.emby.row(jid)['message']
                assert not store.db.execute('SELECT 1 FROM native_media_info_queue').fetchone()
            finally:
                if engine.notice_tasks:
                    await asyncio.gather(*list(engine.notice_tasks), return_exceptions=True)
                await http.aclose()
                store.close()

    asyncio.run(run())


def test_sync_profile_api_validates_and_preserves_selected_target(tmp_path):
    store, cloud, http, engine, _ = setup(tmp_path)
    store.set_many({'emby_proxy_servers': [SERVER]})
    app = FastAPI()
    app.state.cloud, app.state.organizer = cloud, engine
    install_phase5(app, store, lambda: True, None)
    body = {'name': '媒体同步', 'mode': 'incremental', 'source_cids': ['100']}
    try:
        with TestClient(app) as client:
            invalid = client.post('/api/v1/sync/profiles', json={**body, 'emby_target': 'invalid'})
            assert invalid.status_code == 422
            result = client.post('/api/v1/sync/profiles', json={**body, 'emby_target': 'secondary'})
            assert result.status_code == 200 and result.json()['emby_target'] == 'secondary'
            url = '/api/v1/sync/profiles/' + result.json()['id']
            assert client.put(url, json=body).json()['emby_target'] == 'secondary'
            assert client.put(url, json={**body, 'emby_target': 'off'}).json()['emby_target'] == 'off'
            assert 'secondary-key' not in client.get('/api/v1/sync/profiles').text
    finally:
        asyncio.run(http.aclose())
        store.close()


def test_sync_uses_default_notification_or_can_disable_independently(tmp_path):
    async def run():
        store, cloud, http, engine, _ = setup(tmp_path)
        store.set_many({'sync_config': {'notify_emby': True, 'emby_media_info': True}})
        try:
            assert engine.tasks.sync_profile_config(PROFILE)['notify_emby']
            assert not engine.tasks.sync_profile_config({**PROFILE, 'metadata_mode': 'technical'})['notify_emby']
            off = engine.tasks.sync_profile_config({**PROFILE, 'emby_target': 'off'})
            assert not off['notify_emby'] and not off['emby_media_info']
            cloud.add_video(name='Movie.mkv')['sha1'] = 'A' * 40
            jid, _ = engine.tasks.enqueue(off)
            await engine.run_job(jid)
            assert list(engine.root.rglob('*.strm'))
            assert not store.db.execute('SELECT 1 FROM emby_outbox').fetchone()
        finally:
            await http.aclose()
            store.close()

    asyncio.run(run())
