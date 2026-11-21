"""Emby 封面落地（对齐「Emby 封面生成」工具）。

机制与一致：图源是 Emby 自己的海报（Primary，回退 Backdrop），写成媒体目录里的
poster.jpg / fanart.jpg，让 Emby 重扫后自然展示；不从 TMDB 单独拉图。
每日 01:00 自动跑一次，面板可手动触发；写完逐条 Refresh 并记录时序（P3 验证点）。
"""
import asyncio
import time
from pathlib import Path, PurePosixPath

from . import commands
from .native_media_info import emby_request
from .fsutil import atomic_write
from .emby_connection import strm_mount_path


class CoverService:
    MAX_PER_RUN = 300

    def __init__(self, delivery, strms, store):
        self.delivery = delivery
        self.strms = Path(strms)
        self.store = store
        self.running = False
        self.manual_task = None
        self.queue_daily = False
        # 后台协程必须留引用：asyncio 只弱引用任务，裸 create_task 的返回值没人接住时
        # 可能在跑一半被 GC 掉（封面补齐就这么卡在后台过）。
        self.tasks = set()

    @property
    def active(self):
        return self.running or bool(self.manual_task and not self.manual_task.done())

    def spawn(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def close(self):
        """停机时收拢后台协程：取消并等它们退出，避免日志里出现半截异常。"""
        pending = [task for task in self.tasks if not task.done()]
        for task in pending:
            task.cancel()
        for task in pending:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    # ---- 路径映射：Emby Path（/media/...）↔ 本地 STRM 根 ----
    def _prefix(self):
        return str(PurePosixPath(strm_mount_path(self.store))).rstrip('/')

    def local_dir(self, emby_path, item_type):
        prefix = self._prefix()
        if not emby_path or not emby_path.startswith(prefix + '/'):
            return None
        rel = emby_path[len(prefix) + 1:]
        pure = PurePosixPath(rel)
        if item_type == 'Movie':
            pure = pure.parent          # 电影的 Path 是文件，海报放所在目录
        if '..' in pure.parts or not pure.name:
            return None
        return self.strms / Path(*pure.parts)

    # ---- 扫描与落地 ----
    async def sweep(self, limit=MAX_PER_RUN):
        if self.running:
            return {'error': '已有封面任务在执行。'}
        self.running = True
        started = time.time()
        written, timeline = [], []
        skipped = no_image = unmapped = 0
        try:
            connection = self.delivery.connection()
            if not connection:
                return {'error': 'Emby 未配置。'}
            url, key = connection
            headers = {'X-Emby-Token': key}
            response = await emby_request(self.delivery.http, 'GET', url + '/Items', headers=headers,
                params={'Recursive': 'true', 'IncludeItemTypes': 'Movie,Series', 'Limit': 5000,
                        'Fields': 'Path,ImageTags'}, timeout=30)
            response.raise_for_status()
            items = response.json().get('Items') or []
            for item in items:
                if len(written) >= limit:
                    break
                directory = self.local_dir(item.get('Path') or '', item.get('Type', ''))
                if not directory or not directory.is_dir():
                    unmapped += 1
                    continue
                tags = item.get('ImageTags') or {}
                jobs = []
                if not (directory / 'poster.jpg').is_file() and tags.get('Primary'):
                    jobs.append(('Primary', directory / 'poster.jpg'))
                if not (directory / 'fanart.jpg').is_file() and (item.get('BackdropImageTags') or []):
                    jobs.append(('Backdrop', directory / 'fanart.jpg'))
                if not jobs:
                    # 已有本地文件 → 跳过；本地和图源都缺 → 无图源，计入 no_image。
                    if (directory / 'poster.jpg').is_file() or (directory / 'fanart.jpg').is_file():
                        skipped += 1
                    else:
                        no_image += 1
                    continue
                entry = {'id': str(item.get('Id')), 'name': item.get('Name', ''),
                         'files': [], 'write_at': time.time()}
                ok = True
                for kind, target in jobs:
                    try:
                        image = await emby_request(self.delivery.http, 'GET',
                            f"{url}/Items/{item['Id']}/Images/{kind}", headers=headers, timeout=20)
                        image.raise_for_status()
                        if not image.content.startswith(b'\xff\xd8'):
                            raise ValueError('返回的不是 JPEG')
                        atomic_write(target, image.content, self.strms)
                        entry['files'].append(target.name)
                    except Exception:
                        ok = False
                        break
                if not ok:
                    for target in list(entry['files']):
                        (directory / target).unlink(missing_ok=True)
                    entry['files'] = []
                    no_image += 1
                    continue
                written.append(entry)
                # 时序验证点：写完立刻 Refresh，记录两个时刻
                refresh_at = time.time()
                try:
                    await emby_request(self.delivery.http, 'POST',
                        f"{url}/Items/{item['Id']}/Refresh", headers=headers,
                        params={'Recursive': 'true', 'ImageRefreshMode': 'FullRefresh',
                                'MetadataRefreshMode': 'Default'}, timeout=15)
                    entry['refresh_at'] = refresh_at
                    entry['refresh_ms'] = round((time.time() - refresh_at) * 1000)
                    timeline.append(entry)
                except Exception:
                    entry['refresh_error'] = True
            summary = {'at': started, 'seconds': round(time.time() - started, 1),
                       'scanned': len(items), 'written': len(written), 'skipped': skipped,
                       'no_image': no_image, 'unmapped': unmapped, 'items': written[:50],
                       'timeline': timeline[:50], 'running': False}
            self.store.event('cover',
                             f"封面落地完成：新写 {len(written)} 个（poster/fanart），跳过已有 {skipped}，"
                             f"无图源 {no_image}，未映射 {unmapped}；Refresh 记录 {len(timeline)} 条。")
            return summary
        except Exception as exc:
            self.store.event('cover', f'封面任务失败：{str(exc)[:150]}')
            return {'error': str(exc)[:150]}
        finally:
            self.running = False

    async def run(self):
        result = await self.sweep()
        failure = result.get('error', '')
        previous = self.store.meta_json('cover_status', {}) or {}
        last = previous.get('last') if failure else result
        self.store.set_meta('cover_status', {'last': last, 'error': failure})
        if not failure and result.get('timeline'):
            self.spawn(self.verify_refresh(result))
        return result

    async def verify_refresh(self, summary):
        """P3 时序验证：Refresh 后 60 秒回查 Emby 的 DateModified 是否已推进。"""
        entries = summary['timeline']
        connection = self.delivery.connection()
        if not connection or not entries:
            return []
        url, key = connection
        headers = {'X-Emby-Token': key}
        ids = ','.join(e['id'] for e in entries[:50])
        await asyncio.sleep(60)
        try:
            response = await emby_request(self.delivery.http, 'GET', url + '/Items', headers=headers,
                params={'Ids': ids, 'Fields': 'DateModified'}, timeout=20)
            response.raise_for_status()
            by_id = {str(i.get('Id')): i for i in response.json().get('Items') or []}
        except Exception as exc:
            self.store.event('cover', f'Emby 封面刷新回查失败：{type(exc).__name__}')
            return []
        results = []
        for entry in entries[:50]:
            item = by_id.get(entry['id']) or {}
            modified = item.get('DateModified')
            results.append({'id': entry['id'], 'name': entry.get('name', ''),
                            'refresh_ms': entry.get('refresh_ms'),
                            'emby_date_modified': modified,
                            'emby_confirmed': bool(modified)})
        self.store.event('cover',
                         f'Refresh 时序验证：{sum(1 for r in results if r["emby_confirmed"])}/{len(results)} '
                         '条在 60 秒内被 Emby 确认（DateModified 已推进）。')
        status = self.store.meta_json('cover_status', {}) or {}
        last = status.get('last') or {}
        if last.get('at') == summary['at']:
            self.store.set_meta('cover_status', {**status, 'last': {**last, 'refresh_verification': results}})
        return results

    # ---- 调度 ----
    async def tick_daily(self):
        """每天 01:00 自动跑一次（补齐存量与新增缺图）。"""
        today = time.strftime('%Y-%m-%d')
        if time.localtime().tm_hour != 1 or self.store.meta('cover_last_day') == today:
            return
        if self.active:
            return
        if self.queue_daily and commands.submit(self.store, commands.COVER_SWEEP, unique=True) is None:
            return
        with self.store.db:
            self.store.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', ('cover_last_day', today))
        self.store.event('cover', '每日封面补齐已提交（01:00 定时）。' if self.queue_daily
                         else '每日封面补齐开始（01:00 定时）。')
        if not self.queue_daily:
            await self.run()


def install_cover(app, admin):
    from fastapi import Depends, HTTPException

    @app.post('/api/v1/cover/run', dependencies=[Depends(admin)])
    async def run_cover():
        engine = app.state.organizer
        if getattr(engine, 'web_only', False):
            store = app.state.store
            if not commands.worker_ready(store):
                raise HTTPException(503, '后台 Worker 未就绪，请稍后重试。')
            command_id = await asyncio.to_thread(commands.submit, store, commands.COVER_SWEEP, unique=True)
            if command_id is None:
                raise HTTPException(409, '封面补齐已排队或正在执行。')
            return {'started': True, 'queued': True, 'command_id': command_id,
                    'message': '封面补齐已提交后台。'}
        if engine.cover.active:
            raise HTTPException(409, '封面任务正在执行。')
        engine.cover.manual_task = engine.cover.spawn(engine.cover.run())
        return {'started': True, 'queued': False, 'message': '封面补齐已开始。'}

    @app.get('/api/v1/cover/status', dependencies=[Depends(admin)])
    async def cover_status():
        engine = app.state.organizer
        store = app.state.store
        command = None
        if getattr(engine, 'web_only', False):
            command = store.db.execute(
                "SELECT state,message FROM worker_commands WHERE kind=? ORDER BY id DESC LIMIT 1",
                (commands.COVER_SWEEP,)).fetchone()
            state = command['state'] if command else ''
            running = state in ('queued', 'running')
        else:
            state = 'running' if engine.cover.active else ''
            running = state == 'running'
        status = store.meta_json('cover_status', {}) or {}
        error = command['message'] if command and state == 'failed' else status.get('error', '')
        return {'running': running, 'queued': state == 'queued', 'last': status.get('last'),
                'error': '' if running else error}
