"""频道监控接口：公开频道清单、每频道落库方式与正则、全局轮询节奏。"""
import re
from typing import Literal

from croniter import croniter
from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .channel_monitor import normalize_channel
from .telegram_session import SessionError


class ChannelRoute(BaseModel):
    """关键词二次分流：帖子文本命中关键词就落到这个目录。"""
    model_config = ConfigDict(extra='forbid')
    keywords: str = Field(default='', max_length=200)
    target_cid: str = Field(default='', max_length=24)
    target_label: str = Field(default='', max_length=500)


class ChannelRule(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=64)
    enabled: bool = True
    # 分享与磁力/电驴分开配置，一条频道可以同时处理两者。
    share_mode: Literal['', 'strm', 'transfer'] = ''
    organize_after: bool = False      # 落盘后立即整理；False = 交给手动/定时扫描
    source: Literal['preview', 'session'] = 'preview'   # 公开预览页 / 用户会话（私密频道）
    history_pages: int = Field(default=1, ge=1, le=20)   # 首次接入回溯页数（每页约 20 条）
    share_cid: str = Field(default='', max_length=24)
    share_label: str = Field(default='', max_length=500)
    offline_cid: str = Field(default='', max_length=24)
    offline_label: str = Field(default='', max_length=500)
    magnet_cid: str | None = Field(default=None, max_length=24)
    magnet_label: str | None = Field(default=None, max_length=500)
    ed2k_cid: str | None = Field(default=None, max_length=24)
    ed2k_label: str | None = Field(default=None, max_length=500)
    # 兼容旧客户端 / 旧测试：mode + target_cid，进模型后迁移到上面两组字段。
    mode: Literal['strm', 'transfer', 'offline'] | None = None
    target_cid: str = Field(default='', max_length=24)
    target_label: str = Field(default='', max_length=500)
    include: str = Field(default='', max_length=500)
    exclude: str = Field(default='', max_length=500)

    @field_validator('name')
    @classmethod
    def channel(cls, value):
        return normalize_channel(value, strict=True)

    @field_validator('share_cid', 'offline_cid', 'magnet_cid', 'ed2k_cid')
    @classmethod
    def cid(cls, value):
        if value is None:
            return None
        text = str(value or '').strip()
        if text and not text.isdigit():
            raise ValueError('保存目录必须是 115 目录 ID。')
        return text

    @field_validator('include', 'exclude')
    @classmethod
    def pattern(cls, value):
        text = str(value or '').strip()
        if text:
            try:
                re.compile(text)
            except re.error as exc:
                raise ValueError('正则无效：%s' % exc) from None
        return text

    routes: list[ChannelRoute] = Field(default_factory=list, max_length=20)

    @model_validator(mode='after')
    def complete(self):
        if self.mode:
            if self.mode == 'offline' and not self.target_cid:
                raise ValueError('存网盘/离线模式需要先选一个 115 保存目录。')
            if not self.share_mode:
                self.share_mode = {'strm': 'strm', 'transfer': 'transfer', 'offline': 'transfer'}[self.mode]
            if self.mode == 'transfer' and not self.share_cid:
                self.share_cid, self.share_label = self.target_cid, self.target_label
            if self.mode == 'offline' and not self.offline_cid:
                self.offline_cid, self.offline_label = self.target_cid, self.target_label
        if not self.share_mode:
            self.share_mode = 'strm'
        if self.share_mode == 'transfer' and not (self.share_cid or self.offline_cid
                                                   or self.magnet_cid or self.ed2k_cid):
            raise ValueError('115 网盘落盘需要至少选一个目录（分享转存 / 磁力 / ED2K）。')
        if self.source == 'preview' and str(self.name).lstrip('-').isdigit():
            raise ValueError('数字 id 只能配「用户会话」；公开预览页需要频道用户名。')
        return self


class ChannelOptions(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool = False
    notify: bool = True
    schedule_mode: Literal['interval', 'cron'] = 'interval'
    interval: int = Field(default=300, ge=60, le=86400)
    cron: str = Field(default='', max_length=100)
    channels: list[ChannelRule] = Field(default_factory=list, max_length=50)

    @model_validator(mode='after')
    def schedule(self):
        if self.schedule_mode == 'cron':
            expression = (self.cron or '').strip()
            if len(expression.split()) != 5 or not croniter.is_valid(expression):
                raise ValueError('cron 需要 5 段表达式，例如 */10 * * * *')
            self.cron = expression
        return self


def install_channels(app, store, admin):
    def monitor():
        return app.state.organizer.channels

    def payload():
        # 只做提示：目录由用户自己决定要不要当「待整理」，这里不替他改。
        organize = store.get('organize_config') or {}
        return {'config': monitor().config(), 'sources': monitor().sources(),
                'organize_sources': [str(cid) for cid in organize.get('source_cids') or []]}

    def telegram():
        return app.state.organizer.telegram

    class SessionCodeInput(BaseModel):
        model_config = ConfigDict(extra='forbid')
        api_id: str = Field(default='', max_length=32)
        api_hash: str = Field(default='', max_length=64)
        phone: str = Field(default='', max_length=32)

    class SessionCodeVerify(BaseModel):
        model_config = ConfigDict(extra='forbid')
        code: str = Field(default='', max_length=16)

    class SessionPassword(BaseModel):
        model_config = ConfigDict(extra='forbid')
        password: str = Field(default='', max_length=256)

    @app.get('/api/v1/telegram/session', dependencies=[Depends(admin)])
    async def telegram_session():
        return {'session': telegram().status()}

    @app.post('/api/v1/telegram/session/code', dependencies=[Depends(admin)])
    async def telegram_session_code(body: SessionCodeInput):
        try:
            return await telegram().send_code(body.api_id, body.api_hash, body.phone)
        except SessionError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.post('/api/v1/telegram/session/verify', dependencies=[Depends(admin)])
    async def telegram_session_verify(body: SessionCodeVerify):
        try:
            return await telegram().verify(body.code)
        except SessionError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.post('/api/v1/telegram/session/password', dependencies=[Depends(admin)])
    async def telegram_session_password(body: SessionPassword):
        try:
            return await telegram().password(body.password)
        except SessionError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.post('/api/v1/telegram/session/logout', dependencies=[Depends(admin)])
    async def telegram_session_logout():
        await telegram().logout()
        return {'session': telegram().status()}

    @app.get('/api/v1/telegram/session/dialogs', dependencies=[Depends(admin)])
    async def telegram_session_dialogs():
        try:
            return {'items': await telegram().dialogs()}
        except SessionError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.get('/api/v1/channels', dependencies=[Depends(admin)])
    async def channels_get():
        return payload()

    @app.put('/api/v1/channels', dependencies=[Depends(admin)])
    async def channels_put(body: ChannelOptions):
        config = body.model_dump()
        # 目录不锁死：用户可以存到任意目录，甚至可以不是待整理目录。
        # 是否会被自动整理，由「影视整理 → 待整理目录」决定，页面只提示不代改。
        monitor().save(config)
        store.event('channel', '频道监控已保存：%s，%d 个频道，%s。'
                    % ('启用' if config['enabled'] else '停用', len(config['channels']),
                       ('cron ' + config['cron']) if config['schedule_mode'] == 'cron'
                       else ('每 %d 秒' % config['interval'])))
        return payload()

    @app.post('/api/v1/channels/check', dependencies=[Depends(admin)])
    async def channels_check():
        """立即跑一轮，方便刚加完频道就验证能不能读。"""
        results = await monitor().tick(force=True)
        return {'checked': len(results), 'queued': sum(count for _, count in results), **payload()}

    @app.post('/api/v1/channels/rename', dependencies=[Depends(admin)])
    async def channels_rename(body: dict):
        """改频道名字（用户名 ↔ 数字 ID），落点、正则、分流规则全部保留。

        频道从公开转私密后用户名会被释放，`get_entity('旧用户名')` 直接失败，
        只能换成数字 ID。没有这个接口就只能移除重加，配置全丢。
        游标和已处理记录一起搬过去，改名后不会重扫旧帖。
        """
        try:
            old = normalize_channel(body.get('old'), strict=True)
            new = normalize_channel(body.get('new'), strict=True)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        config = monitor().config()
        target = next((c for c in config['channels'] if c['name'] == old), None)
        if target is None:
            raise HTTPException(404, '频道 %s 不在列表里' % old)
        if any(c['name'] == new for c in config['channels']):
            raise HTTPException(409, '频道 %s 已经在列表里了' % new)
        if old == new:
            return payload()
        target['name'] = new
        if str(new).lstrip('-').isdigit():
            # 数字 ID 只能用用户会话读；不改过来的话，下次点「保存修改」会被校验拦下。
            target['source'] = 'session'
        with store.db:
            # 新名字下可能还留着上次删除的残留行（游标是旧的），先清掉再搬。
            for table in ('channel_state', 'channel_backfill', 'channel_posts',
                          'channel_share_attempts'):
                store.db.execute('DELETE FROM %s WHERE name=?' % table, (new,))
            for table in ('channel_state', 'channel_backfill', 'channel_posts',
                          'channel_share_attempts', 'channel_shares'):
                store.db.execute('UPDATE %s SET name=? WHERE name=?' % table, (new, old))
        monitor().save(config)
        store.event('channel', '频道改名：%s → %s（落点与规则保留，跟进位置一并搬过去）。'
                    % (old, new))
        return payload()

    @app.delete('/api/v1/channels/{name}', dependencies=[Depends(admin)])
    async def channels_forget(name: str):
        try:
            clean = normalize_channel(name, strict=True)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        monitor().forget(clean)
        return payload()
