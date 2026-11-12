"""免转存分享链接 → 直接生成 STRM（不落自己 115 网盘，播放时走分享下载通道）。

第二阶段（U01–U05）起，本模块只做入口行为：解析、生成、整理、洗版、
存活检测的业务全部在 share_service 统一管线中执行，整理进入持久化
FIFO 任务。与转存模式的管线对齐：
  解析 → TMDB 识别 → 分类 → 规范命名 → 统一洗版规则 → STRM → Emby 通知
"""
import asyncio
import hashlib
import time
from typing import Literal

from fastapi import Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from croniter import croniter
from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import share_service
from .strm_options import shared_settings, share_effective_settings, effective
from .organize_options import OrganizeOptions
from .quality import UpgradePolicy, public_policy, KEYS, LABELS, CHOICES, OPTIONS, SUGGESTED_ORDER
from .fsops import purge_strm
from .transfer import shares as parse_shares
from .playaddr import ua_label

PENDING_NAME = share_service.PENDING_NAME


def install_share(app, store, admin):
    share_service.install(store)

    strm_root = app.state.strms

    def digest(token):
        return hashlib.sha256(token.encode()).hexdigest()

    class ShareParseInput(BaseModel):
        model_config = ConfigDict(extra='forbid')
        text: str = Field(min_length=1, max_length=2000)

    class ShareGenerateInput(BaseModel):
        model_config = ConfigDict(extra='ignore')
        share_code: str = Field(min_length=1, max_length=64)
        receive_code: str = Field(default='', max_length=64)
        files: list[dict] = Field(min_length=1, max_length=10000)

    class ShareSettingsInput(BaseModel):
        model_config = ConfigDict(extra='forbid')
        auto_organize: bool = True
        share_check_hours: int = Field(default=0, ge=0, le=8760)
        # 存活检测两种执行方式：固定间隔（按小时，锚在 03:00）或 Cron（5 段，按服务器时区）。
        share_check_mode: Literal['interval', 'cron'] = 'interval'
        share_check_cron: str = Field(default='0 3 * * *', max_length=100)
        share_prune_cron: str = Field(default='0 5 * * *', max_length=100)

        @model_validator(mode='after')
        def _validate_cron(self):
            if self.share_check_mode == 'cron':
                expression = (self.share_check_cron or '').strip()
                if len(expression.split()) != 5 or not croniter.is_valid(expression):
                    raise ValueError('cron 需要 5 段表达式，例如 0 3 * * *')
                self.share_check_cron = expression
            prune = (self.share_prune_cron or '').strip() or '0 5 * * *'
            if len(prune.split()) != 5 or not croniter.is_valid(prune):
                raise ValueError('台账核对 cron 需要 5 段表达式，例如 0 5 * * *')
            self.share_prune_cron = prune
            return self

    class ShareProfileInput(BaseModel):
        model_config = ConfigDict(extra='forbid')
        organize: OrganizeOptions
        upgrade_policy: UpgradePolicy
        notify_emby: bool = True

    def profile_data():
        return {'custom':bool(store.get('share_profile')), 'follow':not bool(store.get('share_profile')),
                'effective':share_effective_settings(store), 'base':shared_settings(store)}

    def quality_data():
        policy=share_effective_settings(store)['upgrade_policy']
        return {'config':policy,'labels':{r['key']:LABELS[r['key']] for r in policy['rules']},
                'choices':CHOICES,'options':OPTIONS,'suggested_order':SUGGESTED_ORDER}

    @app.get('/api/v1/share/profile', dependencies=[Depends(admin)])
    async def get_profile():
        return profile_data()

    class FollowInput(BaseModel):
        model_config = ConfigDict(extra='forbid')
        follow: bool

    @app.post('/api/v1/share/profile/follow', dependencies=[Depends(admin)])
    async def set_follow(body: FollowInput):
        """开：清掉独立配置，永远跟随影视整理；关：保留当前独立配置。"""
        if body.follow:
            with store.db:
                store.db.execute("DELETE FROM settings WHERE key='share_profile'")
            store.event('share','分享 STRM 已改为跟随影视整理的设置。')
        return profile_data()

    @app.put('/api/v1/share/profile', dependencies=[Depends(admin)])
    async def set_profile(body: ShareProfileInput):
        cfg=body.organize.model_dump()
        try:effective(store,organize_value=cfg)
        except ValueError as exc:raise HTTPException(422,str(exc)) from None
        policy=body.upgrade_policy
        if (({r.key for r in policy.rules} not in (set(KEYS), set(KEYS) | {'revision'}) if policy.coexist_version == 1 else {r.key for r in policy.rules} != set(KEYS)) or
            policy.season_layout != 'standard' or
            (policy.coexist_version != 1 and (policy.resolution_groups or policy.fps_groups or policy.unknown_fps_low or policy.dynamic_groups not in ('off','dovi')))):
            raise HTTPException(422,'请使用当前版本的多版本设置和标准季目录。')
        store.set_many({'share_profile':{'organize':cfg,'upgrade_policy':public_policy(policy.model_dump()),'notify_emby':body.notify_emby}})
        store.event('share','分享 STRM 独立整理规则已保存，仅后续新任务使用。')
        return profile_data()

    @app.get('/api/v1/share/quality', dependencies=[Depends(admin)])
    async def get_quality():
        return quality_data()

    @app.put('/api/v1/share/quality', dependencies=[Depends(admin)])
    async def set_quality(body: UpgradePolicy):
        snapshot=share_effective_settings(store)
        await set_profile(ShareProfileInput(organize=snapshot['organize'],upgrade_policy=body,
                                           notify_emby=snapshot['notify_emby']))
        return quality_data()

    @app.get('/api/v1/share/settings', dependencies=[Depends(admin)])
    async def get_settings():
        saved = store.get('share_settings', {}) or {}
        return {'auto_organize':saved.get('auto_organize',True),
                'share_check_hours':saved.get('share_check_hours',0),
                'share_check_mode':saved.get('share_check_mode','interval'),
                'share_check_cron':saved.get('share_check_cron','0 3 * * *'),
                'share_prune_cron':saved.get('share_prune_cron','0 5 * * *'),
                'effective':share_effective_settings(store)}

    @app.post('/api/v1/share/settings', dependencies=[Depends(admin)])
    async def set_settings(body: ShareSettingsInput):
        store.set_many({'share_settings':body.model_dump(),'share_prune_cron':body.share_prune_cron,'share_prune_due':0})
        return await get_settings()

    @app.post('/api/v1/share/strms/prune', dependencies=[Depends(admin)])
    async def prune_strms():
        from .share_service import prune_missing_share_strms
        removed = prune_missing_share_strms(store, app.state.strms, min_age=0)
        return {'removed': removed, 'message': '清理了 %d 条文件已不存在的记录。' % removed}

    @app.get('/api/v1/share/strms', dependencies=[Depends(admin)])
    async def list_strms(state: str = Query('all', pattern='^(all|pending|library|revoked)$'),
                         q: str = Query('', max_length=200),
                         offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200)):
        """分页完整返回，不再静默截断（U07）。状态由登记表驱动，不由路径文字推断。"""
        where, params = '1=1', []
        if state == 'pending':
            where = "revoked=0 AND strm_path LIKE ?"
            params = [PENDING_NAME + '/%']
        elif state == 'revoked':
            where = 'revoked=1'
        elif state == 'library':
            where = "revoked=0 AND strm_path NOT LIKE ?"
            params = [PENDING_NAME + '/%']
        if q:
            where += ' AND (name LIKE ? OR share_code LIKE ?)'
            params += ['%' + q + '%', '%' + q + '%']
        total = store.db.execute('SELECT count(*) FROM share_strms WHERE ' + where, params).fetchone()[0]
        rows = store.db.execute(
            'SELECT id,name,strm_path,share_code,created,revoked FROM share_strms WHERE ' + where +
            ' ORDER BY created DESC, id LIMIT ? OFFSET ?', (*params, limit, offset)).fetchall()
        return {'items': [dict(r) for r in rows], 'total': total,
                'offset': offset, 'limit': limit, 'has_more': offset + len(rows) < total}

    @app.post('/api/v1/share/parse', dependencies=[Depends(admin)])
    async def parse_share(body: ShareParseInput):
        try:
            secrets_list = parse_shares(body.text)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        secret = {'share_code': secrets_list[0]['share_code'], 'receive_code': secrets_list[0]['receive_code']}
        videos = await share_service.list_videos(app.state.cloud, secret, share_effective_settings(store)['strm']['video_extensions'])
        videos.sort(key=lambda x: x['name'])
        return {'share_code': secret['share_code'], 'receive_code': secret['receive_code'],
                'videos': videos, 'video_count': len(videos)}

    @app.post('/api/v1/share/generate', dependencies=[Depends(admin)])
    async def generate_strm(body: ShareGenerateInput):
        try:
            result = share_service.generate(store, strm_root,
                                            {'share_code': body.share_code, 'receive_code': body.receive_code},
                                            body.files, origin='web', allow_restore=True)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        return {'created': result['created'], 'skipped': result['skipped'],
                'count': len(result['created']), 'skip_count': len(result['skipped']),
                'target': str(strm_root / PENDING_NAME)}

    @app.post('/api/v1/share/organize', dependencies=[Depends(admin)])
    async def organize_share_strm():
        """整理进入持久化 FIFO 任务（U03）：立即返回任务身份，不在请求里等整批完成。"""
        pending = share_service.pending_count(store)
        if not pending:
            raise HTTPException(409, '没有待整理的分享 STRM。')
        jid = share_service.enqueue_organize(app.state.organizer, origin='web')
        return {'queued': True, 'job_id': jid, 'pending': pending,
                'message': f'整理任务 #{jid} 已入队，可在任务中心查看进度。'}

    async def _notify_emby_refresh():
        connection = app.state.organizer.emby.connection()
        if not connection:
            return
        url, key = connection
        try:
            await app.state.http.post(url + '/Library/Refresh', headers={'X-Emby-Token': key})
        except Exception as exc:
            # 以前这里完全静默：用户以为 Emby 已经重扫，实际什么都没发生。
            store.event('emby', '共享库刷新请求失败（%s）；请在 Emby 里手动扫描媒体库。'
                        % type(exc).__name__)

    @app.delete('/api/v1/share/strm/{sid}', dependencies=[Depends(admin)])
    async def revoke(sid: str):
        async with app.state.organizer.publication_gate:
            row = store.db.execute('SELECT strm_path,revoked FROM share_strms WHERE id=?', (sid,)).fetchone()
            if not row:
                raise HTTPException(404, '分享记录不存在。')
            if row['revoked']:
                return {'ok': True}
            local = strm_root / row['strm_path']
            if local.exists() or local.is_symlink():
                if share_service._verified_share_strm(strm_root, row['strm_path'], sid) is None:
                    raise HTTPException(409, '本地文件归属无法确认，已保留。')
                try:
                    from .internal_delete import remember
                    if not remember(store, [row['strm_path']]):
                        raise RuntimeError('未能登记')
                    purge_strm(local, strm_root, store=store)
                except Exception:
                    raise HTTPException(503, '内部删除登记或本地文件删除失败，记录未撤销。') from None
            with store.db:
                store.db.execute('UPDATE share_strms SET revoked=1 WHERE id=? AND revoked=0 AND strm_path=?', (sid, row['strm_path']))
        return {'ok': True}

    # 分享 STRM 里写的是 /play115/share/<sid>/<文件名>，路由必须带文件名段，
    # 否则整条分享库都是 404（Emby 也就永远探测不到媒体信息）。
    @app.api_route('/play115/share/{sid}/{name:path}', methods=['GET', 'HEAD'])
    @app.api_route('/play115/share/{sid}', methods=['GET', 'HEAD'])
    async def play_share(sid: str, request: Request, name: str = '',
                         token: str = Query('', max_length=200)):
        row = store.db.execute('SELECT * FROM share_strms WHERE id=?', (sid,)).fetchone()
        if not row or row['revoked']:
            raise HTTPException(403, '分享播放链接无效或已撤销。')
        ua = request.headers.get('user-agent', '')
        if '\r' in ua or '\n' in ua:
            raise HTTPException(422, 'UA 请求头无效。')
        # 0.9.58：与网盘播放对齐——每个分享入口 60 次/分钟。
        # 分享取链同样消耗 115 的配额，没有这道闸时一个反复重连的播放器就能刷穿；
        # play_limits 本来就是全进程共用的一份，键带上 share: 前缀即可与 /play115 区分。
        key = 'share:' + sid
        recent = app.state.play_limits[key]
        now = time.monotonic()
        while recent and recent[0] < now - 60:
            recent.popleft()
        if len(recent) >= 60:
            raise HTTPException(429, '播放请求过于频繁，请稍后再试。')
        recent.append(now)
        if len(app.state.play_limits) > 512:
            for stale, attempts in list(app.state.play_limits.items()):
                while attempts and attempts[0] < now - 60:
                    attempts.popleft()
                if not attempts:
                    app.state.play_limits.pop(stale, None)
        # P02：分享播放接入与网盘播放相同的优先取链通道（独立闸门与节流）。
        from .cloud115 import playback_request
        play_token = playback_request.set(True)
        started = time.monotonic()
        outcome = 'error'   # 0.9.58：分享取链结果同样进 /metrics（以前只有事件）
        try:
            async with asyncio.timeout(35):
                url, headers = await app.state.cloud.share_url_download(
                    row['file_id'], row['share_code'], row['receive_code'], ua)
            outcome = 'ok'
        except TimeoutError:
            outcome = 'timeout'
            try:
                store.event('play', '分享播放取链超时（分享 %s…，UA %s）' % (sid[:8], ua_label(ua)))
            except Exception:
                pass
            raise HTTPException(504, '分享播放取链超过时限，请重试。') from None
        except Exception as exc:
            # 分享通道以前成功失败都不留痕，出问题只能看现象。
            try:
                store.event('play', '分享播放取链失败：%s（分享 %s…，UA %s）'
                            % (type(exc).__name__, sid[:8], ua_label(ua)))
            except Exception:
                pass
            raise
        finally:
            playback_request.reset(play_token)
            app.state.metrics.record_play(outcome, (time.monotonic() - started) * 1000)
        try:
            if time.monotonic() - app.state.play_logged.get(key, 0.0) > 60:
                app.state.play_logged[key] = time.monotonic()
                store.event('play', '分享播放取链成功：%s（分享 %s…，%.2fs）'
                            % (ua_label(ua), sid[:8], time.monotonic() - started))
        except Exception:
            pass
        return RedirectResponse(url, status_code=302)

    # ===== 定时扫描监控的分享链接 =====

    app.state.share_play = play_share

    @app.get('/api/v1/share/watchlist', dependencies=[Depends(admin)])
    async def get_watchlist():
        rows = store.db.execute('SELECT share_code,receive_code,last_checked,created FROM share_watchlist ORDER BY created DESC').fetchall()
        return {'items': [dict(r) for r in rows]}

    @app.delete('/api/v1/share/watchlist/{code}', dependencies=[Depends(admin)])
    async def remove_watch(code: str):
        with store.db:
            store.db.execute('DELETE FROM share_watchlist WHERE share_code=?', (code,))
        return {'ok': True}

    async def _scan_watchlist():
        """重新解析监控列表里的分享链接，发现新文件就生成到待整理并入队整理（U04）。"""
        rows = store.db.execute('SELECT share_code,receive_code,last_checked FROM share_watchlist').fetchall()
        if not rows:
            return {'scanned': 0, 'new_files': 0}
        new_total = 0
        scanned = 0
        settings = store.get('share_settings', {}) or {}
        for r in rows:
            code, receive = r['share_code'], r['receive_code']
            try:
                secret = {'share_code': code, 'receive_code': receive}
                videos = await share_service.list_videos(app.state.cloud, secret, share_effective_settings(store)['strm']['video_extensions'])
                scanned += 1
                result = share_service.generate(store, strm_root, secret, videos, origin='monitor')
                new_total += len(result['created'])
                with store.db:
                    store.db.execute('UPDATE share_watchlist SET last_checked=? WHERE share_code=?', (time.time(), code))
                # 已撤销条目不会被监控重新生成：generate 只对登记表中
                # revoked=0 的 (share_code, file_id) 判重，撤销的条目有独立
                # 的撤销记录在案，重新出现的同一 file_id 会创建新登记——
                # 这是监控「分享里新出现的内容」语义，不是复活旧入口。
            except Exception:
                continue
        if new_total:
            store.event('share_scan', '定时扫描发现 %d 个新的分享文件，已放入待整理。' % new_total)
            if settings.get('auto_organize', True):
                share_service.enqueue_organize(app.state.organizer, origin='monitor')
        return {'scanned': scanned, 'new_files': new_total}

    @app.post('/api/v1/share/scan', dependencies=[Depends(admin)])
    async def manual_scan():
        """手动触发一次定时扫描。"""
        return await _scan_watchlist()

    # ===== STRM 存活检测 =====

    @app.post('/api/v1/share/check', dependencies=[Depends(admin)])
    async def check_alive():
        """分享存活检测：业务在 share_service（B08 契约：只有明确失效证据才撤销）。

        0.9.56：改走后台任务，最多等 8 秒。实测 82 个分享串行取链要 41 秒，以前整段
        压在 HTTP 请求上（页面假死）。小库仍然同步拿到结果；大库返回 pending，
        跑完照旧写 `share_check` 事件与 meta.share_check_last。
        """
        return await app.state.organizer.start_share_check(wait=8.0)
