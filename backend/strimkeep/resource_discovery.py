"""Read-only resource search across configured sites and Telegram channels."""
import asyncio
import base64
import ipaddress
import json
import re
from itertools import zip_longest
from typing import Literal
from urllib.parse import urlsplit

import httpx
from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .channel_monitor import normalize_channel
from .resource_inbox import candidates
from .telegram_session import SessionError


SETTING = 'resource_search_sources'
MAX_SITES = 8
MAX_CHANNELS = 8
MAX_RESULTS = 100
MAX_RESPONSE_BYTES = 1024 * 1024
SEARCH_TIMEOUT = 10
PAN_TYPES = {'115': 'share', 'magnet': 'magnet', 'ed2k': 'ed2k'}
LABELS = {'share': '115 网盘', 'magnet': '磁力', 'ed2k': 'ED2K'}


def validate_base(value):
    value = value.strip().rstrip('/')
    if not value or re.search(r'[\s\\]', value):
        raise ValueError('请输入有效的服务地址。')
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError('服务地址无效。') from None
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or parsed.path or (port is not None and not 1 <= port <= 65535)):
        raise ValueError('请填写不含路径和凭据的 HTTP(S) 服务地址。')
    host = parsed.hostname.lower()
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address and (address.is_link_local or address.is_unspecified or address.is_multicast
                    or address.is_reserved):
        raise ValueError('请选择可访问的站点地址。')
    if parsed.scheme == 'http' and not (host == 'localhost' or
                                         (address and (address.is_private or address.is_loopback))):
        raise ValueError('公网或域名地址请使用 HTTPS。')
    try:
        return str(httpx.URL(value)).rstrip('/')
    except httpx.InvalidURL:
        raise ValueError('服务地址无效。') from None


class SearchSite(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    kind: Literal['pansou'] = 'pansou'
    name: str = Field(min_length=1, max_length=60)
    base_url: str = Field(min_length=1, max_length=300)
    enabled: bool = True

    @model_validator(mode='after')
    def validate_site(self):
        self.base_url = validate_base(self.base_url)
        return self


class DiscoveryConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')
    telegram_channels: list[str] = Field(default_factory=list, max_length=MAX_CHANNELS)
    sites: list[SearchSite] = Field(default_factory=list, max_length=MAX_SITES)

    @model_validator(mode='after')
    def unique_sources(self):
        self.telegram_channels = list(dict.fromkeys(
            normalize_channel(channel, strict=True) for channel in self.telegram_channels))
        if (len({site.base_url for site in self.sites}) != len(self.sites)
                or len({site.name.casefold() for site in self.sites}) != len(self.sites)):
            raise ValueError('站点名称和服务地址都不能重复。')
        return self


class SearchInput(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=200)
    tmdb_id: int = Field(gt=0)
    media_type: str = Field(pattern='^(movie|tv)$')
    year: str = Field(default='', pattern='^(|[0-9]{4})$')


class SourceError(ValueError):
    pass


def resource_items(text, title, source, *, kind=None, published_at=''):
    """Accept the link types our existing transfer and offline routes support."""
    out = []
    for key, url, link_type in candidates(text[:16000]):
        if link_type not in LABELS or (kind and kind != link_type) or len(url) > 4096:
            continue
        if link_type == 'magnet':
            match = re.fullmatch(r'magnet:([0-9a-f]{40}|[a-z2-7]{32})', key)
            if not match:
                continue
            if len(match[1]) == 32:
                key = 'magnet:' + base64.b32decode(match[1].upper()).hex()
        elif link_type == 'ed2k' and not key.startswith('ed2k:'):
            continue
        out.append({
            'key': key, 'link': url, 'link_type': link_type, 'title': title[:200],
            'source_names': [source], 'provider_label': LABELS[link_type],
            'remark': text[:400] if kind is None else '', 'published_at': published_at[:64],
        })
    return out[:20]


async def read_response(http, method, url, **kwargs):
    async with http.stream(method, url, **kwargs,
                           timeout=SEARCH_TIMEOUT, follow_redirects=False) as response:
        if response.status_code in (401, 403):
            raise SourceError('站点拒绝访问，请检查授权与接口权限。')
        if response.status_code != 200:
            raise SourceError(f'站点返回 HTTP {response.status_code}。')
        body = bytearray()
        async for chunk in response.aiter_bytes(chunk_size=65536):
            if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                raise SourceError('站点返回的结果过大。')
            body.extend(chunk)
    return body


async def search_pansou(http, site, title):
    payload = {'kw': title, 'res': 'merge', 'cloud_types': list(PAN_TYPES)}
    data = json.loads(await read_response(http, 'POST', site.base_url + '/api/search', json=payload))
    if isinstance(data, dict) and 'code' in data:
        data = data.get('data') if data['code'] == 0 else None
    merged = data.get('merged_by_type') if isinstance(data, dict) else None
    # Servers omit the merged field when no links match the requested disk types.
    if merged is None and isinstance(data, dict) and type(data.get('total')) is int:
        return []
    if not isinstance(merged, dict):
        raise SourceError('返回格式不符合 PanSou 搜索接口。')
    items = []
    for disk_type, kind in PAN_TYPES.items():
        entries = merged.get(disk_type)
        if not isinstance(entries, list):
            continue
        for entry in entries[:MAX_RESULTS + 1]:
            if not isinstance(entry, dict) or not isinstance(entry.get('url'), str):
                continue
            url = entry['url']
            password = entry.get('password')
            text = f'{url} 提取码：{password}' if kind == 'share' and isinstance(password, str) else url
            label = str(entry.get('note') or title)
            items.extend(resource_items(text, label, site.name, kind=kind,
                                        published_at=str(entry.get('datetime') or '')))
    return items


async def search_channel(telegram, channel, source, title):
    messages = await telegram.search_channel(channel, title)
    items = []
    for message in messages:
        text = message['text']
        label = next((line.strip() for line in text.splitlines() if line.strip()), title)
        # Some channel posts only have an inline button or an embedded text link.
        content = '\n'.join([text, *message['links']])
        items.extend(resource_items(content, label, source, published_at=message['published_at']))
    return items


async def search_source(name, search):
    try:
        async with asyncio.timeout(SEARCH_TIMEOUT):
            items = await search
        return {'name': name, 'found': len({item['key'] for item in items})}, items
    except (TimeoutError, httpx.TimeoutException):
        error = '搜索超时。'
    except SourceError as exc:
        error = str(exc)
    except SessionError as exc:
        error = str(exc)
    except (ValueError, httpx.HTTPError):
        error = '站点连接失败或返回了无法读取的结果。'
    return {'name': name, 'error': error}, []


def merge_results(groups):
    merged = {}
    truncated = False
    for row in zip_longest(*groups):
        for item in filter(None, row):
            key = item['key']
            current = merged.get(key)
            if current is not None:
                current['source_names'] = list(dict.fromkeys(current['source_names'] + item['source_names']))
                if '?password=' in item['link'] and '?password=' not in current['link']:
                    current['link'] = item['link']
            elif len(merged) < MAX_RESULTS:
                merged[key] = item
            else:
                truncated = True
    return list(merged.values()), truncated


def discovery_config(store):
    return DiscoveryConfig.model_validate(store.get(SETTING, {}))


async def discover_resources(store, http, telegram, title):
    cfg = discovery_config(store)
    titles = {row['name']: row['title'] for row in
              store.db.execute('SELECT name,title FROM channel_state')}
    searches = [search_source(site.name, search_pansou(http, site, title))
                for site in cfg.sites if site.enabled]
    for channel in cfg.telegram_channels:
        label = titles.get(channel) or channel
        searches.append(search_source(label, search_channel(telegram, channel, label, title)))
    if not searches:
        return {'items': [], 'sources': [], 'truncated': False}
    status, groups = zip(*(await asyncio.gather(*searches)))
    items, truncated = merge_results(groups)
    return {'items': items, 'sources': status, 'truncated': truncated}


def install_resource_discovery(app, store, admin):
    store.forget(('resource_discovery_base', 'resource_discovery_token'))
    saved = store.get(SETTING, {})
    old_sites = saved.get('sites', [])
    sites = [{key: value for key, value in site.items() if key != 'api_key'}
             for site in old_sites if site.get('kind') != 'torznab']
    if 're0' in saved or sites != old_sites:
        saved.pop('re0', None)
        saved['sites'] = sites
        store.set_many({SETTING: saved})

    def channel_options():
        monitor = app.state.organizer.channels
        titles = {row['name']: row['title'] for row in
                  store.db.execute('SELECT name,title FROM channel_state')}
        return [{'name': row['name'], 'title': titles.get(row['name']) or row['name']}
                for row in monitor.config()['channels']]

    @app.get('/api/v1/explore/resource-config', dependencies=[Depends(admin)])
    async def config():
        cfg = discovery_config(store)
        return {
            'telegram_channels': cfg.telegram_channels, 'channels': channel_options(),
            'telegram_logged_in': app.state.organizer.telegram.status()['logged_in'],
            'sites': [site.model_dump() for site in cfg.sites],
        }

    @app.put('/api/v1/explore/resource-config', dependencies=[Depends(admin)])
    async def save(body: DiscoveryConfig):
        store.set_many({SETTING: body.model_dump()})
        return await config()

    @app.post('/api/v1/explore/resources', dependencies=[Depends(admin)])
    async def search(body: SearchInput):
        results = await discover_resources(store, app.state.http, app.state.organizer.telegram, body.title)
        if not results['sources']:
            raise HTTPException(409, '请在「系统设置 → 资源来源」添加站点或选择要搜索的 Telegram 频道。')
        return results
