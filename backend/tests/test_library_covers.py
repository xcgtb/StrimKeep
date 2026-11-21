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
from strimkeep.library_cover_image import COVER_LAYOUTS, SIZE, render_cover
from strimkeep.library_covers import CoverStudio, LibraryCovers, install_library_covers
from strimkeep.store import Store
from strimkeep.worker import command_loop


ROOT = '/api/v1/emby/library-covers'


class Emby:
    def __init__(self):
        self.uploads = []
        self.searches = []
        self.tag = 'current-tag'
        self.poster_reads = 0
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
            self.poster_reads += 1
            return httpx.Response(200, content=self.poster)
        if path in ('/Items/21/Images/Primary', '/Items/76/Images/Primary'):
            assert request.method == 'POST'
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


def test_gallery_reuses_real_posters_and_single_library_write_routes_are_removed(tmp_path):
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
            listing = (await client.get(ROOT)).json()
            scope = listing['scope']
            assert [item['id'] for item in listing['items']] == ['21', '76']
            assert listing['settings']['library_ids'] == ['21', '76']
            assert not listing['settings']['enabled']

            options = {'scope': scope, 'library_id': '21', 'layouts': list(COVER_LAYOUTS)}
            generated = (await client.post(ROOT + '/gallery', json=options)).json()
            assert generated['poster_count'] == 1
            assert [item['id'] for item in generated['items']] == list(COVER_LAYOUTS)
            assert emby.searches == ['21'] and emby.poster_reads == 1
            for item in generated['items']:
                with Image.open(BytesIO(base64.b64decode(item['image']))) as image:
                    assert image.size == (640, 360)
            for route in ('/21/apply', '/21/upload-preview'):
                assert (await client.post(ROOT + route, json={})).status_code == 404
            assert (await client.put(ROOT + '/21/settings', json={})).status_code == 404
            assert not emby.uploads and commands.running(store) is None

            store.set_many({'emby_url': 'http://127.0.0.1:8097'})
            assert (await client.post(ROOT + '/gallery', json=options)).status_code == 409
        store.close()

    asyncio.run(run())


def test_daily_refresh_uses_saved_schedule_and_skips_unchanged_cover(tmp_path):
    async def run():
        store = make_store(tmp_path)
        emby = Emby()
        async with httpx.AsyncClient(transport=httpx.MockTransport(emby)) as upstream:
            covers = LibraryCovers(store, upstream)
            scope = covers.connection()[2]
            store.set_many({'emby_library_covers': {scope: {
                '21': {'enabled': True, 'daily_at': '04:00'},
                '76': {'enabled': False},
            }}})
            assert covers.studio(scope).library_ids == ['21']
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
            covers.save_studio(scope, CoverStudio(library_ids=['21'], layout='hero', enabled=True, daily_at='04:00'))
            await refresh(23)
            assert len(emby.uploads) == 2 and emby.uploads[0][1] != emby.uploads[1][1]
            assert emby.searches == ['21', '21', '21']
        store.close()

    asyncio.run(run())


def test_templates_produce_distinct_valid_covers():
    posters = []
    for color in ('#965572', '#30898a', '#719923', '#973a5c', '#c47a33', '#5f7db3', '#449657', '#7e4d92', '#507487'):
        buffer = BytesIO()
        Image.new('RGB', (175, 260), color).save(buffer, 'JPEG')
        posters.append(buffer.getvalue())
    images = [render_cover('A Media Library', 'Recently added', '#426bad', style, posters) for style in COVER_LAYOUTS]
    assert len(images) == len(set(images)) == 12
    for image in images:
        with Image.open(BytesIO(image)) as photo:
            assert photo.size == SIZE and photo.format == 'JPEG'
    assert images[list(COVER_LAYOUTS).index('minimal')] == render_cover(
        'A Media Library', 'Recently added', '#426bad', 'minimal', [])


def test_collage_stays_readable_on_mobile_and_sizes_single_poster_to_fill_artwork_area():
    sample_size = (264, 148)
    buffer = BytesIO()
    Image.new('RGB', (340, 510), '#e63432').save(buffer, 'JPEG')
    for layout in ('collage', 'grid'):
        image = render_cover('Animation', 'TV SHOWS', '#287e9c', layout,
                             [buffer.getvalue()], size=sample_size)
        with Image.open(BytesIO(image)) as cover:
            art = cover.crop((116, 0, sample_size[0], sample_size[1])).get_flattened_data()
            vivid = sum(r > 175 and g < 90 and b < 90 for r, g, b in art)
            assert vivid > sample_size[0] * sample_size[1] * .22
            text = cover.crop((12, 48, 116, 90))
            white_rows = sum(any(min(text.getpixel((x, y))) >= 225 for x in range(text.width))
                             for y in range(text.height))
            assert white_rows >= 15


def test_batch_uses_one_style_for_selected_libraries_and_saves_it_for_the_schedule(tmp_path):
    async def run():
        store = make_store(tmp_path)
        emby = Emby()
        app = FastAPI()

        async def admin():
            pass

        install_library_covers(app, store, admin)
        async with (httpx.AsyncClient(transport=httpx.MockTransport(emby)) as upstream,
                    httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://app') as client):
            app.state.http = upstream
            store.set_meta('worker_heartbeat', {'at': time.time()})
            listing = (await client.get(ROOT)).json()
            assert {style['id'] for style in listing['layouts']} == set(COVER_LAYOUTS)
            scope = listing['scope']
            covers = LibraryCovers(store, upstream)
            data = {'scope': scope, 'library_ids': ['21', '76', '21'], 'layout': 'minimal',
                    'color': '#446699', 'enabled': True, 'daily_at': '06:30'}
            invalid = await client.post(ROOT + '/batch', json={**data, 'library_ids': ['21', '800']})
            assert invalid.status_code == 409 and commands.running(store) is None
            submitted = (await client.post(ROOT + '/batch', json=data)).json()['items']
            assert [item['id'] for item in submitted] == ['21', '76']
            assert all(item['state'] == 'queued' for item in submitted)
            second_batch = (await client.post(ROOT + '/batch', json=data)).json()['items']
            assert all(item['state'] == 'skipped' for item in second_batch)
            ids = [item['command_id'] for item in submitted]
            assert all(json.loads(commands.get(store, command_id)['payload'])['style'] ==
                       {'layout': 'minimal', 'color': '#446699', 'posters': 6} for command_id in ids)
            params = {'scope': scope, 'command_ids': ','.join(map(str, ids))}
            waiting = (await client.get(ROOT + '/batch-status', params=params)).json()['items']
            assert all(item['state'] == 'queued' for item in waiting)

            stop = asyncio.Event()

            def after_upload():
                if len(emby.uploads) == 2:
                    stop.set()

            emby.on_upload = after_upload
            await asyncio.wait_for(command_loop(store, SimpleNamespace(library_covers=covers),
                                                stop, commands.COVER_OPERATIONS), timeout=8)
            done = (await client.get(ROOT + '/batch-status', params=params)).json()['items']
            assert {item['command_id'] for item in done} == set(ids)
            assert all(item['state'] == 'done' and item['outcome'] == 'updated' for item in done)
            assert [target for target, _ in emby.uploads] == ['/Items/21/Images/Primary', '/Items/76/Images/Primary']
            assert not emby.searches
            assert len({image for _, image in emby.uploads}) == 2
            saved = (await client.get(ROOT)).json()['settings']
            assert (saved['layout'], saved['daily_at'], saved['library_ids']) == ('minimal', '06:30', ['21', '76'])
        store.close()

    asyncio.run(run())
