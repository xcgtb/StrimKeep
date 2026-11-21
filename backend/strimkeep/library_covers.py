"""Manage Emby library cards through its library and image APIs."""
import asyncio
import base64
import binascii
import hashlib
import json
import re
import time
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

import httpx
from fastapi import Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import commands
from .emby_connection import saved_base
from .library_cover_image import MAX_IMAGE_BYTES, SIZE, normalize_upload, open_image, render_cover
from .network import emby_request


MAX_COVER_REQUEST = 3 * 1024 * 1024
LIBRARY_ID = re.compile(r'[A-Za-z0-9_-]{1,128}')
SETTINGS_KEY = 'emby_library_covers'


class CoverError(ValueError):
    pass


class CoverSettings(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    title: str = Field('', max_length=48)
    subtitle: str = Field('', max_length=30)
    layout: Literal['collage', 'grid'] = 'collage'
    color: str = Field('#287e9c', pattern=r'^#[0-9a-fA-F]{6}$')
    posters: int = Field(6, ge=3, le=9)
    enabled: bool = False
    daily_at: str = Field('04:00', pattern=r'^(?:[01]\d|2[0-3]):[0-5]\d$')


class ScopedSettings(CoverSettings):
    scope: str


class CoverImage(BaseModel):
    model_config = ConfigDict(extra='forbid')
    scope: str
    image: str = Field(min_length=4, max_length=((MAX_IMAGE_BYTES + 2) // 3) * 4)


def is_cover_image_request(path):
    return bool(re.fullmatch(r'/api/v1/emby/library-covers/[A-Za-z0-9_-]{1,128}/(?:upload-preview|apply)', path))


async def image_input(request):
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_COVER_REQUEST:
            raise HTTPException(413, '图片请求过大。')
        body.extend(chunk)
    try:
        return CoverImage.model_validate_json(body)
    except ValidationError as exc:
        raise HTTPException(422, '图片或媒体库参数无效。') from exc


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

    def load_settings(self, scope):
        return (self.store.get(SETTINGS_KEY, {}) or {}).get(scope) or {}

    def settings(self, scope, library_id):
        return CoverSettings.model_validate(self.load_settings(scope).get(library_id) or {})

    def save(self, scope, library_id, settings):
        with self.store.transaction(immediate=True):
            values = self.store.get(SETTINGS_KEY, {}) or {}
            values.setdefault(scope, {})[library_id] = settings.model_dump(exclude={'scope'})
            self.store.set_many({SETTINGS_KEY: values})

    def status(self, scope, library_id):
        row = self.store.db.execute(
            "SELECT id,state,message,result FROM worker_commands WHERE kind=? "
            "AND json_extract(payload,'$.scope')=? AND json_extract(payload,'$.library_id')=? "
            "ORDER BY id DESC LIMIT 1", (commands.LIBRARY_COVER, scope, library_id)).fetchone()
        return {'state': row['state'] if row else 'idle',
                'message': (row['message'] or (json.loads(row['result'] or '{}') or {}).get('message', '')) if row else '',
                'last': self.store.meta_json(self.key(scope, library_id), {})}

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

    async def artwork(self, pair, item_id, *, width, tag=None):
        params = {'MaxWidth': width, 'Format': 'jpg'}
        if tag:
            params['Tag'] = tag
        response = await self.request(pair, 'GET', f'/Items/{item_id}/Images/Primary', params=params)
        if response.is_success and 0 < len(response.content) <= MAX_IMAGE_BYTES:
            return response.content
        return None

    async def poster_choices(self, pair, library, settings):
        data = await self.json(pair, '/Items', {'ParentId': library['id'], 'Recursive': 'true',
            'IncludeItemTypes': 'Movie,Series', 'SortBy': 'DateCreated', 'SortOrder': 'Descending',
            'EnableImages': 'true', 'EnableImageTypes': 'Primary', 'ImageTypeLimit': 1,
            'EnableUserData': 'false', 'Limit': settings.posters * 4})
        if not isinstance(data, dict) or not isinstance(data.get('Items'), list):
            raise CoverError('Emby 作品列表格式无效。')
        choices = []
        for entry in data['Items']:
            if not isinstance(entry, dict):
                continue
            item_id = str(entry.get('Id') or '')
            tags = entry.get('ImageTags')
            tag = tags.get('Primary') if isinstance(tags, dict) else None
            if tag and LIBRARY_ID.fullmatch(item_id):
                choices.append((item_id, tag))
                if len(choices) >= settings.posters:
                    break
        return choices

    async def generate(self, pair, library, settings, previous=''):
        title = settings.title or library['name']
        subtitle = settings.subtitle or {'movies': 'MOVIES', 'tvshows': 'TV SHOWS'}.get(library['type'], '')
        choices = await self.poster_choices(pair, library, settings)
        signature = hashlib.sha256(json.dumps(
            [title, subtitle, settings.color, settings.layout, choices], ensure_ascii=False).encode()).hexdigest()
        if previous == signature:
            return None, signature, len(choices)
        # Fetch one small set of images concurrently, keeping order from DateCreated.
        sem = asyncio.Semaphore(3)

        async def fetch(item_id, tag):
            async with sem:
                try:
                    image = await self.artwork(pair, item_id, width=360, tag=tag)
                    if image:
                        await asyncio.to_thread(open_image, image)
                    return image
                except (CoverError, ValueError):
                    return None

        posters = [image for image in await asyncio.gather(*(fetch(*item) for item in choices)) if image]
        if choices and not posters:
            raise CoverError('没有读到可用的作品海报，请稍后重试。')
        image = await asyncio.to_thread(render_cover, title, subtitle, settings.color, settings.layout, posters)
        return image, signature if len(choices) == len(posters) else '', len(posters)

    async def apply(self, payload):
        library_id = payload['library_id']
        pair = self.connection(payload['scope'])
        scope = pair[2]
        library = await self.library(pair, library_id)
        key = self.key(scope, library_id)
        previous = self.store.meta_json(key, {}) or {}
        signature = ''
        if payload.get('automatic'):
            settings = self.settings(scope, library_id)
            if not settings.enabled:
                return {'message': '该库的定时更新已关闭。'}
            image, signature, _ = await self.generate(pair, library, settings, previous.get('signature', ''))
            if image is None:
                return {'message': '入选海报和样式没有变化。'}
        else:
            image = decode_image(payload['image'])
        image_hash = hashlib.sha256(image).hexdigest()
        # A manual apply is explicit. Automatic refresh leaves a valid unchanged cover alone.
        unchanged = payload.get('automatic') and previous.get('image_hash') == image_hash
        if not unchanged:
            self.connection(scope)
            result = await self.request(pair, 'POST', f'/Items/{library_id}/Images/Primary', upload=image)
            if not result.is_success:
                raise CoverError(f'Emby 拒绝应用封面（HTTP {result.status_code}）。')
        self.store.set_meta(key, {'signature': signature, 'image_hash': image_hash,
                                  'applied_at': previous.get('applied_at') if unchanged else time.time()})
        return {'message': '封面画面没有变化。' if unchanged else '媒体库封面已应用到 Emby。'}

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
        queued = 0
        for library_id, raw in self.load_settings(scope).items():
            settings = CoverSettings.model_validate(raw)
            if not settings.enabled or settings.daily_at > clock:
                continue
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

    async def checked(service, library_id, scope):
        try:
            pair = service.connection(scope)
            return pair, await service.library(pair, library_id)
        except CoverError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get(prefix, dependencies=[Depends(admin)])
    async def library_covers(service: LibraryCovers = Depends(covers)):
        try:
            pair = service.connection()
            items = await service.libraries(pair)
        except CoverError as exc:
            raise HTTPException(409, str(exc)) from exc
        scope = pair[2]
        return {'scope': scope, 'timezone': store.get('timezone') or 'Asia/Shanghai',
                'items': [{**row, 'settings': service.settings(scope, key).model_dump(),
                           'status': service.status(scope, key)} for key, row in items.items()]}

    @app.get(prefix + '/{library_id}/status', dependencies=[Depends(admin)])
    async def cover_status(library_id: str, scope: str, service: LibraryCovers = Depends(covers)):
        try:
            service.connection(scope)
        except CoverError as exc:
            raise HTTPException(409, str(exc)) from exc
        return service.status(scope, library_id)

    @app.get(prefix + '/{library_id}/current', dependencies=[Depends(admin)])
    async def current_cover(library_id: str, scope: str, service: LibraryCovers = Depends(covers)):
        pair, _ = await checked(service, library_id, scope)
        try:
            image = await service.artwork(pair, library_id, width=640)
        except CoverError as exc:
            raise HTTPException(502, str(exc)) from exc
        if not image or not image.startswith(b'\xff\xd8'):
            raise HTTPException(404, '该库还没有主图。')
        return Response(image, media_type='image/jpeg', headers={'Cache-Control': 'no-store'})

    @app.put(prefix + '/{library_id}/settings', dependencies=[Depends(admin)])
    async def save_cover(library_id: str, body: ScopedSettings, service: LibraryCovers = Depends(covers)):
        await checked(service, library_id, body.scope)
        service.save(body.scope, library_id, body)
        return {'settings': service.settings(body.scope, library_id).model_dump()}

    @app.post(prefix + '/{library_id}/preview', dependencies=[Depends(admin)])
    async def preview_cover(library_id: str, body: ScopedSettings, service: LibraryCovers = Depends(covers)):
        pair, library = await checked(service, library_id, body.scope)
        try:
            image, _, count = await service.generate(pair, library, body)
        except CoverError as exc:
            raise HTTPException(502, str(exc)) from exc
        return {'image': base64.b64encode(image).decode(), 'poster_count': count}

    @app.post(prefix + '/{library_id}/upload-preview', dependencies=[Depends(admin)])
    async def preview_upload(library_id: str, request: Request, service: LibraryCovers = Depends(covers)):
        body = await image_input(request)
        await checked(service, library_id, body.scope)
        try:
            image = await asyncio.to_thread(normalize_upload, decode_image(body.image))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {'image': base64.b64encode(image).decode()}

    @app.post(prefix + '/{library_id}/apply', dependencies=[Depends(admin)])
    async def apply_cover(library_id: str, request: Request, service: LibraryCovers = Depends(covers)):
        body = await image_input(request)
        await checked(service, library_id, body.scope)
        try:
            image = decode_image(body.image)
            parsed = await asyncio.to_thread(open_image, image)
            if parsed.size != SIZE or not image.startswith(b'\xff\xd8'):
                raise CoverError('请先生成 16:9 封面预览。')
            if not getattr(app.state.organizer, 'web_only', False):
                result = await service.apply({'scope': body.scope, 'library_id': library_id, 'image': body.image})
                return {'state': 'done', **result}
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if not commands.worker_ready(store):
            raise HTTPException(503, '后台 Worker 未就绪，请稍后重试。')
        command_id = service.enqueue(body.scope, library_id, image=body.image)
        if command_id is None:
            raise HTTPException(409, '该媒体库已有封面任务，请等待处理完成。')
        return {'state': 'queued', 'command_id': command_id, 'message': '封面已提交后台。'}
