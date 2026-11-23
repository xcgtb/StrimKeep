"""Emby 反代的设置接口。

每个实例独立管理服务地址、API Key、监听端口和运行状态。
"""
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .emby_connection import strm_mount_path
from .emby_proxy import Config


class ProxyInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str | None = Field(default=None, max_length=40)
    upstream: str | None = Field(default=None, max_length=1000)
    api_key: str | None = Field(default=None, max_length=1000)
    emby_strm_path: str | None = Field(default=None, max_length=1000)
    port: int | None = Field(default=None, ge=1, le=65535)
    enabled: bool | None = None


class VerifyStrmInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    item_id: str = Field(pattern=r'^[A-Za-z0-9]{1,64}$')
    media_source_id: str = Field(default='', pattern=r'^[A-Za-z0-9_-]{0,128}$')


def _clean_url(value):
    try:
        parts = urlsplit(value)
        parts.port
    except ValueError:
        raise HTTPException(422, 'Emby 地址或端口格式不正确。') from None
    if (parts.scheme not in ('http', 'https') or not parts.hostname
            or parts.username or parts.password or parts.query or parts.fragment
            or any(char.isspace() for char in value)):
        raise HTTPException(422, '请填写有效的 HTTP/HTTPS 地址。')
    return value.rstrip('/')


def _updates(body, rule, default_upstream):
    values = body.model_dump(exclude_none=True) if body else {}
    if 'upstream' in values:
        url = values['upstream'].strip()
        values['upstream'] = _clean_url(url) if url else ''
        previous_url = str(rule.get('upstream') or default_upstream or '').rstrip('/')
        if (values['upstream'] or str(default_upstream or '').rstrip('/')) != previous_url:
            values.setdefault('api_key', '')
    if 'api_key' in values:
        values['api_key'] = values['api_key'].strip()
        if any(char.isspace() or ord(char) < 32 for char in values['api_key']):
            raise HTTPException(422, 'Emby API Key 不能包含空格或换行。')
    if 'name' in values:
        values['name'] = values['name'].strip() or 'Emby'
    if 'emby_strm_path' in values:
        raw = values['emby_strm_path'].strip()
        path = raw.rstrip('/')
        if raw and (not path.startswith('/') or '//' in path or '\\' in path
                    or any(part in ('.', '..') for part in path.split('/'))
                    or any(ord(char) < 32 for char in path)):
            raise HTTPException(422, '请填写 Emby 容器内的绝对目录，例如 /media。')
        values['emby_strm_path'] = path
    return values


def install_emby_proxy(app, store, admin, get_manager):
    def manager():
        return get_manager()

    def pick(rule_id):
        rule = manager().find(rule_id)
        if rule is None:
            raise HTTPException(404, '反代服务器不存在，可能已被删除。')
        return rule

    def payload():
        return {'servers': manager().status(), 'emby_url': store.get('emby_url'),
                'emby_key_configured': bool(store.get('emby_key')),
                'emby_strm_path': strm_mount_path(store, '/media')}

    @app.get('/api/v1/emby-proxy', dependencies=[Depends(admin)])
    async def proxy_status():
        return payload()

    @app.post('/api/v1/emby-proxy/servers', dependencies=[Depends(admin)])
    async def proxy_add():
        async with manager().config_lock:
            rule = manager().add_rule()
            return {**payload(), 'id': rule['id']}

    @app.put('/api/v1/emby-proxy/servers/{rule_id}', dependencies=[Depends(admin)])
    async def proxy_save(rule_id: str, body: ProxyInput):
        mgr = manager()
        async with mgr.config_lock:
            original = pick(rule_id)
            updated = {**original, **_updates(body, original, store.get('emby_url'))}
            rules = mgr.rules()
            port = int(updated['port'])
            if any(other['id'] != rule_id and int(other['port']) == port for other in rules):
                raise HTTPException(409, f'端口 {port} 已被其他反代使用。')
            running = mgr.servers.get(rule_id)
            moving = bool(running and running.running() and port != int(original['port']))
            server = await mgr.apply_rule(updated)
            if moving and Config(store, updated).ready and not server.running():
                await mgr.apply_rule(original)
                raise HTTPException(409, f'端口 {port} 无法监听，已保留原设置。')
            mgr.save_rules([updated if rule['id'] == rule_id else rule for rule in rules])
            return payload()

    @app.post('/api/v1/emby-proxy/servers/{rule_id}/enable', dependencies=[Depends(admin)])
    async def proxy_enable(rule_id: str):
        async with manager().config_lock:
            if not Config(store, pick(rule_id)).upstream:
                raise HTTPException(422, '请先配置这条反代的 Emby 地址。')
            await manager().set_enabled(rule_id, True)
            return payload()

    @app.post('/api/v1/emby-proxy/servers/{rule_id}/disable', dependencies=[Depends(admin)])
    async def proxy_disable(rule_id: str):
        async with manager().config_lock:
            pick(rule_id)
            await manager().set_enabled(rule_id, False)
            return payload()

    @app.delete('/api/v1/emby-proxy/servers/{rule_id}', dependencies=[Depends(admin)])
    async def proxy_delete(rule_id: str):
        async with manager().config_lock:
            pick(rule_id)
            await manager().delete_rule(rule_id)
            return payload()

    @app.post('/api/v1/emby-proxy/servers/{rule_id}/test', dependencies=[Depends(admin)])
    async def proxy_test(rule_id: str, body: ProxyInput | None = None):
        rule = pick(rule_id)
        result = await manager().test_rule(rule_id, _updates(body, rule, store.get('emby_url')))
        if result is None:
            raise HTTPException(404, '反代服务器不存在，可能已被删除。')
        return result

    @app.post('/api/v1/emby-proxy/servers/{rule_id}/verify-strm', dependencies=[Depends(admin)])
    async def proxy_verify_strm(rule_id: str, body: VerifyStrmInput, request: Request):
        return await manager().verify_strm(pick(rule_id), body.item_id, body.media_source_id,
                                           request.headers.get('user-agent', ''))

    @app.get('/api/v1/emby-proxy/servers/{rule_id}/plays', dependencies=[Depends(admin)])
    async def proxy_plays(rule_id: str, limit: int = 50):
        pick(rule_id)
        return {'plays': manager().plays(rule_id, limit)}
