"""Persistent FIFO enqueue and interval timers; no replay of missed intervals."""
import json
import hashlib
import re
import time
from .quality import public_policy
from .organize_options import library_root, options
from .strm_options import effective
from datetime import datetime
from zoneinfo import ZoneInfo
from croniter import croniter


class Tasks:
    def __init__(self, engine):
        self.engine, self.store = engine, engine.store

    def account(self):
        match = re.search(r'(?:^|;\s*)UID=(\d+)', self.store.get('cookie'))
        if not match: raise ValueError('请先登录并核实 115 账号。')
        return match[1]

    def watermark_since(self, roots=None):
        """增量起点：指定目录时取这些目录里最早的时间，没跑过就退回全局时间。

        同步目录里除了本程序整理的资源，还会有手动放进去或别的工具落进来的文件：
        它们只能靠扫目录发现，所以「增量」是枚举目录 + 只处理变化的文件。
        """
        watermark = self.store.meta_json('sync_watermark', {}) or {}
        if roots:
            per_root = watermark.get('roots') or {}
            values = [float(per_root.get(str(cid)) or 0) for cid in roots]
            if values and all(value > 0 for value in values):
                return min(values)
        return float(watermark.get('at') or 0)

    def config(self, kind, roots=None, incremental=False):
        account = self.account()
        config = dict(self.store.get('organize_config', {}))
        if kind == 'organize':
            config = {**config, **options(config)}
            if not config.get('source_cids') or not self.store.get('tmdb_key'):
                raise ValueError('请先保存整理目录并配置 TMDB。')
            config['upgrade_policy'] = public_policy(self.store.get('upgrade_policy'))
            config['strm_options'] = effective(self.store)
        elif kind == 'strm_sync':
            sync = self.store.get('sync_config', {})
            # 库根名必须和整理写出的那一套**完全一致**（0.8.30 起都是镜像 115 目录名）：
            # 漏掉它会让同步回落到兜底名字，结果整理写一套树、同步写另一套，Emby 里两份。
            config = {'archive_cid': config.get('archive_cid', ''),
                      'library_dir_name': library_root(config),
                      'directory_labels': config.get('directory_labels', {}),
                      **sync, **effective(self.store),
                      'full_scan': True,  # 发现外部新增必须枚举目录：增量省的是逐文件核对。
                      'source_cids': roots if roots is not None else sync.get('source_cids', [])}
            if not config['source_cids']: raise ValueError('请先选择 STRM 同步目录。')
            if incremental:
                # 定时同步用增量：枚举目录照做（否则发现不了外部放进去的文件），
                # 已登记且没变化的文件跳过逐项核对与重写。
                config.update({'incremental_since': self.watermark_since(config['source_cids']),
                               'trust_scan': True, 'mode': 'incremental'})
        else: raise ValueError('任务类型无效。')
        if not self.store.get('playback_base'): raise ValueError('请先配置 StrimKeep 播放基础地址。')
        return {**config, 'job_kind': kind, 'account_id': account, 'playback_base': self.store.get('playback_base')}

    def sync_profile_config(self, profile):
        roots = [profile['source_cid']]
        incremental = profile['mode'] == 'incremental'
        config = self.config('strm_sync', roots, incremental=incremental)
        config['sync_profile_id'] = profile['id']
        config['sync_profile_name'] = profile['name']
        config['directory_labels'] = {**config.get('directory_labels', {}), roots[0]: profile['source_label']}
        if not incremental:
            config.update({'full_scan': True, '_force_full': True, 'trust_scan': False, 'mode': 'full'})
        return config

    @staticmethod
    def sync_profile_next(profile, now):
        if profile['frequency'] == 'interval':
            return now + int(profile.get('interval_minutes', 60)) * 60
        if profile['frequency'] == 'cron':
            expression = profile.get('cron', '0 3 * * *')
        else:
            hour, minute = (int(part) for part in profile['time'].split(':'))
            day = '*' if profile['frequency'] == 'daily' else str(profile['weekday'])
            expression = f'{minute} {hour} * * {day}'
        return croniter(expression, datetime.fromtimestamp(now, ZoneInfo(profile.get('timezone', 'Asia/Shanghai')))).get_next(datetime).timestamp()

    def migrate_legacy_sync(self):
        """Convert the old sync timer into visible per-directory tasks, preserving its schedule."""
        settings = self.store.get('schedule_config', {})
        if not settings.get('sync_enabled'):
            return
        roots = settings.get('sync_source_cids') or []
        old = self.store.db.execute("SELECT next_due FROM task_schedules WHERE kind='strm_sync'").fetchone()
        profiles = list(self.store.get('sync_profiles', []))
        labels = settings.get('directory_labels') or {}
        added = []
        for cid in roots:
            ident = 'legacy' + hashlib.sha256(str(cid).encode()).hexdigest()[:16]
            if any(p['id'] == ident for p in profiles):
                continue
            def same_schedule(profile):
                if (not profile.get('enabled') or profile.get('mode') != 'incremental'
                        or profile.get('source_cid') != str(cid)
                        or profile.get('timezone', 'Asia/Shanghai') != settings.get('timezone', 'Asia/Shanghai')):
                    return False
                if settings.get('sync_mode') != 'cron':
                    return profile.get('frequency') == 'interval' and int(profile.get('interval_minutes', 60)) == int(settings.get('sync_interval_minutes', 60))
                expression = ' '.join(settings.get('sync_cron', '0 3 * * *').split())
                if profile.get('frequency') == 'cron':
                    return ' '.join(profile.get('cron', '').split()) == expression
                if profile.get('frequency') in ('daily', 'weekly'):
                    hour, minute = (int(value) for value in profile.get('time', '03:00').split(':'))
                    day = '*' if profile['frequency'] == 'daily' else str(profile.get('weekday', 1))
                    return expression == f'{minute} {hour} * * {day}'
                return False
            if any(same_schedule(p) for p in profiles):
                continue
            label = labels.get(str(cid)) or str(cid)
            item = {'id':ident, 'name':'原定时 · ' + label[-60:], 'source_cid':str(cid),
                    'source_label':label, 'mode':'incremental', 'enabled':True,
                    'frequency':'cron' if settings.get('sync_mode') == 'cron' else 'interval',
                    'cron':settings.get('sync_cron', '0 3 * * *'),
                    'interval_minutes':settings.get('sync_interval_minutes', 60),
                    'timezone':settings.get('timezone', 'Asia/Shanghai'),
                    'weekday':1, 'time':'03:00'}
            profiles.append(item); added.append(item)
        self.store.set_many({'sync_profiles':profiles, 'schedule_config':{**settings, 'sync_enabled':False}})
        with self.store.db:
            if old:
                for item in added:
                    self.store.db.execute('INSERT OR IGNORE INTO task_schedules(kind,next_due) VALUES(?,?)',
                                          ('sync_profile:' + item['id'], old['next_due']))
            self.store.db.execute("DELETE FROM task_schedules WHERE kind='strm_sync'")
        if added:
            self.store.event('schedule', f'已将原定时同步迁移为 {len(added)} 个独立任务，原时间和目录保持不变。')
        elif not roots:
            self.store.event('schedule', '原定时同步没有选择目录，已关闭。')

    def tick_sync_profiles(self, now):
        for profile in self.store.get('sync_profiles', []):
            if not profile.get('enabled'):
                continue
            kind = 'sync_profile:' + profile['id']
            row = self.store.db.execute('SELECT * FROM task_schedules WHERE kind=?', (kind,)).fetchone()
            due = self.sync_profile_next(profile, now)
            if not row:
                with self.store.db:
                    self.store.db.execute('INSERT OR IGNORE INTO task_schedules(kind,next_due) VALUES(?,?)', (kind, due))
                continue
            if row['next_due'] > now:
                continue
            try:
                config = self.sync_profile_config(profile)
                jid, merged = self.enqueue(config, scheduled=True, now=now)
            except ValueError:
                jid, merged = None, False
                self.store.event('schedule', '同步任务配置或登录不可用，本次未入队：' + profile['name'])
            with self.store.db:
                self.store.db.execute('UPDATE task_schedules SET next_due=?,last_run=?,last_job_id=?,merged=merged+? WHERE kind=?',
                                      (due, now, jid, int(merged), kind))

    def enqueue(self, config, scheduled=False, now=None):
        now = time.time() if now is None else now
        if config.get('job_kind', 'organize') == 'organize' and not scheduled:
            comparable = {k:v for k,v in config.items() if k != 'scheduled'}
            for row in self.store.db.execute("SELECT id,config FROM organize_jobs WHERE state='queued' ORDER BY id"):
                old = json.loads(row['config'])
                if old.get('job_kind', 'organize') != 'organize':
                    continue
                if {k:v for k,v in old.items() if k != 'scheduled'} == comparable:
                    if old.get('scheduled'):
                        old['scheduled'] = False
                        self.store.jobs.set_config(row['id'], json.dumps(old, ensure_ascii=False))
                    self.store.event('queue', f'相同的整理任务 {row["id"]} 已在等待，合并本次请求。')
                    return row['id'], True
        # Coalesce identical waiting sync requests, not one already running:
        # files may have changed since the in-progress snapshot started.
        if config.get('job_kind') == 'strm_sync':
            comparable = {k:v for k,v in config.items() if k != 'scheduled'}
            for row in self.store.db.execute("SELECT id,config FROM organize_jobs WHERE state='queued' ORDER BY id"):
                old = json.loads(row['config'])
                if {k:v for k,v in old.items() if k != 'scheduled'} == comparable:
                    return row['id'], True
        if scheduled:
            # waiting_login 也算「已有一份在等」：登录失效期间每轮都入队，
            # 恢复后就会被重放成一批重复的空扫描。
            for row in self.store.db.execute("SELECT id,config FROM organize_jobs WHERE state IN ('queued','waiting_login') ORDER BY id"):
                old = json.loads(row['config'])
                if old.get('scheduled') and (old.get('job_kind','organize'),old.get('account_id'),sorted(old.get('source_cids',[])),old.get('sync_profile_id'),old.get('mode')) == (config['job_kind'],config['account_id'],sorted(config.get('source_cids',[])),config.get('sync_profile_id'),config.get('mode')):
                    return row['id'], True
        config = {**config, 'scheduled': scheduled}
        job_id = self.store.jobs.create(config)
        # 定时扫描每 15 分钟一轮，只记「结果」不记「入队」；手动任务照常留痕。
        if not scheduled:
            self.store.event('queue', f'任务 {job_id} 已加入队列。')
        return job_id, False

    def wake(self, kind):
        """有新内容时把下一轮提前，避免退避把新文件压住最多一小时。"""
        settings = self.store.get('schedule_config', {})
        prefix = 'scan' if kind == 'organize' else 'sync'
        if not settings.get(prefix + '_enabled') or settings.get(prefix + '_mode') == 'cron':
            return
        base = settings.get(prefix + '_interval_minutes', 60) * 60
        with self.store.db:
            self.store.db.execute('UPDATE task_schedules SET next_due=MIN(next_due,?) WHERE kind=?', (time.time() + base, kind))

    def tick(self, now=None):
        now = time.time() if now is None else now
        self.migrate_legacy_sync()
        settings = self.store.get('schedule_config', {})
        for kind, prefix in (('organize','scan'),('strm_sync','sync')):
            if not settings.get(prefix + '_enabled'): continue
            interval = settings.get(prefix + '_interval_minutes', 60) * 60
            # 连续多轮没有新内容就放宽间隔（最长 15 分钟）：空扫没有收益，却在
            # 消耗 115 的串行配额。上限压住，新内容最坏也只等 15 分钟就被发现；
            # 一旦有内容，note_scan_result 会立刻把下一轮拉回基础间隔。
            if kind == 'organize' and settings.get(prefix + '_mode') != 'cron':
                streak = int(self.store.meta('scan_idle_streak') or 0)
                if streak >= 2:
                    interval = max(interval, min(interval * 2 ** (streak - 1), 900))
            def next_due():
                if settings.get(prefix+'_mode')=='cron':
                    zone=ZoneInfo(settings.get('timezone','Asia/Shanghai'))
                    return croniter(settings.get(prefix+'_cron','0 3 * * *'),datetime.fromtimestamp(now,zone)).get_next(datetime).timestamp()
                return now+interval
            row = self.store.db.execute('SELECT * FROM task_schedules WHERE kind=?', (kind,)).fetchone()
            if not row:
                with self.store.db: self.store.db.execute('INSERT INTO task_schedules(kind,next_due) VALUES(?,?)', (kind,next_due()))
                continue
            if kind == 'organize' and settings.get('scan_mode') != 'cron':
                # 等待本轮结束后再计时，不能在整理进行中累积下一次定时扫描。
                active = self.store.db.execute(
                    "SELECT 1 FROM organize_jobs WHERE state IN ('queued','running','waiting_login') "
                    "AND COALESCE(json_extract(config,'$.job_kind'),'organize')='organize' LIMIT 1"
                ).fetchone()
                if active:
                    continue
                finished = self.store.db.execute(
                    "SELECT MAX(updated) FROM organize_jobs WHERE state IN ('completed','partial','needs_review','cancelled') "
                    "AND COALESCE(json_extract(config,'$.job_kind'),'organize')='organize'"
                ).fetchone()[0]
                if finished and finished + interval > row['next_due']:
                    due = finished + interval
                    with self.store.db:
                        self.store.db.execute('UPDATE task_schedules SET next_due=? WHERE kind=?', (due, kind))
                    if due > now:
                        continue
            if row['next_due'] > now: continue
            try:
                config = self.config(kind, settings.get('sync_source_cids', []) if kind=='strm_sync' else None,
                                     incremental=(kind == 'strm_sync'))
                jid, merged = self.enqueue(config, scheduled=True, now=now)
            except ValueError:
                jid, merged = None, False
                self.store.event('schedule', '定时任务配置或登录不可用，本周期未入队；请检查系统设置。')
            with self.store.db:
                self.store.db.execute('UPDATE task_schedules SET next_due=?,last_run=?,last_job_id=?,merged=merged+? WHERE kind=?', (next_due(),now,jid,int(merged),kind))
        self.tick_sync_profiles(now)
