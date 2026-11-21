import asyncio
import base64
import json
import time
from datetime import datetime
from io import BytesIO
from types import SimpleNamespace

import httpx
from fastapi import FastAPI
from PIL import Image

from strimkeep import commands
from strimkeep.library_cover_image import SIZE
from strimkeep.library_covers import CoverSettings, LibraryCovers, install_library_covers
from strimkeep.store import Store
from strimkeep.worker import command_loop


ROOT = '/api/v1/emby/library-covers'


class Emby:
    def __init__(self):
        self.uploads = []
        self.searches = []
        self.tag = 'current-tag'
        self.cover = image_bytes((320, 180))
        self.poster = image_bytes((180, 270))
        self.on_upload = None

    def __call__(self, request):
        assert request.url.path.startswith('/emby/')
        path = request.url.path.removeprefix('/emby')
        assert request.headers['X-Emby-Token'] == 'test-emby-key'
        if path == '/Library/VirtualFolders':
            return httpx.Response(200, json=[
                {'ItemId': '21', 'Name': '国产剧', 'CollectionType': 'tvshows'},
                {'ItemId': '76', 'Name': '收藏电影', 'CollectionType': 'movies'},
            ])
        if path == '/Items':
            parents = request.url.params
            self.searches.append(parents['ParentId'])
            assert parents['SortBy'] == 'DateCreated' and parents['SortOrder'] == 'Descending'
            assert parents['IncludeItemTypes'] == 'Movie,Series'
            return httpx.Response(200, json={'Items': [
                {'Id': '800', 'ImageTags': {'Primary': self.tag}},
            ] if parents['ParentId'] == '21' else []})
        if path == '/Items/800/Images/Primary' and request.method == 'GET':
            assert request.url.params['Tag'] == self.tag
            return httpx.Response(200, content=self.poster)
        if path in ('/Items/21/Images/Primary', '/Items/76/Images/Primary'):
            if request.method == 'GET':
                return httpx.Response(200, content=self.cover)
            assert request.headers['content-type'] == 'image/jpeg'
            data = base64.b64decode(request.content, validate=True)
            assert Image.open(BytesIO(data)).size == SIZE
            self.uploads.append((path, data))
            if self.on_upload:
                self.on_upload()
            return httpx.Response(204)
        raise AssertionError(f'unexpected Emby request {request.method} {path}')


def image_bytes(size, format='JPEG'):
    buffer = BytesIO()
    Image.new('RGB', size, '#bd6458').save(buffer, format)
    return buffer.getvalue()


def make_store(tmp_path):
    store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
    store.set_many({'emby_url': 'http://127.0.0.1:8096', 'emby_key': 'test-emby-key'})
    return store


def test_cover_previews_do_not_write_and_worker_applies_exact_library_image(tmp_path):
    async def run():
        store = make_store(tmp_path)
        emby = Emby()
        app = FastAPI()

        async def admin():
            pass

        # Route registration happens before the application's HTTP client is initialized.
        install_library_covers(app, store, admin)
        async with (httpx.AsyncClient(transport=httpx.MockTransport(emby)) as upstream,
                    httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://app') as client):
            app.state.http = upstream
            app.state.organizer = SimpleNamespace(web_only=True)
            store.set_meta('worker_heartbeat', {'at': time.time()})
            listing = (await client.get(ROOT)).json()
            scope = listing['scope']
            assert [item['id'] for item in listing['items']] == ['21', '76']
            assert not any(item['settings']['enabled'] for item in listing['items'])
            current = await client.get(ROOT + '/21/current', params={'scope': scope})
            assert current.content == emby.cover and current.headers['cache-control'] == 'no-store'

            settings = {'scope': scope, 'title': '测试片库', 'layout': 'grid', 'posters': 4}
            saved = await client.put(ROOT + '/21/settings', json=settings)
            assert saved.json()['settings']['title'] == '测试片库'
            generated = (await client.post(ROOT + '/21/preview', json=settings)).json()
            assert generated['poster_count'] == 1
            assert Image.open(BytesIO(base64.b64decode(generated['image']))).size == SIZE
            empty = (await client.post(ROOT + '/76/preview', json=settings)).json()
            assert empty['poster_count'] == 0 and empty['image'] != generated['image']
            uploaded = (await client.post(ROOT + '/21/upload-preview', json={
                'scope': scope, 'image': base64.b64encode(image_bytes((300, 300), 'PNG')).decode()})).json()
            exact = base64.b64decode(uploaded['image'])
            assert Image.open(BytesIO(exact)).size == SIZE
            assert not emby.uploads
            assert (await client.post(ROOT + '/800/apply', json={'scope': scope, **uploaded})).status_code == 409
            result = await client.post(ROOT + '/21/apply', json={'scope': scope, **uploaded})
            assert result.status_code == 200 and result.json()['state'] == 'queued'
            assert (await client.post(ROOT + '/21/apply', json={'scope': scope, **uploaded})).status_code == 409
            stop = asyncio.Event()
            emby.on_upload = stop.set
            service = LibraryCovers(store, upstream)
            await asyncio.wait_for(command_loop(store, SimpleNamespace(library_covers=service),
                                                stop, commands.COVER_OPERATIONS), timeout=5)
            assert emby.uploads == [('/Items/21/Images/Primary', exact)]
            row = commands.get(store, result.json()['command_id'])
            assert row['state'] == 'done' and 'image' not in json.loads(row['payload'])
            status = (await client.get(ROOT + '/21/status', params={'scope': scope})).json()
            assert status['last']['applied_at'] and status['state'] == 'done'

            store.set_many({'emby_url': 'http://127.0.0.1:8097'})
            assert (await client.post(ROOT + '/21/preview', json=settings)).status_code == 409
            assert (await client.get(ROOT)).json()['items'][0]['settings']['title'] == ''
        store.close()

    asyncio.run(run())


def test_daily_refresh_uses_saved_schedule_and_skips_unchanged_cover(tmp_path):
    async def run():
        store = make_store(tmp_path)
        emby = Emby()
        async with httpx.AsyncClient(transport=httpx.MockTransport(emby)) as upstream:
            covers = LibraryCovers(store, upstream)
            scope = covers.connection()[2]
            covers.save(scope, '21', CoverSettings(enabled=True, daily_at='04:00'))
            covers.save(scope, '76', CoverSettings(enabled=False))
            assert covers.schedule(datetime(2026, 11, 21, 3, 59)) == 0

            async def refresh(day):
                now = datetime(2026, 11, day, 4, 0)
                assert covers.schedule(now) == 1
                row = commands.claim(store, commands.COVER_OPERATIONS)
                payload = json.loads(row['payload'])
                assert payload['library_id'] == '21' and payload['automatic']
                result = await covers.apply(payload)
                commands.finish(store, row['id'], result)
                assert covers.schedule(now) == 0
                return result

            await refresh(21)
            assert len(emby.uploads) == 1
            assert '没有变化' in (await refresh(22))['message']
            assert len(emby.uploads) == 1
            covers.save(scope, '21', CoverSettings(title='New title', enabled=True, daily_at='04:00'))
            await refresh(23)
            assert len(emby.uploads) == 2 and emby.uploads[0][1] != emby.uploads[1][1]
            assert emby.searches == ['21', '21', '21']
        store.close()

    asyncio.run(run())
