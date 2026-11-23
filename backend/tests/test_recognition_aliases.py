import asyncio
import json
import sqlite3

import httpx
from fastapi.testclient import TestClient

from strimkeep import recognition_aliases, share_service
from strimkeep.main import create_app
from strimkeep.organizer import Organizer
from test_organize import CONFIG, MOVIE, TV, MemoryCloud, good_probe, handler, new_job
from test_phase2_share_pipeline import share_setup


def test_manual_match_remembers_by_choice_and_conflict_keeps_existing_rule(tmp_path, monkeypatch):
    monkeypatch.setenv('ADMIN_USERNAME', 'admin')
    monkeypatch.setenv('ADMIN_PASSWORD', 'test-password-1234')
    app = create_app(tmp_path / 'data', tmp_path / 'strm')
    store = app.state.store
    cloud = MemoryCloud(store)
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    engine = app.state.organizer = Organizer(store, cloud, http, app.state.strms, good_probe)
    token, csrf = store.new_session()

    async def tmdb(path, params=None):
        if path.startswith('tv/'):
            return TV
        return {**MOVIE, 'id': int(path.split('/')[-1])}

    engine.tmdb = tmdb

    def needs_match(fid, name='旧片别名.2024.1080p.mkv'):
        source = cloud.add_video(fid, name)
        jid = new_job(store, CONFIG)
        item_id = store.jobs.insert_item(jid, fid, source)
        store.jobs.update_item(item_id, state='review', step='未识别')
        store.jobs.update(jid, state='partial')
        return f'/api/v1/organize/jobs/{jid}', f'/items/{item_id}/manual-match'

    body = {'tmdb_id': MOVIE['id'], 'media_type': 'movie'}
    aliases = '/api/v1/organize/recognition-aliases'
    preview = '/api/v1/organize/preview'
    client = TestClient(app, cookies={'strimkeep_session': token}, headers={'X-CSRF-Token': csrf})
    try:
        job, action = needs_match('1100')
        assert client.post(job + action, json=body).json()['saved_alias'] is False
        assert client.get(aliases).json() == {'items': []}

        job, action = needs_match('1101')
        candidate = client.get(job).json()['items'][0]['alias_candidate']
        assert candidate['alias'] == '旧片别名' and candidate['source_year'] == 2024
        original_manifest = store.jobs.set_manifest

        def check_transaction(*args, **kwargs):
            with sqlite3.connect(store.path) as reader:
                assert reader.execute('SELECT COUNT(*) FROM recognition_aliases').fetchone()[0] == 0
            return original_manifest(*args, **kwargs)

        monkeypatch.setattr(store.jobs, 'set_manifest', check_transaction)
        saved = client.post(job + action, json={**body, 'remember_alias': True})
        assert saved.status_code == 200 and saved.json()['saved_alias']
        monkeypatch.setattr(store.jobs, 'set_manifest', original_manifest)
        rule, = client.get(aliases).json()['items']
        assert rule['tmdb_id'] == MOVIE['id']
        name = {'name': '旧片别名.2024.2160p.mkv'}
        for library in ('cloud', 'share'):
            result = client.post(preview, json={**name, 'library': library})
            assert result.status_code == 200
            assert result.json()['hint']['recognition_alias']['id'] == rule['id']
        assert client.post(preview, json={**name, 'tmdb_id': 99, 'use_tmdb': False}).json()['hint']['tmdb_id'] == 99
        different_year = client.post(preview, json={'name': '旧片别名.2025.mkv', 'use_tmdb': False}).json()
        assert 'recognition_alias' not in different_year['hint']

        job, action = needs_match('1102')
        count = store.db.execute('SELECT COUNT(*) FROM organize_jobs').fetchone()[0]
        response = client.post(job + action, json={**body, 'tmdb_id': 99, 'remember_alias': True})
        assert response.status_code == 409
        assert store.db.execute('SELECT COUNT(*) FROM organize_jobs').fetchone()[0] == count
        assert client.get(aliases).json()['items'] == [rule]

        job, action = needs_match('1103', '剧集别名.2024.S01E01.mkv')
        async def absent_episode(*args):
            return None

        engine.tmdb_episode = absent_episode
        series = {'tmdb_id': TV['id'], 'media_type': 'tv', 'season': 1, 'episode': 1, 'remember_alias': True}
        assert client.post(job + action, json={**series, 'episode': 2}).status_code == 422
        assert client.post(job + action, json=series).status_code == 409
        assert client.get(aliases).json()['items'] == [rule]

        assert client.delete(aliases + '/' + str(rule['id'])).json() == {'removed': True}
        result = client.post(preview, json={**name, 'use_tmdb': False}).json()
        assert 'recognition_alias' not in result['hint']
    finally:
        client.close()
        asyncio.run(http.aclose())
        store.close()


def test_organize_and_share_use_tmdb_alias_and_keep_each_episode(tmp_path):
    async def run():
        store, cloud, http, engine, requests = share_setup(tmp_path)
        original = {'name': '频道别名.2024.S01E01.mkv'}
        source = recognition_aliases.candidate(original)
        with store.transaction(immediate=True):
            recognition_aliases.remember(store, source, {'tmdb_id': TV['id'], 'query': TV['name'], 'year': 2024})
        cloud.add_video('1201', '频道别名.2024.S01E02.1080p.mkv')
        videos = [{'id': '2203', 'name': '频道别名.2024.S01E03.mkv', 'size': 5000, 'path': ''}]
        share_service.generate(store, engine.root, {'share_code': 'code_A', 'receive_code': 'abcd'}, videos)
        try:
            drive = new_job(store, {**CONFIG, 'notify_emby': False})
            await engine.run_job(drive)
            share = share_service.enqueue_organize(engine)
            await engine.run_job(share)
            for jid, number in ((drive, 2), (share, 3)):
                row = store.db.execute('SELECT state,plan FROM organize_items WHERE job_id=?', (jid,)).fetchone()
                assert row['state'] == 'done', dict(row)
                plan = json.loads(row['plan'])
                assert plan['hint']['recognition_alias']['tmdb_id'] == TV['id']
                assert plan['hint']['episode'] == number
                assert f'S01E{number:02d}' in plan['relative_path']
            assert not any('/search/tv' in request.url.path for request in requests)
        finally:
            if engine.notice_tasks:
                await asyncio.gather(*list(engine.notice_tasks), return_exceptions=True)
            await http.aclose()
            store.close()

    asyncio.run(run())
