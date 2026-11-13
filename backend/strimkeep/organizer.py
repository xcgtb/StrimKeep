"""Persistent FIFO scan/organize worker. Remote intents precede writes; each result is read back.

Configured upgrades recycle verified individual files; cleanup retires only
task-related, scoped directories whose emptiness is checked immediately before.
"""
import asyncio
import hashlib
import json
import os
import re
import shutil
import secrets
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

import httpx

from .cloud115 import CloudError, fatal
from .playaddr import identity_of, play115_url, play_url
from . import share_service
from .offline_api import OfflinePostProcess
from .organize_options import file_title, filter_files, library_root, options, title_options
from .strm_options import StrmOptions, skip_reason, copy_cloud_metadata
from .archive import ArchiveIndex
from .media import (METADATA, SUBTITLES, VIDEOS, NeedsReview, choose_match, title_candidates, numeral_variants,
                    pick_best, scored_candidates, match_threshold, has_cjk,
                    naming, work_parts, parse_name, bind_subtitles, require_single_episode, split_video_marker, chinese_split_marker, normal, subtitle_suffix,
                    media_from_filename, format_skip_message, require_work_identity, episode_key)
from .probe import probe
from .upgrade import Upgrader, wash_reason
from .cleanup import EmptyDirectoryCleanup
from .unrecognized import Unrecognized
from .cache import MediaCache
from .outputs import Outputs
from .sync import Sync
from .tasks import Tasks
from .message_templates import cloud_sign_notice, cloud_recycle_notice
from .notifications import Notifications
from .transfer import Transfer
from .telegram_bot import TelegramBot
from .store import digest
from .emby import EmbyDelivery, EmbyPrewarm
from .metadata_files import write_sidecars
from .channel_monitor import ChannelMonitor


from .core import Discarded, Stopped, UncertainState, atomic_write, dumps  # noqa: F401


_prepare_diagnostics = ContextVar('prepare_diagnostics', default=None)


def _count_tmdb(category):
    stats = _prepare_diagnostics.get()
    if stats is not None:
        counters = stats.setdefault('_tmdb', {})
        counters[category] = counters.get(category, 0) + 1


@contextmanager
def _prepare_measure(stats, label, cloud):
    """Accumulate elapsed time and scoped 115 reads without changing the operation."""
    if stats is None:
        yield
        return
    started = time.monotonic()
    ledger = getattr(cloud, 'ledger', None)
    before = ledger.get('read', 0) if isinstance(ledger, dict) else None
    try:
        yield
    finally:
        entry = stats.setdefault(label, {'calls': 0, 'seconds': 0.0, 'reads': 0, 'has_reads': before is not None})
        entry['calls'] += 1
        entry['seconds'] += time.monotonic() - started
        if before is not None:
            entry['reads'] += max(0, ledger.get('read', 0) - before)



class Organizer:
    def __init__(self, store, cloud, http, strm_root, probe_fn=None):
        self.store, self.cloud, self.http = store, cloud, http
        self.root = Path(strm_root).resolve()
        self.probe = probe_fn or probe
        self.worker = None
        self.extra_workers = []
        self.drain_task = None
        # Serialize publication/reconciliation at file boundaries while separate
        # workers may scan, transfer and resolve metadata concurrently.
        self.publication_gate = asyncio.Lock()
        self.cleanup_gate = asyncio.Lock()
        self.scan_receive_gate = asyncio.Lock()
        self.clock = None
        self.scheduler_fault = False
        self.lane_faults = {}
        self.closing = False
        self._pruned_at = 0.0
        # 0.8.20：本地 STRM 全量重建（预览/快照/回滚）在独立模块里，避免
        # create_app 继续膨胀；这里只持有实例，不引入额外状态。
        from .rebuild import Rebuild
        self.rebuild = Rebuild(self)
        self._share_checked = 0.0
        self._share_check_task = None
        self._share_scanned = 0.0
        self._offline_scanned = 0.0
        self._offline_scan_cursor = 0
        self._notice_retried = 0.0
        self._login_probe = 0.0
        # boundary() 的热路径缓存：任务事实随 updated 失效，磁盘与账号走短 TTL。
        self._boundary_cache = {}
        self._disk_checked = 0.0
        self._account_cache = None
        self._season_lookup_unavailable = {}
        self.notice_tasks = set()
        self.notice_keys = set()
        self.show_notices = {}
        self.publication_queues = {}
        self.publication_workers = {}
        self.index = ArchiveIndex(self)
        self.upgrader = Upgrader(self)
        self.cleanup = EmptyDirectoryCleanup(self)
        self.unrecognized = Unrecognized(self, UncertainState)
        self.measurements = MediaCache(self)
        self.outputs = Outputs(self)
        self.sync = Sync(self)
        self.tasks = Tasks(self)
        self.notifications = Notifications(store, http)
        self.transfer = Transfer(self)
        self.bot = TelegramBot(self)
        self.emby = EmbyDelivery(self)
        self.prewarm = EmbyPrewarm(self)
        self.offline = OfflinePostProcess(self)
        from .telegram_session import TelegramSession
        self.telegram = TelegramSession(self.store)
        self.channels = ChannelMonitor(self)

    def start(self):
        # Interrupted requests are rechecked in process_item(), not rerun from scratch.
        # 0.9.60：running → queued 与 notify inflight → unknown 都交给仓储层。
        self.jobs.recover_running()
        with self.store.db:
            self.store.db.execute("UPDATE notifications SET telegram_state='unknown' WHERE telegram_state='inflight'")
        if not self.store.meta('phase5_notice_since'):
            with self.store.db:
                self.store.db.execute('INSERT INTO meta VALUES(?,?)',('phase5_notice_since',str(time.time())))
        self.worker = asyncio.create_task(self.supervise_lane('organize'))
        self.extra_workers = [asyncio.create_task(self.supervise_lane('transfer')), asyncio.create_task(self.supervise_lane('sync'))]
        self.clock = asyncio.create_task(self.schedule_loop())
        self.channels.start()
        self.bot.start()
        self.emby.task = asyncio.create_task(self.emby.loop())
        self.emby.media_info.task = asyncio.create_task(self.emby.media_info.loop())
        self.drain_task = asyncio.create_task(self.deep_delete_loop())
        from .cover_service import CoverService
        self.cover = CoverService(self.emby, self.root, self.store)

    async def deep_delete_loop(self):
        """深度删除的后台 worker：按序执行队列，单条回读确认。"""
        from . import deep_delete
        while not self.closing:
            moved = 0
            try:
                moved = await deep_delete.drain(self, limit=3)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 队列异常不能拖垮后台
                self.store.event('deep_delete', '深度删除 · 后台队列异常：%s' % str(exc)[:120])
            await asyncio.sleep(1.0 if moved else 5.0)

    async def close(self):
        self.closing = True
        await self.channels.close()
        await self.bot.close()
        if self.emby.task:
            self.emby.task.cancel()
            await asyncio.gather(self.emby.task, return_exceptions=True)
        if self.emby.media_info.task:
            self.emby.media_info.task.cancel()
            await asyncio.gather(self.emby.media_info.task, return_exceptions=True)
        for task in self.notice_tasks: task.cancel()
        if self.notice_tasks: await asyncio.gather(*list(self.notice_tasks), return_exceptions=True)
        cover = getattr(self, 'cover', None)
        if cover is not None and hasattr(cover, 'close'):
            await cover.close()
        if self.drain_task:
            self.drain_task.cancel()
            await asyncio.gather(self.drain_task, return_exceptions=True)
        if self.clock:
            self.clock.cancel()
            try:
                await self.clock
            except asyncio.CancelledError:
                pass
        for worker in self.extra_workers:
            worker.cancel()
        if self.extra_workers:
            await asyncio.gather(*self.extra_workers, return_exceptions=True)
        if self.worker:
            self.worker.cancel()
            try:
                await self.worker
            except asyncio.CancelledError:
                pass

    @staticmethod
    def family_key(config):
        """任务族：种类 + 账号 + 来源目录集合。

        登录失效期间每个调度周期都会入队一次，恢复时只合并「同一族」的重复
        入队。以前不分组，把不同种类/不同目录的任务一起取消，转存的分享链接
        与 STRM 同步的目录集合都不会被重放，等于静默丢了一批工作。
        """
        if not isinstance(config, dict):
            config = {}
        try:
            sources = tuple(sorted(str(cid) for cid in (config.get('source_cids') or [])))
        except (TypeError, ValueError):
            sources = ()
        return (config.get('job_kind', 'organize'), str(config.get('account_id') or ''), sources)

    def login_ready(self):
        rows = self.store.db.execute(
            "SELECT id,config FROM organize_jobs WHERE state='waiting_login' ORDER BY id").fetchall()
        families, order = {}, []
        for row in rows:
            try:
                config = json.loads(row['config'] or '{}')
            except (TypeError, ValueError):
                config = {}
            key = self.family_key(config)
            if key not in families:
                families[key] = []
                order.append(key)
            families[key].append(row['id'])
        for key in order:
            ids = families[key]
            for job_id in ids[1:]:
                self.jobs.update(job_id, state='cancelled', stage='登录恢复后合并',
                                       message='同一类任务在等待登录期间重复入队，已合并为一次继续。',
                                       notify_state='not_needed')
            self.jobs.update(ids[0], state='queued', stage='登录恢复，等待继续')

    async def probe_login(self):
        """登录失效时的低频只读探测：恢复后自动继续，不必等人工扫码才往下跑。"""
        now = time.time()
        if now - self._login_probe < 600:
            return
        if not self.store.db.execute("SELECT 1 FROM organize_jobs WHERE state='waiting_login' LIMIT 1").fetchone():
            return
        self._login_probe = now
        try:
            await self.cloud.status()
        except CloudError as exc:
            if exc.code in ('LOGIN_REQUIRED', 'LOGIN_INVALID'):
                return
            return
        except Exception:
            return
        self.store.event('system', '115 登录已恢复，等待中的任务继续执行。')
        self.login_ready()

    # 只有「外部原因」停下的任务才能自动重排：115 限流冷却、云端暂时不可用。
    # 识别/命名/核查类（NeedsReview、UncertainState）必须人工看过再继续。
    DISK_CHECK_SECONDS = 10
    ACCOUNT_CACHE_SECONDS = 5
    AUTO_RETRY_STAGES = ('115 限流冷却', 'CLOUD_UNAVAILABLE', 'TMDB 请求失败')
    AUTO_RETRY_BASE_SECONDS = 300
    AUTO_RETRY_MAX = 5

    def auto_retry_stalled(self, now=None):
        """把限流/云端抖动停下的任务在冷却后重新排队，最多 AUTO_RETRY_MAX 次。

        以前这类任务停在 needs_review 就再也没有下文，文案却写着「稍后自动重排」。
        这里补上真正的重排：同一任务族已有排队/运行中的任务时跳过，避免堆积。
        """
        now = time.time() if now is None else now
        rows = self.store.db.execute(
            "SELECT * FROM organize_jobs WHERE state='needs_review' AND updated<=? ORDER BY id LIMIT 20",
            (now - self.AUTO_RETRY_BASE_SECONDS,)).fetchall()
        requeued = []
        for row in rows:
            stage = str(row['stage'] or '')
            if not any(marker in stage for marker in self.AUTO_RETRY_STAGES):
                continue
            try:
                config = json.loads(row['config'] or '{}')
            except (TypeError, ValueError):
                continue
            if not isinstance(config, dict):
                continue
            tries = int(config.get('auto_retry_count') or 0)
            if tries >= self.AUTO_RETRY_MAX:
                continue
            # 指数退避：5 分钟起，翻倍，封顶 1 小时。
            if now - float(row['updated'] or 0) < min(3600, self.AUTO_RETRY_BASE_SECONDS * 2 ** tries):
                continue
            kind = config.get('job_kind', 'organize')
            busy = self.store.db.execute(
                "SELECT 1 FROM organize_jobs WHERE id<>? AND state IN ('queued','running')"
                " AND COALESCE(json_extract(config,'$.job_kind'),'organize')=? LIMIT 1",
                (row['id'], kind)).fetchone()
            if busy:
                continue
            config['auto_retry_count'] = tries + 1
            self.jobs.update(
                row['id'], state='queued',
                stage='外部限制解除，自动重试（%d/%d）' % (tries + 1, self.AUTO_RETRY_MAX),
                message='检测到外部限制已解除，任务自动重新排队；已完成步骤不会重复执行。',
                config=dumps(config))
            transient = ('CLOUD_UNAVAILABLE' in str(row['stage'] or '')
                         or 'TMDB 请求失败' in str(row['stage'] or ''))
            # 抖动引起的首次重排也只是心跳级别的信息：任务中心已经显示「自动重试（1/5）」，
            # 日志中心留到第二次仍失败再记，避免同一次抖动写出两条噪音。
            if not transient or tries >= 1:
                self.store.event('schedule', '任务 %d：外部限制解除，已自动重新排队（第 %d/%d 次）。'
                                 % (row['id'], tries + 1, self.AUTO_RETRY_MAX))
            requeued.append(row['id'])
        return requeued

    def job(self, job_id):
        return self.store.db.execute('SELECT * FROM organize_jobs WHERE id=?', (job_id,)).fetchone()

    @property
    def jobs(self):
        """任务仓储层（0.9.60）。测试里的 FakeStore 没有这个属性，这里按需挂上。"""
        instance = getattr(self.store, 'jobs', None)
        if instance is None:
            from .jobs import Jobs
            instance = Jobs(self.store)
            try:
                self.store.jobs = instance
            except AttributeError:
                pass
        return instance

    def update_job(self, job_id, **values):
        row = self.job(job_id)
        if values.get('stage') and row['stage'] != values['stage']:
            # 定时扫描的阶段进度不进日志：每 15 分钟一轮，只会把日志刷成一堵墙。
            # 手动任务保留，方便排查。
            if not json.loads(row['config']).get('scheduled'):
                self.store.event('progress', f'任务 {job_id}：' + values['stage'])
        # 0.9.60：迁移校验与终态收口都在 jobs 仓储层完成，这里只负责阶段事件的呈现。
        self.jobs.update(job_id, **values)

    def update_item(self, item_id, **values):
        # 0.9.60：条目写入同样过迁移表，拼错状态或非法迁移会当场抛 JobStateError。
        self.jobs.update_item(item_id, **values)
        if values.get('state') == 'review':
            self.note_review(item_id, values.get('step'), values.get('message'))

    def note_review(self, item_id, step=None, message=None):
        """转人工复核时留一条事件。

        `organize_items` 明细会被 prune 按 job_days/keep_jobs 清掉（那张表每行都带整份
        plan JSON，不清理库会一直涨）。于是事后无法回答「这批视频当初为什么没被识别」——
        而那恰恰是提升识别能力唯一有用的数据。事件表保留 90 天/2 万条，且是固定短文案，
        适合承载这类追溯信息。
        """
        try:
            row = self.store.db.execute(
                'SELECT job_id, original FROM organize_items WHERE id=?', (item_id,)).fetchone()
            if row is None:
                return
            try:
                name = str(json.loads(row['original'] or '{}').get('name') or '')
            except Exception:
                name = ''
            detail = '：'.join(x for x in (step, message) if x)
            self.store.event('organize', f'任务 {row["job_id"]} 转人工复核：{name or ("条目 " + str(item_id))}'
                                         + (f'（{detail}）' if detail else ''))
        except Exception:
            # 留痕失败不能反过来影响整理主流程。
            pass

    def halt(self, job_id, state, message, stage='等待核查', quiet=False):
        current = self.job(job_id)['state']
        self.update_job(job_id, state=current if current in ('paused', 'cancelled') else state,
                        message=message, stage=stage)
        # 卡在识别阶段的文件还没进过 update_item，状态留着 discovered：任务已经是
        # 「等待核查」，点进去每个文件却还是「发现文件」，看起来像任务没启动。
        # 这里一并落库，并把失败原因写到条目上。
        self.jobs.mark_discovered_as_review(job_id, stage, message)
        # quiet：传输层抖动（超时/连接重置）会自动退避重排并自愈，首次失败不该刷日志；
        # 任务中心的 stage/message 仍然如实显示，日志只在复发或放弃时才值得看。
        if not quiet:
            self.store.event('organize', f'任务 {job_id} / {stage}：{message}')

    def boundary(self, job_id):
        """文件边界的三个校验：磁盘余量、任务仍在运行、115 账号未变。

        原实现每一步都要重新查库、解析整份任务 config、解密 settings 里的
        cookie、再打两次 disk_usage；而它在 manifest 的按文件循环里被调用。
        这里按「任务 updated 时间戳」与短 TTL 缓存这些事实：任务状态一旦变化
        update_job 就会写新的 updated，缓存自然失效，语义不变。
        """
        self.check_disk()
        row = self.job(job_id)
        if self.closing or row['state'] != 'running':
            raise Stopped()
        key = (row['id'], row['updated'])
        cached = self._boundary_cache.get(job_id)
        if cached and cached[0] == key:
            account = cached[1]
        else:
            try:
                config = json.loads(row['config'] or '{}')
            except (TypeError, ValueError):
                config = {}
            account = str((config or {}).get('account_id') or '')
            if len(self._boundary_cache) > 64:
                self._boundary_cache.clear()
            self._boundary_cache[job_id] = (key, account)
        if account and account != self.current_account():
            raise UncertainState('当前 115 账号与任务创建时不同，禁止继续操作这些文件 ID。')

    def check_disk(self, now=None):
        now = time.time() if now is None else now
        if now - self._disk_checked < self.DISK_CHECK_SECONDS:
            return
        for path in (self.store.root, self.root):
            if path.exists() and shutil.disk_usage(path).free < 32 * 1024 * 1024:
                raise UncertainState('本地磁盘余量不足，已暂停新修改；释放空间后继续任务。')
        self._disk_checked = now

    def current_account(self):
        """当前 cookie 里的 115 UID，5 秒内复用（含解密开销）。"""
        now = time.time()
        cookie = self.store.get('cookie') or ''
        cached = self._account_cache
        if cached and cached[0] == cookie and now - cached[1] < self.ACCOUNT_CACHE_SECONDS:
            return cached[2]
        match = re.search(r'(?:^|;\s*)UID=(\d+)', cookie)
        uid = match[1] if match else ''
        self._account_cache = (cookie, now, uid)
        return uid

    def audit(self, job_id, item_id, step):
        self.store.event('organize', f'任务 {job_id} / 文件步骤 {item_id}：{step}')

    async def validate(self, config):
        selected = set(config['source_cids']) | {config['archive_cid']} | ({config['pending_cid']} if config.get('pending_cid') else set())
        if '0' in selected or len(set(config['source_cids'])) != len(config['source_cids']):
            raise NeedsReview('请选择非根目录，待整理目录不能重复。')
        trails = {cid: await self.cloud.directory(cid) for cid in sorted(selected)}
        archive, pending = config['archive_cid'], config['pending_cid']
        for a in filter(None, (archive, pending)):
            for b in selected - {a}:
                if a in trails[b] or b in trails[a]:
                    raise NeedsReview('待整理、归档、待处理目录必须相互隔离，不能互相包含。')
        if archive == pending or archive in config['source_cids'] or pending in config['source_cids']:
            raise NeedsReview('待整理、归档、待处理目录不能相同。')
        return trails

    async def allowed_scan_cids(self, cids):
        """按当前扫描配置筛出允许整理的目录（等于扫描根或是其后代）。

        用 115 的真实父子链判断（cloud.directory 返回祖先 id 链），不做字符串前缀
        比较；读不到链的一律视为未授权。
        """
        config = self.store.get('organize_config', {}) or {}
        roots = {str(cid) for cid in (config.get('source_cids') or [])}
        if not roots:
            return []
        allowed = []
        for cid in cids:
            cid = str(cid)
            if cid in roots:
                allowed.append(cid)
                continue
            try:
                chain = await self.cloud.directory(cid)
            except Exception:
                continue
            ids = {str(item['id']) if isinstance(item, dict) else str(item) for item in chain}
            if roots & ids:
                allowed.append(cid)
        return allowed

    async def scan_scope_allows(self, cids):
        """任一目录属于待整理范围即返回 True。"""
        return bool(await self.allowed_scan_cids(cids))

    def queue_notice(self, row):
        key=(row['id'],row['state'])
        if key in self.notice_keys:return
        self.notice_keys.add(key)
        async def deliver():
            pending = list(self.show_notices.get(row['id'], set()))
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            await self.notifications.job(row)
        task = asyncio.create_task(deliver())
        self.notice_tasks.add(task)
        def finished(done):
            self.notice_tasks.discard(done)
            self.notice_keys.discard(key)
            if not done.cancelled(): done.exception()
        task.add_done_callback(finished)

    FAST_KINDS = ('transfer',)

    def next_job(self):
        """取件顺序：转存优先（短任务），其余按 FIFO；空闲时恢复让路的整理。

        对应多级队列的「短任务高优先级、长任务让路后自动回到队列」，
        单 worker 不变，115 全局限流器的串行语义也不变。
        """
        fast = ','.join('?' for _ in self.FAST_KINDS)
        kinds = list(self.FAST_KINDS)
        row = self.store.db.execute(
            "SELECT * FROM organize_jobs WHERE state IN ('queued','running')"
            f" AND json_extract(config,'$.job_kind') IN ({fast}) ORDER BY id LIMIT 1", kinds).fetchone()
        if not row:
            row = self.store.db.execute(
                "SELECT * FROM organize_jobs WHERE state IN ('queued','running') ORDER BY id LIMIT 1").fetchone()
        if not row:
            yielded = self.store.db.execute(
                "SELECT * FROM organize_jobs WHERE state='paused' AND stage LIKE '让路%' ORDER BY id LIMIT 1").fetchone()
            if yielded:
                self.update_job(yielded['id'], state='queued', stage='继续整理')
                row = self.job(yielded['id'])
        return row

    def fast_waiting(self):
        """是否有转存类任务在排队（整理应当在文件边界让路）。"""
        fast = ','.join('?' for _ in self.FAST_KINDS)
        return bool(self.store.db.execute(
            "SELECT 1 FROM organize_jobs WHERE state='queued'"
            f" AND json_extract(config,'$.job_kind') IN ({fast}) LIMIT 1", list(self.FAST_KINDS)).fetchone())

    def next_lane_job(self, lane):
        where = {"transfer": "json_extract(config,'$.job_kind')='transfer'",
                 "sync": "json_extract(config,'$.job_kind')='strm_sync'",
                 "organize": "COALESCE(json_extract(config,'$.job_kind'),'organize') NOT IN ('transfer','strm_sync')"}[lane]
        # 手动点「立即整理/同步」要能马上跑：定时任务排在手动之后，
        row = self.store.db.execute(
            "SELECT * FROM organize_jobs WHERE (state='queued' OR (state='paused' AND stage LIKE '让路%')) AND "
            + where + " ORDER BY COALESCE(json_extract(config,'$.scheduled'),0) ASC, id ASC LIMIT 1").fetchone()
        if row:
            # No await between selecting and marking: a single event loop owns all workers.
            self.update_job(row['id'], state='running', stage='准备执行')
            return self.job(row['id'])
        return None

    async def loop(self, lane='organize'):
        while not self.closing:
            # Claim a job under the same gate used by inventory deletion. This
            # prevents a new cloud mutation from starting between the inventory
            # running-job check and its guarded deletion.
            async with self.publication_gate:
                row = self.next_lane_job(lane)
            if row:
                try:
                    await self.run_job(row['id'])
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.halt(row['id'], 'needs_review', '本任务异常，已保留处理记录；其他排队任务继续执行。')
                self.queue_notice(self.job(row['id']))
            await asyncio.sleep(.3)

    async def supervise_lane(self, lane):
        """Restart a failed lane without replaying an uncertain in-flight job."""
        where = {"transfer": "json_extract(config,'$.job_kind')='transfer'",
                 "sync": "json_extract(config,'$.job_kind')='strm_sync'",
                 "organize": "COALESCE(json_extract(config,'$.job_kind'),'organize') NOT IN ('transfer','strm_sync')"}[lane]
        while not self.closing:
            try:
                await self.loop(lane)
                if not self.closing:
                    raise RuntimeError('任务车道意外退出')
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.lane_faults[lane] = True
                # A failure can occur after a remote write was accepted. Never
                # automatically replay a running job: require reconciliation.
                while not self.closing:
                    try:
                        for stuck in self.store.db.execute(
                                "SELECT id FROM organize_jobs WHERE state='running' AND " + where).fetchall():
                            self.jobs.update(stuck['id'], state='needs_review',
                                                   stage='车道异常，等待核查',
                                                   message='任务车道意外中断，请先核查后继续；未自动重放。')
                        self.store.event('system', '任务车道 %s 意外退出（%s），已隔离并重新启动；中断任务等待核查。'
                                         % (lane, type(exc).__name__))
                        self.lane_faults[lane] = False
                        break
                    except Exception:
                        await asyncio.sleep(2)
                if not self.closing:
                    await asyncio.sleep(.5)

    async def start_share_check(self, wait: float = 8.0):
        """把一次分享存活检测丢到后台跑，不重入。

        0.9.56：实测 82 个分享串行取链要 **41 秒**，以前这段时长直接压在 HTTP 请求上
        （页面假死、还把同步 DB/文件操作摊在事件循环里）。现在固定用 task 跑：
        - `wait>0`（手动触发）：小库仍然同步拿到结果，超过 `wait` 秒就返回 pending；
        - `wait=0`（定时巡检）：立即返回，完全后台。
        结果照旧写 `share_check` 事件和 `meta.share_check_last`，不需要调用方等待。
        """
        task = getattr(self, '_share_check_task', None)
        if task is not None and not task.done():
            return {'started': False, 'pending': True,
                    'message': '存活检测正在后台进行，稍后看日志中心。'}

        async def run():
            try:
                return await share_service.check_alive(self)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.store.event('share_check', '存活检测未完成：%s' % str(exc)[:120])
                return None

        task = asyncio.create_task(run())
        self._share_check_task = task
        if wait <= 0:
            return {'started': True, 'pending': True, 'message': '存活检测已在后台执行。'}
        try:
            # shield：超时只放弃等待，不取消后台任务
            return await asyncio.wait_for(asyncio.shield(task), timeout=wait)
        except asyncio.TimeoutError:
            return {'started': True, 'pending': True,
                    'message': '存活检测已转到后台执行，结果会写入日志中心。'}

    async def share_check_tick(self):
        """分享 STRM 存活检测：按「固定间隔」或「Cron」两种方式执行，计划跨重启保留。"""
        settings = self.store.get('share_settings', {}) or {}
        mode = str(settings.get('share_check_mode') or 'interval')
        raw = settings.get('share_check_hours', self.store.get('share_check_hours'))
        hours = 0 if raw in (None, '') else int(raw)
        cron = str(settings.get('share_check_cron') or '').strip()
        if mode == 'cron' and not cron:
            mode = 'interval'        # 表达式缺失时退回间隔，不把整块功能静默关掉。
        if mode != 'cron' and hours <= 0:
            # 关闭时清掉计划时间：以后重新打开是从「下一个固定时刻」算起，不补跑。
            if self.store.get('share_check_due'):
                self.store.set_many({'share_check_due': 0})
            return
        now = time.time()
        due = float(self.store.get('share_check_due') or 0)
        if not due:
            self.store.set_many({'share_check_due': self.next_share_check_due(mode, hours, cron, now)})
            return
        if now < due:
            return
        self.store.set_many({'share_check_due': self.next_share_check_due(mode, hours, cron, now)})
        try:
            await self.start_share_check(wait=0)
        except Exception as exc:
            self.store.event('share_check', '定时存活检测未完成：%s' % str(exc)[:120])

    def next_share_check_due(self, mode, hours, cron, now=None):
        """按当前模式算下一次存活检测：间隔沿用 03:00 锚点，Cron 走表达式。"""
        if mode == 'cron' and cron:
            return self.next_cron_due(cron, now)
        return self.next_check_due(hours, now)

    def next_cron_due(self, cron, now=None):
        """Cron 模式下下一次执行的绝对时间，按设置里的时区解释表达式。"""
        from croniter import croniter
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now = time.time() if now is None else now
        zone = ZoneInfo(self.store.get('timezone') or 'Asia/Shanghai')
        base = datetime.fromtimestamp(now, zone)
        # max_years_between_matches：像 2 月 30 日这种永不成立的表达式直接报错，不静默挂住。
        nxt = croniter(cron, base, max_years_between_matches=8).get_next(datetime)
        return max(now + 30.0, nxt.timestamp())

    async def share_prune_tick(self):
        """每天核对一次分享 STRM 台账：文件已不在的记录标撤销。

        默认 cron 是每天凌晨 5 点；`share_prune_cron` 可以改，空值或无效时回到默认。
        """
        from croniter import croniter
        cron = str(self.store.get('share_prune_cron') or '0 5 * * *')
        now = time.time()
        due = float(self.store.get('share_prune_due') or 0)
        if not croniter.is_valid(cron):
            cron = '0 5 * * *'
        if not due:
            self.store.set_many({'share_prune_due': self.next_cron_due(cron, now)})
            return
        if now < due:
            return
        self.store.set_many({'share_prune_due': self.next_cron_due(cron, now)})
        from .share_service import prune_missing_share_strms
        try:
            prune_missing_share_strms(self.store, self.root, min_age=86400)
        except Exception as exc:
            self.store.event('share', '核对分享 STRM 台账失败：%s' % str(exc)[:120])

    async def ledger_audit_tick(self):
        """账本体检：用「文件自带身份」反向核对登记表，只读不改。

        排期按「上次体检时间」判断，而不是另存一个 due 键：升级后上次体检已经过期
        就立刻补跑（0.9.50 用 due 键，结果 0.9.51 启动时看到「还没到点」直接跳过，
        /metrics 空等了 6 小时）。失败也只等 10 分钟再试，不刷日志。
        """
        from .ledger_audit import INTERVAL, audit
        now = time.time()
        last = self.store.meta_json('ledger_audit_last', {}) or {}
        try:
            last_at = float(last.get('at') or 0)
        except (TypeError, ValueError):
            last_at = 0.0
        if last_at and now - last_at < INTERVAL:
            return
        tried = getattr(self, '_ledger_tried', 0.0)
        if now - tried < 600:
            return
        self._ledger_tried = now
        try:
            result = await asyncio.to_thread(audit, self.store, self.root)
        except Exception as exc:
            self.store.event('system', '账本体检未完成：%s' % str(exc)[:120])
            return
        self.store.set_meta('ledger_audit_last', result)
        drift = result['stale_active'] + result['unregistered'] + result['orphan_sidecar']
        if drift:
            self.store.event('system', '账本体检：%d 处登记与磁盘不一致（悬空登记 %d、'
                                       '未登记 STRM %d、孤儿侧车 %d）。'
                             % (drift, result['stale_active'], result['unregistered'],
                                result['orphan_sidecar']))

    async def maintenance_tick(self):
        """115 签到与回收站清空：各自按 Cron 执行，互不影响。"""
        config = self.store.get('cloud_maintenance_config', {}) or {}
        now = time.time()
        for kind, enabled_key, cron_key, due_key, last_key in (
                ('sign', 'sign_enabled', 'sign_cron', 'cloud_sign_due', 'cloud_sign_last'),
                ('recycle', 'recycle_enabled', 'recycle_cron', 'cloud_recycle_due', 'cloud_recycle_last')):
            if not config.get(enabled_key):
                if self.store.get(due_key):
                    self.store.set_many({due_key: 0})
                continue
            cron = str(config.get(cron_key) or '')
            if not cron:
                continue
            due = float(self.store.get(due_key) or 0)
            if not due:
                self.store.set_many({due_key: self.next_cron_due(cron, now)})
                continue
            if now < due:
                continue
            self.store.set_many({due_key: self.next_cron_due(cron, now)})
            try:
                if kind == 'sign':
                    result = await self.cloud.sign()
                    self.store.set_many({last_key: now})
                    self.store.event('cloud', '定时签到：' + str(result.get('message') or '完成'))
                    days = result.get('days')
                    from datetime import datetime
                    from zoneinfo import ZoneInfo
                    date_text = datetime.now(ZoneInfo(self.store.get('timezone') or 'Asia/Shanghai')).strftime('%Y-%m-%d')
                    heading, body = cloud_sign_notice(result.get('signed'), days, date_text)
                    await self.notifications.publish(
                        'cloud:sign:%d' % int(now), heading, body,
                        'success' if result.get('signed') else 'info', category='cloud')
                else:
                    await self.cloud.recycle_clear(str(config.get('recycle_password') or '000000'))
                    self.store.set_many({last_key: now})
                    self.store.event('cloud', '定时清空 115 回收站完成。')
                    from datetime import datetime
                    from zoneinfo import ZoneInfo
                    when_text = datetime.now(ZoneInfo(self.store.get('timezone') or 'Asia/Shanghai')).strftime('%m/%d %H:%M')
                    heading, body = cloud_recycle_notice(None, when_text)
                    await self.notifications.publish(
                        'cloud:recycle:%d' % int(now), heading, body, 'success', category='cloud')
            except Exception as exc:
                label = '签到' if kind == 'sign' else '清空回收站'
                self.store.event('cloud', '定时%s未完成：%s' % (label, str(exc)[:120]))

    def next_check_due(self, hours, now=None):
        """下一次存活检测的绝对时间，锚在 share_check_hour（默认 03:00）。"""
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        now = time.time() if now is None else now
        zone = ZoneInfo(self.store.get('timezone') or 'Asia/Shanghai')
        raw = self.store.get('share_check_hour')
        hour = 3 if raw in (None, '') else int(raw)
        due = datetime.fromtimestamp(now, zone).replace(hour=max(0, min(23, hour)), minute=0, second=0, microsecond=0)
        step = timedelta(hours=max(1, hours))
        while due.timestamp() <= now:
            due += step
        return due.timestamp()

    async def share_scan_tick(self):
        """分享 STRM 待整理目录的定时整理，对齐每 5 分钟一轮的分享整理循环。

        share_scan_minutes：0 关闭，默认 5。空目录不产生任务，避免把任务历史刷满。
        """
        raw = self.store.get('share_scan_minutes')
        minutes = 5.0 if raw in (None, '') else float(raw)
        if minutes <= 0:
            return
        now = time.time()
        if now - self._share_scanned < minutes * 60:
            return
        busy = self.store.db.execute(
            "SELECT 1 FROM organize_jobs WHERE state IN ('queued','running') "
            "AND json_extract(config,'$.job_kind')='share_organize' LIMIT 1").fetchone()
        if busy:
            return
        pending = self.root / share_service.PENDING_NAME
        if not pending.is_dir() or not any(pending.rglob('*.strm')):
            self._share_scanned = now
            return
        self._share_scanned = now
        jid = share_service.enqueue_organize(self, origin='schedule')
        self.store.event('share', f'定时扫描发现待整理分享 STRM，任务 {jid} 已入队。')

    async def offline_scan_tick(self):
        """离线目录兜底扫描：已停用。

        第一阶段约定「不得使用离线兜底逻辑跨目录整理」：离线落盘后是否整理，
        只由「落盘后立即整理」开关或扫描模块的手动/定时扫描决定。
        原实现（轮询离线目录、发现未整理视频就入队）保留在 git 历史里。
        """
        return

    async def _flush_show_notify(self, job_id, show_dir, acc):
        """一部剧/电影处理完毕，立刻发一条带海报的电报通知。"""
        if not acc or not acc.get('episodes'):
            return
        details = acc.get('details') or {}
        title = details.get('name') or details.get('title') or show_dir.rsplit('/', 1)[-1]
        year = (details.get('first_air_date') or details.get('release_date') or '')[:4]
        episodes = acc.get('episodes', [])
        count = len(episodes)
        poster = None
        backdrop = details.get('backdrop_path') or details.get('poster_path')
        if isinstance(backdrop, str) and backdrop.startswith('/'):
            poster = 'https://image.tmdb.org/t/p/w500' + backdrop
        from .cards import media_card
        card_items = [(source, plan) for source, plan in episodes]
        # 卡片只放作品本身：未完成/跳过是批次级信息，由任务摘要那条消息交代，
        # 塞进海报说明里既挤又不属于这部片。
        notice_config = json.loads(self.job(job_id)['config'])
        title_text, msg, card_photo = media_card(card_items, shared=bool(acc.get('shared')),
            transferred=bool(notice_config.get('transfer_parent')),
            emby_status='pending' if notice_config.get('notify_emby') else '',
            other_items=acc.get('other_items',()), unassigned=acc.get('unassigned',0))
        poster = card_photo or poster
        await self.notifications.publish(
            key=f'show_done:{job_id}:{show_dir}',
            title=title_text,
            message=msg,
            level='info', job_id=job_id, photo=poster,
            category='media')
        await self.notifications.group_strm(job_id, show_dir, acc.get('items', []))

    def queue_show_notice(self, job_id, event_suffix, acc):
        with self.store.db:
            self.store.db.execute('INSERT OR IGNORE INTO strm_group_outbox VALUES(?,?,?)',
                                  (job_id,event_suffix,json.dumps([i for i,_ in acc.get('items',[])])))
            self.store.db.execute('INSERT OR IGNORE INTO media_notice_outbox(job_id) VALUES(?)',(job_id,))
            self.store.db.execute('INSERT OR IGNORE INTO media_notice_groups(event_key,job_id,items) VALUES(?,?,?)',
                                  (f'show_done:{job_id}:{event_suffix}',job_id,json.dumps([i for i,_ in acc.get('items',[])])))
        notice = asyncio.create_task(self._flush_show_notify(job_id, event_suffix, acc))
        self.notice_tasks.add(notice)
        self.show_notices.setdefault(job_id, set()).add(notice)
        def notice_done(done):
            self.notice_tasks.discard(done)
            self.show_notices.get(job_id, set()).discard(done)
            if not done.cancelled(): done.exception()
        notice.add_done_callback(notice_done)

    async def maintain(self):
        """Hourly housekeeping. A cleanup failure must not mark the scheduler faulty."""
        now = time.time()
        if now - self._pruned_at < 3600:
            return
        self._pruned_at = now
        try:
            # 0.8.20：prune 会删最多两万行，放到线程里跑，别占事件循环。
            # 必须用 prune_in_thread()：它在线程里自己开连接；共用连接跨线程用会 SIGSEGV。
            result = await asyncio.to_thread(self.store.prune_in_thread)
        except Exception:
            self.store.event('system', '历史记录清理未完成，将在下个周期重试。')
            return
        if result['events'] or result['notifications'] or result.get('items'):
            self.store.event('system', f'已清理过期记录：日志 {result["events"]} 条，通知 {result["notifications"]} 条'
                                       f'，已完成任务明细 {result.get("items", 0)} 条。')
        # 同一文件后来整理成功的旧未完成条目：没有人工价值，自动闭环。
        try:
            self.close_recovered_items()
        except Exception:
            pass
        # 账本对账：文件没了/已入库/已回到待整理 → 账本自己闭环；再让没活干的任务自己结项。
        try:
            await self.reconcile_ledger()
        except Exception:
            pass
        try:
            self.settle_jobs()
        except Exception:
            pass
        # 历史未确认投递归档：超过 24 小时还没确认的，多半是当时 Emby 库路径没配好，
        # 留着只会让任务中心一直挂红点。归档后不再作为当前状态展示。
        try:
            with self.store.db:
                archived = self.store.db.execute(
                    "UPDATE emby_deliveries SET state='archived' "
                    "WHERE state IN ('unconfirmed','partial') AND deadline < ?",
                    (now - 86400,)).rowcount
        except Exception:
            archived = 0
        if archived:
            self.store.event('emby', f'历史未确认投递已归档 {archived} 条（超过 24 小时未确认）。')

    async def schedule_loop(self):
        while not self.closing:
            try:
                self.tasks.tick()
                self.auto_retry_stalled()
                self.scheduler_fault = False
                await self.maintain()
                await self.probe_login()
                await self.offline.tick()
                if not getattr(self, 'media_notice_task', None) or self.media_notice_task.done():
                    self.media_notice_task = asyncio.create_task(self.notifications.drain_media())
                    self.notice_tasks.add(self.media_notice_task)
                    def media_drain_done(task):
                        self.notice_tasks.discard(task)
                        if not task.cancelled() and task.exception():
                            self.store.event('telegram', '海报状态更新暂时失败，下次将继续尝试。')
                    self.media_notice_task.add_done_callback(media_drain_done)
                await self.share_check_tick()
                await self.maintenance_tick()
                await self.ledger_audit_tick()
                await self.share_prune_tick()
                await self.share_scan_tick()
                await self.offline_scan_tick()
                await self.cover.tick_daily()
                await self.prewarm.tick()
                if time.time() - self._notice_retried >= 30:
                    self._notice_retried = time.time()
                    await self.notifications.retry()
                rows = self.store.db.execute("SELECT * FROM organize_jobs WHERE updated>=? AND state IN ('completed','partial','needs_review','waiting_login','cancelled') AND notify_state!='not_needed' AND NOT EXISTS (SELECT 1 FROM notifications n WHERE n.event_key='job:'||organize_jobs.id||':'||organize_jobs.state) ORDER BY id LIMIT 20",(float(self.store.meta('phase5_notice_since') or 0),)).fetchall()
                for row in rows:
                    self.queue_notice(row)
            except asyncio.CancelledError:
                raise
            except Exception:
                if not self.scheduler_fault:
                    self.store.event('schedule', '调度或通知发生异常，请检查系统状态。')
                self.scheduler_fault = True
            await asyncio.sleep(1)

    def remember_directory(self, job_id, parent, name, cid):
        account = self.index.account(json.loads(self.job(job_id)['config']))
        with self.store.db:
            self.store.db.execute('INSERT OR REPLACE INTO directory_cache VALUES(?,?,?,?)', (account, cid, parent, name))

    async def find_dir(self, job_id, parent, name):
        self.boundary(job_id)
        account = self.index.account(json.loads(self.job(job_id)['config']))
        # Historical IDs never authorize writes. Resolve the current child by
        # name; a restored/re-created folder needs no manual index repair.
        matches = [x for x in await self.cloud.children(parent) if x['name'] == name]
        with self.store.db:
            self.store.db.execute('DELETE FROM directory_cache WHERE account_id=? AND parent_id=? AND name=?', (account, parent, name))
        if matches:
            if len(matches) != 1 or not matches[0]['is_dir']:
                raise UncertainState('目标目录存在重名文件或多个同名目录，停止处理。')
            self.remember_directory(job_id, parent, name, matches[0]['id'])
            return matches[0]['id']
        return None

    async def ensure_dir(self, job_id, parent, name):
        self.boundary(job_id)
        found = await self.find_dir(job_id, parent, name)
        if found is not None: return found
        self.audit(job_id, 0, '创建目录意图：' + name)
        self.boundary(job_id)
        try:
            await self.cloud.mkdir(parent, name)
        except CloudError as exc:
            if fatal(exc):
                raise
        # An ambiguous mkdir is resolved from the actual parent listing.
        matches = [x for x in await self.cloud.children(parent) if x['name'] == name and x['is_dir']]
        if len(matches) != 1:
            raise UncertainState('创建目录后未能唯一核实结果，已停止后续修改。')
        self.boundary(job_id)
        self.remember_directory(job_id, parent, name, matches[0]['id'])
        return matches[0]['id']

    async def manifest(self, job_id, config):
        # 目录名要跟着文件一起存下来：分集文件常常只有「28.mkv」这种名字，
        # 剧名只在父目录上。folder 只用于识别推断，不参与任何云端修改。
        # 扫描必须看到网盘当前目录，不能复用其他任务的历史清单。
        fresh_children = getattr(self.cloud, 'fresh_children', self.cloud.children)

        async def scan_children(cid):
            # 只保护单个目录的完整读取：已等待的转存可在两个目录之间进入，
            # 但正在接收/核查的批次仍不会被整理扫描看到一半。
            if self.scan_receive_gate.locked():
                self.update_job(job_id, stage='等待目录扫描或转存核对')
            async with self.scan_receive_gate:
                self.boundary(job_id)
                return await fresh_children(cid)
        stack, visited, files = [(cid, '') for cid in reversed(config['source_cids'])], set(), {}
        missing_count = 0
        if config.get('source_entries') is not None:
            current={}
            for root in config['source_cids']:
                current.update({e['id']:e for e in await scan_children(root)})
            missing = [e for e in config['source_entries'] if e['id'] not in current]
            missing_count = len(missing)
            entries=[current[e['id']] for e in config['source_entries'] if e['id'] in current]
            stack=[(e['id'], e['name']) for e in entries if e['is_dir']]
            files={e['id']:{**e, 'folder': ''} for e in entries if not e['is_dir']}
        while stack:
            self.boundary(job_id)
            cid, folder = stack.pop()
            if cid in visited:
                continue
            visited.add(cid)
            for entry in await scan_children(cid):
                self.boundary(job_id)
                if entry['is_dir']:
                    # 保留相对层级：剧集常在「作品目录/Season 1」下，只留最后一层
                    # 会把作品名和 tmdb id 丢掉。
                    stack.append((entry['id'], f'{folder}/{entry["name"]}'.strip('/')))
                else:
                    entry['folder'] = folder
                    files.setdefault(entry['id'], entry)
            self.update_job(job_id, stage=f'扫描目录：{len(visited)} 个，发现文件：{len(files)} 个')
            if len(files) and len(files) % 200 == 0:
                self.store.event('organize',
                                 f'📄 [扫描] 进度：已扫描 {len(visited)} 个目录、{len(files)} 个文件')
        self.store.event('organize', f'📦 [扫描] 完成：目录 {len(visited)} 个，文件 {len(files)} 个')
        # Explicit transfer/source entries define this batch's package scope.
        # Record its scanned directories, including empty siblings, without
        # expanding cleanup to unrelated folders under a general source root.
        config['scan_bundle_dirs'] = sorted(visited) if config.get('source_entries') is not None else []
        accepted, config['scan_filter'] = filter_files(list(files.values()), config)
        config['scan_filter']['directories'] = len(visited)
        config['scan_filter']['files'] = len(files)
        from .untyped_cleanup import candidates as untyped_candidates
        config['untyped_companions'] = untyped_candidates(list(files.values()))
        if missing_count:
            config['scan_filter']['missing_batch_entries'] = missing_count
            self.store.event('organize', f'任务 {job_id}：本批 {missing_count} 个条目不在原目标目录，已跳过并继续处理其他文件。')
        junk = config['scan_filter'].get('recycle') or []
        recycled = failed = 0
        for entry in junk[:300]:
            self.boundary(job_id)
            try:
                await self.cloud.recycle(entry['id'], entry['parent_id'])
                recycled += 1
            except CloudError as exc:
                if exc.code in ('LOGIN_REQUIRED', 'LOGIN_INVALID'):
                    raise
                failed += 1
        if junk:
            config['scan_filter']['recycled'] = recycled
            self.store.event('organize', '垃圾清理：回收 %d 个小文件%s。'
                             % (recycled, '，%d 个未完成' % failed if failed else ''))
        # Only metadata actually seen during this scan can ever be cleared.
        # Missing snapshots on older/resumed manifests mean preserve by default.
        config['cleanup_metadata_snapshot'] = [
            {k: entry[k] for k in ('id', 'name', 'parent_id', 'size', 'sha1') if k in entry}
            for entry in files.values() if Path(entry['name']).suffix.lower() in METADATA]
        cfg = options(config)
        videos = bind_subtitles(accepted, cfg['video_extensions'], cfg['subtitle_extensions'])
        # Persist only scan-time, unbound subtitle identities: later arrivals,
        # excluded extensions and subtitles bound to unresolved videos are not
        # authorized for cleanup by this task.
        bound_ids = {str(sub['id']) for video in videos for sub in video.get('subtitles', [])}
        config['orphan_subtitle_snapshot'] = [
            {k: entry[k] for k in ('id','name','parent_id','size','sha1') if k in entry}
            for entry in accepted if Path(entry['name']).suffix.lower() in set(cfg['subtitle_extensions'])
            and str(entry['id']) not in bound_ids]
        # A P1/P2 pair must share both its source directory and the rest of
        # its filename. A lone marker is uncertain and is not treated as a
        # complete movie; Dolby Vision profiles are excluded by the parser.
        split_groups = {}
        for video in videos:
            marker = split_video_marker(video['name'])
            if marker:
                split_groups.setdefault((video['parent_id'], marker[0]), set()).add(marker[1])
        for video in videos:
            marker = split_video_marker(video['name'])
            if marker:
                pair = split_groups[(video['parent_id'], marker[0])] >= {1, 2}
                video['split_reason'] = ('P1/P2 分段文件' if pair else '疑似 P1/P2 分段，未找到配对文件')
        chinese_groups = {}
        for video in videos:
            marker = chinese_split_marker(video['name'])
            if marker:
                chinese_groups.setdefault((video['parent_id'], marker[0]), set()).add(marker[1])
        for video in videos:
            marker = chinese_split_marker(video['name'])
            if marker and chinese_groups[(video['parent_id'], marker[0])] >= {'上', '下'}:
                video['split_reason'] = '上/下配对分段文件'
        by_folder = {}
        for video in videos:
            key = episode_key(video['name'])
            if key:
                by_folder.setdefault(video.get('folder') or '', set()).add(key[2])
        for video in videos:
            numbers = sorted(by_folder.get(video.get('folder') or '', set()))
            key = episode_key(video['name'])
            video['siblings'] = [n for n in numbers if not key or n != key[2]] if len(numbers) > 1 else []
        self.jobs.replace_manifest(job_id, [(f['id'], dumps(f)) for f in videos])
        self.update_job(job_id, config=dumps(config), stage=f'扫描完成：{len(videos)} 个视频；按格式跳过 {config["scan_filter"]["excluded"]} 个')

    TMDB_SEARCH_SECONDS = 86400
    TMDB_DETAIL_SECONDS = 7 * 86400
    TMDB_EPISODE_SECONDS = 30 * 86400

    @staticmethod
    def tmdb_cache_key(path, params=None):
        # language 必须进 key：同一个关键词在不同语言下返回的标题不一样。混用一份缓存
        # 会让「英文发布名」命中「中文候选」，标题相似度变成 0，误判成无法识别。
        extra = {k: str(v) for k, v in (params or {}).items() if k not in ('api_key',)}
        return path + ('?' + '&'.join(f'{k}={extra[k]}' for k in sorted(extra)) if extra else '')

    async def tmdb(self, path, params=None):
        key = self.store.get('tmdb_key')
        if not key:
            raise CloudError('缺少 TMDB API Key。', 'TMDB_UNAVAILABLE', 409)
        # 详情与季集信息几乎不变：缓存能显著减少长剧集的请求数和限流失败；
        # 搜索按天缓存，避免同一批文件里重复查询同一部作品。
        ttl = (self.TMDB_EPISODE_SECONDS if '/episode/' in path else
               self.TMDB_SEARCH_SECONDS if path.startswith('search/') else self.TMDB_DETAIL_SECONDS)
        cache_key = self.tmdb_cache_key(path, params)
        now = time.time()
        cached = self.store.db.execute('SELECT body,updated FROM tmdb_cache WHERE key=?', (cache_key,)).fetchone()
        if cached and now - cached['updated'] < ttl:
            try:
                result = json.loads(cached['body'])
                _count_tmdb('缓存命中')
                return result
            except ValueError:
                pass
        try:
            _count_tmdb('网络请求')
            response = await self.http.get('https://api.themoviedb.org/3/' + path,
                params={'api_key': key, 'language': 'zh-CN', **(params or {})})
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status == 404:
                # 404 = 「TMDB 里没有这个条目」。用专门的 code 让调用方自己决定：
                # 作品详情缺失 → 转人工；单集缺失 → 按文件名兜底照常归档。
                raise CloudError('TMDB 中找不到该条目（HTTP 404）。', 'TMDB_NOT_FOUND', 404) from None
            raise CloudError(f'TMDB 请求失败（HTTP {status}）。', 'TMDB_UNAVAILABLE', 502) from None
        except httpx.TimeoutException:
            raise CloudError('TMDB 请求失败（连接或响应超时）。', 'TMDB_UNAVAILABLE') from None
        except httpx.HTTPError:
            raise CloudError('TMDB 请求失败（网络连接异常）。', 'TMDB_UNAVAILABLE') from None
        except ValueError:
            raise CloudError('TMDB 请求失败（返回数据无效）。', 'TMDB_UNAVAILABLE') from None
        with self.store.db:
            self.store.db.execute('INSERT OR REPLACE INTO tmdb_cache VALUES(?,?,?)',
                                  (cache_key, json.dumps(data, ensure_ascii=False), now))
            self.store.db.execute('DELETE FROM tmdb_cache WHERE updated<?', (now - 90 * 86400,))
        return data

    async def tmdb_episode(self, tv_id, season, number):
        """优先复用单集缓存，再按季查询；缺失或无法验证时回退单集接口。"""
        path = f'tv/{tv_id}/season/{season}/episode/{number}'
        cached = self.store.db.execute('SELECT body,updated FROM tmdb_cache WHERE key=?', (path,)).fetchone()
        if cached and time.time() - cached['updated'] < self.TMDB_EPISODE_SECONDS:
            try:
                data = json.loads(cached['body'])
                if isinstance(data, dict) and data.get('season_number') == season and data.get('episode_number') == number:
                    _count_tmdb('单集缓存命中')
                    return data
            except (TypeError, ValueError):
                pass
        season_path = f'tv/{tv_id}/season/{season}'
        unavailable_until = self._season_lookup_unavailable.get(season_path, 0)
        if time.monotonic() >= unavailable_until:
            try:
                data = await self.tmdb(season_path)
            except CloudError as exc:
                if exc.code not in ('TMDB_UNAVAILABLE', 'TMDB_NOT_FOUND'):
                    raise
                # 季接口不支持或暂时不可达时沿用原单集接口，不在每一集重复撞季接口。
                self._season_lookup_unavailable[season_path] = time.monotonic() + 60
            else:
                if isinstance(data, dict) and data.get('season_number') == season and isinstance(data.get('episodes'), list):
                    matches = [ep for ep in data['episodes'] if isinstance(ep, dict)
                               and ep.get('season_number') == season and ep.get('episode_number') == number]
                    required = ('id', 'name', 'overview', 'air_date', 'season_number', 'episode_number')
                    if len(matches) == 1 and matches[0].get('id') and all(key in matches[0] for key in required):
                        return matches[0]
        try:
            return await self.tmdb(path)
        except CloudError as exc:
            if exc.code == 'TMDB_NOT_FOUND':
                # TMDB 还没登记这一集（国产剧/短剧数据常常滞后于播出）：返回 None，
                # 让 build_plan 按文件名里的集号兜底归档，而不是整条转人工。
                return None
            raise

    async def search_tmdb(self, kind, query, year, attempts, variant, language=None):
        """一次 TMDB 搜索，并把尝试记进证据链。剧集不传年份，年份留到证据校验。

        language 决定返回标题的语言：文件名不是中日韩文字时用 en-US，否则用 zh-CN。
        TMDB 的 results 只给「当前语言的标题 + 原始语言标题」，语言用错时英文发布名
        会去和中文候选名比相似度，分数全是 0，被阈值判掉，报成「未找到唯一匹配」。
        """
        params = {'query': query, 'include_adult': 'false'}
        if kind != 'tv' and year:
            params['year'] = year
        if language:
            params['language'] = language
        record = {'endpoint': 'search/' + kind, 'variant': variant,
                  'params': {k: v for k, v in params.items() if k != 'include_adult'}}
        try:
            data = await self.tmdb('search/' + kind, params)
        except Exception as exc:
            record.update({'candidates': 0, 'reason': (type(exc).__name__ + ':' + str(exc))[:60]})
            attempts.append(record)
            raise
        results = data.get('results') or []
        record.update({
            'candidates': len(results),
            'top': ' | '.join('%s(%s)#%s' % (r.get('name') or r.get('title') or '',
                                             (r.get('first_air_date') or r.get('release_date') or '')[:4],
                                             r.get('id')) for r in results[:3])})
        attempts.append(record)
        return results

    async def year_ok(self, kind, candidate, hint):
        """年份证据：电影看上映年；剧集允许首播年 / 本季年份 / 该集播出年（对齐 ）。"""
        year = str(hint.get('year') or '')
        if not year:
            return True, 'no_year'
        first = str(candidate.get('first_air_date') or candidate.get('release_date') or '')[:4]
        if first and first == year:
            return True, 'first_air'
        if kind != 'tv':
            return False, 'mismatch'
        season, episode = int(hint.get('season') or 0), int(hint.get('episode') or 0)
        details = candidate if candidate.get('seasons') is not None else await self.tmdb('tv/%s' % candidate['id'])
        for item in details.get('seasons') or []:
            if int(item.get('season_number') or -1) == season and str(item.get('air_date') or '')[:4] == year:
                return True, 'season'
        if season and episode:
            try:
                data = await self.tmdb('tv/%s/season/%s/episode/%s' % (candidate['id'], season, episode))
            except CloudError as exc:
                if exc.code == 'TMDB_UNAVAILABLE' and exc.status == 404:
                    # 候选剧集没有这一集，只能排除此候选，不能当成 TMDB 服务故障。
                    return False, 'episode_not_found'
                raise
            except Exception:
                data = {}
            if str(data.get('air_date') or '')[:4] == year:
                return True, 'episode'
        return False, 'mismatch'

    async def pick(self, kind, hint, results, attempts):
        """挑最高分：对齐——标题评分 + 中文优先，不要求唯一。

        电影先走精确匹配（同年同名的唯一解最省事），多候选时才退回评分；
        剧集直接评分，年份只作加分证据、不作硬过滤（「年份档:0」仍会选中）。
        """
        if kind != 'tv':
            try:
                return choose_match(hint, results)
            except NeedsReview:
                best = pick_best(hint, results)
                return best['id'] if best else None
        threshold = match_threshold(hint)
        scored = scored_candidates(hint, results)
        pool = [(score, row) for score, row in scored if score >= threshold][:8]
        if not pool:
            attempts.append({'endpoint': 'pick', 'variant': 'score_below_threshold',
                             'threshold': threshold,
                             'top': ' | '.join('%s(%s):%0.2f' % (
                                 row.get('name') or row.get('title') or '',
                                 (row.get('first_air_date') or row.get('release_date') or '')[:4],
                                 score) for score, row in scored[:3])})
            return None
        # 给了年份但「首播年/季年份/集播出年」都不符时直接拒绝，不按标题硬选。
        want_year = bool(str(hint.get('year') or ''))
        ranked = []
        for score, candidate in pool:
            ok, evidence = await self.year_ok(kind, candidate, hint)
            attempts.append({'endpoint': 'tv/%s' % candidate.get('id'), 'variant': 'year_evidence',
                             'title': candidate.get('name') or candidate.get('title'),
                             'year': hint.get('year'), 'evidence': evidence})
            if want_year and not ok:
                continue
            rank = round(score * 100 + (65 if ok else 0) + float(candidate.get('popularity') or 0), 2)
            ranked.append((rank, 1 if has_cjk(candidate.get('name') or candidate.get('title')) else 0,
                           candidate))
        if not ranked:
            return None
        ranked.sort(key=lambda item: item[:2], reverse=True)
        return ranked[0][2]['id']

    async def identify(self, hint):
        require_work_identity(hint)
        kind = hint['media_type']
        tmdb_id = hint['tmdb_id']
        if tmdb_id is None:
            attempts = []
            # 文件名语言决定搜索语言：英文等拉丁文字走 en-US，中日韩走 zh-CN。
            primary_language = 'zh-CN' if has_cjk(hint['query']) else 'en-US'
            results = await self.search_tmdb(kind, hint['query'], hint['year'], attempts, 'title',
                                             language=primary_language)
            if kind != 'tv' and hint.get('year') and not results:
                previous = str(int(str(hint['year'])) - 1)
                results = await self.search_tmdb(kind, hint['query'], previous, attempts,
                                                 'previous_release_year')
            first_count = len(results)
            tmdb_id = await self.pick(kind, hint, results, attempts)
            if tmdb_id is None and 0 < len(results) <= 5:
                # 中文别名可能与 TMDB 当前显示名不同：补齐别名再判一次，仍要求唯一。
                enriched = []
                for candidate in results:
                    alt = await self.tmdb(f'{kind}/{candidate["id"]}/alternative_titles')
                    enriched.append({**candidate,
                                     'aliases': [x.get('title', '') for x in alt.get('results', alt.get('titles', []))]})
                tmdb_id = await self.pick(kind, hint, enriched, attempts)
            # 中文数字尾数兜底：原查询一无所获时，试「转阿拉伯数字」与「去尾数按分季」。
            # 每一步仍要求唯一匹配；季必须真实存在，否则照旧进未识别，绝不猜。
            if tmdb_id is None:
                for variant_query, season_override in numeral_variants(hint['query']):
                    variant_hint = {**hint, 'query': variant_query}
                    variant_results = await self.search_tmdb(kind, variant_query, hint['year'], attempts,
                                                             'numeral:' + variant_query)
                    variant_id = await self.pick(kind, variant_hint, variant_results, attempts)
                    if variant_id is None:
                        continue
                    variant_details = await self.tmdb(f'{kind}/{variant_id}')
                    if int(variant_details.get('id', 0)) != int(variant_id):
                        continue
                    if kind == 'tv' and season_override:
                        seasons = {s.get('season_number') for s in variant_details.get('seasons') or []}
                        if season_override not in seasons:
                            continue
                        variant_hint['season'] = season_override
                    # 原地修正调用方的 hint：季数可能被去尾数分支调整。
                    hint.update({'query': variant_hint['query'], 'season': variant_hint['season']})
                    tmdb_id = variant_id
                    break
            alt_results = []
            if tmdb_id is None:
                # 换语言重搜：发布名和 TMDB 显示名经常不是同一语言（英文发布名 + 中文译名，
                # 或者反过来）。旧实现在这里用同样的参数再搜一次，命中同一份缓存，等于空转。
                other_language = 'zh-CN' if primary_language == 'en-US' else 'en-US'
                try:
                    alt_results = await self.search_tmdb(kind, hint['query'], hint['year'], attempts,
                                                         'language:' + other_language,
                                                         language=other_language)
                except (asyncio.CancelledError, CloudError):
                    raise
                except Exception:
                    alt_results = []
                if alt_results:
                    tmdb_id = await self.pick(kind, hint, alt_results, attempts)
                if tmdb_id is None and results:
                    alt_results = results
            if tmdb_id is None and hint.get('year'):
                # 唯一命中 + 年份可核实的兜底（TMDB 原名与发布名不同语言时）。
                for pool in (alt_results, results):
                    if len(pool) == 1:
                        ok, _evidence = await self.year_ok(kind, pool[0], hint)
                        if ok:
                            tmdb_id = pool[0]['id']
                            break
            hint['tmdb_attempt'] = attempts
            if tmdb_id is None:
                tried = '、'.join(dict.fromkeys(a.get('variant', '') for a in attempts if a.get('variant')))
                raise NeedsReview(
                    'TMDB 未找到唯一匹配：关键词「%s」%s，候选 %d 条；已停止自动命名。%s'
                    % (hint['query'], ('年份 %s' % hint['year']) if hint.get('year') else '未带年份',
                       first_count, ('已尝试：' + tried + '。') if tried else ''))
        try:
            details = await self.tmdb(f'{kind}/{tmdb_id}')
        except CloudError as exc:
            if exc.code == 'TMDB_NOT_FOUND':
                raise NeedsReview('TMDB 中找不到对应条目（HTTP 404），已转人工核查。') from None
            raise
        if int(details.get('id', 0)) != int(tmdb_id):
            raise NeedsReview('TMDB 详情身份不匹配。')
        return details

    async def build_plan(self, job_id, original, diagnostics=None):
        if original.get('split_reason'):
            raise NeedsReview(original['split_reason'] + '，不按完整视频自动整理。')
        hint = require_single_episode(parse_name(original['name'], original.get('folder')), original['name'])
        kind = hint['media_type']
        with _prepare_measure(diagnostics, '作品识别', self.cloud):
            details = await self.identify(hint)
        tmdb_id = details['id']
        episodes = []
        tmdb_missing = []
        if kind == 'tv':
            for number in range(hint['episode'], hint['end_episode'] + 1):
                with _prepare_measure(diagnostics, '季集资料', self.cloud):
                    episode = await self.tmdb_episode(tmdb_id, hint['season'], number)
                if episode is None:
                    # TMDB 还没登记这一集：作品本身识别成功就够了。
                    # 集号以文件名解析结果为准（命名里的「第N集」本来就不看 TMDB），照常归档。
                    episode = {'season_number': hint['season'], 'episode_number': number,
                               'name': '', 'air_date': '', 'overview': '', 'tmdb_missing': True}
                    tmdb_missing.append('TMDB 未登记 S%02dE%02d，集号按文件名归档。'
                                        % (hint['season'], number))
                elif episode.get('season_number') != hint['season'] or episode.get('episode_number') != number:
                    raise NeedsReview('TMDB 季集映射不匹配。')
                episodes.append(episode)
        self.boundary(job_id)
        with _prepare_measure(diagnostics, '源文件身份核对', self.cloud):
            current = await self.cloud.file(original['id'])
        self.check_source(original, current, original['name'], original['parent_id'])
        original['sha1'] = current.get('sha1', '')
        config = json.loads(self.job(job_id)['config'])
        if config.get('upgrade_policy', {}).get('mode') == 'skip':
            if config.get('classify_enabled', True):
                parts, _, _ = work_parts(hint, details)
            else:
                parts = ['电影'] if kind == 'movie' else ['剧集']
            preliminary = {'hint': hint, 'details': details, 'parts': parts}
            if await self.index.candidates(job_id, original, preliminary):
                return {**preliminary, 'subtitles': [], 'warnings': tmdb_missing, 'media': {}}
        # 用户开关：ffprobe 关 → 从文件名解析质量标签（降级）。
        # 不对任何后缀写死例外：加进扩展名列表的格式（含 ISO 原盘）都走同一条探测路径；
        # 探测读不出来按既有规则转待处理，和其他格式一致。
        if config.get('ffprobe_enabled', True):
            with _prepare_measure(diagnostics, '媒体信息获取', self.cloud):
                media, _, origin = await self.measurements.measure(job_id, current, original['name'], config.get('upgrade_policy'))
        else:
            media, origin = media_from_filename(original['name']), 'filename'
        with _prepare_measure(diagnostics, '标题与命名', self.cloud):
            titles = await title_options(self, hint, details, config)
            parts, name, category, region = naming(hint, details, episodes, media, original['name'], config.get('upgrade_policy'), titles)
        # 用户开关：分类关 → 统一放到 电影/剧集 目录
        return {'hint': hint, 'details': details, 'episodes': episodes, 'media': media,
                'recognition': self.recognition(hint, details, original),
                'parts': parts, 'target_name': name, 'category': category, 'region': region,
                'subtitles': [], 'warnings': tmdb_missing, 'pending': False, 'media_origin': origin, 'strm_filtered': skip_reason(original, config['strm_options']) if config.get('strm_options') else ''}

    @staticmethod
    def check_source(original, current, name, parent):
        differences = []
        for key, expected, label in (('id', original['id'], '文件 ID'),
                ('size', original.get('size'), '大小'), ('name', name, '名称'),
                ('parent_id', parent, '父目录')):
            if current.get(key) is None or current[key] != expected:
                differences.append(label)
        if differences:
            raise UncertainState('文件核查不一致（' + '、'.join(differences) + '），已停止修改。')
        if original.get('sha1') and current.get('sha1') and original['sha1'].upper() != current['sha1'].upper():
            raise UncertainState('源文件内容校验信息已变化，停止修改。')

    async def change_file(self, job_id, item_id, original, target_name, dest, retries, rename_only=False):
        # Each sub-step is idempotent: only original/target states are accepted.
        for step in (('rename',) if rename_only else ('rename', 'move')):
            for attempt in range(retries + 1):
                self.boundary(job_id)
                current = await self.cloud.file(original['id'])
                if current.get('parent_id') not in (original['parent_id'], dest) or current['name'] not in (original['name'], target_name):
                    raise UncertainState('修改核查发现意外名称或位置，已停止以避免重复操作。')
                self.check_source(original, current, current['name'], current['parent_id'])
                if step == 'rename' and current['name'] == target_name:
                    break
                if step == 'move' and current['parent_id'] == dest:
                    break
                if step == 'rename' and current['parent_id'] != original['parent_id']:
                    raise UncertainState('文件已经移动但名称不符，需要人工核查。')
                trail = await self.cloud.directory(dest)
                config = json.loads(self.job(job_id)['config'])
                if not ({config['archive_cid'], config['pending_cid']} & set(trail)):
                    raise UncertainState('目标目录已离开所选归档或待处理范围。')
                listing = await self.cloud.children(current['parent_id'] if step == 'rename' else dest)
                if any(x['name'] == target_name and x['id'] != original['id'] for x in listing):
                    raise UncertainState('写入前出现重名冲突，保留两端文件并停止。')
                self.audit(job_id, item_id, ('重命名' if step == 'rename' else '移动') + '意图，文件 ID ' + original['id'])
                self.boundary(job_id)
                try:
                    if step == 'rename':
                        await self.cloud.rename(original['id'], target_name)
                    else:
                        await self.cloud.move(original['id'], dest)
                except CloudError as exc:
                    # 登录失效与限流是任务级信号：吞掉会被后面的一致性核对误报成
                    # 「移动结果不匹配」，还会让整批继续对着 115 撞墙。
                    if exc.code in ('LOGIN_REQUIRED', 'LOGIN_INVALID', 'RATE_LIMITED'):
                        raise
                # Even if paused/cancelled while the request was in flight, finish readback.
                checked = await self.cloud.file(original['id'])
                if checked['name'] != target_name:
                    if step == 'rename' and checked['name'] == original['name'] and checked['parent_id'] == original['parent_id']:
                        if attempt < retries:
                            await asyncio.sleep(config.get('retry_interval', 2))
                            continue
                    raise UncertainState('重命名结果不匹配或未完成，需要核查。')
                if step == 'move' and checked['parent_id'] != dest:
                    if checked['parent_id'] == original['parent_id'] and attempt < retries:
                        await asyncio.sleep(config.get('retry_interval', 2))
                        continue
                    raise UncertainState('移动结果不匹配或未完成，需要核查。')
                self.audit(job_id, item_id, ('重命名' if step == 'rename' else '移动') + '结果已核实')
                break
        checked = await self.cloud.file(original['id'])
        self.check_source(original, checked, target_name, original['parent_id'] if rename_only else dest)
        return checked

    async def quarantine(self, job_id, row, original, config, reason):
        # Defer diversion until all siblings have completed normal processing.
        # Failed videos remain untouched and travel with the remaining package.
        if not config.get('pending_cid'):
            suffix = '' if '原位保留' in reason else '；未配置未识别目录，文件原位保留。'
            message = reason.rstrip('。') + suffix
            # 原位保留同样是「还有活要干」：和配了未识别目录的 pending 一个口径，
            # 否则同一件事，配没配未识别目录会让任务在「部分未完成 / 完成」之间跳。
            self.update_item(row['id'], state='skipped',
                             plan={'failure':True,'reason':reason,'blocks_job':True},
                             step='整理失败，原位保留', message=message)
            return
        plan = json.loads(row['plan']) or {'pending':True, 'reason':reason,
            'dest':config['pending_cid'], 'failure':True}
        self.update_item(row['id'], plan=plan, original=dumps(original),
            step='保留原样，等待整目录分流', state='planned', message=reason)

    async def failed_item(self, job_id, row, config, reason):
        current = self.store.db.execute('SELECT * FROM organize_items WHERE id=?', (row['id'],)).fetchone()
        plan = json.loads(current['plan'])
        if not plan:
            try:
                await self.quarantine(job_id, current, json.loads(current['original']), config, reason)
                return
            except CloudError as exc:
                if exc.code in ('LOGIN_REQUIRED', 'LOGIN_INVALID'):
                    raise
                reason += '；原名转移未完成：' + str(exc)
            except (UncertainState, NeedsReview) as exc:
                reason += '；原名转移未完成：' + str(exc)
            plan = json.loads(self.store.db.execute('SELECT plan FROM organize_items WHERE id=?', (row['id'],)).fetchone()[0])
        # Once an archive/replace has started, keep its durable record. Never
        # pretend to roll back a successful remote move because output failed.
        if plan and not plan.get('pending'):
            # 这里是真的失败（归档/洗版已经动手，后续步骤没走完），不是「识别不了、
            # 放进未识别目录等人决定」。必须打 failure 标记：任务收尾的 blocking 只认
            # plan['failure'] 和 warnings，不打标记的话「回收未确认」的洗版任务会报
            # 「已完成」，旧文件还留在盘上却没人知道。
            plan = {**plan, 'failure': True, 'blocks_job': True}
            self.update_item(row['id'], state='review', plan=plan, step='本文件后续步骤未完成', message=reason)
        else:
            self.update_item(row['id'], state='skipped', plan={**plan,'failure':True,'reason':reason}, step='整理失败，原位保留', message=reason)
        self.audit(job_id, row['id'], reason)

    async def discard_incoming(self, job_id, row, original, config, reason):
        """ 口径：洗版判定「保留现有」→ 新文件进回收站，不占用待处理。"""
        if not config.get('discard_inferior', True):
            raise NeedsReview(reason + '；未开启自动回收，新文件转待处理。')
        current = await self.cloud.file(original['id'])
        self.check_source(original, current, original['name'], original['parent_id'])
        await self.cloud.recycle(original['id'], original['parent_id'])
        self.update_item(row['id'], state='skipped', step='已回收（洗版）',
                         plan={'discarded': True, 'reason': reason}, message=reason)
        self.audit(job_id, row['id'], '回收新文件：' + reason)
        self.store.event('organize', '保留现有并回收新文件：' + original['name'])

    @staticmethod
    def recognition(hint, details, original):
        """识别证据链：对齐  的 keyword/身份来源/集号证据/兄弟集号/tmdb 尝试。"""
        attempts = hint.pop('tmdb_attempt', None) or []
        return {'keyword': hint.get('query'), 'year': hint.get('year'),
                'media_type': hint.get('media_type'), 'season': hint.get('season'),
                'episode': hint.get('episode'), 'end_episode': hint.get('end_episode'),
                'tmdb_id': (details or {}).get('id') or hint.get('tmdb_id'),
                'title': ((details or {}).get('title') or (details or {}).get('name')),
                'identity_source': hint.get('identity_source') or 'filename',
                'siblings': original.get('siblings') or [],
                'tmdb_attempt': attempts}

    @staticmethod
    def empty_scan_hint(config):
        """扫描为空时把来源写清楚：来源指错目录时，光说「未发现视频」查不出来。"""
        labels = config.get('directory_labels') or {}
        names = [labels.get(cid) or cid for cid in config.get('source_cids') or []]
        scanned = (config.get('scan_filter') or {}).get('scanned') or 0
        return '未发现待整理视频（来源：%s；扫描 %d 个文件）' % ('、'.join(names) or '未配置', scanned)

    async def check_conflict(self, job_id, original, plan, dest):
        if 'wash' not in plan:
            await self.index.conflict(job_id, original, plan)
        allowed = {old['source']['id'] for old in plan.get('wash', {}).get('old', [])}
        for entry in await self.cloud.children(dest):
            if entry['is_dir'] or entry['id'] == original['id'] or Path(entry['name']).suffix.lower() not in VIDEOS:
                continue
            if entry['id'] in allowed:
                continue
            if plan.get('wash'):
                # Different version groups may share a standard Season folder.
                # Any exact target collision still requires a tracked replacement.
                if entry['name'] == plan['target_name']:
                    raise UncertainState('目标名称已存在未授权替换的视频，停止写入。')
                config = json.loads(self.job(job_id)['config'])
                identity = self.index.managed_identity(self.index.account(config), config['archive_cid'], entry, tuple(plan['parts']))
                other, details = identity or await self.index.identify(entry, tuple(plan['parts']), {})
                if other['media_type'] != plan['hint']['media_type'] or details['id'] != plan['details']['id']:
                    raise NeedsReview('目标目录存在其他作品，保留文件并跳过本次写入。')
                continue
            if plan['hint']['media_type'] == 'movie':
                raise Discarded('保留现有：优先级未形成差异，保留目标文件')
            try:
                other = parse_name(entry['name'])
            except NeedsReview:
                raise NeedsReview('目标季目录有无法可靠判断的旧视频，不自动覆盖。') from None
            hint = plan['hint']
            if other['season'] is None:
                raise NeedsReview('目标季目录有未建立季集索引的视频，不自动覆盖。')
            if other['season'] == hint['season'] and max(other['episode'], hint['episode']) <= min(other['end_episode'], hint['end_episode']):
                raise Discarded('保留现有：优先级未形成差异，保留目标文件')

    async def prefetch_identify(self, job_id, rows, config):
        """开工前并发预热 TMDB 搜索，逐条处理时直接命中 tmdb_cache。

        只并发读：写操作（改名/移动）仍然一条条按 3 秒间隔来，这个顺序不能变。
        """
        if not config.get('identify_prefetch', True):
            return
        hints = []
        for row in rows:
            original = json.loads(row['original'])
            if original.get('split_reason'):
                continue
            plan = row['plan']
            if plan:
                try:
                    if json.loads(plan).get('details'):
                        continue
                except (TypeError, ValueError):
                    pass
            try:
                hints.append(require_single_episode(parse_name(original['name'], original.get('folder')), original['name']))
            except NeedsReview:
                continue
        if len(hints) < 2:
            return
        limit = max(1, min(4, int(config.get('probe_concurrency') or 2)))
        semaphore = asyncio.Semaphore(limit)

        async def warm(hint):
            async with semaphore:
                self.boundary(job_id)
                try:
                    await self.tmdb("search/" + hint['media_type'],
                                    {'query': hint['query'], 'include_adult': 'false'})
                except Stopped:
                    raise
                except CloudError as exc:
                    if exc.code in ('LOGIN_REQUIRED', 'LOGIN_INVALID', 'RATE_LIMITED'):
                        raise
                except Exception:
                    pass

        tasks = [asyncio.create_task(warm(hint)) for hint in hints]
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    async def prefetch_probes(self, job_id, rows, config):
        """整理前并发预取 FFprobe 结果，主流程逐个处理时直接命中 media_cache。

        只提前测量、不改动云端：命中缓存就省掉一次远程探测，没命中则主流程
        照原样再探一次，行为与不做预取完全一致。没有有效指纹（sha1+size）的
        文件不预取；结果无法复用，探了也白探。单文件失败只计数、不抛错，仍由
        主流程按既有逻辑单独处理它；暂停、取消、登录失效照常向上冒泡。
        """
        if not config.get('ffprobe_enabled', True) or not config.get('probe_prefetch', True):
            return
        targets = []
        for row in rows:
            original = json.loads(row['original'])
            if original.get('split_reason'):
                continue
            if not MediaCache.fingerprint(original):
                continue
            plan = row['plan']
            if plan:
                try:
                    if json.loads(plan).get('media'):
                        continue
                except (TypeError, ValueError):
                    pass
            targets.append(original)
        # 单个文件没有并发收益，直接交给主流程，避免多一次目录/状态写入。
        if len(targets) < 2:
            return
        limit = max(1, min(4, int(config.get('probe_concurrency') or 2)))
        policy = config.get('upgrade_policy')
        total, done = len(targets), 0
        counts = {'probed': 0, 'cached': 0, 'failed': 0}
        semaphore = asyncio.Semaphore(limit)

        async def prefetch_one(source):
            nonlocal done
            async with semaphore:
                self.boundary(job_id)
                try:
                    _, _, origin = await self.measurements.measure(job_id, source, source['name'], policy)
                except CloudError as exc:
                    if exc.code in ('LOGIN_REQUIRED', 'LOGIN_INVALID', 'RATE_LIMITED'):
                        raise
                    key = 'failed'
                except (UncertainState, NeedsReview):
                    key = 'failed'
                else:
                    key = 'cached' if origin != 'probed' else 'probed'
                counts[key] += 1
                done += 1
                # 进度只是提示，节流写入，别让 12 个文件刷出 12 条事件。
                if done == total or done % 3 == 0:
                    self.update_job(job_id, stage=f'批量探测 {done}/{total}')

        tasks = [asyncio.create_task(prefetch_one(source)) for source in targets]
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            # 任务级中止（暂停/取消/登录失效）：把还在飞的探测一起收掉，
            # probe() 的 finally 会杀掉 ffprobe 子进程，不留孤儿。
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        if counts['probed'] or counts['cached']:
            self.store.event('organize', '任务 %d：批量探测预取完成，命中缓存 %d 个、新探测 %d 个、失败 %d 个（并发 %d）。'
                             % (job_id, counts['cached'], counts['probed'], counts['failed'], limit))

    def _archive_batch_fingerprint(self, config, plan):
        """Snapshot the relevant listed video identities for a final live comparison."""
        parents = sorted(set(plan.get('conflict_parent_ids', [])))
        if not parents:
            return ()
        snapshot = self.index.latest(self.index.account(config), config['archive_cid'])
        if not snapshot:
            raise UncertainState('归档核查快照不可用，停止批量整理。')
        placeholders = ','.join('?' for _ in parents)
        rows = self.store.db.execute(
            'SELECT file_id,parent_id,name,size,sha1,state,media_type,tmdb_id,season,episode,end_episode '
            f'FROM archive_entries WHERE snapshot_id=? AND parent_id IN ({placeholders}) ORDER BY file_id',
            (snapshot['id'], *parents)).fetchall()
        return tuple(tuple(row) for row in rows)

    async def _recheck_new_batch_target(self, job_id, saved, moves, config):
        """Only a previously absent TV work with non-destructive actions may use this path."""
        if (not saved['new_work'] or saved['plan'].get('current_archive_ids')
                or saved['plan'].get('conflict_parent_ids')):
            return False
        wanted = set(saved['items'])
        selected = [(item_id, plan) for item_id, _, plan in moves if item_id in wanted]
        if len(selected) < 2 or {item_id for item_id, _ in selected} != wanted:
            return False
        plans = [plan for _, plan in selected]
        if any(plan.get('wash', {}).get('decision') not in (None, 'archive')
               or plan.get('wash', {}).get('old') for plan in plans):
            return False
        dests = {str(plan.get('dest') or '') for plan in plans}
        paths = {tuple(plan.get('parts') or ()) for plan in plans}
        if len(dests) != 1 or '' in dests or len(paths) != 1:
            return False
        dest, parts = next(iter(dests)), next(iter(paths))
        self.boundary(job_id)
        trail = await self.cloud.path_info(dest)
        ids = [str(entry['id']) for entry in trail]
        root = str(config['archive_cid'])
        if root not in ids or ids[-1] != dest:
            raise UncertainState('目标目录位置已变化，停止批量整理。')
        names = tuple(entry['name'] for entry in trail[ids.index(root) + 1:])
        if names != parts:
            raise UncertainState('目标目录路径已变化，停止批量整理。')
        fresh = getattr(self.cloud, 'fresh_children', self.cloud.children)
        if await fresh(dest):
            raise UncertainState('目标目录出现新内容，停止批量整理并重新核查。')
        return True

    async def prepare_batch_move(self, job_id, rows, config):
        """一个目录一批：先定完计划，再一次改名、一次删除、一次移动，最后读一次目录对账。

        对齐形态：批量单位是「目标目录」，不再要求同剧同季、不再排除字幕，
        也不在批量前后对每个文件做 file() 回查（每次 2 个请求）。
        """
        if len(rows) < 2 or config.get('repair_only') or not hasattr(self.cloud, 'move_many'):
            return
        pending = [r for r in rows if r['state'] == 'discovered' and not json.loads(r['plan'] or '{}')]
        if not pending:
            return
        started_at = time.time()
        source_dir = json.loads(pending[0]['original']).get('parent_id')
        # → 预分析 → 组内决策 → 动作规划。source_path 我们只有目录 ID，拿不到就用 -。
        source_path = '-'
        self.store.event('organize', f'📂 扫描源文件夹: {source_dir}')
        self.store.event('organize', f'🔎 [115整理] 进入目录: fid={source_dir}, source_path={source_path}, parent_tmdb_id=-')
        self.store.event('organize', f'📡 [115整理] 开始准备目录内文件: fid={source_dir}, source_path={source_path}')
        timings = {'识别和方案': 0.0, '已有资源核查': 0.0, '准备目标目录': 0.0, '目标冲突核查': 0.0}
        diagnostics = {}
        batch_reuse = {}
        for row in pending:
            self.boundary(job_id)
            try:
                await self.process_item(job_id, row, config, prepare_only=True, timings=timings, batch_reuse=batch_reuse, diagnostics=diagnostics)
            except CloudError as exc:
                if exc.code in ('LOGIN_REQUIRED', 'LOGIN_INVALID', 'RATE_LIMITED', 'TMDB_UNAVAILABLE', 'CLOUD_UNAVAILABLE'):
                    raise
            except Stopped:
                raise
            except (NeedsReview, UncertainState):
                pass  # 逐文件路径会复核这一条
            except Exception:
                pass
        ids = [r['id'] for r in pending]
        marks = ','.join('?' for _ in ids)
        current = self.store.db.execute(
            f'SELECT * FROM organize_items WHERE id IN ({marks}) ORDER BY id', ids).fetchall()
        discard, moves = [], []
        for row in current:
            if row['state'] != 'planned':
                continue
            plan, original = json.loads(row['plan'] or '{}'), json.loads(row['original'])
            decision = plan.get('wash', {}).get('decision')
            if decision == 'discard':
                discard.append((row['id'], original, plan))
            elif decision in (None, 'archive', 'replace') and plan.get('dest') and plan.get('target_name') \
                    and not plan.get('pending') and not plan.get('subtitles') is None:
                moves.append((row['id'], original, plan))
        planned = sum(1 for r in current if r['state'] == 'planned')
        dest_count = len({str(p['dest']) for _, _, p in moves})
        renames = sum(1 for row in current
                      if (json.loads(row['plan'] or '{}').get('target_name')
                          and json.loads(row['plan']).get('target_name') != json.loads(row['original']).get('name')))
        self.store.event('organize',
                         f'📦 [115整理] 逐项准备完成: fid={source_dir}, total={len(current)}, '
                         f'dirs=0, files={len(current)}, 耗时={time.time() - started_at:.2f}s')
        self.store.event('organize', '⏱️ 任务 %d 准备耗时：%s。' %
                         (job_id, '；'.join('%s %.2fs' % (key, value) for key, value in timings.items())))
        if diagnostics:
            details = []
            for key, value in diagnostics.items():
                if key == '_tmdb':
                    continue
                reads = '，115读%d次' % value['reads'] if value['has_reads'] else ''
                details.append('%s %d次/%.2fs%s' % (key, value['calls'], value['seconds'], reads))
            counters = diagnostics.get('_tmdb', {})
            details.append('TMDB网络请求%d次，通用缓存命中%d次，单集缓存命中%d次' %
                           (counters.get('网络请求', 0), counters.get('缓存命中', 0), counters.get('单集缓存命中', 0)))
            self.store.event('organize', '🔬 任务 %d 准备明细（累计耗时含等待）：%s。' % (job_id, '；'.join(details)))
        for saved in batch_reuse.values():
            if not saved.get('reused'):
                continue
            self.boundary(job_id)
            # 操作前重新读取分类树和作品；新出现的旧版本不允许使用之前的空清单。
            started = time.monotonic()
            self._archive_directory_reads = {}
            if hasattr(self.cloud, '_children_memo'):
                self.cloud._children_memo.clear()
            if await self._recheck_new_batch_target(job_id, saved, moves, config):
                self.store.event('organize', f'任务 {job_id}：新作品批次复核目标目录完成（快速路径），耗时 {time.monotonic()-started:.2f}s。')
                continue
            proof = json.loads(json.dumps(saved['plan'], ensure_ascii=False))
            await self.index.refresh_related(job_id, proof, saved['original'])
            if (sorted(proof.get('current_archive_ids', [])) != sorted(saved['plan'].get('current_archive_ids', []))
                    or self._archive_batch_fingerprint(config, proof) != saved['fingerprint']
                    or proof.get('parts') != saved['plan']['parts']
                    or proof.get('target_name') != saved['plan']['target_name']):
                raise UncertainState('批量准备期间已有资源发生变化，已停止并保留文件，请重新核查。')
            label = '新作品' if saved['new_work'] else '已有作品'
            self.store.event('organize', f'任务 {job_id}：同一{label}归档核查已复用；操作前复核耗时 {time.monotonic()-started:.2f}s。')
        self.store.event('organize',
                         f'🧾 [115整理] 目录预分析完成: fid={source_dir}, 子目录=0, '
                         f'可处理媒体={planned}, 已确认未识别=0, 计划重命名={renames}')
        self.store.event('organize',
                         f'🧠 [115整理] 组内决策完成: fid={source_dir}, '
                         f'计划处理={planned}, 计划删除(组内)={len(discard)}')
        self.store.event('organize',
                         f'🗺️ [115整理] 动作规划完成: fid={source_dir}, 目标目录数={dest_count}, '
                         f'待移动={len(moves)}, 待删除(含替换)={len(discard)}, 待重命名={renames}')
        if len(discard) >= 2 and hasattr(self.cloud, 'recycle_many'):
            try:
                await self.upgrader.discard_many(job_id, discard, config)
            except Stopped:
                raise
            except CloudError as exc:
                if exc.code in ('LOGIN_REQUIRED', 'LOGIN_INVALID', 'RATE_LIMITED'):
                    raise
                self.store.event('organize', f'任务 {job_id}：批量淘汰需逐项核对（{exc.code}）。')
            except (NeedsReview, UncertainState) as exc:
                self.store.event('organize', f'任务 {job_id}：批量淘汰核查未通过，逐项处理。')
        by_dest = {}
        for item_id, original, plan in moves:
            by_dest.setdefault(str(plan['dest']), []).append((item_id, original, plan))
        for dest, group in by_dest.items():
            self.boundary(job_id)
            await self.batch_archive(job_id, dest, group, config)
        self.store.event('organize',
                         f'✅ [整理] 目录处理完成 fid={source_dir}：归档 {len(moves)} 条，'
                         f'洗版淘汰 {len(discard)} 条，耗时 {time.time() - started_at:.2f}s')

    @staticmethod
    def _batch_name(plan):
        return plan['wash']['stage_name'] if plan.get('wash', {}).get('decision') == 'replace' else plan['target_name']

    async def batch_archive(self, job_id, dest, group, config):
        """目标目录内批量归档：读一次目录 → 改名一次 → 移动一次 → 再读一次对账。"""
        existing = {e['name'].casefold() for e in await self.cloud.children(dest)}
        # 仅对已实测的无字幕、旧版均在同一目标目录的批次采用此流程。
        upstream_replace = {item_id for item_id, original, plan in group
            if hasattr(self.cloud, 'recycle_many') and plan.get('wash', {}).get('decision') == 'replace'
            and plan['wash'].get('old') and not plan.get('subtitles')
            and all(not old.get('subtitles') and str(old['source'].get('parent_id')) == str(dest)
                    for old in plan['wash']['old'])}
        ready, renames = [], []
        for item_id, original, plan in group:
            self.boundary(job_id)
            decision = plan.get('wash', {}).get('decision')
            # Wash replacements stay under temporary names until new playback
            # output is verified; old versions are never deleted in this batch.
            name = plan['target_name'] if item_id in upstream_replace else self._batch_name(plan)
            if name.casefold() in existing and not (item_id in upstream_replace and any(
                    old['source'].get('name','').casefold() == name.casefold()
                    for old in plan['wash']['old'])):
                continue
            ready.append((item_id, original, plan))
            if name != original['name']:
                renames.append((original['id'], name))
            for sub in plan.get('subtitles', []):
                target = sub.get('stage_name') if decision == 'replace' else sub['target_name']
                if not target or target.casefold() in existing:
                    ready.pop()
                    renames = [(fid, value) for fid, value in renames
                               if fid != original['id'] and fid not in {s['source']['id'] for s in plan.get('subtitles', [])}]
                    break
                if target != sub['source']['name']:
                    renames.append((sub['source']['id'], target))
        if not ready:
            return
        for item_id, original, plan in ready:
            plan['batch_prepared'] = True
            self.update_item(item_id, plan=plan, step='正在批量归档')
        try:
            rename_started = time.time()
            if renames:
                if hasattr(self.cloud, 'rename_many') and len(renames) > 1:
                    self.update_job(job_id, stage=f'正在批量重命名 {len(renames)} 个文件')
                    self.store.event('organize', f'任务 {job_id}：准备批量重命名 {len(renames)} 个文件（每批最多 50 个）。')
                    await self.cloud.rename_many(renames)
                    # 即便接口返回成功，也必须先核对每个 ID 和名称；部分成功不能整批移动。
                    sources = {str(original['id']): original for _, original, _ in ready}
                    for _, _, plan in ready:
                        for sub in plan.get('subtitles', []):
                            source = sub['source']
                            sources[str(source['id'])] = source
                    parents = {str(sources[str(fid)]['parent_id']) for fid, _ in renames}
                    fresh = getattr(self.cloud, 'fresh_children', self.cloud.children)
                    listings = {parent: {str(e['id']): e for e in await fresh(parent)} for parent in parents}
                    for fid, name in renames:
                        self.boundary(job_id)
                        source = sources[str(fid)]
                        parent = str(source['parent_id'])
                        actual = listings[parent].get(str(fid))
                        if not actual:
                            raise UncertainState('批量重命名后有文件未找到，转逐项核对。')
                        self.check_source(source, actual, name, parent)
                    self.store.event('organize',
                                     f'✏️ 批量重命名并逐项核实: {len(renames)} 个文件'
                                     f'（{time.time() - rename_started:.2f}s）')
                else:
                    for file_id, name in renames:
                        self.boundary(job_id)
                        await self.cloud.rename(file_id, name)
            replacement_sources = [(item_id, plan, old['source'])
                for item_id, original, plan in ready if item_id in upstream_replace
                for old in plan['wash']['old']]
            if replacement_sources:
                old_ids = [str(source['id']) for _, _, source in replacement_sources]
                for item_id, plan, source in replacement_sources:
                    plan['wash'].setdefault('actions', {})[source['id']] = {
                        'state': 'intent', 'source': source, 'at': time.time()}
                    self.update_item(item_id, plan=plan, step='正在批量回收旧版本')
                self.store.event('organize',
                                 f'🗑️ 批量删除 {len(old_ids)} 个文件（源侧淘汰 0 + 洗版替换 {len(old_ids)}）...')
                await self.cloud.recycle_many(old_ids, dest)
                for item_id, plan, source in replacement_sources:
                    plan['wash']['actions'][source['id']]['state'] = 'accepted'
                    plan['wash']['batch_finalized'] = True
                    self.update_item(item_id, plan=plan, step='旧版本回收请求已接受')
            moving = [original['id'] for _, original, _ in ready]
            moving += [sub['source']['id'] for _, _, plan in ready for sub in plan.get('subtitles', [])]
            move_started = time.time()
            self.store.event('organize', f'任务 {job_id}：批量移动 {len(moving)} 个文件 → {dest}（一次请求）。')
            if len(moving) == 1:
                await self.cloud.move(moving[0], dest)
            else:
                await self.cloud.move_many(moving, dest)
            self.store.event('organize',
                             f'🚚 批量移动成功: {len(moving)} 个文件 → {dest}'
                             f'（{time.time() - move_started:.2f}s）')
        except CloudError as exc:
            if exc.code in ('LOGIN_REQUIRED', 'LOGIN_INVALID', 'RATE_LIMITED'):
                raise
            # 响应丢失可能已经成功：不重放，交回逐文件路径按当前状态核对。
            self.store.event('organize', f'任务 {job_id}：批量归档结果未确认，逐文件核对。')
        except Exception as exc:                      # noqa: BLE001
            # 批量中途异常（含某个文件消失）不能把整批作废：下面的目录对账才是事实来源。
            self.store.event('organize', f'任务 {job_id}：批量归档中途异常（{type(exc).__name__}），按目录对账继续。')
        landed = {str(e['id']): e for e in await self.cloud.children(dest)}
        queue = self.publication_queues.get(job_id)
        for item_id, original, plan in ready:
            entry = landed.get(str(original['id']))
            if entry is None:
                continue          # 没落地：保持 planned，逐文件路径会再处理一次
            actual = {'id': str(original['id']), 'name': entry['name'], 'parent_id': str(dest),
                      'size': entry.get('size', original.get('size')), 'sha1': entry.get('sha1') or original.get('sha1', ''),
                      'pickcode': entry.get('pickcode') or original.get('pickcode', '')}
            expected = plan['target_name'] if item_id in upstream_replace else self._batch_name(plan)
            try:
                self.check_source(original, actual, expected, dest)
                for sub in plan.get('subtitles', []):
                    entry_sub = landed.get(str(sub['source']['id']))
                    target = sub.get('stage_name') if plan.get('wash', {}).get('decision') == 'replace' else sub['target_name']
                    if not entry_sub:
                        raise UncertainState('批量归档字幕未落地。')
                    self.check_source(sub['source'], entry_sub, target, dest)
            except UncertainState:
                continue
            self.update_item(item_id, state='archived', plan=plan, step='批量归档已核实')
            if plan.get('wash', {}).get('decision') != 'replace':
                self.index.record_managed(job_id, original, actual, plan)
            if queue is not None:
                plan['publication_source'] = actual
                if config.get('strm_delay', 0) and 'ready_at' not in plan:
                    plan['ready_at'] = time.time() + config['strm_delay']
                plan['queued'] = True
                self.update_item(item_id, plan=plan, state='archived', step='已加入后台 STRM 队列')
            else:
                await self.finish_publication(job_id, item_id, original, actual, plan, config)
        if queue is not None:
            queued = [item_id for item_id, _, _ in ready if self.store.db.execute(
                'SELECT state FROM organize_items WHERE id=?', (item_id,)).fetchone()['state'] == 'archived']
            if queued:
                queue.put_nowait((str(dest), queued))
                self.store.event('organize', f'任务 {job_id}：归档目录 {dest} 已加入后台 STRM 队列（{len(queued)} 个文件）。')

    async def prune_empty_chain(self, job_id, cid, config):
        """源目录整理空后立即回收，并自底向上把空的上级也收掉（同步根保留）。

        对齐「目录整理后为空，立即删除」：每层一次 children 确认 + 一次受保护的
        recycle_empty_directory（它自己会再确认位置与空目录，不靠调用方保证）。
        """
        roots = {str(x) for x in config.get('source_cids', [])}
        while cid and str(cid) not in roots:
            self.boundary(job_id)
            trail = await self.cloud.directory(cid)
            if len(trail) < 2 or str(trail[-1]) != str(cid):
                return
            # 只回收「同步根之下」的目录：根以上、或压根不在同步树里的目录一概不动。
            if not roots & {str(x) for x in trail[:-1]}:
                return
            parent = str(trail[-2])
            if await self.cloud.children(cid):
                return          # 还有字幕/元数据/未识别的文件：整目录保留
            try:
                await self.cloud.recycle_empty_directory(cid, parent)
            except CloudError as exc:
                if exc.code in ('LOGIN_REQUIRED', 'LOGIN_INVALID', 'RATE_LIMITED'):
                    raise
                self.store.event('organize', f'任务 {job_id}：空目录 {cid} 回收未完成（{exc.code}），留待收尾清理。')
                return
            current = json.loads(self.job(job_id)['config'])
            removed = current.setdefault('immediate_cleanup_ids', [])
            if str(cid) not in removed:
                removed.append(str(cid))
                self.update_job(job_id, config=json.dumps(current, ensure_ascii=False))
            self.store.event('organize', f'任务 {job_id}：源目录已整理空，立即删除 {cid}')
            cid = parent

    async def prune_source_parent(self, job_id, parent_id, config):
        """这个源目录下的条目全部终结后，才去判空——没终结的一律不动。"""
        if not parent_id or config.get('repair_only') or config.get('cleanup_disabled'):
            return
        # 只有「整理」任务清源目录；转存/同步等任务的目标目录不是待整理的源，不能当空目录删掉。
        if config.get('job_kind', 'organize') != 'organize':
            return
        if not config.get('cleanup_empty_enabled', True):
            return
        if not hasattr(self.cloud, 'recycle_empty_directory'):
            return
        rows = self.store.db.execute(
            'SELECT state FROM organize_items WHERE job_id=? AND json_extract(original, "$.parent_id")=?',
            (job_id, str(parent_id))).fetchall()
        if any(r['state'] not in ('done', 'skipped', 'recycled', 'pending', 'review') for r in rows):
            return
        try:
            async with self.publication_gate:
                await self.prune_empty_chain(job_id, parent_id, config)
        except CloudError as exc:
            if exc.code in ('LOGIN_REQUIRED', 'LOGIN_INVALID', 'RATE_LIMITED'):
                raise
            self.store.event('organize', f'任务 {job_id}：空目录即时清理未完成（{exc.code}），留待收尾清理。')
        except Exception as exc:                      # noqa: BLE001
            # 清空目录只是收尾动作，失败绝不能把已经归档的结果判成待核查。
            self.store.event('organize', f'任务 {job_id}：空目录即时清理异常（{type(exc).__name__}），留待收尾清理。')

    async def process_item(self, job_id, row, config, prepare_only=False, timings=None, batch_reuse=None, diagnostics=None):
        if row['state'] in ('done', 'recycled', 'skipped', 'pending'):
            return          # 已终结的条目不再重复读云端
        if row['state'] == 'archived':
            # 批量归档（或上次中断）已经搬完文件：只补发布，绝不再动云端。
            plan = json.loads(row['plan'] or '{}')
            queue = self.publication_queues.get(job_id)
            if queue is not None:
                if plan.get('dest') and not plan.get('queued'):
                    plan['queued'] = True
                    self.update_item(row['id'], plan=plan, step='已加入后台 STRM 队列')
                    queue.put_nowait((str(plan['dest']), [row['id']]))
                return
            # repair_only 不建后台队列。这里以前直接 return，条目就永远停在
            # 「等待补齐本地输出」，任务收尾被判 partial —— 「修复输出」等于没跑。
            original = json.loads(row['original'])
            actual = plan.get('publication_source')
            if not isinstance(actual, dict) or not actual.get('id'):
                actual = await self.cloud.file(original['id'])
            await self.finish_publication(job_id, row['id'], original, actual, plan, config)
            return
        original, plan = json.loads(row['original']), json.loads(row['plan'])
        if plan.get('pending'):
            return await self.quarantine(job_id, row, original, config, plan['reason'])
        if not plan:
            try:
                self.update_item(row['id'], step='TMDB 识别与 FFprobe 探测')
                phase_started = time.monotonic()
                try:
                    for attempt in range(config['retries'] + 1):
                        try:
                            token = _prepare_diagnostics.set(diagnostics)
                            try:
                                plan = await self.build_plan(job_id, original, diagnostics)
                            finally:
                                _prepare_diagnostics.reset(token)
                            break
                        except CloudError as exc:
                            if exc.code not in ('TMDB_UNAVAILABLE','CLOUD_UNAVAILABLE','PROBE_TIMEOUT') or attempt == config['retries']:
                                raise
                            await asyncio.sleep(config.get('retry_interval', 2))
                            self.boundary(job_id)
                    self.boundary(job_id)
                finally:
                    if timings is not None:
                        timings['识别和方案'] += time.monotonic() - phase_started
                phase_started = time.monotonic()
                hint = plan.get('hint') or {}
                key = (hint.get('media_type'), plan.get('details', {}).get('id'),
                       hint.get('season'), tuple(plan.get('parts', [])))
                cached = batch_reuse.get(key) if batch_reuse is not None else None
                if cached and config.get('keep_existing_names', False) and (
                        file_title(plan.get('target_name', '')) != cached['source_title']):
                    cached = None
                if cached and cached['rename_from'] and (
                        file_title(plan.get('target_name', '')) != cached['rename_from']
                        or not plan.get('target_name', '').startswith(cached['rename_from'])):
                    cached = None  # A different naming shape needs its own fresh lookup.
                if cached:
                    plan['conflict_parent_ids'] = list(cached['plan'].get('conflict_parent_ids', []))
                    plan['current_archive_ids'] = list(cached['plan'].get('current_archive_ids', []))
                    plan['parts'] = list(cached['plan']['parts'])
                    if cached['rename_from']:
                        plan['target_name'] = cached['rename_to'] + plan['target_name'][len(cached['rename_from']):]
                    cached['reused'] = True
                    cached['items'].append(row['id'])
                else:
                    before_name = plan.get('target_name', '')
                    with _prepare_measure(diagnostics, '作品目录与资源刷新', self.cloud):
                        no_work = await self.index.refresh_related(job_id, plan, original)
                    before_title, after_title = file_title(before_name), file_title(plan.get('target_name', ''))
                    name_changed = before_name != plan.get('target_name', '')
                    safe_rename = (not name_changed or (before_title and after_title and
                        plan['target_name'] == after_title + before_name[len(before_title):]))
                    if (batch_reuse is not None and hint.get('media_type') == 'tv'
                            and no_work in (True, False) and safe_rename and key not in batch_reuse):
                        batch_reuse[key] = {'plan': json.loads(json.dumps(plan, ensure_ascii=False)),
                                            'original': dict(original), 'reused': False, 'new_work': no_work,
                                            'source_title': before_title,
                                            'rename_from': before_title if name_changed else '',
                                            'rename_to': after_title if name_changed else '',
                                            'items': [row['id']],
                                            'fingerprint': self._archive_batch_fingerprint(config, plan)}
                prepare_config = config
                if plan.get('strm_filtered'):
                    prepare_config = {**config, 'upgrade_policy':{**config.get('upgrade_policy',{}),'mode':'pending'}}
                with _prepare_measure(diagnostics, '洗版及已有集数判断', self.cloud):
                    await self.upgrader.prepare(job_id, original, plan, prepare_config)
                if timings is not None:
                    timings['已有资源核查'] += time.monotonic() - phase_started
                if plan.get('wash', {}).get('decision') in ('skip', 'discard'):
                    wash = plan.get('wash') or {}
                    if wash.get('reason') == 'identical_copy':
                        step_text = '将回收本次转存的重复副本（与库内内容完全相同）'
                    elif wash.get('decision') == 'skip':
                        step_text = '库内已有同片，原位置跳过'
                    else:
                        step_text = '将回收本次转存的旧版本'
                    self.update_item(row['id'], plan=plan, original=dumps(original), state='planned', step=step_text)
                    if prepare_only: return
                    return await self.process_item(job_id, self.store.db.execute('SELECT * FROM organize_items WHERE id=?', (row['id'],)).fetchone(), config)
                dest = config['archive_cid']
                phase_started = time.monotonic()
                for part in plan['parts']:
                    dest = await self.ensure_dir(job_id, dest, part)
                if timings is not None:
                    timings['准备目标目录'] += time.monotonic() - phase_started
                plan['dest'] = dest
                try:
                    phase_started = time.monotonic()
                    await self.check_conflict(job_id, original, plan, dest)
                    if timings is not None:
                        timings['目标冲突核查'] += time.monotonic() - phase_started
                except Discarded as exc:
                    if prepare_only: return
                    return await self.discard_incoming(job_id, row, original, config, str(exc))
                library = library_root(config)
                plan['relative_path'] = str(Path(library, *plan['parts'], Path(plan['target_name']).stem + '.strm'))
                plan['playback_base'] = config.get('playback_base') or self.store.get('playback_base')
                # Allocate the playback intent before any source modification.
                plan['playback_id'] = secrets.token_hex(16)
                plan['token_cipher'] = self.store.crypto.encrypt(b'').decode()
                subtitle_targets = set()
                for sub in sorted(original.get('subtitles', []), key=lambda s: (s['name'].casefold(), str(s['id']))):
                    suffix = subtitle_suffix(original['name'], sub['name'])
                    target = Path(plan['target_name']).stem + suffix
                    if target.casefold() in subtitle_targets:
                        plan.setdefault('subtitle_recycle', {'rule_version': 'subtitle-conflict-v1', 'actions': {}, 'sources': []})['sources'].append(sub)
                        plan['subtitle_info'] = '字幕目标名称冲突，已选择一份归档，其余字幕回收结果见任务记录。'
                        continue
                    subtitle_targets.add(target.casefold())
                    if len(target.encode()) > 250:
                        raise NeedsReview('字幕名称超长，不能可靠规范命名。')
                    plan['subtitles'].append({'source': sub, 'target_name': target})
                    if plan.get('wash', {}).get('decision') == 'replace':
                        plan['subtitles'][-1]['stage_name'] = Path(plan['wash']['stage_name']).stem + suffix
                        if len(plan['subtitles'][-1]['stage_name'].encode()) > 250:
                            raise NeedsReview('洗版临时字幕名称超长，保留新旧资源待处理。')
                if original.get('subtitle_warning'):
                    plan['warnings'].append('存在同主体多视频，未自动关联歧义外挂字幕。')
            except (NeedsReview, CloudError) as exc:
                if isinstance(exc, CloudError) and exc.code in (
                        'LOGIN_REQUIRED', 'LOGIN_INVALID', 'RATE_LIMITED', 'FILE_CHANGED', 'FILE_NOT_FOUND',
                        'TMDB_UNAVAILABLE', 'CLOUD_UNAVAILABLE',
                        'INVALID_FILE', 'SCHEMA_CHANGED',
                        'UPGRADE_UNCERTAIN', 'RECYCLE_UNVERIFIED', 'RECYCLE_REVIEW_REQUIRED'):
                    raise
                reason = str(exc) if isinstance(exc, (NeedsReview, CloudError)) else '识别失败。'
                return await self.quarantine(job_id, row, original, config, reason)
            self.update_item(row['id'], plan=plan, original=dumps(original), state='planned', step='已定好归档计划，等待执行')
        if prepare_only:
            return
        decision = plan.get('wash', {}).get('decision')
        wash = plan.get('wash') or {}
        # 括号里必须是「规则+胜负」，不能再印 unknown_fallback 这类内部代码。
        evidence = wash_reason(wash).replace('洗版覆盖：', '').strip()
        bracket = f'({evidence}) ' if evidence else ''
        if decision == 'skip':
            self.store.event('organize', f'⏭️ [洗版] {bracket}{original["name"][:80]}')
            self.update_item(row['id'], state='skipped', step='已有冲突，原位置跳过', message=plan['wash']['reason'])
            return
        if decision == 'discard':
            self.store.event('organize', f'🗑️ [洗版] 淘汰新片 {bracket}{original["name"][:80]}')
            return await self.upgrader.discard(job_id, row['id'], original, plan, config)
        if decision == 'replace':
            older = [o['source'].get('name', '')[:80] for o in (plan.get('wash') or {}).get('old', [])[:1]]
            self.store.event('organize',
                             f'🔄 [洗版] {bracket}{original["name"][:70]}'
                             + (f' ← 替换 {older[0]}' if older else ''))
        if decision == 'replace':
            if config.get('repair_only'):
                return await self.upgrader.repair(job_id, row['id'], original, plan, config)
            return await self.upgrader.replace(job_id, row['id'], original, plan, config, stage_only=job_id in self.publication_queues)
        self.boundary(job_id)
        self.update_item(row['id'], step='核实并归档视频')
        if config.get('repair_only'):
            actual = await self.cloud.file(original['id'])
            self.check_source(original, actual, plan['target_name'], plan['dest'])
        else:
            actual = await self.change_file(job_id, row['id'], original, plan['target_name'], plan['dest'], config['retries'])
        self.update_item(row['id'], state='archived', step='视频归档已核实')
        for sub in plan['subtitles']:
            self.boundary(job_id)
            if config.get('repair_only'):
                current = await self.cloud.file(sub['source']['id'])
                self.check_source(sub['source'], current, sub['target_name'], plan['dest'])
            else:
                await self.change_file(job_id, row['id'], sub['source'], sub['target_name'], plan['dest'], config['retries'])
        self.boundary(job_id)
        self.index.record_managed(job_id, original, actual, plan)
        queue = self.publication_queues.get(job_id)
        if queue is not None and not config.get('repair_only'):
            # The archived item and its exact publication plan are the durable
            # queue. A restart resumes it through the normal identity checks.
            plan['publication_source'] = actual
            if config.get('strm_delay', 0) and 'ready_at' not in plan:
                plan['ready_at'] = time.time() + config['strm_delay']
            self.update_item(row['id'], plan=plan, state='archived', step='已加入后台 STRM 队列')
            return
        await self.finish_publication(job_id, row['id'], original, actual, plan, config)

    async def finish_publication(self, job_id, item_id, original, actual, plan, config):
        self.update_item(item_id, step='写入字幕与 STRM')
        if config.get('strm_delay', 0):
            if 'ready_at' not in plan:
                plan['ready_at'] = time.time() + config['strm_delay']
                self.update_item(item_id, plan=plan, step='等待生成 STRM')
            while time.time() < plan['ready_at']:
                await asyncio.sleep(min(.5, max(0, plan['ready_at'] - time.time())))
                self.boundary(job_id)
        await self.export(job_id, item_id, original, actual, plan)
        await self.upgrader.recycle_subtitle_conflicts(job_id, item_id, original, plan, config)
        message = '；'.join(filter(None, [plan.get('strm_filtered'), plan.get('subtitle_info'), *plan['warnings']]))
        self.update_item(item_id, state='done', plan=plan, step='归档完成，STRM 按规则跳过' if plan.get('strm_filtered') else '归档及 STRM 完成', message=message)
        self.audit(job_id, item_id, '整理与本地输出完成')

    async def fill_directory(self, job_id, dest, entries, config):
        """对目标目录做一次「按目录增量」，补齐目录内所有缺 STRM 的视频。

        对齐实测行为（docs/upstream/_FULLSTRM_TRACE.md 第十一节）：
        整理完成后只对刚整理进去的那一级目录做增量 —— 列该目录（分页）→ 批量详情
        → 写缺失的 .strm，**与「本批整理了哪些文件」无关**。
        目录清单在上面已经读过，这里不额外发请求；只为缺输出的视频补一条本地 STRM。
        """
        rules = {**StrmOptions().model_dump(), **(config.get('strm_options') or {})}
        archive = str(config.get('archive_cid') or '')
        account = self.index.account(config)
        base = config.get('playback_base') or self.store.get('playback_base')
        trail = await self.cloud.path_info(dest)
        ids = [str(node['id']) for node in trail]
        if archive not in ids:
            return 0
        # 0.8.30：库根镜像 115 目录名，这里直接取扫描到的归档目录名字（比标签更可靠）。
        library = library_root(config, trail)
        folder = self.root / Path(library, *[node['name'] for node in trail[ids.index(archive) + 1:]])
        videos = set(rules['video_extensions'])
        created = 0
        # 本任务的条目（含尚未发布成功的）和已有播放映射都不算「孤儿」：
        # 前者由正常发布路径负责，后者已经登记过，重复建会多出一条播放身份。
        owned = {str(row[0]) for row in self.store.db.execute(
            'SELECT file_id FROM organize_items WHERE job_id=?', (job_id,))}
        owned |= {str(row[0]) for row in self.store.db.execute('SELECT file_id FROM playbacks')}
        for entry in entries:
            self.boundary(job_id)
            if entry.get('is_dir') or Path(entry['name']).suffix.lower() not in videos:
                continue
            if str(entry['id']) in owned:
                continue
            if skip_reason(entry, rules):
                continue
            relative = str(folder.joinpath(Path(entry['name']).stem + '.strm').relative_to(self.root))
            if (self.root / relative).exists():
                continue
            if self.store.db.execute('SELECT 1 FROM playbacks WHERE file_id=? AND revoked=1', (entry['id'],)).fetchone():
                continue  # 明确撤销过的播放入口不重新启用
            identity = identity_of(entry)
            if not identity:
                continue  # 缺 pickcode/sha1/size 时不猜地址，留给完整同步处理
            pid = secrets.token_hex(16)
            token_cipher = self.store.crypto.encrypt(b'').decode()
            url = play115_url(base, *identity, entry['name'])
            (self.root / relative).parent.mkdir(parents=True, exist_ok=True)
            with self.store.db:
                self.store.db.execute('INSERT OR IGNORE INTO playbacks(id,file_id,pickcode,name,relative_path,token_hash,created) VALUES(?,?,?,?,?,?,?)',
                    (pid, entry['id'], entry['pickcode'], entry['name'], relative, digest(''), time.time()))
            atomic_write(self.root / relative, url.encode(), self.root)
            plan = {'playback_id': pid, 'token_cipher': token_cipher, 'relative_path': relative,
                    'local_files': {relative: hashlib.sha256(url.encode()).hexdigest()}}
            self.outputs.remember(account, entry, ids, plan)
            with self.store.db:
                self.store.db.execute('INSERT OR IGNORE INTO strm_changes VALUES(?,?,?)', (job_id, 'updated', relative))
            created += 1
        if created:
            self.store.event('organize', f'任务 {job_id}：目录 {dest} 增量补齐 {created} 个 STRM'
                                         f'（含非本批整理的文件）。')
        return created

    async def publication_loop(self, job_id, queue, config, on_published):
        while True:
            dest, item_ids = await queue.get()
            try:
                pending = self.store.db.execute(
                    'SELECT plan FROM organize_items WHERE id IN (%s) AND state=?' % ','.join('?' for _ in item_ids),
                    (*item_ids, 'archived')).fetchall()
                due = max((json.loads(r['plan'] or '{}').get('ready_at', 0) or
                           json.loads(r['plan'] or '{}').get('wash', {}).get('ready_at', 0) for r in pending), default=0)
                while time.time() < due:
                    await asyncio.sleep(min(.5, due - time.time()))
                    self.boundary(job_id)
                async with self.publication_gate:
                    self.boundary(job_id)
                    try:
                        entries = {str(e['id']): e for e in await self.cloud.children(dest) if not e.get('is_dir')}
                    except CloudError:
                        raise
                    except Exception as exc:
                        raise UncertainState('增量同步目录读取失败：' + type(exc).__name__) from exc
                    stage_started = time.time()
                    self.store.event('organize', f'任务 {job_id}：增量同步目录 {dest}，扫描 {len(entries)} 个文件，处理 {len(item_ids)} 个归档。')
                    # First publish all staged replacements; only successful
                    # publications may enter the guarded bulk recycle.
                    replacements = []
                    for item_id in item_ids:
                        row = self.store.db.execute('SELECT * FROM organize_items WHERE id=? AND job_id=?', (item_id, job_id)).fetchone()
                        if not row or row['state'] != 'archived':
                            continue
                        original, plan = json.loads(row['original']), json.loads(row['plan'])
                        if plan.get('wash', {}).get('decision') != 'replace' or plan.get('wash', {}).get('batch_finalized'):
                            continue
                        try:
                            await self.upgrader.replace(job_id, item_id, original, plan, config, publish_only=True)
                            replacements.append((item_id, original, plan))
                        except (asyncio.CancelledError, Stopped):
                            raise
                        except CloudError as exc:
                            if exc.code in ('LOGIN_REQUIRED', 'LOGIN_INVALID', 'RATE_LIMITED'):
                                raise
                        except Exception:
                            pass  # Normal item path records failure or retries.
                    if len(replacements) >= 2:
                        try:
                            await self.upgrader.recycle_published_batch(job_id, replacements, config)
                        except (asyncio.CancelledError, Stopped):
                            raise
                        except CloudError as exc:
                            if exc.code in ('LOGIN_REQUIRED', 'LOGIN_INVALID', 'RATE_LIMITED'):
                                raise
                            self.store.event('organize', f'任务 {job_id}：批量回收未确认，按持久化意图逐项核对。')
                        except Exception as exc:
                            self.store.event('organize', f'任务 {job_id}：批量回收核查未完成（{type(exc).__name__}），逐项核对。')
                    for item_id in item_ids:
                        try:
                            row = self.store.db.execute('SELECT * FROM organize_items WHERE id=? AND job_id=?', (item_id, job_id)).fetchone()
                            if not row or row['state'] != 'archived':
                                continue
                            original, plan = json.loads(row['original']), json.loads(row['plan'])
                            current = entries.get(str(original['id']))
                            if current is None:
                                # A listing can race with a move or be incomplete; confirm before skipping.
                                current = await self.cloud.file(original['id'])
                            wash = plan.get('wash', {})
                            if wash.get('batch_finalized'):
                                self.check_source(original, current, plan['target_name'], plan['dest'])
                                await self.finish_publication(job_id, item_id, original, current, plan, config)
                                for old_version in wash.get('old', []):
                                    self.upgrader.cleanup_old(job_id, item_id, old_version, plan)
                            elif wash.get('decision') == 'replace':
                                await self.upgrader.replace(job_id, item_id, original, plan, config)
                            else:
                                self.check_source(original, current, plan['target_name'], plan['dest'])
                                await self.finish_publication(job_id, item_id, original, current, plan, config)
                            if config.get('notify_emby'):
                                self.emby.enqueue(job_id)
                        except (asyncio.CancelledError, Stopped):
                            raise
                        except CloudError as exc:
                            if exc.code in ('LOGIN_REQUIRED', 'LOGIN_INVALID', 'RATE_LIMITED'):
                                raise
                            if exc.code == 'FILE_NOT_FOUND':
                                self.update_item(item_id, state='skipped', step='源文件不可用，已跳过', message='归档后源文件已不存在，继续处理其他文件。')
                            else:
                                self.blocked_item(item_id, '后台 STRM 生成未完成', str(exc))
                        except Exception as exc:
                            self.blocked_item(item_id, '后台 STRM 生成未完成', type(exc).__name__)
                            self.store.event('organize', f'任务 {job_id}：后台 STRM 生成未完成（{type(exc).__name__}）。')
                        finally:
                            try:
                                await on_published(item_id)
                            except Exception as exc:
                                self.store.event('organize', f'任务 {job_id}：后台通知未完成（{type(exc).__name__}）。')
                # 但缺 STRM」的文件也要补上（例如别的工具放进来的文件）。
                try:
                    await self.fill_directory(job_id, dest, list(entries.values()), config)
                except (asyncio.CancelledError, Stopped):
                    raise
                except CloudError as exc:
                    if exc.code in ('LOGIN_REQUIRED', 'LOGIN_INVALID', 'RATE_LIMITED'):
                        raise
                    self.store.event('organize', f'任务 {job_id}：目录增量补齐未完成（{exc.code}）。')
                except Exception as exc:
                    self.store.event('organize', f'任务 {job_id}：目录增量补齐未完成（{type(exc).__name__}）。')
                self.store.event('organize',
                                 f'✅ 增量 STRM 完成 dest={dest}：{len(item_ids)} 个归档，'
                                 f'耗时 {time.time() - stage_started:.2f}s')
            finally:
                queue.task_done()

    async def download(self, url, headers, limit):
        async with self.http.stream('GET', url, headers=headers, follow_redirects=True) as response:
            response.raise_for_status()
            content = bytearray()
            async for part in response.aiter_bytes():
                content.extend(part)
                if len(content) > limit:
                    raise ValueError('download size limit')
        return bytes(content)

    async def export(self, job_id, item_id, original, actual, plan):
        if plan.get('strm_filtered'):
            with self.store.db:
                self.store.db.execute('INSERT OR REPLACE INTO organized_files VALUES(?,?,?,?,?,?)',
                    (original['id'],job_id,item_id,plan['playback_id'],plan['relative_path'],dumps({key:plan[key] for key in ('hint','details','media')})))
            plan['warnings'] = []
            self.store.event('organize','视频已归档，未生成 STRM：'+plan['strm_filtered'])
            return
        path = self.root / plan['relative_path']
        account = self.index.account(json.loads(self.job(job_id)['config']))
        token = self.store.crypto.decrypt(plan['token_cipher'].encode()).decode()
        pid = plan['playback_id']
        # Mapping is durable before publishing a usable local URL.
        with self.store.db:
            existing = self.store.db.execute('SELECT file_id,token_hash,revoked FROM playbacks WHERE id=?', (pid,)).fetchone()
            if existing and existing['file_id'] != original['id']:
                raise UncertainState('播放映射身份冲突。')
            if existing and existing['revoked']:
                raise UncertainState('播放入口已被撤销，普通输出不会重新启用。')
            self.store.db.execute('INSERT OR IGNORE INTO playbacks(id,file_id,pickcode,name,relative_path,token_hash,created) VALUES(?,?,?,?,?,?,?)',
                (pid, original['id'], actual['pickcode'], plan['target_name'], plan['relative_path'], digest(token), time.time()))
            self.store.db.execute('UPDATE playbacks SET name=?,relative_path=?,pickcode=? WHERE id=?', (plan['target_name'],plan['relative_path'],actual['pickcode'],pid))
        # Emby and its plugins own NFO and artwork, including pre-existing files.
        # Apply this policy to resumed and repair jobs as well as new tasks.
        self.boundary(job_id)
        verified_subtitles = []
        for sub in plan['subtitles']:
            if Path(sub['target_name']).suffix.lower() not in set(json.loads(self.job(job_id)['config']).get('strm_options',{}).get('metadata_extensions', SUBTITLES)):
                continue
            self.boundary(job_id)
            local = path.parent / sub['target_name']
            if local.is_file() and plan.get('wash', {}).get('decision') != 'replace':
                continue
            try:
                current = await self.cloud.file(sub['source']['id'])
                cloud_name = sub.get('export_cloud_name') or (sub.get('stage_name', sub['target_name']) if not plan.get('wash', {}).get('finalizing') else sub['target_name'])
                self.check_source(sub['source'], current, cloud_name, plan['dest'])
                ua = 'StrimKeep/0.2 Subtitle'
                url, headers = await self.cloud.url(current['pickcode'], ua)
                content = await self.download(url, {**headers, 'User-Agent': ua}, 20 * 1024 * 1024)
                self.upgrader.allow_local_write(local, plan)
                atomic_write(local, content, self.root)
                plan.setdefault('local_files', {})[str(local.relative_to(self.root))] = hashlib.sha256(content).hexdigest()
                fingerprint = self.measurements.fingerprint(current)
                if (fingerprint and len(content) == fingerprint[1]
                        and hashlib.sha1(content).hexdigest().upper() == fingerprint[0]):
                    verified_subtitles.append({k: current[k] for k in ('id', 'name', 'parent_id', 'size', 'sha1')})
            except (httpx.HTTPError, ValueError, CloudError) as exc:
                if isinstance(exc, CloudError) and exc.code in ('LOGIN_REQUIRED', 'LOGIN_INVALID', 'UPGRADE_UNCERTAIN'):
                    raise
                plan['warnings'].append('外挂字幕下载失败；视频已归档，后续需补同步。')
        metadata_types = set(json.loads(self.job(job_id)['config']).get('strm_options',{}).get('metadata_extensions', [])) - SUBTITLES
        if metadata_types:
            attachments = [v for v in await self.cloud.children(original['parent_id']) if not v['is_dir'] and Path(v['name']).suffix.lower() in metadata_types]
            await copy_cloud_metadata(self,job_id,attachments,path.parent)
        self.boundary(job_id)
        # 公共播放地址构造（B02）：与样例、同步、补齐输出共用同一实现。
        # 可读段统一取 STRM 文件名主干，保证重生成 Token / 补齐输出 /
        # 重复同步产生字节一致的地址；slug 只是显示，不参与定位或授权。
        base = plan.get('playback_base') or self.store.get('playback_base')
        identity = identity_of(actual) or identity_of(original)
        if identity:
            url = play115_url(base, *identity, actual.get('name') or original['name'])
        else:
            # 清单里缺 sha1/size（极少见）时退回旧格式：宁可少一个字段，也不写错地址。
            url = play_url(base, pid, plan.get('target_name') or Path(plan['relative_path']).stem, token)
        self.upgrader.allow_local_write(path, plan)
        # 「新增」= 写之前磁盘上没有这个文件，也没有登记过同路径输出。重复整理同一
        # 文件、洗版重发同一季都不算新增，通知里也就不再重复列明细。
        plan['strm_created'] = bool(not path.is_file() and not self.store.db.execute(
            'SELECT 1 FROM organized_files WHERE relative_path=?', (plan['relative_path'],)).fetchone())
        replacing_strm = bool(path.is_file() and any(
            plan['relative_path'] in old.get('local_files', {})
            and old['local_files'][plan['relative_path']] == hashlib.sha256(path.read_bytes()).hexdigest()
            for old in plan.get('wash', {}).get('old', [])))
        if not plan.get('wash'):
            previous = {plan['relative_path']: hashlib.sha256(url.encode()).hexdigest(), **plan.get('local_files', {})}
            self.outputs.guard(account, original['id'], plan['relative_path'], previous)
        atomic_write(path, url.encode(), self.root)
        if replacing_strm:
            with self.store.db:
                self.store.db.execute('INSERT OR IGNORE INTO strm_changes VALUES(?,?,?)', (job_id, 'replaced', plan['relative_path']))
                self.store.db.execute('INSERT OR IGNORE INTO strm_change_items VALUES(?,?,?,?)', (job_id,item_id,'replaced',plan['relative_path']))
        plan.setdefault('local_files', {})[plan['relative_path']] = hashlib.sha256(url.encode()).hexdigest()
        try:
            extras = await write_sidecars(self, path, plan,
                                           json.loads(self.job(job_id)['config']).get('strm_options') or {})
            if extras:
                plan['metadata_files'] = extras
        except Exception as exc:
            # 附属文件只是锦上添花，写不出来不能拖垮已经成功的整理。
            plan['warnings'].append('NFO 或海报生成失败：%s' % str(exc)[:80])
        with self.store.db:
            self.store.db.execute('INSERT OR REPLACE INTO organized_files VALUES(?,?,?,?,?,?)',
                (original['id'], job_id, item_id, pid, plan['relative_path'], dumps({key: plan[key] for key in ('hint', 'details', 'media')})))
        # Output publication does not depend on a second ancestry request after
        # the successful move. The path was resolved from live parent listings.
        trail = [json.loads(self.job(job_id)['config'])['archive_cid'], actual['parent_id']]
        self.outputs.remember(account, {**actual, 'subtitles': verified_subtitles}, trail, plan)

    async def retry_cleanup(self, job_id, config):
        state = config.get('empty_cleanup')
        unfinished = self.store.db.execute("SELECT 1 FROM organize_items WHERE job_id=? AND state NOT IN ('done','pending','skipped','recycled')", (job_id,)).fetchone()
        if (config.get('repair_only') or config.get('job_kind', 'organize') != 'organize'
                or not self.job(job_id)['manifest'] or unfinished
                or not isinstance(state, dict) or not (state.get('entries') or self.cleanup.needs_candidate_retry(state))):
            raise UncertainState('该任务没有可单独恢复的目录清理记录，已停止。')
        self.update_job(job_id, stage='核查并重试空目录清理')
        cleanup = await self.cleanup.run(job_id, config)
        self.boundary(job_id)
        counts = dict(self.store.db.execute('SELECT state,count(*) FROM organize_items WHERE job_id=? GROUP BY state', (job_id,)).fetchall())
        plans = [json.loads(r['plan']) for r in self.store.db.execute('SELECT plan FROM organize_items WHERE job_id=?', (job_id,))]
        partial = (counts.get('pending') or any(p.get('warnings') for p in plans) or cleanup['warning']
                   or self.job(job_id)['notify_state'] in ('failed', 'unknown'))
        # Keep this mode across pause/login/restart; clear it only after the
        # attempt ends. Video states, plans, tokens and notification stay intact.
        current = json.loads(self.job(job_id)['config'])
        current.pop('cleanup_only', None)
        self.update_job(job_id, config=dumps(current), state='partial' if partial else 'completed',
                        stage='空目录清理结束', message=f'已确认清理 {cleanup["removed"]} 个空目录' + (f'，回收 {cleanup["attachments"]} 个海报/元数据文件' if cleanup.get('attachments') else '') + (f'，回收 {cleanup["subtitles"]} 个无法跟随的字幕' if cleanup.get('subtitles') else '') + '。' + cleanup['warning'])

    async def run_job(self, job_id):
        config = json.loads(self.job(job_id)['config'])
        # 目录清单缓存与请求账本按「账号 + 任务」隔离：并发泳道之间不再互相清空，
        # 换过 115 账号后也不会把上一个账号的目录清单当成当前的。
        if hasattr(self.cloud, 'use_scope'):
            self.cloud.use_scope(self.current_account(), job_id)
            if hasattr(self.cloud, 'reset_ledger'):
                self.cloud.reset_ledger()
        elif hasattr(self.cloud, '_children_memo'):
            self.cloud._children_memo.clear()
            if hasattr(self.cloud, 'reset_ledger'):
                self.cloud.reset_ledger()
        if config.get('job_kind', 'organize') not in ('transfer', 'strm_sync'):
            self._archive_directory_reads = {}
        self.update_job(job_id, state='running', message='')
        try:
            # Unknown/retired task types must never enter the organize pipeline.
            if config.get('job_kind', 'organize') not in (
                    'organize', 'transfer', 'strm_sync', 'archive_index',
                    'share_organize', 'classification_migration'):
                self.update_job(job_id, state='cancelled', stage='任务类型不可用',
                                message='此任务类型已停用，不再执行。', notify_state='not_needed')
                return
            self.boundary(job_id)
            scope_config = self.store.get('organize_config', {}) or {}
            if (config.get('job_kind', 'organize') == 'organize' and config.get('source_cids')
                    and scope_config.get('source_cids')):
                # 恢复或执行前按当前配置复校：目录被移出扫描范围就不再动它。
                allowed = await self.allowed_scan_cids(list(config['source_cids']))
                if not allowed:
                    self.update_job(job_id, state='cancelled', stage='扫描目录已移出范围',
                                    message='待整理目录已不在扫描范围，本次未执行。',
                                    notify_state='not_needed')
                    return
                config['source_cids'] = allowed
                self.update_job(job_id, config=dumps(config))
            # Old queued jobs must not fall through to ordinary organization.
            if config.get('job_kind') == 'classification_migration':
                self.update_job(job_id, state='cancelled', stage='历史分类迁移已停用',
                    message='历史分类迁移功能已移除；保留已有步骤，不再自动移动文件。旧分类可手动移回转存后重新整理。')
                return
            # 是就跳过登录/状态检查，115 挂了也能把本地输出修好。
            sweep_now = (self.sync.should_sweep(config)
                         if config.get('job_kind') == 'strm_sync' else None)
            if not sweep_now:
                await self.cloud.status()
            if config.get('job_kind') == 'transfer':
                await self.transfer.run(job_id, config)
                return
            if config.get('job_kind') == 'strm_sync':
                await self.sync.run(job_id, config, sweep=sweep_now)
                return
            if config.get('cleanup_only'):
                self.sync.invalidate(self.index.account(config))
                async with self.publication_gate:
                    try:
                        await self.retry_cleanup(job_id, config)
                    finally:
                        self.sync.invalidate(self.index.account(config))
                return
            if config.get('job_kind') == 'archive_index':
                snapshot = await self.index.refresh(job_id, config)
                self.update_job(job_id, state='partial' if snapshot['unresolved'] else 'completed',
                    stage='归档索引完成', message=f'已读取 {snapshot["total"]}；身份确认 {snapshot["resolved"]}；待确认 {snapshot["unresolved"]}；延期格式 {snapshot["unsupported"]}。未修改云端文件。')
                return
            if config.get('job_kind') == 'share_organize':
                async with self.publication_gate:
                    try:
                        await share_service.run_organize(self, job_id, config)
                    finally:
                        self.sync.invalidate(self.index.account(config))
                return
            if config.get('repair_only'):
                await self.cloud.directory(config['archive_cid'])
            else:
                # 只有"这次真的要动云端"才让同步清单失效。空扫描每 5 分钟一次，
                # 无条件失效会让清单复用永远命中不了，全量同步因此每次都重扫目录。
                await self.validate(config)
            if not self.job(job_id)['manifest']:
                await self.manifest(job_id, config)
                # 扫描出待处理文件才失效：空扫描不改变云端，清单继续可用。
                if self.store.db.execute('SELECT count(*) FROM organize_items WHERE job_id=?',
                                         (job_id,)).fetchone()[0]:
                    self.sync.invalidate(self.index.account(config))
            report = config.get('scan_filter', {})
            if report.get('scanned') and not self.store.db.execute('SELECT 1 FROM organize_items WHERE job_id=?', (job_id,)).fetchone():
                message = format_skip_message(report) or f"发现 {report.get('attachments', 0)} 个字幕或元数据附属文件，没有参与整理的视频；保留原文件，无需人工核实。"
                if report.get('missing_batch_entries'): message += f"；本批 {report['missing_batch_entries']} 个条目不在原目标目录，已跳过"
                self.note_scan_result(config, True)
                self.update_job(job_id, state='completed', stage='已按格式规则跳过', message=message, manifest=1)
                self.store.event('organize', f'任务 {job_id}：{message}')
                return
            if not self.store.get('playback_base') or not self.store.get('tmdb_key'):
                raise UncertainState('请先配置 TMDB 和播放基础地址，再继续任务。')
            if not config.get('repair_only'):
                await self.index.ensure(job_id, config)
            if config.get('only_file_ids'):
                # 离线下载批次（U06）：只处理本批产物，不顺带处理目录里其他资源。
                allowed = {str(x) for x in config['only_file_ids']}
                self.jobs.prune_manifest(job_id, sorted(allowed))
            all_rows = self.store.db.execute('SELECT * FROM organize_items WHERE job_id=? ORDER BY id', (job_id,)).fetchall()
            rows = [r for r in all_rows if r['state'] not in ('done','pending','skipped','recycled')]
            # Group only clear title/season matches from the fixed scan manifest.
            # Unknown identities remain independent rather than guessing.
            groups, membership = {}, {}
            for item in all_rows:
                source = json.loads(item['original'])
                try:
                    hint = require_single_episode(parse_name(source['name'], source.get('folder')), source['name'])
                    title = normal(hint.get('query') or '')
                    key = ('tv', title, hint.get('year'), hint['season']) if hint['media_type'] == 'tv' and title else ('file', item['id'])
                except NeedsReview:
                    key = ('file', item['id'])
                groups.setdefault(key, []).append(item['id'])
                membership[item['id']] = key
            finished = {r['id'] for r in all_rows if r['state'] in ('done','pending','skipped','recycled')}
            notified_groups = set()

            async def emit_ready(key):
                ids = groups[key]
                if key in notified_groups or not set(ids).issubset(finished):
                    return
                notified_groups.add(key)
                marks = ','.join('?' for _ in ids)
                current = self.store.db.execute(f'SELECT * FROM organize_items WHERE id IN ({marks}) ORDER BY id', ids).fetchall()
                successes = [r for r in current if r['state'] == 'done']
                # Resolve identity again after TMDB: ambiguous same-name shows
                # are never combined into a single notification.
                confirmed = {}
                for r in successes:
                    p = json.loads(r['plan'] or '{}')
                    if not p.get('relative_path'): continue
                    h, d = p.get('hint', {}), p.get('details', {})
                    identity = (h.get('media_type'), d.get('id'), h.get('season')) if key[0] == 'tv' and d.get('id') else ('file', r['id'])
                    confirmed.setdefault(identity, []).append((r, p))
                for subset in confirmed.values():
                    if len(ids) == 1:
                        r, p = subset[0]
                        event_suffix = p['relative_path'] + ':' + str(r['id'])
                    else:
                        event_suffix = 'group:' + ','.join(str(r['id']) for r, _ in subset)
                    other = [r for r in current if r['state'] != 'done']
                    acc = {'details': subset[0][1].get('details', {}),
                           'episodes': [(json.loads(r['original']), p) for r, p in subset],
                           'items': [(r['id'], p) for r,p in subset],
                           'other_items': [(json.loads(r['original']), json.loads(r['plan'] or '{}'), r['state'], r['step']) for r in other] if len(confirmed)==1 else [],
                           'unassigned': len(other) if len(confirmed)!=1 else 0,
                           'unfinished': len(other)}
                    self.queue_show_notice(job_id, event_suffix, acc)
                await asyncio.sleep(0)

            async def on_published(item_id):
                item = self.store.db.execute('SELECT state FROM organize_items WHERE id=?', (item_id,)).fetchone()
                if item and item['state'] in ('done','pending','skipped','recycled','review'):
                    finished.add(item_id)
                    await emit_ready(membership[item_id])

            if not config.get('repair_only'):
                # A process restart destroys the in-memory queue; rebuild it
                # from archived rows rather than trusting stale queue flags.
                self.jobs.clear_publication_flags(job_id)
                queue = asyncio.Queue()
                self.publication_queues[job_id] = queue
                self.publication_workers[job_id] = asyncio.create_task(self.publication_loop(job_id, queue, config, on_published))

            async def drain_publications():
                queue = self.publication_queues.get(job_id)
                if queue is None:
                    return
                join = asyncio.create_task(queue.join())
                worker = self.publication_workers[job_id]
                try:
                    done, _ = await asyncio.wait({join, worker}, return_when=asyncio.FIRST_COMPLETED)
                    if worker in done:
                        await worker
                    await join
                finally:
                    if not join.done(): join.cancel()
                    await asyncio.gather(join, return_exceptions=True)
                for item in self.store.db.execute('SELECT id,state FROM organize_items WHERE job_id=?', (job_id,)):
                    if item['state'] in ('done','pending','skipped','recycled','review'):
                        finished.add(item['id'])
                for key in groups:
                    await emit_ready(key)

            def enqueue_archived(key):
                queue = self.publication_queues.get(job_id)
                if queue is None:
                    return
                marks = ','.join('?' for _ in groups[key])
                archived = self.store.db.execute(
                    f"SELECT id,plan FROM organize_items WHERE id IN ({marks}) AND state='archived'", groups[key]).fetchall()
                by_dest = {}
                for item in archived:
                    if json.loads(item['plan']).get('queued'):
                        continue
                    dest = json.loads(item['plan']).get('dest')
                    if dest:
                        by_dest.setdefault(dest, []).append(item['id'])
                for dest, item_ids in by_dest.items():
                    queue.put_nowait((dest, item_ids))
                    self.store.event('organize', f'任务 {job_id}：归档目录 {dest} 已加入后台 STRM 队列（{len(item_ids)} 个文件）。')

            for key in groups:
                await emit_ready(key)
            # 先把媒体信息并发探好，下面逐条处理时直接命中缓存；预取失败不影响主流程。
            # A bounded window prevents a large directory delaying the first
            # file and limits the number of remote probe subprocesses.
            if rows:
                await self.prefetch_identify(job_id, rows[:2], config)
                await self.prefetch_probes(job_id, rows[:2], config)
            prefetch_window = max(2, min(4, int(config.get('probe_concurrency') or 2)))
            batch_attempted = set()
            queue = self.publication_queues.get(job_id)
            pruned_parent = None
            for position, row in enumerate(rows):
                self.boundary(job_id)
                parent_now = json.loads(row['original']).get('parent_id')
                if pruned_parent is not None and str(parent_now) != str(pruned_parent):
                    await self.prune_source_parent(job_id, pruned_parent, config)
                pruned_parent = parent_now
                # A second version of the same episode must compare with the
                # published winner, not the old index. Flush that group first.
                if queue is not None and membership[row['id']][0] == 'tv':
                    source = json.loads(row['original'])
                    try:
                        hint = require_single_episode(parse_name(source['name'], source.get('folder')), source['name'])
                        for previous in rows[:position]:
                            if membership[previous['id']] != membership[row['id']]:
                                continue
                            old = json.loads(previous['original'])
                            old_hint = require_single_episode(parse_name(old['name'], old.get('folder')), old['name'])
                            if (old_hint.get('episode'), old_hint.get('end_episode')) == (hint.get('episode'), hint.get('end_episode')):
                                enqueue_archived(membership[row['id']])
                                await drain_publications()
                                break
                    except NeedsReview:
                        pass
                if position >= 2 and (position - 2) % prefetch_window == 0:
                    upcoming = rows[position:position + prefetch_window]
                    await self.prefetch_identify(job_id, upcoming, config)
                    await self.prefetch_probes(job_id, upcoming, config)
                if row['id'] not in batch_attempted and hasattr(self.cloud, 'move_many'):
                    source_parent = json.loads(row['original'])['parent_id']
                    upcoming = [r for r in rows if r['id'] not in batch_attempted
                                and membership[r['id']] == membership[row['id']]
                                and json.loads(r['original'])['parent_id'] == source_parent][:50]
                    batch_attempted.update(r['id'] for r in upcoming)
                    await self.prefetch_identify(job_id, upcoming, config)
                    await self.prefetch_probes(job_id, upcoming, config)
                    async with self.publication_gate:
                        account = self.index.account(config)
                        self.sync.invalidate(account)
                        try:
                            await self.prepare_batch_move(job_id, upcoming, config)
                        finally:
                            self.sync.invalidate(account)
                self.boundary(job_id)
                row = self.store.db.execute('SELECT * FROM organize_items WHERE id=?', (row['id'],)).fetchone()
                self.update_job(job_id, stage='处理：' + json.loads(row['original'])['name'])
                try:
                    async with self.publication_gate:
                        self.boundary(job_id)
                        account = self.index.account(config)
                        self.sync.invalidate(account)
                        try:
                            await self.process_item(job_id, row, config)
                        finally:
                            self.sync.invalidate(account)
                except CloudError as exc:
                    if exc.code in ('LOGIN_REQUIRED', 'LOGIN_INVALID', 'RATE_LIMITED', 'TMDB_UNAVAILABLE', 'CLOUD_UNAVAILABLE'):
                        raise
                    source_missing = False
                    current_item = self.store.db.execute('SELECT plan FROM organize_items WHERE id=?', (row['id'],)).fetchone()
                    saved_plan = json.loads(current_item['plan']) if current_item else {}
                    if exc.code == 'FILE_NOT_FOUND' and current_item and (not saved_plan or saved_plan.get('batch_prepared')):
                        try:
                            await self.cloud.file(json.loads(row['original'])['id'])
                        except CloudError as check:
                            if check.code in ('LOGIN_REQUIRED', 'LOGIN_INVALID', 'RATE_LIMITED'):
                                raise
                            source_missing = check.code == 'FILE_NOT_FOUND'
                    async with self.publication_gate:
                        if source_missing:
                            self.update_item(row['id'], state='skipped', step='源文件不可用，已跳过', message='源文件不存在或暂不可访问，已继续处理其他文件。')
                            self.audit(job_id, row['id'], '源文件不存在或暂不可访问，已跳过。')
                        else:
                            await self.failed_item(job_id, row, config, str(exc))
                        self.sync.invalidate(self.index.account(config))
                except (UncertainState, NeedsReview) as exc:
                    async with self.publication_gate:
                        await self.failed_item(job_id, row, config, str(exc))
                        self.sync.invalidate(self.index.account(config))
                except Stopped:
                    raise
                except Exception as exc:
                    async with self.publication_gate:
                        await self.failed_item(job_id, row, config, '处理异常：' + type(exc).__name__)
                        self.sync.invalidate(self.index.account(config))
                if config.get('notify_emby') and not config.get('repair_only'):
                    try:
                        self.emby.enqueue(job_id)
                    except Exception as exc:
                        self.store.event('emby', f'任务 {job_id}：路径通知入队失败（{type(exc).__name__}），收尾时重试。')
                # A movie or the completed scan-manifest group is notified now.
                try:
                    item_state = self.store.db.execute('SELECT state FROM organize_items WHERE id=?', (row['id'],)).fetchone()
                    if item_state and item_state['state'] != 'archived':
                        finished.add(row['id'])
                        await emit_ready(membership[row['id']])
                except Exception as exc:
                    self.store.event('organize', f'任务 {job_id}：单文件通知未完成（{type(exc).__name__}）。')
                key = membership[row['id']]
                remaining = {r['id'] for r in rows[position + 1:]}
                if queue is not None and not remaining.intersection(groups[key]):
                    enqueue_archived(key)
                # 每处理完一个文件看一眼：有转存排队就让路。第一批文件至少处理一个，
                # 进度已逐文件落盘，重入只会扫到剩余文件，绝不重复移动。
                # Transfer and sync are served by independent bounded workers.
            queue = self.publication_queues.get(job_id)
            if queue is not None:
                if queue._unfinished_tasks:
                    self.update_job(job_id, stage='等待后台 STRM 队列完成')
                await drain_publications()
            self.boundary(job_id)
            if rows:
                await self.prune_source_parent(job_id, pruned_parent, config)
            # 收尾阶段（未识别分流 / 空目录清理 / Emby 通知）失败只记警告，不再把整批判成待核查：
            # 视频归档结果已经落盘，收尾失败交给下个周期自然重试，避免一次抖动拖住整条流水线。
            tail = []

            async def tail_stage(name, coro):
                try:
                    return await coro
                except Stopped:
                    raise
                except Exception as exc:
                    detail = str(exc)[:100] or type(exc).__name__
                    tail.append(f'{name}未完成（{detail}）')
                    self.store.event('organize', f'任务 {job_id}：{name}未完成，已保留归档结果（{detail}）。')
                    return None

            self.update_job(job_id, stage='正在处理未识别文件')
            async with self.publication_gate:
                try:
                    await tail_stage('未识别分流', self.unrecognized.flush(job_id, config))
                finally:
                    self.sync.invalidate(self.index.account(config))
            self.boundary(job_id)
            from .untyped_cleanup import recycle_companions
            recycled_companions = 0
            if not config.get('cleanup_disabled'):
                recycled_companions = await tail_stage('无扩展名附属文件清理', recycle_companions(self, job_id, config)) or 0
            if recycled_companions:
                self.update_job(job_id, config=dumps(config))
                self.sync.invalidate(self.index.account(config))
            if config.get('cleanup_disabled') or not config.get('cleanup_empty_enabled', True):
                # 离线批次等场景明确关闭目录清理：不顺带处理目录里的其他资源。
                cleanup = {'removed': 0, 'attachments': 0, 'warning': ''}
            else:
                self.update_job(job_id, stage='正在清理空目录')
                async with self.publication_gate:
                    try:
                        cleanup = await tail_stage('空目录清理', self.cleanup.run(job_id, config)) or {
                            'removed': 0, 'attachments': 0, 'warning': ''}
                    finally:
                        self.sync.invalidate(self.index.account(config))
            self.boundary(job_id)
            counts = dict(self.store.db.execute('SELECT state,count(*) FROM organize_items WHERE job_id=? GROUP BY state', (job_id,)).fetchall())
            plans = [json.loads(r['plan']) for r in self.store.db.execute('SELECT plan FROM organize_items WHERE job_id=?', (job_id,))]
            # 失败标记只对「还没结束」的条目生效：条目重跑成功后 plan 里可能还留着上一次
            # 的 failure / blocks_job（plan 是合并写的），拿全部条目判 blocking 会让任务
            # 永远挂在「部分未完成」。本地导出失败 → 重跑成功就是现场例子。
            # 但 skipped 不能一律放过：身份校验失败、未识别文件转移未完成都记成
            # skipped + failure（文件还留在盘上、没入库），任务不能报「已完成」。
            unfinished = self.store.db.execute(
                "SELECT state,plan FROM organize_items WHERE job_id=? AND state NOT IN ('done','recycled')",
                (job_id,)).fetchall()
            failed_plans = []
            for item in unfinished:
                plan = json.loads(item['plan'])
                if not plan.get('failure'):
                    continue
                # 分流到未识别目录且成功了（条目 state 是 pending）：文件已经在未识别目录里，
                # 是「正常结果」口径，不拉回「部分未完成」。同一个 pending 标记却留在
                # skipped 上的，说明分流没做完，文件还在盘上，必须拦住。
                if plan.get('pending') and item['state'] != 'skipped':
                    continue
                failed_plans.append(plan)
            warnings = (any(p.get('warnings') for p in plans) or bool(cleanup['warning'])
                        or bool(failed_plans) or bool(tail))
            merged_subtitles = any(s.get('ready') for p in plans for s in p.get('wash', {}).get('duplicate', {}).get('subtitles', []))
            if (counts.get('done') or merged_subtitles) and config.get('notify_emby') and not config.get('repair_only'):
                await tail_stage('Emby 通知', self.notify_emby(job_id))
            self.boundary(job_id)
            summary = [f'{label} {counts[key]} 个视频' for key, label in
                       (('done','已整理'),('pending','待处理'),('review','未完成'),('archived','等待输出'),('skipped','已跳过'))
                       if counts.get(key)]
            scan_report = config.get('scan_filter', {})
            if scan_report.get('directories'):
                summary.insert(0, f"扫描 {scan_report['directories']} 个目录 / {scan_report.get('files', 0)} 个文件")
            if scan_report.get('missing_batch_entries'):
                summary.append(f"本批 {config['scan_filter']['missing_batch_entries']} 个条目不在原目标目录，已跳过")
            if cleanup['removed']:
                summary.append(f'已清理 {cleanup["removed"]} 个空目录')
            if cleanup.get('subtitles'):
                summary.append(f'已回收 {cleanup["subtitles"]} 个无法跟随的字幕')
            if recycled_companions:
                summary.append(f'已回收 {recycled_companions} 个无扩展名附属文件')
            if config.get('scan_filter', {}).get('excluded'):
                summary.append(format_skip_message(config['scan_filter']))
            filtered_outputs = sum(bool(p.get('strm_filtered')) for p in plans)
            if filtered_outputs:summary.append(f'按 STRM 生成规则跳过 {filtered_outputs} 个输出')
            summary.extend(tail)
            found = bool(counts.get('done') or counts.get('pending') or counts.get('review')
                         or config.get('scan_filter', {}).get('scanned'))
            self.note_scan_result(config, found)
            # 账本按作用域存：读之前先把作用域钉回本任务（并发泳道可能刚切过）。
            if hasattr(self.cloud, 'use_scope'):
                self.cloud.use_scope(self.current_account(), job_id)
            ledger = self.cloud.ledger_summary() if hasattr(self.cloud, 'ledger_summary') else None
            if ledger:
                # 用最新的任务配置合并，别拿任务开头的 config 覆盖：
                # 空目录清理在过程中会往 config 里写 empty_cleanup（含可重试状态），
                # 覆盖掉就再也点不了「重试空目录清理」。
                latest = json.loads(self.job(job_id)['config'])
                latest['request_ledger'] = ledger
                self.update_job(job_id, config=dumps(latest))
                # 账本只留在操作记录里排查用，不塞进通知正文：
                # 「读 575 次 · 写 40 次 · 合计 615 次」对看通知的人没有意义。
                self.store.event('organize', f'📊 任务 {job_id} 请求账本：{ledger["text"]}')
            # 未识别（review）是正常结果：文件已经放进未识别目录，等人工决定要不要处理。
            # 它不该让整个任务变成「部分未完成」——现场用户抱怨「干预完成功了还要报错」。
            # 「后续步骤未完成」（blocks_job）必须让任务停在「部分未完成」：洗版回收
            # 未确认时新文件已入库、旧文件还留在盘上，报「已完成」等于让人以为洗完了。
            # 单独查库，不走 plans 列表 —— 这条路径的条目不在收尾的 plans 里。
            blocking = (counts.get('pending') or counts.get('archived') or warnings
                        or self._blocks_job(job_id)
                        or self.job(job_id)['notify_state'] in ('failed', 'unknown'))
            self.update_job(job_id, state='partial' if blocking else 'completed',
                            stage='批次结束', message=('；'.join(summary) or self.empty_scan_hint(config)) + '。' + cleanup['warning'])
            # 同一批文件在更早的批次里失败过时，把那些批次的「部分未完成」闭环掉。
            self.close_superseded_batches(job_id)
        except Stopped:
            pass
        except CloudError as exc:
            if exc.code == 'RATE_LIMITED':
                self.halt(job_id, 'needs_review', str(exc), '115 限流冷却，稍后自动重排')
            elif exc.code in ('TMDB_UNAVAILABLE', 'CLOUD_UNAVAILABLE'):
                label = 'TMDB 请求失败' if exc.code == 'TMDB_UNAVAILABLE' else 'CLOUD_UNAVAILABLE'
                try:
                    retries = int(json.loads(self.job(job_id)['config'] or '{}').get('auto_retry_count') or 0)
                except (ValueError, TypeError):
                    retries = 0
                self.halt(job_id, 'needs_review', str(exc), label + '，保留未完成文件并稍后重试',
                          quiet=retries == 0)
            else:
                self.halt(job_id, 'waiting_login' if exc.code in ('LOGIN_REQUIRED', 'LOGIN_INVALID') else 'needs_review', str(exc), '等待登录' if exc.code in ('LOGIN_REQUIRED', 'LOGIN_INVALID') else '等待核查')
        except (UncertainState, NeedsReview) as exc:
            self.halt(job_id, 'needs_review', str(exc))
        finally:
            worker = self.publication_workers.pop(job_id, None)
            self.publication_queues.pop(job_id, None)
            if worker:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)

    def note_scan_result(self, config, found):
        """记录「连续多少次定时扫描没有新内容」，调度器据此放宽间隔。"""
        if config.get('job_kind', 'organize') != 'organize' or not config.get('scheduled'):
            return
        current = int(self.store.meta('scan_idle_streak') or 0)
        streak = 0 if found else current + 1
        if streak != current:
            with self.store.db:
                self.store.db.execute("INSERT OR REPLACE INTO meta VALUES('scan_idle_streak',?)", (str(streak),))
        if streak == 0 and current > 0:
            self.tasks.wake('organize')
        if streak == 3:
            self.store.event('schedule', '连续 3 次没有发现待整理内容，扫描间隔已自动放宽；出现新内容会立即恢复。')

    def blocked_item(self, item_id, step, message):
        """把条目挂成「后续步骤未完成」，并让整个任务停在部分未完成。

        洗版的回收没核实（RECYCLE_REVIEW_REQUIRED 等）会冒到后台发布队列这层：
        新文件已经入库、旧文件还留在盘上。以前这里只把条目置 review，任务收尾照样
        报「已完成」，等于把「没洗成」说成「洗好了」。blocks_job 是给收尾看的强阻塞键，
        和「识别不了、放进未识别目录」的正常结果区分开。
        """
        row = self.store.db.execute('SELECT plan FROM organize_items WHERE id=?', (item_id,)).fetchone()
        try:
            plan = json.loads(row['plan'] or '{}') if row else {}
        except ValueError:
            plan = {}
        plan = {**(plan or {}), 'failure': True, 'blocks_job': True}
        self.update_item(item_id, state='review', plan=plan, step=step, message=message)

    def _blocks_job(self, job_id):
        """还有「后续步骤未完成」的条目吗？只认 blocks_job，不碰未识别的正常结果。"""
        for row in self.store.db.execute(
                "SELECT plan FROM organize_items WHERE job_id=? AND state IN ('review','skipped')", (job_id,)):
            try:
                if (json.loads(row['plan'] or '{}') or {}).get('blocks_job'):
                    return True
            except ValueError:
                continue
        return False

    def settle_jobs(self):
        """条目全部终态、又没有待发通知的任务，自动结项，不再挂 partial。

        现场 10 个历史任务的待办条目都是 0，却因为「1 个目录读取失败，已保留待下次清理」
        这类一句话永远显示部分未完成。partial 应该是「还有活要干」，不是「当时出过岔子」。
        """
        settled = []
        rows = self.store.db.execute(
            "SELECT id,message,notify_state FROM organize_jobs WHERE state IN ('partial','needs_review')").fetchall()
        for row in rows:
            left = self.store.db.execute(
                "SELECT COUNT(*) FROM organize_items WHERE job_id=?"
                " AND state NOT IN ('done','skipped','recycled','review')",
                (row['id'],)).fetchone()[0]
            if left:
                continue
            if (row['notify_state'] or '') in ('failed', 'unknown'):
                continue
            message = (row['message'] or '').rstrip('。')
            message = (message + '。' if message else '') + '（待办条目已全部结束，任务自动结项。）'
            # notify_state 一并置终态：结项不是「刚入库」，绝不能再播一遍历史通知。
            self.update_job(row['id'], state='completed', stage='已自动结项', message=message,
                            notify_state='not_needed')
            settled.append(row['id'])
        if settled:
            self.store.event('organize', '%d 个任务的待办条目已全部结束，自动结项：%s。'
                             % (len(settled), '、'.join('#%d' % x for x in settled[:8])))
        return settled

    async def reconcile_ledger(self, now=None, limit=150):
        """账本对账：文件没了 / 已入库 / 已回到待整理，账本自己闭环。

        只读 115 文件语义接口 + 只改账本状态：不移动文件、不删除文件、不碰扫描范围。
        现场教训：用户在 115 里手动删除或移回待整理之后，账本一直挂着，还要人来点完结。
        判定只用 file_id（不看名字），所以手动重命名也不影响对账。
        """
        now = time.time() if now is None else now
        rows = self.store.db.execute(
            "SELECT i.id,i.job_id,i.file_id FROM organize_items i"
            " JOIN organize_jobs j ON j.id=i.job_id"
            " WHERE i.state IN ('review','pending')"
            # 任务已结束的立刻对账：用户在未识别目录里删掉文件后，账本马上跟着闭环，
            # 不用等满 24 小时；还在跑的任务保持原窗口，避免和正在处理的文件抢。
            " AND (j.state IN ('completed','cancelled') OR j.updated<?)"
            " ORDER BY i.id LIMIT ?",
            (now - 86400, limit)).fetchall()
        if not rows:
            return 0
        archived = {r[0] for r in self.store.db.execute('SELECT file_id FROM organized_files')}
        live = {r[0] for r in self.store.db.execute('SELECT file_id FROM share_strms WHERE revoked=0')}
        revoked = {r[0] for r in self.store.db.execute('SELECT file_id FROM share_strms WHERE revoked=1')}
        config = self.store.get('organize_config', {}) or {}
        roots = {str(c) for c in (config.get('source_cids') or [])}
        closed, fails, touched = 0, 0, set()
        for row in rows:
            fid = str(row['file_id'] or '')
            if not fid:
                continue
            reason = None
            if fid in archived:
                reason = '文件已归档'
            elif fid in live:
                reason = '文件已入库'
            elif fid in revoked:
                reason = '分享已失效'
            else:
                try:
                    found = await self.cloud.file(fid)
                except CloudError as exc:
                    if exc.code == 'FILE_NOT_FOUND':
                        reason = '源文件已不在网盘'
                    else:
                        # 限流/网络异常不是「文件没了」，一律不动，下次再对。
                        fails += 1
                        if fails >= 3:
                            break
                        continue
                except Exception:
                    fails += 1
                    if fails >= 3:
                        break
                    continue
                else:
                    parent = str(found.get('parent_id') or '')
                    if parent == '0':
                        reason = '源文件已不在网盘'
                    elif parent in roots:
                        reason = '已人工移回待整理，旧账本闭环'
                    else:
                        continue
            note = reason + '，账本自动闭环。'
            self.jobs.skip_reconciled(row['id'], reason, note)
            closed += 1
            touched.add(row['job_id'])
        if closed:
            self.store.event('organize', '账本自动对账闭环 %d 条（文件已归档/已入库/分享失效/已不在网盘/已移回待整理）。' % closed)
            self.settle_jobs()
        return closed

    def close_recovered_items(self):
        """同一文件名在后续批次已经成功归档的，旧批次的未识别/待处理自动闭环。

        现场 231 条历史积压里有 99 条属于这种情况（未识别目录里的 08.mp4 后来又整理成功），
        留着没有任何人工价值，只会让任务中心一直挂着「部分未完成」。只按「更晚的任务、
        完全相同的原始文件名」判定，不做模糊匹配。
        """
        rows = self.store.db.execute(
            "SELECT i.id,i.job_id,i.original FROM organize_items i"
            " WHERE i.state IN ('review','pending')").fetchall()
        if not rows:
            return 0
        closed = 0
        touched = set()
        for row in rows:
            try:
                name = (json.loads(row['original'] or '{}') or {}).get('name', '')
            except (ValueError, TypeError):
                continue
            if not name:
                continue
            hit = self.store.db.execute(
                "SELECT 1 FROM organize_items i2 WHERE i2.state='done' AND i2.job_id>?"
                " AND json_extract(i2.original,'$.name')=? LIMIT 1", (row['job_id'], name)).fetchone()
            if not hit:
                continue
            # 状态也要落到终态，否则任务中心的「待处理/未识别」计数不会减少。
            self.jobs.skip_recovered(row['id'])
            closed += 1
            touched.add(row['job_id'])
        if not closed:
            return 0
        # 只闭环本次真的补上了条目的任务，别去动「partial 但原因无关」的批次。
        for old in self.store.db.execute("SELECT * FROM organize_jobs WHERE state='partial'").fetchall():
            if old['id'] not in touched:
                continue
            left = self.store.db.execute(
                "SELECT count(*) FROM organize_items WHERE job_id=? AND state NOT IN ('done','skipped','recycled')",
                (old['id'],)).fetchone()[0]
            if left or (old['notify_state'] or '') in ('failed', 'unknown'):
                continue
            self.update_job(old['id'], state='completed', stage='已由后续批次补齐',
                            message='未完成条目已由后续批次补齐，本任务闭环。')
        self.store.event('organize', f'自动闭环 {closed} 条已由后续批次补齐的未完成条目。')
        return closed

    def close_superseded_batches(self, job_id):
        """仅在同账号后续整理真实补齐时标记旧失败；保留原始原因。"""
        current = self.job(job_id)
        if not current:
            return
        try:
            current_config = json.loads(current['config'] or '{}')
        except (ValueError, TypeError):
            return
        account = current_config.get('account_id')
        if not account or current_config.get('job_kind', 'organize') != 'organize':
            return
        done_ids = [row[0] for row in self.store.db.execute(
            "SELECT file_id FROM organize_items WHERE job_id=? AND state='done'", (job_id,))]
        if not done_ids:
            return
        marks = ','.join('?' * len(done_ids))
        olds = self.store.db.execute(
            'SELECT DISTINCT j.id,j.config FROM organize_jobs j JOIN organize_items i ON i.job_id=j.id '
            'WHERE j.id<? AND j.state=? AND i.file_id IN (%s) '
            "AND (i.state IN ('pending','review') OR (i.state='skipped' AND i.step LIKE '%%未识别%%'))" % marks,
            (job_id, 'partial', *done_ids)).fetchall()
        for old in olds:
            try:
                old_config = json.loads(old['config'] or '{}')
            except (ValueError, TypeError):
                continue
            if old_config.get('account_id') != account or old_config.get('job_kind', 'organize') != 'organize':
                continue
            old_id = old['id']
            self.jobs.note_superseded(
                old_id, list(done_ids), '已由任务 %d 补齐' % job_id, '后续批次已成功处理同一文件。')
            left = self.store.db.execute(
                "SELECT count(*) FROM organize_items WHERE job_id=? AND "
                "(state NOT IN ('done','recycled','review') AND NOT (state IN ('skipped','pending') AND step LIKE '已由任务 % 补齐'))",
                (old_id,)).fetchone()[0]
            if left:
                continue
            previous = self.job(old_id)
            if not previous or previous['state'] != 'partial':
                continue
            note = '已由任务 %d 全部补齐' % job_id
            message = previous['message'] or ''
            if note in message:
                continue
            # 旧批次里的「待处理 N 个视频」是当时的计数：既然已被后续批次补齐，
            # 再留着只会让人以为还没做完（现场 job 647 就是这样显示「待处理 7 个视频」）。
            message = re.sub(r'[；，]?待处理 \d+ 个视频', '', message).strip('；，')
            self.update_job(old_id, state='completed',
                            message=(message.rstrip('。') + '。' if message else '') + note + '。')
            self.store.event('organize', '任务 %d：本批已完成，旧任务 %d 的失败记录随之闭环。'
                             % (job_id, old_id))

    async def notify_emby(self, job_id):
        self.emby.enqueue(job_id)
