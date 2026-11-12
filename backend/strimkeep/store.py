"""Small persistent store. Credentials and sessions never leave the server in clear text."""
import asyncio
import hashlib
import hmac
import contextlib
import json
import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path

from cryptography.fernet import Fernet

# 启动 schema 的版本号：加表/改表时 +1。Store 初始化会执行（幂等的）建表脚本，
# 然后把库抬到这个版本。仍兼容 1：老库只会被补齐缺表并打上新版本号，不动既有数据。
SCHEMA_VERSION = '2'

SESSION_SECONDS = 30 * 24 * 60 * 60

# 任务的「结束」状态：进入这些状态后不会再有任何条目被处理。
# 0.8.19 起在 Store 里收口（settle_terminal_job / reconcile_terminal_items），
# 避免「任务结束了、条目还停在 discovered」的行永远留在库里污染统计。
TERMINAL_JOB_STATES = ('completed', 'partial', 'cancelled')
# 转存任务的分享码/提取码在 settings 里的键前缀（值经 Fernet 加密）。
TRANSFER_SECRET = 'transfer-secret:'
TRANSFER_UPDATE = 'transfer-update:'

# 0.9.55：事件表按 kind 单独封顶。全局 2 万 FIFO 时，高频低价值事件稳定占掉一半名额
# （现场 organize 7343 + progress 3536 + deep_delete 1949），把 emby/system/error 这些
# 真正要看的告警冲掉——0.9.47 之前的媒体信息空窗就是这么被埋的。明细不会丢：
# `Store.event()` 同时写 /data/logs 的按天日志文件，那里保留全部。
EVENT_KIND_CAPS = (('progress', 800), ('organize', 4000), ('deep_delete', 1200))



def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def password_hash(password: str, salt: bytes) -> str:
    return hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1).hex()


class Store:
    def __init__(self, root: Path, username: str, password: str):
        self.root = root
        for name in ('config', 'data', 'logs'):
            (root / name).mkdir(parents=True, exist_ok=True)
        self.path = root / 'data/state.db'
        # 事件除了进 events 表，再落一份按天的文件日志：容器 stdout 之外，
        # 用户要能下载、能留存（data/logs/strimkeep-YYYYMMDD.log，保留 7 天）。
        self.log_dir = root / 'logs'
        self._log_day = ''
        self._log_handle = None
        self._log_lock = threading.Lock()
        # 0.8.21：一线程一连接。
        # 事件循环线程与 asyncio.to_thread 的工作线程各用各的 sqlite 连接——同一个连接
        # 跨线程并发使用会在 C 层崩掉（0.8.20 的 SIGSEGV 就是这么来的）。`self.db` 变成
        # 属性，按调用线程懒创建连接；因此任何现有代码在 to_thread 里跑都自动安全。
        self._connections = []
        self._connections_lock = threading.Lock()
        self._local = threading.local()
        self._override = None
        self._local.conn = self._connect()
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone():
            previous = self.meta('schema_version')
            if (previous not in ('', '1', SCHEMA_VERSION)
                    or self.meta('organize_schema_version') not in ('', '1')
                    or self.meta('archive_schema_version') not in ('', '1')):
                raise RuntimeError('数据库版本不兼容，已停止写入。')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions(hash TEXT PRIMARY KEY,csrf TEXT NOT NULL,expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS strm_changes(job_id INTEGER NOT NULL, action TEXT NOT NULL, path TEXT NOT NULL,
          PRIMARY KEY(job_id,action,path));
        CREATE TABLE IF NOT EXISTS strm_change_items(job_id INTEGER NOT NULL, item_id INTEGER NOT NULL, action TEXT NOT NULL, path TEXT NOT NULL,
          PRIMARY KEY(job_id,item_id,action,path));
        CREATE TABLE IF NOT EXISTS strm_reported_items(job_id INTEGER NOT NULL, item_id INTEGER NOT NULL, PRIMARY KEY(job_id,item_id));
        CREATE TABLE IF NOT EXISTS strm_group_outbox(job_id INTEGER NOT NULL, group_key TEXT NOT NULL, items TEXT NOT NULL, PRIMARY KEY(job_id,group_key));
        CREATE TABLE IF NOT EXISTS playbacks(
          id TEXT PRIMARY KEY,file_id TEXT NOT NULL,pickcode TEXT NOT NULL,name TEXT NOT NULL,
          relative_path TEXT NOT NULL,token_hash TEXT NOT NULL,revoked INTEGER NOT NULL DEFAULT 0,created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS events(
          id INTEGER PRIMARY KEY AUTOINCREMENT,at REAL NOT NULL,kind TEXT NOT NULL,message TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS organize_jobs(
          id INTEGER PRIMARY KEY AUTOINCREMENT,state TEXT NOT NULL,config TEXT NOT NULL,
          created REAL NOT NULL,updated REAL NOT NULL,stage TEXT NOT NULL DEFAULT '排队',
          message TEXT NOT NULL DEFAULT '',manifest INTEGER NOT NULL DEFAULT 0,
          notify_state TEXT NOT NULL DEFAULT 'not_started');
        CREATE TABLE IF NOT EXISTS organize_items(
          id INTEGER PRIMARY KEY AUTOINCREMENT,job_id INTEGER NOT NULL,file_id TEXT NOT NULL,
          original TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'discovered',plan TEXT NOT NULL DEFAULT '{}',
          step TEXT NOT NULL DEFAULT '发现文件',message TEXT NOT NULL DEFAULT '',
          UNIQUE(job_id,file_id));
        -- 0.9.55：organize_items 的唯一键是 (job_id,file_id)，所以 `WHERE file_id=?` 用不上
        -- 索引；生产库实测 25.36ms/次（补索引后 0.01ms，2351×）。分享产线在 per-file 循环里
        -- 查它，直接把事件循环堵成秒级（现场「事件循环被阻塞」中位 1.28 秒）。
        CREATE INDEX IF NOT EXISTS organize_items_file ON organize_items(file_id);
        -- organize_jobs 原来一个索引都没有：`WHERE state=?` 实测 2.23ms → 0.01ms（200×）。
        CREATE INDEX IF NOT EXISTS organize_jobs_state ON organize_jobs(state);
        CREATE TABLE IF NOT EXISTS organized_files(
          file_id TEXT PRIMARY KEY,job_id INTEGER NOT NULL,item_id INTEGER NOT NULL,
          playback_id TEXT NOT NULL,relative_path TEXT NOT NULL,metadata TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS archive_snapshots(
          id INTEGER PRIMARY KEY AUTOINCREMENT,job_id INTEGER NOT NULL,
          account_id TEXT NOT NULL,root_id TEXT NOT NULL,state TEXT NOT NULL,
          created REAL NOT NULL,updated REAL NOT NULL,total INTEGER NOT NULL DEFAULT 0,
          resolved INTEGER NOT NULL DEFAULT 0,unresolved INTEGER NOT NULL DEFAULT 0,
          unsupported INTEGER NOT NULL DEFAULT 0);
        CREATE INDEX IF NOT EXISTS archive_snapshot_scope ON archive_snapshots(account_id,root_id,state,id);
        CREATE TABLE IF NOT EXISTS archive_entries(
          snapshot_id INTEGER NOT NULL,file_id TEXT NOT NULL,parent_id TEXT NOT NULL,
          name TEXT NOT NULL,relative_path TEXT NOT NULL,size INTEGER NOT NULL,sha1 TEXT NOT NULL,
          state TEXT NOT NULL,message TEXT NOT NULL,media_type TEXT NOT NULL,tmdb_id INTEGER,
          season INTEGER,episode INTEGER,end_episode INTEGER,metadata TEXT NOT NULL,
          PRIMARY KEY(snapshot_id,file_id));
        CREATE INDEX IF NOT EXISTS archive_identity ON archive_entries(snapshot_id,media_type,tmdb_id,season,episode);
        CREATE TABLE IF NOT EXISTS media_cache(
          account_id TEXT NOT NULL,file_id TEXT NOT NULL,sha1 TEXT NOT NULL,size INTEGER NOT NULL,
          media TEXT NOT NULL,raw_name TEXT NOT NULL,updated REAL NOT NULL,PRIMARY KEY(account_id,file_id));
        CREATE TABLE IF NOT EXISTS media_by_sha(
          account_id TEXT NOT NULL,sha1 TEXT NOT NULL,size INTEGER NOT NULL,media TEXT NOT NULL,
          raw_name TEXT NOT NULL,at REAL NOT NULL,PRIMARY KEY(account_id,sha1,size));
        CREATE TABLE IF NOT EXISTS tmdb_cache(
          key TEXT PRIMARY KEY,body TEXT NOT NULL,updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS directory_cache(
          account_id TEXT NOT NULL,id TEXT NOT NULL,parent_id TEXT NOT NULL,name TEXT NOT NULL,
          PRIMARY KEY(account_id,id));
        CREATE INDEX IF NOT EXISTS directory_by_name ON directory_cache(account_id,parent_id,name);
        CREATE TABLE IF NOT EXISTS output_registry(
          account_id TEXT NOT NULL,file_id TEXT NOT NULL,source TEXT NOT NULL,trail TEXT NOT NULL,
          playback_id TEXT NOT NULL,token_cipher TEXT NOT NULL,relative_path TEXT NOT NULL,
          files TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1,updated REAL NOT NULL,
          PRIMARY KEY(account_id,file_id));
        CREATE TABLE IF NOT EXISTS owned_outputs(
          relative_path TEXT PRIMARY KEY,account_id TEXT NOT NULL,file_id TEXT NOT NULL,digest TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS deep_delete_queue(
          id INTEGER PRIMARY KEY AUTOINCREMENT,batch TEXT NOT NULL DEFAULT '',
          kind TEXT NOT NULL,file_id TEXT NOT NULL DEFAULT '',playback_id TEXT NOT NULL DEFAULT '',
          relative_path TEXT NOT NULL DEFAULT '',share_id TEXT NOT NULL DEFAULT '',source TEXT NOT NULL DEFAULT '',
          dry_run INTEGER NOT NULL DEFAULT 0,state TEXT NOT NULL DEFAULT 'queued',
          message TEXT NOT NULL DEFAULT '',created REAL NOT NULL,updated REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS deep_delete_pending ON deep_delete_queue(state,id);
        -- 以下 12 张表原来分散在 share_service / channel_monitor / offline_api /
        -- native_media_info / emby 里各自建：启动时不全建就会在用到时 500
        -- （output_stats 就出过这种事）。统一收到这里，模块里的原 DDL 暂时保留，
        -- 等回归通过后再逐模块删除。
        CREATE TABLE IF NOT EXISTS share_strms(
          id TEXT PRIMARY KEY, share_code TEXT NOT NULL, receive_code TEXT NOT NULL DEFAULT '',
          file_id TEXT NOT NULL, pickcode TEXT NOT NULL DEFAULT '', name TEXT NOT NULL,
          strm_path TEXT NOT NULL, token_hash TEXT NOT NULL,
          revoked INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS share_watchlist(
          share_code TEXT NOT NULL, receive_code TEXT NOT NULL DEFAULT '',
          last_checked REAL, created REAL NOT NULL, PRIMARY KEY(share_code));
        CREATE TABLE IF NOT EXISTS share_revocations(
          share_code TEXT NOT NULL, file_id TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
          created REAL NOT NULL, PRIMARY KEY(share_code, file_id));
        CREATE TABLE IF NOT EXISTS share_wash_intents(
          id TEXT PRIMARY KEY, job_id INTEGER NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS channel_state(
          name TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '',
          last_message_id INTEGER, checked REAL NOT NULL DEFAULT 0,
          state TEXT NOT NULL DEFAULT 'new', error TEXT NOT NULL DEFAULT '',
          posts INTEGER NOT NULL DEFAULT 0, added REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS channel_shares(
          share_code TEXT PRIMARY KEY, name TEXT NOT NULL,
          message_id INTEGER NOT NULL, job_id INTEGER, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS channel_posts(
          name TEXT NOT NULL, message_id INTEGER NOT NULL,
          created REAL NOT NULL, PRIMARY KEY(name, message_id));
        CREATE TABLE IF NOT EXISTS channel_backfill(
          name TEXT PRIMARY KEY, cursor INTEGER NOT NULL, before_id INTEGER NOT NULL,
          title TEXT NOT NULL, messages TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS offline_submissions(
          id INTEGER PRIMARY KEY AUTOINCREMENT, urls TEXT NOT NULL,
          target_cid TEXT NOT NULL, target_label TEXT NOT NULL DEFAULT '',
          before_ids TEXT NOT NULL DEFAULT '[]', submitted_at REAL NOT NULL,
          last_checked REAL NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'submitted',
          message TEXT NOT NULL DEFAULT '', organize_job_id INTEGER,
          organize INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS native_media_info_queue(
          job_id INTEGER NOT NULL, item_id TEXT NOT NULL, target TEXT NOT NULL,
          connection TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
          attempts INTEGER NOT NULL DEFAULT 0, next_due REAL NOT NULL DEFAULT 0,
          emby_state TEXT NOT NULL DEFAULT 'pending',
          emby_attempts INTEGER NOT NULL DEFAULT 0, emby_next_due REAL NOT NULL DEFAULT 0,
          PRIMARY KEY(job_id,item_id));
        CREATE TABLE IF NOT EXISTS emby_outbox(
          job_id INTEGER PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 1,
          next_due REAL NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS task_schedules(
          kind TEXT PRIMARY KEY,next_due REAL NOT NULL,last_run REAL NOT NULL DEFAULT 0,
          last_job_id INTEGER,merged INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS notifications(
          id INTEGER PRIMARY KEY AUTOINCREMENT,at REAL NOT NULL,level TEXT NOT NULL,
          job_id INTEGER,event_key TEXT NOT NULL UNIQUE,title TEXT NOT NULL,message TEXT NOT NULL,
          read_at REAL,telegram_state TEXT NOT NULL DEFAULT 'disabled');
        CREATE TABLE IF NOT EXISTS emby_deliveries(
          job_id INTEGER PRIMARY KEY,state TEXT NOT NULL,payload TEXT NOT NULL,
          attempts INTEGER NOT NULL DEFAULT 0,next_due REAL NOT NULL,
          deadline REAL NOT NULL,message TEXT NOT NULL DEFAULT '',
          total INTEGER NOT NULL DEFAULT 0,confirmed INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS sync_snapshots(
          account_id TEXT NOT NULL,scope TEXT NOT NULL,job_id INTEGER NOT NULL,
          checked REAL NOT NULL,revision TEXT NOT NULL,paths TEXT NOT NULL,
          PRIMARY KEY(account_id,scope));
        CREATE INDEX IF NOT EXISTS events_by_time ON events(at);
        CREATE INDEX IF NOT EXISTS notifications_by_time ON notifications(at);
        ''')
        migrated = {row[1] for row in self.db.execute('PRAGMA table_info(notifications)')}
        for name, ddl in (('attempts', ' INTEGER NOT NULL DEFAULT 0'),
                          ('next_try', ' REAL NOT NULL DEFAULT 0'),
                          ('photo', ' TEXT')):
            if name not in migrated:
                self.db.execute(f'ALTER TABLE notifications ADD COLUMN {name}{ddl}')
        # emby_deliveries 的「总数 / 已确认数」摘列（0.8.20）。
        # 目的：任务列表与 SSE 每 2 秒都要显示「已确认 N/M」，以前每条任务都要
        # json.loads 整段 payload 再遍历 targets；改成列之后这两条路径不再解析 JSON。
        delivered = {row[1] for row in self.db.execute('PRAGMA table_info(emby_deliveries)')}
        upgraded = False
        for name, ddl in (('total', ' INTEGER NOT NULL DEFAULT 0'),
                          ('confirmed', ' INTEGER NOT NULL DEFAULT 0')):
            if name not in delivered:
                self.db.execute(f'ALTER TABLE emby_deliveries ADD COLUMN {name}{ddl}')
                upgraded = True
        # offline_submissions 的重试计数（0.9.18）：核查碰到 115 超时/限流时不能一次
        # 就判死——现场 99 条「核查未完成：115 请求未完成」全卡在 needs_review 再没人碰。
        subs = {row[1] for row in self.db.execute('PRAGMA table_info(offline_submissions)')}
        if 'attempts' not in subs:
            self.db.execute('ALTER TABLE offline_submissions ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0')
        if 'stalled_at' not in subs:
            # 卡住回执只发一次：发过就记时间，避免同一个任务反复提醒。
            self.db.execute('ALTER TABLE offline_submissions ADD COLUMN stalled_at REAL')
        if upgraded:
            # 历史行用 SQLite 的 JSON 函数一次性回填，不做逐行 Python 解析。
            self.db.execute(
                "UPDATE emby_deliveries SET total=json_array_length(payload,'$.targets'),"
                " confirmed=(SELECT count(*) FROM json_each(payload,'$.targets')"
                " WHERE json_extract(value,'$.confirmed')=1)"
                " WHERE json_valid(payload) AND json_type(payload,'$.targets')='array'")
        # 0.9.54：两张纯派生表退场。`output_stats` 只是 STRM 的 size/mtime 摘要缓存
        # （STRM 只有几百字节，缓存省不下什么，却要求每次搬文件都对账）；
        # `media_info_outputs` 的归属判定完全能由 sidecar 自带的 version/playback_id 得到。
        # 旧库在启动时丢掉，只做一次（meta 记标记），失败也不该阻塞启动。
        if not self.meta('schema_pruned_0954'):
            for table in ('output_stats', 'media_info_outputs'):
                try:
                    self.db.execute('DROP TABLE IF EXISTS ' + table)
                except sqlite3.OperationalError:
                    pass
            self.db.execute("INSERT OR REPLACE INTO meta VALUES('schema_pruned_0954',?)",
                            (json.dumps(True),))
        self.db.commit()
        key_path = root / 'config/master.key'
        if not key_path.exists():
            if self.db.execute('SELECT 1 FROM settings LIMIT 1').fetchone():
                raise RuntimeError('加密密钥缺失；请恢复 config/master.key，禁止覆盖已有配置。')
            fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as f:
                f.write(Fernet.generate_key())
        self.crypto = Fernet(key_path.read_bytes().strip())
        os.chmod(key_path, 0o600)
        os.chmod(self.path, 0o600)
        version = self.meta('schema_version')
        if version not in ('', '1', SCHEMA_VERSION):
            raise RuntimeError('数据库版本不兼容，已停止写入。')
        if not self.meta('username'):
            if not username.strip() or len(password) < 12:
                raise RuntimeError('首次启动请设置 ADMIN_USERNAME 和至少 12 位的 ADMIN_PASSWORD。')
            salt = secrets.token_bytes(16)
            self.db.executemany('INSERT INTO meta VALUES(?,?)', [
                ('username', username), ('salt', salt.hex()),
                ('password_hash', password_hash(password, salt)), ('schema_version', '1')])
        self.db.execute("INSERT OR REPLACE INTO meta VALUES('organize_schema_version','1')")
        self.db.execute("INSERT OR REPLACE INTO meta VALUES('archive_schema_version','1')")
        # 建表脚本已（幂等）执行完，把库抬到当前版本；老库只补缺表，不动数据。
        self.db.execute("INSERT OR REPLACE INTO meta VALUES('schema_version',?)", (SCHEMA_VERSION,))
        self.db.commit()
        # Fail at startup if a supplied key cannot decrypt existing settings.
        for row in self.db.execute('SELECT value FROM settings'):
            self.crypto.decrypt(row['value'].encode())

    def remove_retired_modules(self):
        """Remove retired module metadata; never touch cloud files or local outputs.

        Called under the application instance lock, before workers start. Keep
        shared playback/revocation records so removed items cannot reappear.
        The transaction is atomic and safe to repeat after an interrupted start.
        """
        # 已下线模块的任务种类。历史版本分别用过短名和带阶段后缀的名字，
        # 两种都要清理，否则旧记录会一直留在任务列表里。
        kinds = ('mapping', 'mapping_rebuild', 'governance', 'governance_execute')
        with self.db:
            # Explicit BEGIN also makes DDL transactional on Python's sqlite3.
            self.db.execute('BEGIN')
            for row in self.db.execute('SELECT id,config,state FROM organize_jobs').fetchall():
                try:
                    config = json.loads(row['config'])
                except (ValueError, TypeError):
                    continue
                if not isinstance(config, dict):
                    continue
                if config.get('job_kind') in kinds:
                    for table in ('organize_items', 'notifications', 'emby_deliveries'):
                        self.db.execute(f'DELETE FROM {table} WHERE job_id=?', (row['id'],))
                    self.db.execute('DELETE FROM organize_jobs WHERE id=?', (row['id'],))
                elif str(config.get('origin', '')).startswith(('mapping-repair:', 'governance-restore:')):
                    # These are ordinary output-sync jobs. Retain their evidence,
                    # but stop unfinished module-originated work from resuming.
                    config['origin'] = 'retired_module'
                    self.jobs.set_config(row['id'], json.dumps(config, ensure_ascii=False))
                    if row['state'] not in ('completed', 'partial', 'cancelled'):
                        self.jobs.update(row['id'], state='cancelled', stage='来源模块已移除',
                                         message='停止未完成任务，保留已有输出。',
                                         notify_state='not_needed')
                        self.db.execute('DELETE FROM emby_deliveries WHERE job_id=?', (row['id'],))
            for table in ('mapping_entries', 'mapping_works', 'mapping_snapshots', 'governance_plans'):
                self.db.execute(f'DROP TABLE IF EXISTS {table}')
            self.db.executemany('DELETE FROM settings WHERE key=?',
                                [(key,) for key in ('mapping_config', 'governance_config', 'governance_policy')])
            self.db.executemany('DELETE FROM meta WHERE key=?',
                                [(key,) for key in ('mapping_revision', 'governance_last_scan')])
            self.db.executemany('DELETE FROM task_schedules WHERE kind=?', [(key,) for key in kinds])
            self.db.executemany('DELETE FROM events WHERE kind=?', [('mapping',), ('governance',)])

    def directory_label(self, cid, fallback: str = '') -> str:
        """按当前配置解析目录名：目录改名或换了目录，文案跟着变，不用旧快照。"""
        try:
            labels = (self.get('organize_config') or {}).get('directory_labels') or {}
        except Exception:
            labels = {}
        return labels.get(str(cid)) or fallback or ''

    def meta(self, key: str) -> str:
        row = self.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return row['value'] if row else ''

    def set_meta(self, key: str, value) -> None:
        """写一条 meta（JSON 序列化）：跨模块共享的小状态都走这里。"""
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',
                            (key, json.dumps(value, ensure_ascii=False)))

    def meta_json(self, key: str, default=None):
        raw = self.meta(key)
        if not raw:
            return default
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return default

    def webhook_token(self) -> str:
        """深度删除 Webhook 令牌的唯一读取口径。

        令牌存在 `sync_config.webhook_token`（接口与页面都写这里）。旧版本在
        读取端用了顶层 `settings['webhook_token']`，写入与读取不是同一个键，
        导致「生成令牌」后 Webhook 永远 403。这里统一读取，并兼容早期把令牌
        写在顶层的安装。
        """
        config = self.get('sync_config', {})
        if isinstance(config, dict):
            token = config.get('webhook_token')
            if token:
                return str(token)
        legacy = self.get('webhook_token', '')
        return str(legacy or '')

    def authenticate(self, username: str, password: str) -> bool:
        valid = hmac.compare_digest(password_hash(password, bytes.fromhex(self.meta('salt'))), self.meta('password_hash'))
        return hmac.compare_digest(username, self.meta('username')) and valid

    def get(self, key: str, default=''):
        row = self.db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return json.loads(self.crypto.decrypt(row['value'].encode())) if row else default

    def set_many(self, values: dict):
        with self.db:
            for key, value in values.items():
                encrypted = self.crypto.encrypt(json.dumps(value, ensure_ascii=False).encode()).decode()
                self.db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', (key, encrypted))

    def forget(self, keys):
        """删掉设置项并返回真正删掉的键（用于"迁移完就清理"的场景）。

        设置页不展示、代码也不再读的键长期留在库里，会让人以为"改了没效果"——
        它们只该作为迁移来源被读一次，随后删除。
        """
        removed = []
        with self.db:
            for key in keys:
                cursor = self.db.execute('DELETE FROM settings WHERE key=?', (key,))
                if cursor.rowcount:
                    removed.append(key)
        return removed

    def new_session(self):
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        with self.db:
            self.db.execute('DELETE FROM sessions WHERE expires<?', (time.time(),))
            self.db.execute('INSERT INTO sessions VALUES(?,?,?)', (digest(token), csrf, time.time() + SESSION_SECONDS))
        return token, csrf

    def session(self, token: str):
        return self.db.execute('SELECT * FROM sessions WHERE hash=? AND expires>?', (digest(token), time.time())).fetchone()

    def event(self, kind: str, message: str):
        # Callers pass fixed messages, never raw upstream responses or credential values.
        with self.db:
            self.db.execute('INSERT INTO events(at,kind,message) VALUES(?,?,?)', (time.time(), kind, message))
        self._append_log(kind, message)

    def _append_log(self, kind: str, message: str):
        """把事件同时写进按天的日志文件；写不了也不能影响主流程。"""
        try:
            now = time.time()
            day = time.strftime('%Y%m%d', time.localtime(now))
            stamp = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now))
            with self._log_lock:
                if self._log_day != day:
                    self._log_day = day
                    if self._log_handle:
                        self._log_handle.close()
                    self._log_handle = None
                    self._prune_logs()
                if self._log_handle is None:
                    self._log_handle = open(self.log_dir / f'strimkeep-{day}.log', 'a', encoding='utf-8')
                self._log_handle.write(f'{stamp} [{kind}] {message}\n')
                self._log_handle.flush()
        except OSError:
            pass

    def _prune_logs(self, keep: int = 7):
        """按天日志只留最近 `keep` 天；卡顿转储单独一组，同样只留最近 `keep` 天。"""
        try:
            for old in sorted(self.log_dir.glob('strimkeep-*.log'))[:-keep]:
                old.unlink(missing_ok=True)
        except OSError:
            pass
        # 0.9.55：看门狗的线程栈转储也落在这里（stall-YYYYMMDD.log），
        # 以前只进 docker logs，容器一重建证据就没了。
        try:
            for old in sorted(self.log_dir.glob('stall-*.log'))[:-keep]:
                old.unlink(missing_ok=True)
        except OSError:
            pass


    @contextlib.contextmanager
    def transaction(self):
        """显式事务：`store.jobs` 的写入会并入它，而不是各自提交。"""
        depth = getattr(self, '_tx_depth', 0)
        self._tx_depth = depth + 1
        try:
            with self.db:
                yield
        finally:
            self._tx_depth = depth

    @property
    def jobs(self):
        """任务状态机入口（0.9.60）：organize_jobs / organize_items 的唯一写入点。

        之前的写入散在 9 个模块里（审计 A1），各自拼 SQL、各自决定能写哪一步；
        现在统一走 `strimkeep/jobs.py` 的迁移表，写错状态或非法迁移会当场抛异常。
        """
        instance = getattr(self, '_jobs_instance', None)
        if instance is None:
            from .jobs import Jobs
            instance = Jobs(self)
            self._jobs_instance = instance
        return instance

    def settle_terminal_job(self, job_id, message='任务结束未处理'):
        """任务进入终态时收口：归一没走到终态的条目，并释放该任务的转存密钥。

        为什么需要：`Organizer.manifest()` 会先把整批文件写成 `state='discovered'`，
        再逐条推进。中途 pause / cancel / 异常退出时，剩下的行会永远停在
        `discovered` —— 它们所属的任务已经结束，不会再有人处理，却仍然参与任务中心
        的「未处理」统计（0.8.18 现场残留 294 条）。这里在唯一的终态入口收口，
        把它们标成 `skipped` 并写清原因，历史可以追溯、统计也不再骗人。

        同时删除 `settings['transfer-secret:<job>']`：分享码/提取码只在转存任务
        生命周期内需要，留着既没用，又多一份加密凭据（0.8.18 现场残留 21 条）。

        只动 `discovered` / `planned` 这两种「还没有开始操作云端」的状态；
        `done` / `archived` / `review` / `pending` 一律不碰，避免覆盖真实结果。
        幂等：同一任务重复调用只会在第一次产生变更。
        """
        # 0.9.60：实现搬到 jobs.settle()，「进终态」与「收口」不再可能分家。
        return self.jobs.settle(job_id, message)

    def reconcile_terminal_items(self):
        """启动自检：把「任务已结束、条目还停在发现态」的历史残留一次性归一。

        返回被归一的条目数，调用方据此写操作记录。幂等：再跑一次得到 0。
        不修历史任务状态、不删台账，只补条目状态。
        """
        rows = self.db.execute(
            "SELECT i.job_id AS job_id FROM organize_items i JOIN organize_jobs j ON j.id=i.job_id "
            "WHERE i.state IN ('discovered','planned') AND j.state IN (?,?,?) GROUP BY i.job_id",
            TERMINAL_JOB_STATES).fetchall()
        total = 0
        for row in rows:
            total += self.settle_terminal_job(row['job_id'])['items']
        return total

    def prune(self, *args, **kwargs):
        """主线程入口：沿用 0.8.19 的调用方式（同步、用共享连接）。

        定期维护请用 prune_in_thread()：它在线程里跑，但会自己开连接。
        """
        return self._prune(self.db, *args, **kwargs)

    def prune_in_thread(self, *args, **kwargs):
        """在线程池里执行的 prune（**必须**从 `asyncio.to_thread` 调用）。

        0.8.21 起 `self.db` 就是「当前线程自己的连接」，所以这里直接用即可。
        早期（0.8.20）在这里手写 `sqlite3.connect` 是因为当时只有一个共享连接，
        共用它会 SIGSEGV；现在连接按线程懒创建，那段重复逻辑已经不需要了。
        """
        return self._prune(self.db, *args, **kwargs)

    def _prune(self, db, event_days: int = 90, event_max: int = 20000, notification_days: int = 180,
               job_days: int = 30, keep_jobs: int = 20, queue_days: int = 7):
        """Bound growth of the live log, read notices, dead sessions and finished item rows.

        The log centre only needs recent history; without a bound the database
        (and every backup of it) grew with each scheduled scan for ever.

        `organize_items` 是最大的一张表（每条明细都带一份 plan JSON），已完成任务
        的明细没有保留价值：按 `job_days` 清掉，但无论多老都保留最近 `keep_jobs` 个
        任务，方便回头查刚跑过的那几批。台账（organized_files）不动。
        """
        now = time.time()
        with db:
            expired = db.execute('DELETE FROM events WHERE at<?',
                                      (now - event_days * 86400,)).rowcount
            # Keep the newest rows even when an old clock or a burst outruns the age rule.
            overflow = db.execute(
                'DELETE FROM events WHERE id<=COALESCE('
                '(SELECT id FROM events ORDER BY id DESC LIMIT 1 OFFSET ?),0)', (event_max,)).rowcount
            # 0.9.55：再按 kind 单独封顶，别让高频事件把告警挤掉（见 EVENT_KIND_CAPS）。
            for kind, limit in EVENT_KIND_CAPS:
                cut = db.execute('SELECT id FROM events WHERE kind=? ORDER BY id DESC '
                                 'LIMIT 1 OFFSET ?', (kind, limit)).fetchone()
                if cut:
                    overflow += db.execute('DELETE FROM events WHERE kind=? AND id<=?',
                                           (kind, cut[0])).rowcount
            notices = db.execute('DELETE FROM notifications WHERE at<? AND read_at IS NOT NULL',
                                      (now - notification_days * 86400,)).rowcount
            sessions = db.execute('DELETE FROM sessions WHERE expires<?', (now,)).rowcount
            items = db.execute(
                'DELETE FROM organize_items WHERE job_id IN ('
                'SELECT id FROM organize_jobs WHERE state IN (\'completed\',\'partial\',\'cancelled\') '
                'AND updated<? ORDER BY updated DESC LIMIT -1 OFFSET ?)',
                (now - job_days * 86400, max(0, int(keep_jobs)))).rowcount
            queue = db.execute(
                "DELETE FROM deep_delete_queue WHERE state!='queued' AND updated<?",
                (now - queue_days * 86400,)).rowcount
            # 0.9.56：`skipped` 只是「这一轮跳过了什么」，留 2 天足够。现场 11-04 的一次
            # 爆发留下 12,129 行（6.8MB），按 7 天规则要等满一周才清；而全库 freelist
            # 只有 0.7MB，说明这些是真占地方、VACUUM 也救不回来。
            queue += db.execute(
                "DELETE FROM deep_delete_queue WHERE state='skipped' AND updated<?",
                (now - 2 * 86400,)).rowcount
            # 媒体信息补扫队列：终态行没有时间戳，只能「每个 target 只留最新一行」+ 封顶。
            # 0.9.16 之前补扫会对同一文件反复入队，现场 2609 行里 484 行是重复。
            media_queue = db.execute(
                "DELETE FROM native_media_info_queue WHERE state IN ('done','failed') AND rowid NOT IN ("
                "SELECT MAX(rowid) FROM native_media_info_queue GROUP BY target)").rowcount
            media_queue += db.execute(
                "DELETE FROM native_media_info_queue WHERE rowid NOT IN "
                "(SELECT rowid FROM native_media_info_queue ORDER BY rowid DESC LIMIT 3000)").rowcount
            # 转存密钥的兜底清理：正常路径在任务进终态时由 settle_terminal_job 删除
            # （含崩溃/取消）；这里再按「所属任务已结束」扫一遍，兼容 0.8.19 之前
            # 留下的行，也覆盖任务被人工删除的情况。
            secrets = db.execute(
                "DELETE FROM settings WHERE key LIKE 'transfer-secret:%' AND substr(key,17) IN "
                "(SELECT CAST(id AS TEXT) FROM organize_jobs WHERE state IN (?,?,?))",
                TERMINAL_JOB_STATES).rowcount
            # 转存「同一条消息不重复执行」的去重键没有时间戳，按条数封顶：
            # 保留最近 200 条，避免 settings 随转存次数单调增长。
            stale_updates = db.execute(
                "DELETE FROM settings WHERE key LIKE 'transfer-update:%' AND rowid NOT IN "
                "(SELECT rowid FROM settings WHERE key LIKE 'transfer-update:%' "
                "ORDER BY rowid DESC LIMIT 200)").rowcount
        return {'events': max(0, expired) + max(0, overflow),
                'notifications': max(0, notices), 'sessions': max(0, sessions),
                'items': max(0, items), 'queue': max(0, queue), 'media_queue': max(0, media_queue),
                'transfer_secrets': max(0, secrets), 'stale_updates': max(0, stale_updates)}

    def _connect(self):
        """新建一条连接：WAL + 5 秒忙等 + NORMAL 同步，并登记以便统一关闭。

        每条连接都要自己设 PRAGMA：`journal_mode` 是库级持久设置，而 `busy_timeout`
        与 `synchronous` 是连接级的。
        """
        conn = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA busy_timeout=5000')
        conn.execute('PRAGMA synchronous=NORMAL')
        with self._connections_lock:
            self._connections.append((threading.get_ident(), conn))
        return conn

    @property
    def db(self):
        """当前线程的 sqlite 连接（懒创建）。

        0.8.21 起所有代码（事件循环、`asyncio.to_thread` 的工作线程）都可以照常写
        `store.db.execute(...)`：拿到的是自己线程的连接。跨线程共用连接会在 C 层崩，
        这条属性把「一线程一连接」变成默认行为，不需要改 456 个调用点。
        """
        if self._override is not None:
            return self._override
        conn = getattr(self._local, 'conn', None)
        if conn is None:
            conn = self._connect()
            self._local.conn = conn
        return conn

    @db.setter
    def db(self, value):
        # 只给测试替身用（例如包一层统计查询次数的包装）；生产代码不要赋值。
        self._override = value

    async def run_in_thread(self, fn, *args, **kwargs):
        """把同步的 DB/文件逻辑放到工作线程执行。

        配合一线程一连接：`fn` 内部照常写 `store.db...`，拿到的是该线程自己的连接，
        既不阻塞事件循环，也不会跨线程共用连接。
        """
        return await asyncio.to_thread(fn, *args, **kwargs)

    def close(self):
        """关闭连接。

        0.8.22 修一个会崩进程的竞态：以前这里无条件关闭**所有**线程的连接。事件循环
        这一侧调 `close()`（应用关闭、测试收尾）时，如果某个 `to_thread` 工作线程正在
        `prune_in_thread()` 里执行语句，它的连接就被从脚下抽掉了 —— sqlite3 在 C 层
        访问已释放的内存，进程直接 SIGSEGV（门禁实测退出码 139，栈顶是 `_prune`）。

        现在只关闭「本线程的连接」和「所属线程已经退出、没人再能用的连接」；活着的线程
        自己管自己的连接，线程结束时 `threading.local` 会在**它自己的线程里**释放，安全。
        """
        current = threading.get_ident()
        alive = {thread.ident for thread in threading.enumerate()}
        with self._connections_lock:
            keep, closing = [], []
            for owner, conn in self._connections:
                if owner == current or owner not in alive:
                    closing.append(conn)
                else:
                    keep.append((owner, conn))
            self._connections = keep
        for conn in closing:
            try:
                conn.close()
            except sqlite3.Error:
                pass
        if getattr(self._local, 'conn', None) is not None:
            self._local.conn = None        # 本线程下次访问会重新建连接
