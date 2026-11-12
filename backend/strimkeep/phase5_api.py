"""Phase-five admin endpoints. All mutations share session/CSRF authorization."""
import asyncio
import json
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
        """STRM 体检：哪些是本项目管的、哪些是别的工具写的、哪些登记了却没文件。

        现场踩过的坑：另一条 STRM 产线在同一个库里也写 STRM（/play/share/<id>/<下划线名>），
        它们不进我们的台账，却在 Emby 里制造重复条目；台账里也有几条文件已经没了。
        只读，不改任何文件。
        """
        def scan():
            root=Path(engine().root)
            heads={}
            for path in root.rglob('*.strm'):
                rel=path.relative_to(root).as_posix()
                try: heads[rel]=path.read_text(encoding='utf-8')[:200]
                except (OSError,UnicodeError): heads[rel]=''
            managed={r['relative_path'] for r in store.db.execute("SELECT relative_path FROM playbacks WHERE revoked=0")}
            managed|={r['strm_path'] for r in store.db.execute("SELECT strm_path FROM share_strms WHERE revoked=0")}
            external=sorted(rel for rel in heads if rel not in managed)
            missing=sorted(rel for rel in managed if rel not in heads)
            def kind(head):
                if '/play115/' in head: return 'ours'
                if '/play/share/' in head: return 'legacy'
                return 'unknown'
            return {'total':len(heads),'managed':len(heads)-len(external),
                    'external_total':len(external),
                    'external':[{'path':rel,'kind':kind(heads[rel])} for rel in external[:limit]],
                    'missing_total':len(missing),'missing':missing[:limit]}
        return await asyncio.to_thread(scan)

    @app.post('/api/v1/strm/audit/cleanup',dependencies=[Depends(admin)])
    async def strm_audit_cleanup():
        """清理残次品：非本项目的 STRM + 台账里文件已经不在的登记。

        只动两类：
        1. 内容不是 /play115/ 的外部 STRM（另一条产线或旧工具留下的，路径变更时没收管）；
        2. 登记在 playbacks / share_strms 里、但磁盘上已经没有的输出（置撤销）。
        本项目格式（/play115/）的 STRM 一个都不删，哪怕没登记——那多半是台账丢了。
        """
        def run():
            from .fsops import purge_strm
            root=Path(engine().root)
            heads={}
            for path in root.rglob('*.strm'):
                rel=path.relative_to(root).as_posix()
                try: heads[rel]=path.read_text(encoding='utf-8')[:200]
                except (OSError,UnicodeError): heads[rel]=''
            managed={r['relative_path'] for r in store.db.execute("SELECT relative_path FROM playbacks WHERE revoked=0")}
            managed|={r['strm_path'] for r in store.db.execute("SELECT strm_path FROM share_strms WHERE revoked=0")}
            removed, kept, failed = [], [], []
            for rel in sorted(heads):
                if rel in managed:
                    continue
                if '/play115/' in heads[rel]:
                    kept.append(rel)
                    continue
                try:
                    purge_strm(root/rel, root, store=store)
                    removed.append(rel)
                except Exception:
                    failed.append(rel)
            revoked_playbacks, revoked_shares = 0, 0
            with store.db:
                for row in store.db.execute("SELECT id,relative_path FROM playbacks WHERE revoked=0").fetchall():
                    if not (root/row['relative_path']).is_file():
                        store.db.execute('UPDATE playbacks SET revoked=1 WHERE id=?', (row['id'],))
                        revoked_playbacks += 1
                for row in store.db.execute("SELECT id,strm_path FROM share_strms WHERE revoked=0").fetchall():
                    if not (root/row['strm_path']).is_file():
                        store.db.execute('UPDATE share_strms SET revoked=1 WHERE id=?', (row['id'],))
                        revoked_shares += 1
            return {'removed':len(removed),'kept':len(kept),'failed':len(failed),
                    'revoked_playbacks':revoked_playbacks,'revoked_shares':revoked_shares,
                    'samples':removed[:10]}
        result=await asyncio.to_thread(run)
        if result['removed'] or result['revoked_playbacks'] or result['revoked_shares']:
            store.event('strm_sync',
                        f"STRM 体检清理：删除非本项目 STRM {result['removed']} 个，"
                        f"撤销悬空登记 {result['revoked_playbacks']+result['revoked_shares']} 条"
                        f"（网盘库 {result['revoked_playbacks']} / 分享库 {result['revoked_shares']}）。")
        return result

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
