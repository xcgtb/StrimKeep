"""Optional read-only  resource discovery through its documented API."""
import ipaddress
import re
from urllib.parse import urlsplit

import httpx
from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .transfer import shares


def validate_base(value):
    value = str(value or '').strip().rstrip('/')
    if not value:
        return ''
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError('服务地址无效。') from None
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or
            parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/') or
            (port is not None and not 1 <= port <= 65535)):
        raise ValueError('请填写不含路径和凭据的 HTTP(S) 服务地址。')
    if parsed.scheme == 'http':
        try:
            host = ipaddress.ip_address(parsed.hostname)
            local = host.is_private or host.is_loopback
        except ValueError:
            local = parsed.hostname.lower() == 'localhost'
        if not local:
            raise ValueError('非内网地址必须使用 HTTPS。')
    return value


class DiscoveryConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')
    base_url: str = Field(default='', max_length=300)
    token: str = Field(default='', max_length=512)
    clear_token: bool = False


class SearchInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    title: str = Field(min_length=1, max_length=200)
    tmdb_id: int = Field(gt=0)
    media_type: str = Field(pattern='^(movie|tv)$')
    year: str = Field(default='', pattern='^(|[0-9]{4})$')
    sources: list[str] = Field(default_factory=lambda: ['re0'], max_length=3)


class ResolveInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    slug: str = Field(min_length=1, max_length=300)
    provider: str = Field(pattern='^115$')
    source: str = Field(pattern='^re0$')


# 资源卡要能看清「这个版本到底是什么」，上游给的备注等字段必须原样同步过来：
# remark 是发布者写的版本说明（字幕、修复、来源片源都在这句里），以前被白名单丢掉，
# 界面上就只剩标题和体积，根本判断不了资源详情。
TEXT_FIELDS = ('title', 'source', 'provider', 'provider_label', 'resolution', 'size', 'release_group',
               'link_type', 'validate_status', 'quality', 'slug', 'sharer', 'remark', 'message_text',
               'message_body', 'published_at', 'resource_specification', 'validate_message',
               'validation_label', 'channel_title')
LIST_FIELDS = ('resource_spec_tags', 'technical_tags', 'subtitle_languages', 'subtitle_types',
               'resolution_tags', 'source_tags', 'supported_targets')
FLAG_FIELDS = ('is_unlocked', 'is_official', 'dolby_vision', 'validation_known', 'unlocked_users_known')
SPEC_KEYS = ('resource_pix', 'web_source', 'resource_type', 'resource_effect', 'audio_encode',
             'resource_team')


def clean_item(item):
    if not isinstance(item, dict):
        return None
    result = {}
    for key in TEXT_FIELDS:
        value = item.get(key)
        result[key] = (str(value)[:300]
                       if isinstance(value, (str, int, float)) and not isinstance(value, bool) else '')
    for key in FLAG_FIELDS:
        result[key] = item.get(key) is True
    for key in LIST_FIELDS:
        raw = item.get(key)
        result[key] = [str(x)[:80] for x in raw[:12] if isinstance(x, str)] if isinstance(raw, list) else []
    specs = item.get('resource_specs')
    result['resource_specs'] = ({k: specs[k][:80] for k in SPEC_KEYS
                                 if isinstance(specs.get(k), str) and specs[k]}
                                if isinstance(specs, dict) else {})
    episode = item.get('episode') if isinstance(item.get('episode'), dict) else {}
    result['episode'] = {}
    for key in ('season_num', 'episode_num', 'end_episode_num'):
        if type(episode.get(key)) is int and 0 <= episode[key] <= 10000:
            result['episode'][key] = episode[key]
    for key in ('kind', 'confidence'):
        if isinstance(episode.get(key), str):
            result['episode'][key] = episode[key][:40]
    count = item.get('unlocked_users_count')
    result['unlocked_users_count'] = count if type(count) is int and 0 <= count <= 10_000_000 else None
    points = item.get('unlock_points') if item.get('points_known') is True else None
    result['unlock_points'] = points if type(points) in (int, float) and 0 <= points <= 100000 else None
    # Only links already present in a result can be queued. Never call the
    # provider's copy-link endpoint, which may unlock resources at a cost.
    text = ' '.join(str(item.get(k) or '')[:10000] for k in ('message_text', 'message_body', 'url', 'share_url'))
    try:
        found = shares(text)
    except ValueError:
        found = []
    result['share_url'] = ('https://115.com/s/' + found[0]['share_code'] +
        ('?password=' + found[0]['receive_code'] if found[0]['receive_code'] else '')) if len(found) == 1 and result['provider'] == '115' else ''
    return result


def install_resource_discovery(app, store, admin):
    @app.get('/api/v1/explore/resource-config', dependencies=[Depends(admin)])
    async def config():
        return {'base_url': store.get('resource_discovery_base', ''),
                'token_configured': bool(store.get('resource_discovery_token', ''))}

    @app.put('/api/v1/explore/resource-config', dependencies=[Depends(admin)])
    async def save(body: DiscoveryConfig):
        try:
            base = validate_base(body.base_url)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        token = body.token.strip()
        if token and (re.search(r'[\x00-\x20\x7f]', token) or body.clear_token):
            raise HTTPException(422, '令牌格式无效。')
        old_base = store.get('resource_discovery_base', '')
        values = {'resource_discovery_base': base}
        if body.clear_token or (base != old_base and not token): values['resource_discovery_token'] = ''
        elif token: values['resource_discovery_token'] = token
        store.set_many(values)
        return await config()

    @app.post('/api/v1/explore/resources', dependencies=[Depends(admin)])
    async def search(body: SearchInput):
        base, token = store.get('resource_discovery_base', ''), store.get('resource_discovery_token', '')
        if not base or not token:
            raise HTTPException(409, '请先配置  服务地址和 OpenAPI 令牌。')
        sources = list(dict.fromkeys(body.sources))
        if not sources or any(s not in ('re0', 'guanying', 'seedhub') for s in sources):
            raise HTTPException(422, '请选择受支持的资源来源。')
        payload = {'title': body.title.strip(), 'tmdb_id': body.tmdb_id, 'media_type': body.media_type,
                   'year': body.year, 'sources': sources, 'aliases': [], 'provider': '', 'preferences': {}}
        if not payload['title']: raise HTTPException(422, '作品名称不能为空。')
        try:
            response = await app.state.http.post(base + '/api/media/resources/search',
                json=payload, headers={'Authorization': 'Bearer ' + token}, timeout=60, follow_redirects=False)
            if response.status_code in (401, 403):
                raise HTTPException(502, ' 认证失败，请检查 OpenAPI 令牌。')
            if response.status_code != 200:
                raise HTTPException(502, ' 资源检索暂不可用。')
            data = response.json()
        except (httpx.HTTPError, ValueError):
            raise HTTPException(502, ' 连接或响应失败。') from None
        if not isinstance(data, dict) or data.get('success') is not True or not isinstance(data.get('data'), dict):
            raise HTTPException(502, ' 返回了无法识别的检索结果。')
        result = data['data']; items = result.get('items'); errors = result.get('errors')
        if not isinstance(items, list) or not isinstance(errors, list):
            raise HTTPException(502, ' 检索结果不完整。')
        return {'items': [clean for x in items[:100] if (clean := clean_item(x))],
                'partial': bool(errors), 'error_count': len(errors), 'truncated': len(items) > 100}

    @app.post('/api/v1/explore/resources/resolve', dependencies=[Depends(admin)])
    async def resolve(body: ResolveInput):
        base, token = store.get('resource_discovery_base', ''), store.get('resource_discovery_token', '')
        if not base or not token:
            raise HTTPException(409, '请先配置  服务地址和 OpenAPI 令牌。')
        # The user explicitly initiates this action after seeing the point warning.
        # An ambiguous outcome must never be retried automatically.
        try:
            response = await app.state.http.post(base + '/api/media/resources/copy-link',
                json={'source':body.source, 'provider':body.provider, 'slug':body.slug},
                headers={'Authorization':'Bearer ' + token}, timeout=60, follow_redirects=False)
            if response.status_code != 200:
                raise HTTPException(502, '未确认结果；请先到  核查是否已解锁，再决定是否重试。')
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            raise HTTPException(502, '结果未确认；请先到  核查是否已解锁，再决定是否重试。') from None
        data = payload.get('data') if isinstance(payload, dict) else None
        if not isinstance(payload, dict) or payload.get('success') is not True or not isinstance(data, dict):
            raise HTTPException(502, '未返回可用链接，请先在  核查解锁状态。')
        try:
            parsed = shares(str(data.get('share_url') or ''))
        except ValueError:
            parsed = []
        if len(parsed) != 1:
            raise HTTPException(502, '未返回可用的 115 分享链接，请在  核查。')
        share = parsed[0]
        return {'share_url': 'https://115.com/s/' + share['share_code'] +
                ('?password=' + share['receive_code'] if share['receive_code'] else '')}
