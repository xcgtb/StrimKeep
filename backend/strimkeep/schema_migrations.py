"""顺序 schema 迁移：每个版本一段幂等 SQL，替代散落在启动各处的猜列补丁。

约定：
- 迁移按编号顺序执行，`meta.migration_version` 记录已应用到的编号；
- 每条迁移必须幂等（IF NOT EXISTS / IF EXISTS / 缺列检测），失败抛错阻止启动；
- base schema（store.py 的建表脚本）始终先跑，迁移只负责「老库补差」。
"""
import json

INVENTORY_DISPATCH_DDL = (
    "CREATE TABLE IF NOT EXISTS inventory_plan_dispatch ("
    "plan_id TEXT PRIMARY KEY, queued_at REAL NOT NULL, "
    "current TEXT NOT NULL DEFAULT '', confirmed INTEGER NOT NULL DEFAULT 0, "
    "work_groups TEXT NOT NULL DEFAULT '')"
)

# (编号, 说明, 升级 SQL 列表)。列表内多条语句逐条执行。
MIGRATIONS = [
    (1, '撤销表下线与热路径索引', [
        "DROP TABLE IF EXISTS share_revocations",
        "CREATE INDEX IF NOT EXISTS playbacks_relative ON playbacks(relative_path)",
        "CREATE INDEX IF NOT EXISTS playbacks_relative_nocase ON playbacks(relative_path COLLATE NOCASE)",
        "CREATE INDEX IF NOT EXISTS playbacks_file ON playbacks(file_id)",
        "CREATE INDEX IF NOT EXISTS share_strms_path ON share_strms(strm_path)",
        "CREATE INDEX IF NOT EXISTS share_strms_path_nocase ON share_strms(strm_path COLLATE NOCASE)",
        "CREATE INDEX IF NOT EXISTS organize_items_state ON organize_items(state)",
        "CREATE INDEX IF NOT EXISTS organize_items_job_state ON organize_items(job_id,state)",
        "CREATE INDEX IF NOT EXISTS notifications_job_event ON notifications(job_id,event_key)",
        "CREATE INDEX IF NOT EXISTS deep_delete_batch ON deep_delete_queue(batch,state)",
        "CREATE INDEX IF NOT EXISTS organized_files_relative ON organized_files(relative_path)",
        "CREATE INDEX IF NOT EXISTS organized_files_relative_nocase ON organized_files(relative_path COLLATE NOCASE)",
        "CREATE INDEX IF NOT EXISTS output_registry_relative_nocase ON output_registry(relative_path COLLATE NOCASE)",
        "CREATE INDEX IF NOT EXISTS archive_entries_relative_nocase ON archive_entries(relative_path COLLATE NOCASE)",
    ]),
    (2, '通知重试与交付摘列', [
        # 缺列时补；SQLite 无 ADD COLUMN IF NOT EXISTS，先查再补在 apply() 里做。
    ]),
    (3, '离线任务重试与卡住标记', []),
    (4, '派生表退场', [
        "DROP TABLE IF EXISTS output_stats",
        "DROP TABLE IF EXISTS media_info_outputs",
    ]),
    (5, 'Web 与 Worker 命令队列', [
        "CREATE TABLE IF NOT EXISTS worker_commands("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "kind TEXT NOT NULL,"
        "payload TEXT NOT NULL DEFAULT '{}',"
        "state TEXT NOT NULL DEFAULT 'queued',"
        "attempts INTEGER NOT NULL DEFAULT 0,"
        "created REAL NOT NULL,"
        "updated REAL NOT NULL,"
        "started REAL,"
        "finished REAL,"
        "message TEXT NOT NULL DEFAULT '',"
        "result TEXT NOT NULL DEFAULT '')",
        "CREATE INDEX IF NOT EXISTS worker_commands_state ON worker_commands(state,id)",
    ]),
    (6, '库存清理派发与确认进度', [INVENTORY_DISPATCH_DDL]),
    (7, '订阅已拒绝的资源与频道身份', [
        '''CREATE TABLE IF NOT EXISTS media_subscription_rejected (
            link_key TEXT PRIMARY KEY, media_type TEXT NOT NULL, tmdb_id INTEGER NOT NULL,
            season INTEGER NOT NULL, episode INTEGER NOT NULL, inbox_id INTEGER, job_id INTEGER,
            created REAL NOT NULL, rejected REAL NOT NULL
        )''',
        'CREATE INDEX IF NOT EXISTS subscription_rejected_work '
        'ON media_subscription_rejected(media_type,tmdb_id,season,episode)',
    ]),
    (8, '跨入口入库任务去重', [
        "CREATE INDEX IF NOT EXISTS organize_jobs_intake ON organize_jobs(json_extract(config,'$.intake_key')) "
        "WHERE json_valid(config)",
    ]),
]

# 列补丁：表 → [(列名, DDL)]。幂等：先 PRAGMA table_info 再补。
COLUMN_PATCHES = {
    'media_subscriptions': [('auto_switch', ' INTEGER NOT NULL DEFAULT 1'),
                            ('minimum_resolution', ' INTEGER NOT NULL DEFAULT 0'),
                            ('preferred_sources', " TEXT NOT NULL DEFAULT '[]'")],
    'channel_state': [('peer_id', " TEXT NOT NULL DEFAULT ''")],
    'events': [('level', " TEXT NOT NULL DEFAULT ''"), ('job_id', ' INTEGER'),
               ('error_code', " TEXT NOT NULL DEFAULT ''")],
    'notifications': [
        ('attempts', ' INTEGER NOT NULL DEFAULT 0'),
        ('next_try', ' REAL NOT NULL DEFAULT 0'),
        ('photo', ' TEXT'),
        ('telegram_options', " TEXT NOT NULL DEFAULT '{}'"),
    ],
    'emby_deliveries': [
        ('total', ' INTEGER NOT NULL DEFAULT 0'),
        ('confirmed', ' INTEGER NOT NULL DEFAULT 0'),
    ],
    'offline_submissions': [
        ('attempts', ' INTEGER NOT NULL DEFAULT 0'),
        ('stalled_at', ' REAL'),
        ('notice_context', " TEXT NOT NULL DEFAULT '{}'"),
    ],
}


def applied_version(db):
    row = db.execute("SELECT value FROM meta WHERE key='migration_version'").fetchone()
    try:
        return int(json.loads(row[0])) if row else 0
    except (TypeError, ValueError):
        return 0


def _mark(db, number):
    db.execute("INSERT OR REPLACE INTO meta VALUES('migration_version',?)",
               (json.dumps(number),))


def apply(db):
    """执行尚未应用的迁移；返回本次应用的迁移编号列表。"""
    done = []
    current = applied_version(db)
    for number, _note, statements in MIGRATIONS:
        if number <= current:
            continue
        for sql in statements:
            db.execute(sql)
        done.append(number)
    # 列补丁独立于编号执行（幂等，且新库天然有列）。
    # 迁移测试会先建一个只含部分表的库；表不存在时跳过，不能对不存在的表 ALTER。
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table, columns in COLUMN_PATCHES.items():
        if table not in tables:
            continue
        existing = {row[1] for row in db.execute('PRAGMA table_info(%s)' % table)}
        for name, ddl in columns:
            if name not in existing:
                db.execute('ALTER TABLE %s ADD COLUMN %s%s' % (table, name, ddl))
    if done:
        _mark(db, max(done))
    db.commit()
    return done
