"""Manage Emby library cards through its library and image APIs."""
import asyncio
import base64
import binascii
import hashlib
import json
import re
import time
from datetime import datetime
from typing import NamedTuple
from zoneinfo import ZoneInfo

import httpx
from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from . import commands
from .emby_connection import saved_base
from .library_cover_image import (
    COVER_LAYOUTS, MAX_IMAGE_BYTES, RENDER_VERSION, CoverColorMode, CoverLayout, cover_poster_limit,
    open_image, render_cover,
)
from .network import emby_request


LIBRARY_ID = re.compile(r'[A-Za-z0-9_-]{1,128}')
SETTINGS_KEY = 'emby_cover_studio'
LEGACY_SETTINGS_KEY = 'emby_library_covers'


class CoverError(ValueError):
    pass


class CoverArt(NamedTuple):
    item_id: str
    poster_tag: str = ''
    backdrop_tag: str = ''


class CoverStyle(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    layout: CoverLayout = 'collage'
    color: str = Field('#287e9c', pattern=r'^#[0-9a-fA-F]{6}$')
    color_mode: CoverColorMode = 'auto'
    prefer_poster: bool = True
    blur_background: bool = True
    posters: int = Field(6, ge=3, le=9)


class QueuedCoverSettings(CoverStyle):
    title: str = Field('', max_length=48)
    subtitle: str = Field('', max_length=30)
    enabled: bool = False
    daily_at: str = Field('04:00', pattern=r'^(?:[01]\d|2[0-3]):[0-5]\d$')


class CoverStudio(CoverStyle):
    library_ids: list[str] = Field(default_factory=list, max_length=100)
    enabled: bool = False
    daily_at: str = Field('04:00', pattern=r'^(?:[01]\d|2[0-3]):[0-5]\d$')


class ScopedStudio(CoverStudio):
    scope: str


class CoverBatch(ScopedStudio):
    library_ids: list[str] = Field(min_length=1, max_length=100)


class CoverGallery(CoverStyle):
    scope: str
    library_id: str
    layouts: list[CoverLayout] = Field(min_length=1, max_length=len(COVER_LAYOUTS))


def decode_image(image):
    try:
        data = base64.b64decode(image, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise CoverError('图片数据无效。') from exc
    if len(data) > MAX_IMAGE_BYTES:
        raise CoverError('图片超过 2 MB。')
    return data


class LibraryCovers:
    def __init__(self, store, http):
        self.store = store
        self.http = http

    def connection(self, expected_scope=None):
        pair = saved_base(self.store)
        if not pair:
            raise CoverError('请先配置 Emby 连接。')
        # The URL identifies the configured server without resetting settings when a token is rotated.
        configured = str(self.store.get('emby_url') or pair[0]).rstrip('/')
        scope = hashlib.sha256(configured.encode()).hexdigest()
        if expected_scope is not None and expected_scope != scope:
            raise CoverError('Emby 连接已变更，请重新读取媒体库。')
        return (*pair, scope)

    def studio(self, scope, default_ids=()):
        saved = (self.store.get(SETTINGS_KEY, {}) or {}).get(scope)
        if saved is not None:
            return CoverStudio.model_validate(saved)
        legacy = (self.store.get(LEGACY_SETTINGS_KEY, {}) or {}).get(scope) or {}
        enabled_ids = [key for key, value in legacy.items() if value.get('enabled')]
        first = legacy[enabled_ids[0]] if enabled_ids else next(iter(legacy.values()), {})
        common = {key: first[key] for key in (*CoverStyle.model_fields, 'daily_at') if key in first}
        return CoverStudio(**common, enabled=bool(enabled_ids), library_ids=list(enabled_ids or default_ids)[:100])

    def save_studio(self, scope, settings):
        with self.store.transaction(immediate=True):
            values = self.store.get(SETTINGS_KEY, {}) or {}
            values[scope] = settings.model_dump(exclude={'scope'})
            self.store.set_many({SETTINGS_KEY: values})

    def status(self, scope, library_id):
        row = self.store.db.execute(
            "SELECT id,state,message,result FROM worker_commands WHERE kind=? "
            "AND json_extract(payload,'$.scope')=? AND json_extract(payload,'$.library_id')=? "
            "ORDER BY id DESC LIMIT 1", (commands.LIBRARY_COVER, scope, library_id)).fetchone()
        return {**self.command_status(row), 'last': self.store.meta_json(self.key(scope, library_id), {})}

    @staticmethod
    def command_status(row):
        if not row:
            return {'state': 'idle', 'message': ''}
        result = json.loads(row['result'] or '{}') or {}
        return {'state': row['state'], 'message': row['message'] or result.get('message', ''),
                **({'outcome': result['outcome']} if 'outcome' in result else {})}

    def batch_status(self, scope, ids):
        rows = self.store.db.execute(
            "SELECT id,state,message,result FROM worker_commands WHERE kind=? "
            "AND json_extract(payload,'$.scope')=? "
            "AND id IN (SELECT value FROM json_each(?))",
            (commands.LIBRARY_COVER, scope, json.dumps(ids))).fetchall()
        return [{'command_id': row['id'], **self.command_status(row)} for row in rows]

    @staticmethod
    def key(scope, library_id):
        return f'emby-library-cover:{scope}:{library_id}'

    async def request(self, pair, method, path, *, params=None, upload=None):
        url, token, _ = pair
        payload = {'headers': {'X-Emby-Token': token}, 'params': params, 'timeout': 15}
        if upload is not None:
            payload['headers']['Content-Type'] = 'image/jpeg'
            payload['content'] = base64.b64encode(upload)
        try:
            return await emby_request(self.http, method, url + path, **payload)
        except httpx.HTTPError as exc:
            raise CoverError('访问 Emby 失败，请检查连接。') from exc

    async def json(self, pair, path, params=None):
        response = await self.request(pair, 'GET', path, params=params)
        if not response.is_success:
            raise CoverError(f'Emby 返回 HTTP {response.status_code}。')
        try:
            return response.json()
        except ValueError as exc:
            raise CoverError('Emby 返回了无法识别的数据。') from exc

    async def libraries(self, pair):
        data = await self.json(pair, '/Library/VirtualFolders')
        entries = data.get('Items') if isinstance(data, dict) else data
        if not isinstance(entries, list):
            raise CoverError('Emby 媒体库列表格式无效。')
        return {library_id: {'id': library_id, 'name': row.get('Name') or library_id,
                            'type': row.get('CollectionType') or ''}
                for row in entries if isinstance(row, dict)
                and LIBRARY_ID.fullmatch(library_id := str(row.get('ItemId') or row.get('Id') or ''))}

    async def library(self, pair, library_id):
        item = (await self.libraries(pair)).get(library_id)
        if not item:
            raise CoverError('Emby 中找不到这个媒体库，请刷新列表。')
        return item

    async def artwork(self, pair, item_id, *, width, tag=None, backdrop=False):
        params = {'MaxWidth': width, 'Format': 'jpg'}
        if tag:
            params['Tag'] = tag
        kind = 'Backdrop' if backdrop else 'Primary'
        if backdrop:
            params['Index'] = 0
        response = await self.request(pair, 'GET', f'/Items/{item_id}/Images/{kind}', params=params)
        if response.is_success and 0 < len(response.content) <= MAX_IMAGE_BYTES:
            return response.content
        return None

    async def poster_choices(self, pair, library, limit):
        if not limit:
            return []
        data = await self.json(pair, '/Items', {'ParentId': library['id'], 'Recursive': 'true',
            'IncludeItemTypes': 'Movie,Series', 'SortBy': 'DateCreated', 'SortOrder': 'Descending',
            'EnableImages': 'true', 'EnableImageTypes': 'Primary,Backdrop',
            'ImageTypeLimit': 1,
            'EnableUserData': 'false', 'Limit': limit * 4})
        if not isinstance(data, dict) or not isinstance(data.get('Items'), list):
            raise CoverError('Emby 作品列表格式无效。')
        choices = []
        for entry in data['Items']:
            if not isinstance(entry, dict):
                continue
            item_id = str(entry.get('Id') or '')
            tags = entry.get('ImageTags')
            tag = tags.get('Primary') if isinstance(tags, dict) else None
            scene_tags = entry.get('BackdropImageTags')
            scene = scene_tags[0] if isinstance(scene_tags, list) and scene_tags else ''
            if (tag or scene) and LIBRARY_ID.fullmatch(item_id):
                choices.append(CoverArt(item_id, tag or '', scene or ''))
                if len(choices) >= limit:
                    break
        return choices

    async def fetch_material(self, pair, item_id, tag, width, *, backdrop=False):
        if not tag:
            return None
        try:
            image = await self.artwork(pair, item_id, width=width, tag=tag, backdrop=backdrop)
            if image:
                await asyncio.to_thread(open_image, image)
            return image
        except (CoverError, ValueError):
            return None

    async def fetch_posters(self, pair, choices, width, *, prefer_poster=True):
        sem = asyncio.Semaphore(3)

        async def fetch(choice):
            order = ((choice.poster_tag, False), (choice.backdrop_tag, True))
            if not prefer_poster:
                order = tuple(reversed(order))
            available = [(tag, backdrop) for tag, backdrop in order if tag]
            async with sem:
                for index, (tag, backdrop) in enumerate(available):
                    image = await self.fetch_material(pair, choice.item_id, tag, width, backdrop=backdrop)
                    if image:
                        return image, index == 0
            return None, False

        results = await asyncio.gather(*(fetch(item) for item in choices))
        posters = [image for image, _ in results if image]
        if choices and not posters:
            raise CoverError('没有读到可用的作品海报，请稍后重试。')
        return posters, all(preferred for _, preferred in results)

    async def fetch_backdrop(self, pair, choice):
        if choice and choice.poster_tag:
            return await self.fetch_material(pair, choice.item_id, choice.backdrop_tag, 1280, backdrop=True)

    @staticmethod
    def titles(library):
        return library['name'], {'movies': 'MOVIES', 'tvshows': 'TV SHOWS'}.get(library['type'], '')

    async def generate(self, pair, library, settings, previous=''):
        title, subtitle = self.titles(library)
        if isinstance(settings, QueuedCoverSettings):
            title, subtitle = settings.title or title, settings.subtitle or subtitle
        wants_scene = settings.layout == 'cinema-cut'
        choices = await self.poster_choices(
            pair, library, cover_poster_limit(settings.layout, settings.posters, settings.color_mode))
        scene_source = choices[0] if wants_scene and settings.prefer_poster and choices else None
        inputs = [RENDER_VERSION, title, subtitle, settings.color, settings.color_mode, settings.layout,
                  settings.prefer_poster, settings.blur_background, choices]
        signature = hashlib.sha256(json.dumps(inputs, ensure_ascii=False).encode()).hexdigest()
        if previous == signature:
            return None, signature, len(choices)
        width = 1280 if settings.layout in ('panorama', 'cinema-cut') else 640 if len(choices) <= 3 else 360
        (posters, preferred), scene = await asyncio.gather(
            self.fetch_posters(pair, choices, width, prefer_poster=settings.prefer_poster),
            self.fetch_backdrop(pair, scene_source))
        image = await asyncio.to_thread(render_cover, title, subtitle, settings.color, settings.layout,
                                        posters, scene=scene, color_mode=settings.color_mode,
                                        blur_background=settings.blur_background)
        all_art = preferred and (
            not scene_source or not scene_source.poster_tag or not scene_source.backdrop_tag or scene)
        return image, signature if all_art else '', len(posters)

    async def gallery(self, pair, library, options):
        layouts = list(dict.fromkeys(options.layouts))
        limit = max(cover_poster_limit(key, options.posters, options.color_mode) for key in layouts)
        wants_scene = 'cinema-cut' in layouts
        choices = await self.poster_choices(pair, library, limit)
        (posters, _), scene = await asyncio.gather(
            self.fetch_posters(pair, choices, 640, prefer_poster=options.prefer_poster),
            self.fetch_backdrop(pair, choices[0] if wants_scene and options.prefer_poster and choices else None))
        title, subtitle = self.titles(library)

        def render():
            return [{'id': key, 'image': base64.b64encode(
                render_cover(title, subtitle, options.color, key, posters, size=(640, 360),
                             scene=scene if key == 'cinema-cut' else None, color_mode=options.color_mode,
                             blur_background=options.blur_background)).decode()}
                    for key in layouts]

        return {'items': await asyncio.to_thread(render), 'poster_count': len(posters)}

    async def apply(self, payload):
        library_id = payload['library_id']
        pair = self.connection(payload['scope'])
        scope = pair[2]
        library = await self.library(pair, library_id)
        key = self.key(scope, library_id)
        previous = self.store.meta_json(key, {}) or {}
        signature = ''
        generate = 'image' not in payload
        if 'style' in payload:
            settings = CoverStyle.model_validate(payload['style'])
        elif payload.get('automatic'):
            settings = self.studio(scope)
            if not settings.enabled or library_id not in settings.library_ids:
                return {'outcome': 'skipped', 'message': '该库的定时更新已关闭。'}
        elif generate:
            settings = QueuedCoverSettings.model_validate(payload['settings'])
        if generate:
            image, signature, _ = await self.generate(pair, library, settings, previous.get('signature', ''))
            if image is None:
                return {'outcome': 'unchanged', 'message': '入选海报和样式没有变化。'}
        else:
            image = decode_image(payload['image'])
        image_hash = hashlib.sha256(image).hexdigest()
        # A manual apply is explicit. Automatic refresh leaves a valid unchanged cover alone.
        unchanged = generate and previous.get('image_hash') == image_hash
        if not unchanged:
            self.connection(scope)
            result = await self.request(pair, 'POST', f'/Items/{library_id}/Images/Primary', upload=image)
            if not result.is_success:
                raise CoverError(f'Emby 拒绝应用封面（HTTP {result.status_code}）。')
        self.store.set_meta(key, {'signature': signature, 'image_hash': image_hash,
                                  'applied_at': previous.get('applied_at') if unchanged else time.time()})
        return {'outcome': 'unchanged' if unchanged else 'updated',
                'message': '封面画面没有变化。' if unchanged else '媒体库封面已应用到 Emby。'}

    def enqueue(self, scope, library_id, *, scheduled_day=None, **options):
        payload = {'scope': scope, 'library_id': library_id, **options}
        with self.store.transaction(immediate=True):
            daily_key = self.key(scope, library_id) + ':daily'
            if scheduled_day and self.store.meta_json(daily_key) == scheduled_day:
                return None
            previous = self.store.db.execute(
                "SELECT id FROM worker_commands WHERE kind=? AND state IN ('queued','running') "
                "AND json_extract(payload,'$.scope')=? AND json_extract(payload,'$.library_id')=?",
                (commands.LIBRARY_COVER, scope, library_id)).fetchone()
            if previous:
                return None
            command_id = commands.submit(self.store, commands.LIBRARY_COVER, payload)
            if scheduled_day:
                self.store.set_meta(daily_key, scheduled_day)
            return command_id

    def schedule(self, now=None):
        pair = saved_base(self.store)
        if not pair:
            return 0
        scope = self.connection()[2]
        zone = ZoneInfo(self.store.get('timezone') or 'Asia/Shanghai')
        now = now or datetime.now(zone)
        day, clock = now.strftime('%Y-%m-%d'), now.strftime('%H:%M')
        settings = self.studio(scope)
        queued = 0
        if not settings.enabled or settings.daily_at > clock:
            return queued
        for library_id in settings.library_ids:
            if self.enqueue(scope, library_id, scheduled_day=day, automatic=True):
                queued += 1
        return queued

    async def schedule_loop(self):
        while True:
            try:
                await asyncio.to_thread(self.schedule)
            except Exception as exc:
                self.store.event('cover', f'媒体库封面调度失败 · {type(exc).__name__}')
            await asyncio.sleep(30)


def install_library_covers(app, store, admin):
    prefix = '/api/v1/emby/library-covers'

    def covers():
        return LibraryCovers(store, app.state.http)

    async def available(service, scope=None):
        try:
            pair = service.connection(scope)
            return pair, await service.libraries(pair)
        except CoverError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get(prefix, dependencies=[Depends(admin)])
    async def library_covers(service: LibraryCovers = Depends(covers)):
        pair, items = await available(service)
        scope = pair[2]
        settings = service.studio(scope, items)
        settings.library_ids = [key for key in settings.library_ids if key in items]
        return {'scope': scope, 'timezone': store.get('timezone') or 'Asia/Shanghai',
                'settings': settings.model_dump(),
                'layouts': [{'id': key, 'name': name, 'max_posters': limit}
                            for key, (name, limit) in COVER_LAYOUTS.items()],
                'items': [{**row, 'status': service.status(scope, key)} for key, row in items.items()]}

    @app.put(prefix + '/settings', dependencies=[Depends(admin)])
    async def studio_settings(body: ScopedStudio, service: LibraryCovers = Depends(covers)):
        _, libraries = await available(service, body.scope)
        body.library_ids = list(dict.fromkeys(body.library_ids))
        if any(key not in libraries for key in body.library_ids):
            raise HTTPException(409, '部分媒体库已变更，请刷新列表后重新选择。')
        if body.enabled and not body.library_ids:
            raise HTTPException(422, '开启定时更新前请选择媒体库。')
        service.save_studio(body.scope, body)
        return {'settings': body.model_dump(exclude={'scope'})}

    @app.post(prefix + '/gallery', dependencies=[Depends(admin)])
    async def cover_gallery(body: CoverGallery, service: LibraryCovers = Depends(covers)):
        pair, libraries = await available(service, body.scope)
        library = libraries.get(body.library_id)
        if not library:
            raise HTTPException(409, 'Emby 中找不到这个媒体库，请刷新列表。')
        try:
            return await service.gallery(pair, library, body)
        except CoverError as exc:
            raise HTTPException(502, str(exc)) from exc

    @app.post(prefix + '/batch', dependencies=[Depends(admin)])
    async def batch_covers(body: CoverBatch, service: LibraryCovers = Depends(covers)):
        _, libraries = await available(service, body.scope)
        library_ids = list(dict.fromkeys(body.library_ids))
        if any(key not in libraries for key in library_ids):
            raise HTTPException(409, '部分媒体库已变更，请刷新列表后重新选择。')
        if not commands.worker_ready(store):
            raise HTTPException(503, '后台 Worker 未就绪，请稍后重试。')

        items = []
        style = body.model_dump(include=set(CoverStyle.model_fields))
        with store.transaction(immediate=True):
            for key in library_ids:
                command_id = service.enqueue(body.scope, key, style=style)
                items.append({**libraries[key], 'command_id': command_id,
                              'state': 'queued' if command_id else 'skipped',
                              'message': '' if command_id else '已有封面任务在处理。'})
            body.library_ids = library_ids
            service.save_studio(body.scope, body)
        return {'items': items}

    @app.get(prefix + '/batch-status', dependencies=[Depends(admin)])
    async def batch_cover_status(scope: str, command_ids: str, service: LibraryCovers = Depends(covers)):
        if not re.fullmatch(r'[0-9]{1,19}(?:,[0-9]{1,19}){0,99}', command_ids):
            raise HTTPException(422, '封面任务编号无效。')
        try:
            service.connection(scope)
        except CoverError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {'items': service.batch_status(scope, [int(item) for item in command_ids.split(',')])}
