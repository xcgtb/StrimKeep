"""Emby 反代的设置接口。

对齐上游 TgtoDrive「Emby 与飞牛反代」页的服务器卡片：列表 / 新增 / 保存并应用 /
开启·关闭 / 测试 / 播放记录 / 删除。上游地址与 API Key 留空即沿用「Emby 服务连接」。
"""
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field


class ProxyInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str | None = Field(default=None, max_length=40)
    upstream: str | None = Field(default=None, max_length=1000)
    api_key: str | None = Field(default=None, max_length=1000)
    port: int | None = Field(default=None, ge=1, le=65535)
    enabled: bool | None = None


def _clean_url(value):
    parts = urlsplit(value)
    try:
        parts.port
    except ValueError:
        raise HTTPException(422, '端口格式不正确。') from None
    if (parts.scheme not in ('http', 'https') or not parts.hostname
            or parts.username or parts.password or parts.query or parts.fragment
            or any(char.isspace() for char in value)):
        raise HTTPException(422, '请填写有效的 HTTP/HTTPS 地址。')
    return value.rstrip('/')


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
                'emby_key_configured': bool(store.get('emby_key'))}

    @app.get('/api/v1/emby-proxy', dependencies=[Depends(admin)])
    async def proxy_status():
        return payload()

    @app.post('/api/v1/emby-proxy/servers', dependencies=[Depends(admin)])
    async def proxy_add():
        rule = manager().add_rule()
        return {**payload(), 'id': rule['id']}

    @app.put('/api/v1/emby-proxy/servers/{rule_id}', dependencies=[Depends(admin)])
    async def proxy_save(rule_id: str, body: ProxyInput):
        pick(rule_id)
        values = body.model_dump(exclude_none=True)
        if 'upstream' in values and values['upstream']:
            values['upstream'] = _clean_url(values['upstream'])
        if 'name' in values:
            values['name'] = (values['name'] or '').strip()[:40] or 'Emby'
        if 'port' in values:
            for other in manager().rules():
                if other['id'] != rule_id and int(other.get('port') or 0) == values['port']:
                    raise HTTPException(409, '端口 %d 已被「%s」占用。' % (values['port'], other['name']))
        rules = manager().rules()
        for rule in rules:
            if rule['id'] == rule_id:
                rule.update(values)
        manager().save_rules(rules)
        await manager().apply_rule(pick(rule_id))
        return payload()

    @app.post('/api/v1/emby-proxy/servers/{rule_id}/enable', dependencies=[Depends(admin)])
    async def proxy_enable(rule_id: str):
        pick(rule_id)
        await manager().set_enabled(rule_id, True)
        return payload()

    @app.post('/api/v1/emby-proxy/servers/{rule_id}/disable', dependencies=[Depends(admin)])
    async def proxy_disable(rule_id: str):
        pick(rule_id)
        await manager().set_enabled(rule_id, False)
        return payload()

    @app.delete('/api/v1/emby-proxy/servers/{rule_id}', dependencies=[Depends(admin)])
    async def proxy_delete(rule_id: str):
        pick(rule_id)
        await manager().delete_rule(rule_id)
        return payload()

    @app.post('/api/v1/emby-proxy/servers/{rule_id}/test', dependencies=[Depends(admin)])
    async def proxy_test(rule_id: str):
        pick(rule_id)
        result = await manager().test_rule(rule_id)
        if result is None:
            raise HTTPException(404, '反代服务器不存在，可能已被删除。')
        return result

    @app.get('/api/v1/emby-proxy/servers/{rule_id}/plays', dependencies=[Depends(admin)])
    async def proxy_plays(rule_id: str, limit: int = 50):
        pick(rule_id)
        return {'plays': manager().plays(rule_id, limit)}
