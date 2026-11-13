"""媒体信息接口：把本项目的 sidecar 写进 Emby 的 library.db。

等价于装「神医助手」插件后它的入库动作，区别只在于从外部写：
Emby 的公开 API 写不了 MediaStreams，它自己也不会重算 STRM（实测四种方式都是 0），
所以只能直接写库。库路径默认 ``/emby-data/library.db``（docker-compose 里把 Emby 的
data 目录挂进来），可用设置项 ``emby_library_db`` 或环境变量覆盖。
"""
import asyncio
import os
import time
from pathlib import Path

from fastapi import Depends, HTTPException, Query
from croniter import croniter
from pydantic import BaseModel, ConfigDict, Field

from .emby_media_inject import DEFAULT_DB, Injector, emby_strm_mount, library_db
from .media_info_log import install as install_failures, recorder, summary as failures_summary


class MediaInfoConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')
    cron_enabled: bool | None = None
    cron: str | None = Field(default=None, max_length=100)


def make_injector(app, store, path=''):
    """带失败清单的注入器：逐条结果落库，才能回答"哪一部没写进去、为什么"。

    必须是模块级函数：cron 兜底的 ``inject_loop`` 也要用它。它原来是
    ``install_media_info`` 里的闭包，兜底路径一跑就 NameError —— 0.9.84 起每次启动
    都往操作记录里写一条「兜底写入媒体信息失败：NameError」，只是没人翻日志。
    """
    return Injector(path or library_db(store), str(app.state.strms),
                    emby_strm_mount(store), recorder=recorder(store))


async def inject_once(app, store):
    """兜底跑一轮并返回统计；抽出来才能测（不依赖 croniter 和 while True）。"""
    path = library_db(store)
    if not path:
        raise ValueError('emby_library_db 未配置')
    injector = make_injector(app, store, path)
    return await asyncio.to_thread(injector.run)


def install_media_info(app, store, admin):
    install_failures(app, store, admin)

    def run_inject(dry_run=False, limit=0):
        return make_injector(app, store).run(limit, bool(dry_run))

    @app.get('/api/v1/emby/media-info/config', dependencies=[Depends(admin)])
    async def media_info_config():
        schedule = store.get('schedule_config', {}) or {}
        sync_config = store.get('sync_config', {}) or {}
        return {'extract': bool(sync_config.get('emby_media_info', False)),
                'cron_enabled': bool(schedule.get('media_info_enabled', False)),
                'cron': schedule.get('media_info_cron', '0 4 * * *'),
                'timezone': schedule.get('timezone', 'Asia/Shanghai'),
                'library_db': library_db(store)}

    @app.put('/api/v1/emby/media-info/config', dependencies=[Depends(admin)])
    async def save_media_info_config(body: MediaInfoConfig):
        patch = body.model_dump(exclude_none=True)
        schedule = dict(store.get('schedule_config', {}) or {})
        if 'cron_enabled' in patch:
            schedule['media_info_enabled'] = bool(patch['cron_enabled'])
        if 'cron' in patch:
            expression = str(patch['cron']).strip()
            if len(expression.split()) != 5 or not croniter.is_valid(expression):
                raise HTTPException(422, 'Cron 表达式需要 5 段，例如 0 4 * * *。')
            schedule['media_info_cron'] = expression
        if patch:
            store.set_many({'schedule_config': schedule})
        return await media_info_config()
    @app.get('/api/v1/emby/media-info/status', dependencies=[Depends(admin)])
    async def media_info_status():
        path = library_db(store)
        if not path:
            return {'library_db': '', 'exists': False, 'pending': None, 'configured': False,
                    'failures': failures_summary(store),
                    'hint': '还没有配置 Emby 的 library.db 路径：挂载 Emby 的 data 目录并设置 '
                            'STRIMKEEP_EMBY_LIBRARY_DB（例如 /emby-data/library.db）。'}
        exists = Path(path).is_file()
        pending = None
        if exists:
            def count():
                injector = Injector(path, str(app.state.strms), emby_strm_mount(store))
                conn = injector.connect()
                try:
                    return len(injector.targets(conn))
                finally:
                    conn.close()
            try:
                pending = await asyncio.to_thread(count)
            except Exception:
                pending = None
        return {'library_db': path, 'exists': exists, 'pending': pending, 'configured': True,
                'failures': failures_summary(store)}

    @app.post('/api/v1/emby/media-info/inject', dependencies=[Depends(admin)])
    async def media_info_inject(dry_run: int = Query(0, ge=0, le=1),
                                limit: int = Query(0, ge=0, le=200000)):
        path = library_db(store)
        if not path:
            raise HTTPException(409, '还没有配置 Emby 的 library.db 路径：请挂载 Emby 的 data 目录，'
                                     '并设置环境变量 STRIMKEEP_EMBY_LIBRARY_DB（例如 /emby-data/library.db）。')
        if not Path(path).is_file():
            raise HTTPException(409, '找不到 Emby 的 library.db：%s' % path)
        injector = make_injector(app, store, path)
        try:
            stats = await asyncio.to_thread(injector.run, limit, bool(dry_run))
        except Exception as exc:
            raise HTTPException(500, '写入失败：%s' % type(exc).__name__) from None
        if not stats.get('ok'):
            raise HTTPException(409, stats.get('message') or '写入失败')
        if not dry_run:
            store.event('emby', '媒体信息写入 Emby：补 %d 条（%d 条流、%d 条章节），'
                                '%d 条没有 sidecar 跳过、%d 条失败。'
                        % (stats['injected'], stats['streams'], stats['chapters'],
                           stats['no_sidecar'], stats['failed']))
        return stats


async def inject_cycle(app, store):
    """兜底只跑一轮；没配置 Emby 数据库路径时安静跳过。

    这条链路是可选功能：Emby 自己能探测 STRM 的媒体流时根本用不到它。
    以前未配置也会抛 ValueError，由 inject_loop 记成「兜底写入媒体信息失败」，
    现场把它当成媒体信息写入故障来查了。
    """
    if not library_db(store):
        return {'ok': True, 'skipped': 'unconfigured'}
    return await inject_once(app, store)


async def inject_loop(app, store, tick=60.0):
    """媒体信息兜底：只在 cron 到点时扫一次「漏写的」，不掺和入库那条实时链路。

    分工是明确的：
    - 新条目：``native_media_info.write_sidecar()`` 写完 sidecar **立刻**写进 Emby（追更）；
    - 漏网的（写库瞬间失败、旧条目、库被重算）：由这里的 cron 兜底补一次；
    - 手动：设置页的「写入媒体信息」按钮。
    只补缺的条目、幂等，失败只记一条操作记录。
    """
    from croniter import croniter
    from datetime import datetime
    from zoneinfo import ZoneInfo

    last_fire = 0.0
    warned = False
    while True:
        due = False
        try:
            schedule = store.get('schedule_config', {}) or {}
            if schedule.get('media_info_enabled'):
                zone = ZoneInfo(schedule.get('timezone', 'Asia/Shanghai'))
                expression = schedule.get('media_info_cron', '0 4 * * *')
                now = datetime.now(zone)
                fire = croniter(expression, now).get_prev(datetime)
                if fire.timestamp() > last_fire:
                    last_fire = fire.timestamp()
                    due = True
        except Exception:
            due = False
        if due:
            try:
                stats = await inject_cycle(app, store)
                if stats.get('skipped') == 'unconfigured':
                    # 每个进程最多提示一次，且写成「未启用」而不是「失败」。
                    if not warned:
                        warned = True
                        store.event('emby', '媒体信息兜底未启用：未配置 Emby 数据库路径'
                                           '（可选功能，不影响入库与播放）。')
                elif stats.get('ok') and stats.get('injected'):
                    store.event('emby', '兜底写入媒体信息：补 %d 条（%d 条流、%d 条章节）。'
                                % (stats['injected'], stats['streams'], stats['chapters']))
                elif stats.get('ok') and stats.get('failed'):
                    store.event('emby', '兜底写入媒体信息：%d 条失败（明细见媒体信息失败清单）。'
                                % stats['failed'])
            except Exception as exc:
                store.event('emby', '兜底写入媒体信息失败：%s'
                            % (str(exc)[:80] or type(exc).__name__))
        await asyncio.sleep(tick)

