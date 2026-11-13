"""Phase-five admin endpoints. All mutations share session/CSRF authorization."""
import asyncio
import json
import os
import re
import secrets
import time
from croniter import croniter
from pathlib import Path
from zoneinfo import ZoneInfo
from datetime import datetime
from fastapi import Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid')


from .strm_options import StrmOptions, effective, shared_settings


class SyncConfig(StrmOptions, Input):
    model_config = ConfigDict(extra="forbid")
    source_cids: list[str] = Field(default_factory=list,max_length=20)
    directory_labels: dict[str,str] = Field(default_factory=dict,max_length=30)
    notify_emby: bool = True
    full_scan: bool = True
    cleanup_missing: bool = True


class SyncRunInput(Input):
    """全量 / 增量同步的显式参数：目录可覆盖，增量可指定起点。"""
    source_cids: list[str] = Field(default_factory=list, max_length=20)
    since: float | None = Field(default=None, ge=0)
    dry_run: bool = False    # 只按台账估算影响面，不入队、不读云端


class UnidentifiedCleanupInput(Input):
    paths: list[str] = Field(default_factory=list, max_length=500)


class SyncProfileInput(Input):
    name: str = Field(min_length=1, max_length=80)
    source_cid: str = Field(default='', max_length=24)
    source_label: str = Field(default='', max_length=300)
    source_cids: list[str] = Field(default_factory=list, max_length=20)
    source_labels: dict[str,str] = Field(default_factory=dict, max_length=20)
    mode: str = Field(pattern='^(incremental|full)$')
    enabled: bool = False
    frequency: str = Field(default='daily', pattern='^(daily|weekly|interval|cron)$')
    interval_minutes: int = Field(default=60, ge=5, le=43200)
    cron: str = Field(default='0 3 * * *', max_length=100)
    timezone: str = Field(default='Asia/Shanghai', max_length=100)
    weekday: int = Field(default=1, ge=0, le=6)
    time: str = Field(default='03:00', pattern=r'^([01]\d|2[0-3]):[0-5]\d$')


class ScheduleConfig(Input):
    scan_enabled: bool = False
    scan_interval_minutes: int = Field(default=5,ge=5,le=43200)
    sync_enabled: bool = False
    sync_interval_minutes: int = Field(default=60,ge=5,le=43200)
    sync_source_cids: list[str] = Field(default_factory=list,max_length=20)
    directory_labels: dict[str,str] = Field(default_factory=dict,max_length=30)
    scan_mode:str=Field(default='interval',pattern='^(interval|cron)$')
    sync_mode:str=Field(default='interval',pattern='^(interval|cron)$')
    scan_cron:str=Field(default='*/5 * * * *',max_length=100)
    sync_cron:str=Field(default='0 3 * * *',max_length=100)
    timezone:str=Field(default='Asia/Shanghai',max_length=100)
    archive_refresh_hours: int = Field(default=0,ge=0,le=8760)


class NotificationConfig(Input):
    telegram_enabled: bool = False
    bot_token: str = Field(default='',max_length=200)
    chat_id: str = Field(default='',max_length=100)
    categories:dict[str,bool]=Field(default_factory=dict,max_length=20)


def install_phase5(app,store,admin,job_public):
    def engine(): return app.state.organizer

    async def validate_roots(roots):
        if not roots or len(set(roots)) != len(roots) or any(not re.fullmatch(r'[1-9]\d{0,23}',cid) for cid in roots):
            raise HTTPException(422,'请选择一个或多个不重复的非根目录。')
        for cid in roots: await app.state.cloud.directory(cid)

    def config_for(kind,roots=None):
        try: return engine().tasks.config(kind,roots)
        except ValueError as exc: raise HTTPException(409,str(exc)) from None

    @app.get('/api/v1/sync/config',dependencies=[Depends(admin)])
    async def sync_config(): return {'config':{**SyncConfig().model_dump(),**store.get('sync_config',{}), **effective(store), 'full_scan': True}, 'effective_shared': shared_settings(store), 'output_root': str(app.state.strms),
        'stale_strms': json.loads(store.get('stale_strms') or '[]')}

    @app.get('/api/v1/strm/audit',dependencies=[Depends(admin)])
    async def strm_audit(limit:int=Query(20,ge=1,le=200)):
        """登记检查只提供线索，不推断未登记文件的来源。"""
        from .share_service import UNIDENT_NAME, PENDING_NAME
        def scan():
            root = Path(engine().root)
            files = {p.relative_to(root).as_posix() for p in root.rglob('*.strm') if p.is_file() and not p.is_symlink()}
            managed = {r[0] for r in store.db.execute('SELECT relative_path FROM playbacks WHERE revoked=0')}
            managed |= {r[0] for r in store.db.execute('SELECT strm_path FROM share_strms WHERE revoked=0')}
            special = {p for p in files if p.split('/', 1)[0] in (UNIDENT_NAME, PENDING_NAME)}
            unknown = sorted(files - managed - special)
            missing = sorted(managed - files)
            return {'total':len(files), 'managed':len(files & managed),
                    'external_total':len(unknown), 'external':[{'path':p, 'kind':'unknown'} for p in unknown[:limit]],
                    'missing_total':len(missing), 'missing':missing[:limit],
                    'unidentified_total':sum(p.startswith(UNIDENT_NAME + '/') for p in special),
                    'pending_total':sum(p.startswith(PENDING_NAME + '/') for p in special)}
        return await asyncio.to_thread(scan)

    @app.post('/api/v1/strm/audit/cleanup',dependencies=[Depends(admin)])
    async def strm_audit_cleanup():
        raise HTTPException(410, '旧版自动清理已停用，请在分享未识别目录中选择需要清理的 STRM。')

    def unidentified_files():
        from .share_service import UNIDENT_NAME
        root = Path(engine().root) / UNIDENT_NAME
        if root.is_symlink() or not root.is_dir():
            return []
        found = []
        for parent, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = [name for name in dirs if not (Path(parent) / name).is_symlink()]
            for name in files:
                path = Path(parent) / name
                if path.suffix.lower() == '.strm' and not path.is_symlink() and path.is_file():
                    found.append(path.relative_to(Path(engine().root)).as_posix())
        return sorted(found)

    @app.get('/api/v1/strm/unidentified',dependencies=[Depends(admin)])
    async def list_unidentified():
        files = await asyncio.to_thread(unidentified_files)
        return {'total':len(files), 'items':files[:500]}

    @app.post('/api/v1/strm/unidentified/cleanup',dependencies=[Depends(admin)])
    async def cleanup_unidentified(body: UnidentifiedCleanupInput):
        from .share_service import UNIDENT_NAME
        from .internal_delete import remember
        root = Path(engine().root)
        prefix = UNIDENT_NAME + '/'
        requested = list(dict.fromkeys(body.paths))
        for rel in requested:
            parts = rel.split('/')
            if (not rel.startswith(prefix) or len(parts) < 2 or any(p in ('', '.', '..') or '\\' in p or '\0' in p for p in parts)
                    or Path(parts[-1]).suffix.lower() != '.strm'):
                raise HTTPException(422, '只能选择分享STRM未识别目录中的 STRM 文件。')
        removed, failed, empty_dirs = [], [], 0
        async with engine().publication_gate:
            for rel in requested:
                path = root.joinpath(*rel.split('/'))
                try:
                    if any(part.is_symlink() for part in [root / UNIDENT_NAME, *list(path.parents)[:-1]] if part != root):
                        raise ValueError('符号链接')
                    if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to((root / UNIDENT_NAME).resolve()):
                        raise ValueError('文件不存在或路径不安全')
                    if store.db.execute('SELECT 1 FROM share_strms WHERE revoked=0 AND strm_path=? UNION SELECT 1 FROM playbacks WHERE revoked=0 AND relative_path=? LIMIT 1', (rel, rel)).fetchone():
                        raise ValueError('文件仍有有效登记')
                    remember(store, [rel])
                    path.unlink()
                    removed.append(rel)
                    parent = path.parent
                    boundary = root / UNIDENT_NAME
                    while parent != boundary and boundary in parent.parents:
                        try:
                            parent.rmdir()
                            empty_dirs += 1
                        except OSError:
                            break
                        parent = parent.parent
                except (OSError, ValueError, RuntimeError):
                    failed.append(rel)
            boundary = root / UNIDENT_NAME
            if boundary.is_dir() and not boundary.is_symlink():
                for parent, dirs, _ in os.walk(boundary, topdown=False, followlinks=False):
                    for name in dirs:
                        directory = Path(parent) / name
                        if directory.is_symlink():
                            continue
                        try:
                            directory.rmdir()
                            empty_dirs += 1
                        except OSError:
                            pass
        if removed or empty_dirs:
            store.event('strm_sync', f'清理分享未识别 STRM：删除 {len(removed)} 个本地文件、{empty_dirs} 个空目录。')
        return {'removed':len(removed), 'empty_dirs':empty_dirs, 'failed':failed}

    @app.post('/api/v1/strm/stale/cleanup',dependencies=[Depends(admin)])
    async def stale_cleanup():
        async with engine().publication_gate:
            cleaned, rest = await engine().sync.stale_cleanup()
        if cleaned: store.event('strm_sync', '失效 STRM 清理：已删除本地 %d 条输出与台账（网盘不受影响）。' % len(cleaned))
        return {'cleaned': cleaned, 'remaining': rest}

    @app.put('/api/v1/sync/config',dependencies=[Depends(admin)])
    async def save_sync(body:SyncConfig):
        if body.source_cids:await validate_roots(body.source_cids)
        try:effective(store,body.model_dump())
        except ValueError as exc:raise HTTPException(422,str(exc)) from None
        store.set_many({'sync_config':{**body.model_dump(), 'full_scan':True}})
        return await sync_config()

    @app.post('/api/v1/sync/jobs',dependencies=[Depends(admin)])
    async def start_sync():
        jid,_ = engine().tasks.enqueue(config_for('strm_sync'))
        return job_public(engine().job(jid))

    @app.get('/api/v1/sync/profiles', dependencies=[Depends(admin)])
    async def sync_profiles():
        profiles = store.get('sync_profiles', [])
        states = {row['kind']:dict(row) for row in store.db.execute("SELECT * FROM task_schedules WHERE kind LIKE 'sync_profile:%'")}
        return {'items':[{**profile, 'next_due':states.get('sync_profile:' + profile['id'], {}).get('next_due')} for profile in profiles]}

    def validate_profile_schedule(body: SyncProfileInput):
        try:
            zone = ZoneInfo(body.timezone)
            if body.frequency == 'cron':
                if len(body.cron.split()) != 5 or not croniter.is_valid(body.cron): raise ValueError('invalid cron')
                croniter(body.cron, datetime.now(zone), max_years_between_matches=8).get_next(datetime)
        except (ValueError, KeyError):
            raise HTTPException(422, '请填写有效的五段 Cron 表达式和时区。') from None

    @app.post('/api/v1/sync/profiles', dependencies=[Depends(admin)])
    async def create_sync_profile(body: SyncProfileInput):
        validate_profile_schedule(body)
        roots = body.source_cids or [body.source_cid]
        await validate_roots(roots)
        if any(len(value) > 300 for value in body.source_labels.values()): raise HTTPException(422, '目录名称过长。')
        labels = {cid:body.source_labels.get(cid) or (body.source_label if cid == body.source_cid else '') or cid for cid in roots}
        profiles = store.get('sync_profiles', [])
        if len(profiles) >= 30: raise HTTPException(422, '最多创建 30 个同步任务。')
        profile = {'id': secrets.token_hex(8), **body.model_dump(), 'source_cids':roots, 'source_labels':labels, 'source_cid':roots[0], 'source_label':labels[roots[0]]}
        store.set_many({'sync_profiles': [*profiles, profile]})
        engine().tasks.tick_sync_profiles(time.time())
        return profile

    @app.put('/api/v1/sync/profiles/{profile_id}', dependencies=[Depends(admin)])
    async def update_sync_profile(profile_id: str, body: SyncProfileInput):
        validate_profile_schedule(body)
        roots = body.source_cids or [body.source_cid]
        await validate_roots(roots)
        if any(len(value) > 300 for value in body.source_labels.values()): raise HTTPException(422, '目录名称过长。')
        labels = {cid:body.source_labels.get(cid) or (body.source_label if cid == body.source_cid else '') or cid for cid in roots}
        profiles = store.get('sync_profiles', [])
        if not any(p['id'] == profile_id for p in profiles): raise HTTPException(404, '同步任务不存在。')
        profile = {'id': profile_id, **body.model_dump(), 'source_cids':roots, 'source_labels':labels, 'source_cid':roots[0], 'source_label':labels[roots[0]]}
        store.set_many({'sync_profiles': [profile if p['id'] == profile_id else p for p in profiles]})
        with store.db: store.db.execute('DELETE FROM task_schedules WHERE kind=?', ('sync_profile:' + profile_id,))
        engine().tasks.tick_sync_profiles(time.time())
        return profile

    @app.delete('/api/v1/sync/profiles/{profile_id}', dependencies=[Depends(admin)])
    async def delete_sync_profile(profile_id: str):
        profiles = store.get('sync_profiles', [])
        if not any(p['id'] == profile_id for p in profiles): raise HTTPException(404, '同步任务不存在。')
        store.set_many({'sync_profiles': [p for p in profiles if p['id'] != profile_id]})
        with store.db: store.db.execute('DELETE FROM task_schedules WHERE kind=?', ('sync_profile:' + profile_id,))
        return {'deleted': True}

    @app.post('/api/v1/sync/profiles/{profile_id}/run', dependencies=[Depends(admin)])
    async def run_sync_profile(profile_id: str):
        profile = next((p for p in store.get('sync_profiles', []) if p['id'] == profile_id), None)
        if not profile: raise HTTPException(404, '同步任务不存在。')
        try: config = engine().tasks.sync_profile_config(profile)
        except ValueError as exc: raise HTTPException(409, str(exc)) from None
        jid, _ = engine().tasks.enqueue(config)
        return job_public(engine().job(jid))

    @app.post('/api/v1/sync/full', dependencies=[Depends(admin)])
    async def start_full_sync(body: SyncRunInput):
        """全量 STRM 同步：重建云端清单并逐文件核对。

        与增量相对：文件没变也会完整核对一遍。改过库根、命名口径或换账号之后用它。
        """
        roots = body.source_cids or None
        if roots: await validate_roots(roots)
        config = {**config_for('strm_sync', roots), 'full_scan': True, '_force_full': True,
                  'trust_scan': False, 'mode': 'full'}
        jid, _ = engine().tasks.enqueue(config)
        return {**job_public(engine().job(jid)), 'mode': 'full'}

    @app.post('/api/v1/sync/incremental', dependencies=[Depends(admin)])
    async def start_incremental_sync(body: SyncRunInput):
        """增量 STRM 同步：只处理上次同步之后新增/变化的文件。

        这个时间点是上一次成功同步的**开始**时刻；显式传 since 可覆盖。
        本地输出缺失的文件不会被跳过，仍会完整核对并重建。
        """
        roots = body.source_cids or None
        if roots: await validate_roots(roots)
        # 与定时同步共用同一套时间口径（按目录取最早、没跑过退回全局）。
        since = float(body.since) if body.since is not None else engine().tasks.watermark_since(roots)
        config = {**config_for('strm_sync', roots), 'full_scan': True, 'trust_scan': True,
                  'incremental_since': since, 'mode': 'incremental'}
        if body.dry_run:
            # 台账级预估：不读云端、不入队，用它判断「这次增量大概会动多少」。
            account = engine().tasks.account()
            changed = store.db.execute(
                "SELECT count(*) FROM output_registry WHERE account_id=? AND active=1 AND updated>=?",
                (account, since)).fetchone()[0]
            running = store.db.execute(
                "SELECT count(*) FROM organize_jobs WHERE state IN ('queued','running') "
                "AND json_extract(config,'$.job_kind')='strm_sync'").fetchone()[0]
            return {'mode': 'incremental', 'since': since, 'dry_run': True,
                    'changed': int(changed or 0), 'running': int(running or 0),
                    'stale_count': len(store.meta_json('stale_strms', []) or [])}
        jid, _ = engine().tasks.enqueue(config)
        return {**job_public(engine().job(jid)), 'mode': 'incremental', 'since': since}

    @app.get('/api/v1/sync/changes', dependencies=[Depends(admin)])
    async def sync_changes(since: float = Query(0, ge=0), limit: int = Query(200, ge=1, le=1000)):
        """上次同步之后的输出变更明细（只读台账，不访问云端）。

        外部要写报表/发通知时不必自己比对云盘：这里直接列出这段时间里
        新增、更新过的输出，以及已停用的旧登记。
        """
        account = engine().tasks.account()
        rows = store.db.execute(
            "SELECT file_id, relative_path, active, updated FROM output_registry "
            "WHERE account_id=? AND updated>=? ORDER BY updated DESC LIMIT ?",
            (account, since, limit)).fetchall()
        return {'since': since, 'count': len(rows), 'items': [dict(row) for row in rows]}

    @app.get('/api/v1/sync/state', dependencies=[Depends(admin)])
    async def sync_state():
        """上次同步时间与最近一次任务：外部调用方据此决定跑全量还是增量。"""
        watermark = store.meta_json('sync_watermark', {}) or {}
        last = store.db.execute(
            "SELECT id,state,stage,created,updated FROM organize_jobs "
            "WHERE json_extract(config,'$.job_kind')='strm_sync' ORDER BY id DESC LIMIT 1").fetchone()
        running = store.db.execute(
            "SELECT count(*) FROM organize_jobs WHERE state IN ('queued','running') "
            "AND json_extract(config,'$.job_kind')='strm_sync'").fetchone()[0]
        stale = store.meta_json('stale_strms', []) or []
        return {'watermark': watermark, 'last_job': dict(last) if last else None,
                'running': int(running or 0), 'stale_count': len(stale)}

    @app.post('/api/v1/sync/webhook-token',dependencies=[Depends(admin)])
    async def rotate_webhook_token():
        """深度删除 Webhook 令牌：服务端用 CSPRNG 生成，前端只负责展示。

        这个令牌能触发「网盘文件进回收站 + 删本地 STRM/NFO」，以前由前端用
        Math.random()+Date.now() 拼出来，熵不足且可预测，等于给了弱口令。
        """
        token = secrets.token_urlsafe(24)
        current = {**SyncConfig().model_dump(), **store.get('sync_config', {})}
        current['webhook_token'] = token
        store.set_many({'sync_config': current})
        store.event('system', '深度删除 Webhook 令牌已由服务端重新生成；旧令牌立即失效。')
        return {'webhook_token': token}

    @app.get('/api/v1/system/schedules',dependencies=[Depends(admin)])
    async def schedules():
        config = {**ScheduleConfig().model_dump(),**store.get('schedule_config',{})}
        return {'config':config,'states':[dict(r) for r in store.db.execute('SELECT * FROM task_schedules')]}

    @app.put('/api/v1/system/schedules',dependencies=[Depends(admin)])
    async def save_schedules(body:ScheduleConfig):
        previous = store.get('schedule_config',ScheduleConfig().model_dump())
        patch = body.model_dump(exclude_unset=True)
        if 'directory_labels' in patch:
            patch['directory_labels'] = {**previous.get('directory_labels',{}), **patch['directory_labels']}
        config = ScheduleConfig.model_validate({**previous, **patch}).model_dump()
        try:
            zone=ZoneInfo(config['timezone'])
            for prefix in ('scan','sync'):
                if config[prefix+'_mode']=='cron':
                    expression=config[prefix+'_cron']
                    if len(expression.split())!=5 or not croniter.is_valid(expression):raise ValueError('invalid cron')
                    # Reject impossible dates here instead of breaking the scheduler later.
                    croniter(expression,datetime.now(zone),max_years_between_matches=8).get_next(datetime)
        except (ValueError,KeyError):raise HTTPException(422,'请填写有效的五段 Cron 表达式和时区。')
        if config['scan_enabled']: config_for('organize')
        if config['sync_enabled']:
            await validate_roots(config['sync_source_cids'])
            config_for('strm_sync',config['sync_source_cids'])
        with store.db:
            for kind,prefix in (('organize','scan'),('strm_sync','sync')):
                if config['timezone']!=previous.get('timezone','Asia/Shanghai') or any(config.get(prefix+k)!=previous.get(prefix+k) for k in ('_enabled','_interval_minutes','_source_cids','_mode','_cron')):
                    store.db.execute('DELETE FROM task_schedules WHERE kind=?',(kind,))
        store.set_many({'schedule_config':config,'archive_refresh_hours':config['archive_refresh_hours']})
        engine().tasks.tick()
        store.event('schedule','定时扫描、同步与归档复核周期已保存。')
        return await schedules()

    @app.get('/api/v1/notifications/config',dependencies=[Depends(admin)])
    async def notification_config():
        config = store.get('notification_config',{})
        from .notifications import CATEGORY_DEFAULTS
        return {'categories':{**CATEGORY_DEFAULTS,**config.get('categories',{})},'telegram_enabled':config.get('telegram_enabled',False),'bot_token_configured':bool(config.get('bot_token')),'chat_id_configured':bool(config.get('chat_id'))}

    @app.put('/api/v1/notifications/config',dependencies=[Depends(admin)])
    async def save_notifications(body:NotificationConfig):
        old = store.get('notification_config',{})
        config = {**old,'telegram_enabled':body.telegram_enabled,'categories':{**old.get('categories',{}),**body.categories}}
        if body.bot_token:
            if not re.fullmatch(r'\d{3,20}:[a-zA-Z0-9_-]{20,160}',body.bot_token): raise HTTPException(422,'Bot Token 格式无效。')
            config['bot_token']=body.bot_token
        if body.chat_id:
            if not re.fullmatch(r'-?\d{1,24}|@[a-zA-Z0-9_]{5,64}',body.chat_id): raise HTTPException(422,'Chat ID 格式无效。')
            config['chat_id']=body.chat_id
        if config['telegram_enabled'] and (not config.get('bot_token') or not config.get('chat_id')): raise HTTPException(422,'请填写 Bot Token 与 Chat ID。')
        store.set_many({'notification_config':config})
        return await notification_config()

    @app.post('/api/v1/notifications/test',dependencies=[Depends(admin)])
    async def test_notifications():
        config=store.get('notification_config',{})
        if not config.get('bot_token') or not config.get('chat_id'): raise HTTPException(409,'请先保存 Bot Token 与 Chat ID。')
        state=await engine().notifications.publish('test:'+secrets.token_hex(12),'测试通知','Telegram 通道测试。',test=True)
        return {'state':state,'message':'Telegram 已接受。' if state=='sent' else '发送失败或结果未确认；不会自动重试。'}

    @app.get('/api/v1/notifications',dependencies=[Depends(admin)])
    async def notifications(before:int=Query(2**62,ge=1)):
        return {'items':[dict(r) for r in store.db.execute("SELECT * FROM notifications WHERE level<>'log' AND id<? ORDER BY id DESC LIMIT 50",(before,))],
                'unread':store.db.execute("SELECT count(*) FROM notifications WHERE read_at IS NULL AND level<>'log'").fetchone()[0]}

    @app.post('/api/v1/notifications/read',dependencies=[Depends(admin)])
    async def read_notifications():
        with store.db: store.db.execute("UPDATE notifications SET read_at=? WHERE read_at IS NULL AND level<>'log'",(time.time(),))
        return {'read':True}

    @app.get('/api/v1/tasks',dependencies=[Depends(admin)])
    async def tasks(before:int=Query(2**62,ge=1)):
        # needs_review 是「已停下等人处理」的终态，不该一直占着进行中列表；
        # 它进历史，带失败原因与「核查并继续」，需要时再点。
        running="('queued','running','paused','waiting_login')"
        def build_list():
            # 0.8.21：整段构建放工作线程（2 条任务查询 + 3 条统计 + 每条 job_public）。
            # 40 条历史任务时这里约 100+ 次 SQL，以前全部压在事件循环上。
            active_rows=list(store.db.execute("SELECT * FROM organize_jobs WHERE state IN "+running+" ORDER BY id"))
            history_rows=list(store.db.execute("SELECT * FROM organize_jobs WHERE id<? AND state NOT IN "+running+" ORDER BY id DESC LIMIT 40",(before,)))
            # 0.8.19：一次算好三张表（状态计数 / Emby 交付 / 未识别与过滤计数），
            # 单次请求的查询数从「3×任务数」降为常数（40 条历史任务时约 120 -> 6）。
            ids=[r['id'] for r in active_rows+history_rows]
            counts_map,emby_map=app.state.job_maps(ids)
            stats=app.state.job_item_stats(ids)
            active=[job_public(r,counts_map,emby_map,stats) for r in active_rows]
            history=[job_public(r,counts_map,emby_map,stats) for r in history_rows]
            return {'active':active,'history':history,'has_more':len(history)==40}
        return await asyncio.to_thread(build_list)

    @app.get('/api/v1/tasks/events',dependencies=[Depends(admin)])
    async def task_events(request:Request):
        def stamp():
            # 0.8.20：一条廉价签名决定「这一轮要不要重建 payload」。
            # 4 个子查询覆盖所有会改变列表内容的来源：任务的 updated（阶段/消息/状态）、
            # 条目总数、已结束条目数、未读通知数。空闲时每 2 秒只花 1 条查询；
            # 任务在跑时 update_job 会不断推进 updated，界面照旧是实时的。
            return tuple(store.db.execute(
                "SELECT (SELECT MAX(updated) FROM organize_jobs),"
                " (SELECT COUNT(*) FROM organize_items),"
                " (SELECT COUNT(*) FROM organize_items WHERE state IN ('done','skipped','recycled')),"
                " (SELECT COUNT(*) FROM notifications WHERE read_at IS NULL AND level<>'log')").fetchone())
        def build():
            rows=[r for r in store.db.execute("SELECT * FROM organize_jobs WHERE state IN ('queued','running','paused','waiting_login','needs_review') OR id IN (SELECT id FROM organize_jobs ORDER BY id DESC LIMIT 30) ORDER BY id DESC")]
            ids=[r['id'] for r in rows]
            # 0.8.19：三条批量查询替代「每条任务 3 条」的 N+1。现场 30 个任务时
            # 单轮查询从 ~62 次降到 ~6 次，且与任务数无关（每 2 秒一轮）。
            counts_map,emby_map=app.state.job_maps(ids)
            stats=app.state.job_item_stats(ids)
            return {'items':[app.state.job_public(r,counts_map,emby_map,stats) for r in rows],
                'unread':store.db.execute("SELECT count(*) FROM notifications WHERE read_at IS NULL AND level<>'log'").fetchone()[0]}
        async def events():
            previous=''
            signature=None
            while not await request.is_disconnected():
                if not store.session(request.cookies.get('strimkeep_session','')): break
                # 0.8.21：stamp() 与 build() 都放工作线程（每 2 秒都会跑）。
                current=await asyncio.to_thread(stamp)
                if current!=signature or not previous:
                    # 只有签名变化才重建：build() 是 6 条批量查询 + 一次序列化。
                    signature=current
                    payload=json.dumps(await asyncio.to_thread(build),ensure_ascii=False,separators=(',',':'))
                    if payload!=previous:
                        yield 'event: tasks\ndata: '+payload+'\n\n';previous=payload
                    else:
                        yield ': heartbeat\n\n'
                else:
                    yield ': heartbeat\n\n'
                await asyncio.sleep(2)
        return StreamingResponse(events(),media_type='text/event-stream',headers={'X-Accel-Buffering':'no'})
