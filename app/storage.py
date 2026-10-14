# -*- coding: utf-8 -*-
"""SQLite 存储层：治理计划 / 执行历史 / 追更订阅状态 / 消息去重。

设计约定（与全项目其它模块一致）：
- 库文件固定为 STATE_DIR/strimkeep.db（WAL 模式）；路径随 engine.STATE_DIR 惰性解析，
  测试改 AGENT_DATA / monkeypatch 路径后自动落到新库，不串数据；
- 配置、计划、订阅和缓存只写 SQLite；旧 JSON / JSONL 仅在首次迁移时读取，
  成功后使用库内标记防止旧文件重新覆盖新状态；
- 执行历史（审计）保存治理清理结果，运行日志独立滚动保存；
- 消息去重 / TG offset：只存库（此前完全没有持久化，重启会重复处理旧消息）；
- public 函数仍按旧接口降级返回默认值；失败进入限流日志和当前进程诊断。
- 批量替换及索引更新必须在一个事务内提交，失败回滚。
"""
import json
import contextlib
import contextvars
import functools
import os
import sqlite3
import threading
import time
import sys
from pathlib import Path

try:
    from . import storage_status as _status
except ImportError:
    import storage_status as _status

DB_NAME = 'strimkeep.db'
PLAN_KEEP_DAYS = 7          # 未执行计划保留 7 天；执行存档保留
DEDUP_TTL = 7 * 86400       # 去重键保留 7 天

_DBS = {}                   # path_str -> sqlite3.Connection（按路径缓存，测试换目录即换库）
_LOCK = threading.RLock()   # 可重入：_execute/db_save_sub_state 持锁时还会调 _conn()

_ATTEMPT = contextvars.ContextVar('storage_attempt', default=None)
_STATE_SCOPE = contextvars.ContextVar('sqlite_state_scope', default=None)


@contextlib.contextmanager
def state_scope(directory):
    token = _STATE_SCOPE.set(Path(directory))
    try:
        yield
    finally:
        _STATE_SCOPE.reset(token)


def _diagnostic_dir():
    try:
        return _state_dir()
    except Exception:
        return Path(os.environ.get('AGENT_DATA', '/data')) / 'state'


def _note_failure(operation, error):
    attempt = _ATTEMPT.get()
    if attempt is not None:
        attempt['failed'] = True
    _status.record_failure(_diagnostic_dir(), operation, error)


def storage_health():
    return _status.snapshot(_diagnostic_dir())


def _observed(fn):
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        attempt = {'failed': False, 'io_succeeded': False}
        token = _ATTEMPT.set(attempt)
        try:
            result = fn(*args, **kwargs)
        finally:
            _ATTEMPT.reset(token)
            parent = _ATTEMPT.get()
            if parent is not None:
                parent['failed'] |= attempt['failed']
                parent['io_succeeded'] |= attempt['io_succeeded']
        # Validation failures are not evidence that a broken write has recovered.
        read = fn.__name__ in ('db_load_plan', 'db_list_plans', 'db_media_index_load',
                              'db_media_index_stats', 'db_recent_audit', 'db_get_audit',
                              'db_load_sub_state', 'db_kv_get', 'db_dedup_seen', 'db_doc_get')
        if not attempt['failed'] and attempt['io_succeeded'] and (read or result is not False or fn.__name__ == 'db_doc_mirror_file'):
            _status.record_success(_diagnostic_dir(), fn.__name__)
        return result
    return wrapped


@contextlib.contextmanager
def _transaction():
    with _LOCK:
        con = _conn()
        try:
            yield con
            con.commit()
            attempt = _ATTEMPT.get()
            if attempt is not None:
                attempt['io_succeeded'] = True
        except Exception:
            con.rollback()
            raise


_SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
    id TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    state TEXT NOT NULL,
    rule_sig TEXT DEFAULT '',
    stats TEXT DEFAULT '{}',
    executed_at REAL,
    payload TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_plans_ts ON plans(ts);

CREATE TABLE IF NOT EXISTS media_index (
    root TEXT NOT NULL,
    path TEXT NOT NULL,
    mtime_ns INTEGER NOT NULL DEFAULT 0,
    size INTEGER NOT NULL DEFAULT 0,
    payload TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (root, path)
);
CREATE INDEX IF NOT EXISTS idx_media_index_root ON media_index(root);

CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    ts_epoch REAL NOT NULL DEFAULT 0,
    category TEXT DEFAULT '',
    title TEXT DEFAULT '',
    details TEXT DEFAULT '[]',
    rule_sig TEXT DEFAULT '',
    extra TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS sub_state (
    store TEXT NOT NULL,
    sid TEXT NOT NULL,
    payload TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (store, sid)
);

CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT,
    ts REAL
);

CREATE TABLE IF NOT EXISTS dedup (
    k TEXT PRIMARY KEY,
    ts REAL NOT NULL
);

-- 文档状态（洗版残留 / 最近扫描 / 巡检设置 / 手动完结 …）。
-- 一个文档一行，payload 为 JSON 文本；SQLite 是持久状态主存储。
CREATE TABLE IF NOT EXISTS docs (
    name TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS cache_entries (
    namespace TEXT NOT NULL,
    k TEXT NOT NULL,
    payload TEXT NOT NULL,
    ts REAL NOT NULL,
    PRIMARY KEY(namespace,k)
);
CREATE TABLE IF NOT EXISTS runtime_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    level TEXT NOT NULL,
    source TEXT NOT NULL,
    message TEXT NOT NULL
);
"""

# schema 版本（PRAGMA user_version）：新增表/列且向后兼容时递增。
# v1 = 原 6 张表；v2 = 增加 docs。老库打开时 CREATE IF NOT EXISTS 会自动补表，无需数据迁移。
SCHEMA_VERSION = 4


def _state_dir() -> Path:
    """当前生效的 state 目录。优先取 engine.STATE_DIR（含 monkeypatch），
    engine 不可用时（脚本单独 import 本模块）退回 AGENT_DATA 环境变量。"""
    scoped = _STATE_SCOPE.get()
    if scoped is not None:
        return scoped
    # Config is loaded while engine is importing; never import engine from here.
    for name in (('app.engine', 'engine') if __package__ else ('engine', 'app.engine')):
        module = sys.modules.get(name)
        if module is not None and hasattr(module, 'STATE_DIR'):
            return Path(module.STATE_DIR)
    return Path(os.environ.get('AGENT_DATA', '/data')) / 'state'


def _conn():
    d = _state_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = str(d / DB_NAME)
    with _LOCK:
        con = _DBS.get(path)
        if con is None:
            con = sqlite3.connect(path, check_same_thread=False, timeout=10)
            try:
                con.execute('PRAGMA journal_mode=WAL')
                con.execute('PRAGMA synchronous=NORMAL')
                con.executescript(_SCHEMA)
                _ensure_audit_extra(con)
                _ensure_docs_version(con)
                try:
                    cur_v = con.execute('PRAGMA user_version').fetchone()[0]
                    if cur_v < SCHEMA_VERSION:   # 只升不降：被新版本打开过的库，回退旧版本也不会改写
                        con.execute('PRAGMA user_version = %d' % SCHEMA_VERSION)
                except sqlite3.Error:
                    raise
            except Exception:
                con.close()
                raise
            _DBS[path] = con
        return con


def _ensure_audit_extra(con):
    """老库的 audit 表没有 extra 列（结构化执行明细）；幂等补列，不动已有数据。"""
    try:
        cols = {r[1] for r in con.execute('PRAGMA table_info(audit)').fetchall()}
        if 'extra' not in cols:
            con.execute("ALTER TABLE audit ADD COLUMN extra TEXT DEFAULT ''")
            con.commit()
    except sqlite3.Error:
        raise


def _ensure_docs_version(con):
    """Keep both historical four-column and newer three-column docs compatible."""
    columns = {r[1] for r in con.execute('PRAGMA table_info(docs)')}
    if 'version' not in columns:
        con.execute('ALTER TABLE docs ADD COLUMN version INTEGER NOT NULL DEFAULT 1')
        con.commit()


_DOC_UPSERT_SQL = ('INSERT INTO docs(name,payload,version,updated_at) VALUES(?,?,1,?) '
                   'ON CONFLICT(name) DO UPDATE SET payload=excluded.payload, '
                   'version=docs.version+1, updated_at=excluded.updated_at')


def _write_doc(con, name, payload, updated_at):
    # Historical databases require an explicit version (no column default).
    # UPDATE preserves any additional legacy columns rather than replacing rows.
    con.execute(_DOC_UPSERT_SQL, (str(name), payload, updated_at))


def _execute(sql, args=()):
    with _transaction() as con:
        return con.execute(sql, args)


# ═══════════════════ 治理计划 ═══════════════════

@_observed
def db_save_plan(payload):
    """写入/更新一条计划（payload 为 save_plan 生成的完整 dict）。失败返回 False。"""
    try:
        if not isinstance(payload, dict) or payload.get('schema_version') != 2:
            return False
        pid = str(payload.get('id') or '')
        if not pid:
            return False
        _execute(
            'INSERT OR REPLACE INTO plans (id, ts, state, rule_sig, stats, executed_at, payload, updated_at)'
            ' VALUES (?,?,?,?,?,?,?,?)',
            (pid, float(payload.get('ts') or 0), str(payload.get('state') or ''),
             str(payload.get('rule_sig') or ''), json.dumps(payload.get('stats') or {}, ensure_ascii=False),
             payload.get('executed_at'), json.dumps(payload, ensure_ascii=False), time.time()))
        return True
    except Exception as _storage_error:
        _note_failure('db_save_plan', _storage_error)
        return False


@_observed
def db_load_plan(plan_id):
    """按 id 读计划（schema_v2 校验与 JSON 文件路径一致）；没有返回 None。"""
    try:
        cur = _execute('SELECT payload FROM plans WHERE id=?', (str(plan_id),))
        row = cur.fetchone()
        if not row:
            return None
        data = json.loads(row[0])
        if not isinstance(data, dict) or data.get('schema_version') != 2:
            return None
        return data
    except Exception as _storage_error:
        _note_failure('db_load_plan', _storage_error)
        return None


@_observed
def db_save_plan_state(plan_id, state, extra=None):
    """更新计划状态（先读库内 payload 再改，保持与文件版一致的合并语义）。"""
    try:
        data = db_load_plan(plan_id)
        if data is None:
            return False
        data['state'] = state
        if extra:
            data.update(extra)
        return db_save_plan(data)
    except Exception as _storage_error:
        _note_failure('db_save_plan_state', _storage_error)
        return False


@_observed
def db_list_plans(limit=20):
    """最近计划列表（新→旧），元素与 /api/plans 既有结构一致。"""
    try:
        cur = _execute(
            'SELECT id, ts, state, stats, executed_at FROM plans ORDER BY ts DESC LIMIT ?',
            (int(limit),))
        out = []
        for pid, ts, state, stats, executed_at in cur.fetchall():
            try:
                stats_d = json.loads(stats) if stats else {}
            except ValueError as _storage_error:
                _note_failure('db_list_plans', _storage_error)
                stats_d = {}
            out.append({'id': pid, 'ts': ts, 'state': state,
                        'stats': stats_d, 'executed_at': executed_at})
        return out
    except Exception as _storage_error:
        _note_failure('db_list_plans', _storage_error)
        return []


@_observed
def db_delete_plan(plan_id):
    try:
        _execute('DELETE FROM plans WHERE id=?', (str(plan_id),))
        return True
    except Exception as _storage_error:
        _note_failure('db_delete_plan', _storage_error)
        return False


@_observed
def db_purge_plans(cut_ts, exclude_ids=None):
    """删除 ts 早于 cut_ts 的未执行计划；保留执行存档。返回删除条数。"""
    try:
        if exclude_ids:
            protected = {str(pid) for pid in exclude_ids}
            with _transaction() as con:
                rows = con.execute("SELECT id FROM plans WHERE ts < ? AND state IN ('pending','expired','stale')", (float(cut_ts),)).fetchall()
                gone = [(row[0],) for row in rows if row[0] not in protected]
                con.executemany('DELETE FROM plans WHERE id=?', gone)
            return len(gone)
        cur = _execute("DELETE FROM plans WHERE ts < ? AND state IN ('pending','expired','stale')", (float(cut_ts),))
        return cur.rowcount or 0
    except Exception as _storage_error:
        _note_failure('db_purge_plans', _storage_error)
        return 0


@_observed
def db_sync_plans_from_disk():
    # Compatibility entry: import-once, never re-synchronize old exports.
    try:
        from . import state_store
    except ImportError:
        import state_store
    return state_store.migrate(_state_dir())


# ═══════════════════ 媒体增量索引 ═══════════════════

@_observed
def db_media_index_load(root):
    """读取一个媒体根目录的增量索引。返回 path -> {mtime_ns,size,payload}。"""
    try:
        cur = _execute('SELECT path, mtime_ns, size, payload FROM media_index WHERE root=?', (str(root),))
        out = {}
        for path, mtime_ns, size, payload in cur.fetchall():
            try:
                out[path] = {'mtime_ns': int(mtime_ns or 0), 'size': int(size or 0),
                             'payload': json.loads(payload) if payload else {}}
            except (TypeError, ValueError) as _storage_error:
                _note_failure('db_media_index_load', _storage_error)
                continue
        return out
    except Exception as _storage_error:
        _note_failure('db_media_index_load', _storage_error)
        return {}


@_observed
def db_media_index_upsert(root, rows):
    """批量更新媒体索引；rows 为 [(path, mtime_ns, size, payload), ...]。"""
    if not rows:
        return True
    try:
        now = time.time()
        values = [(str(root), str(path), int(mtime_ns or 0), int(size or 0),
                   json.dumps(payload or {}, ensure_ascii=False, separators=(',', ':')), now)
                  for path, mtime_ns, size, payload in rows]
        with _transaction() as con:
            con.executemany(
                'INSERT OR REPLACE INTO media_index (root,path,mtime_ns,size,payload,updated_at) VALUES (?,?,?,?,?,?)',
                values)
        return True
    except Exception as _storage_error:
        _note_failure('db_media_index_upsert', _storage_error)
        return False


@_observed
def db_media_index_delete_missing(root, seen_paths):
    """删除本次扫描中已不存在的媒体索引项。"""
    try:
        root = str(root)
        seen = {str(x) for x in (seen_paths or ())}
        with _transaction() as con:
            cur = con.execute('SELECT path FROM media_index WHERE root=?', (root,))
            old = [r[0] for r in cur.fetchall()]
            gone = [x for x in old if x not in seen]
            if gone:
                con.executemany('DELETE FROM media_index WHERE root=? AND path=?', [(root, x) for x in gone])
            return len(gone)
    except Exception as _storage_error:
        _note_failure('db_media_index_delete_missing', _storage_error)
        return 0


@_observed
def db_media_index_stats(root=None):
    try:
        if root:
            row = _execute('SELECT COUNT(*), MAX(updated_at) FROM media_index WHERE root=?', (str(root),)).fetchone()
        else:
            row = _execute('SELECT COUNT(*), MAX(updated_at) FROM media_index').fetchone()
        return {'count': int(row[0] or 0), 'updated_at': float(row[1] or 0)}
    except Exception as _storage_error:
        _note_failure('db_media_index_stats', _storage_error)
        return {'count': 0, 'updated_at': 0}


# ═══════════════════ 执行历史（审计） ═══════════════════

@_observed
def db_add_audit(rec):
    """追加一条审计记录（rec 与 logger JSONL 行结构一致）。失败返回 False。
    rec['extra']（可选）是结构化明细，如执行跨库清理的逐项清单快照，整体存为 JSON。"""
    try:
        extra = rec.get('extra')
        _execute(
            'INSERT INTO audit (ts, ts_epoch, category, title, details, rule_sig, extra) VALUES (?,?,?,?,?,?,?)',
            (str(rec.get('ts') or ''), time.time(), str(rec.get('category') or ''),
             str(rec.get('title') or ''), json.dumps(rec.get('details') or [], ensure_ascii=False),
             str(rec.get('rule_sig') or ''),
             json.dumps(extra, ensure_ascii=False) if extra else ''))
        return True
    except Exception as _storage_error:
        _note_failure('db_add_audit', _storage_error)
        return False


def _audit_row(row):
    rid, ts, category, title, details, rule_sig, extra = row
    try:
        details_l = json.loads(details) if details else []
    except ValueError:
        details_l = []
    rec = {'schema_version': 2, 'id': rid, 'ts': ts, 'category': category,
           'title': title, 'details': details_l, 'rule_sig': rule_sig or ''}
    if extra:
        try:
            rec['extra'] = json.loads(extra)
        except ValueError:
            pass
    return rec


_AUDIT_COLS = 'id, ts, category, title, details, rule_sig, extra'


@_observed
def db_recent_audit(limit=50):
    """最近 N 条审计（新→旧），结构对齐 logger.read_recent 的返回（多一个 id 和可选 extra）。"""
    try:
        cur = _execute('SELECT %s FROM audit ORDER BY id DESC LIMIT ?' % _AUDIT_COLS, (int(limit),))
        return [_audit_row(r) for r in cur.fetchall()]
    except Exception as _storage_error:
        _note_failure('db_recent_audit', _storage_error)
        return []


@_observed
def db_get_audit(rid):
    """按 id 取单条审计（含完整 extra）；没有返回 None。"""
    try:
        cur = _execute('SELECT %s FROM audit WHERE id=?' % _AUDIT_COLS, (int(rid),))
        row = cur.fetchone()
        return _audit_row(row) if row else None
    except Exception as _storage_error:
        _note_failure('db_get_audit', _storage_error)
        return None


def _import_audit_file(path):
    """一次性导入既有 audit.jsonl（逐行解析，坏行跳过）。返回导入条数。"""
    n = 0
    try:
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict) or not rec.get('ts'):
                    continue
                if db_add_audit(rec):
                    n += 1
    except OSError:
        pass
    return n


# ═══════════════════ 追更订阅状态 ═══════════════════

@_observed
def db_load_sub_state(store):
    """整份读订阅状态：{sid: payload}。store 为状态文件名（测试隔离用）。"""
    try:
        cur = _execute('SELECT sid, payload FROM sub_state WHERE store=?', (str(store),))
        out = {}
        for sid, payload in cur.fetchall():
            try:
                out[sid] = json.loads(payload)
            except ValueError as _storage_error:
                _note_failure('db_load_sub_state', _storage_error)
                continue
        return out
    except Exception as _storage_error:
        _note_failure('db_load_sub_state', _storage_error)
        return {}


@_observed
def db_save_sub_state(store, state):
    """整份替换订阅状态（DELETE+INSERT 一个事务，语义对齐旧的整文件重写）。"""
    try:
        store = str(store)
        rows = [(store, str(sid), json.dumps(v, ensure_ascii=False), time.time())
                for sid, v in (state or {}).items() if isinstance(v, dict)]
        with _transaction() as con:
            con.execute('DELETE FROM sub_state WHERE store=?', (store,))
            con.executemany(
                'INSERT OR REPLACE INTO sub_state (store, sid, payload, updated_at) VALUES (?,?,?,?)',
                rows)
        return True
    except Exception as _storage_error:
        _note_failure('db_save_sub_state', _storage_error)
        return False


@_observed
def db_clear_sub_state(store):
    """清空某份状态（状态文件被删除时对齐「重置」语义，防止旧状态从库里复活）。"""
    try:
        _execute('DELETE FROM sub_state WHERE store=?', (str(store),))
        return True
    except Exception as _storage_error:
        _note_failure('db_clear_sub_state', _storage_error)
        return False


# ═══════════════════ KV / 消息去重 ═══════════════════

@_observed
def db_kv_get(key, default=None):
    try:
        cur = _execute('SELECT v FROM kv WHERE k=?', (str(key),))
        row = cur.fetchone()
        return row[0] if row else default
    except Exception as _storage_error:
        _note_failure('db_kv_get', _storage_error)
        return default


@_observed
def db_kv_set(key, value):
    try:
        _execute('INSERT OR REPLACE INTO kv (k, v, ts) VALUES (?,?,?)',
                 (str(key), str(value), time.time()))
        return True
    except Exception as _storage_error:
        _note_failure('db_kv_set', _storage_error)
        return False


@_observed
def db_dedup_add(key):
    """登记已处理键；首次及每小时一次清理超过七天的去重键。"""
    try:
        now = time.time()
        # Existing seven-day retention, reliably checked once per hour (not a wall-clock modulo).
        with _transaction() as con:
            con.execute('INSERT OR REPLACE INTO dedup (k, ts) VALUES (?,?)', (str(key), now))
            row = con.execute('SELECT ts FROM kv WHERE k=?', ('dedup_last_purge',)).fetchone()
            if not row or now - float(row[0] or 0) >= 3600:
                con.execute('DELETE FROM dedup WHERE ts < ?', (now - DEDUP_TTL,))
                con.execute('INSERT OR REPLACE INTO kv (k,v,ts) VALUES (?,?,?)',
                            ('dedup_last_purge', str(now), now))
        return True
    except Exception as _storage_error:
        _note_failure('db_dedup_add', _storage_error)
        return False


@_observed
def db_dedup_seen(key):
    try:
        cur = _execute('SELECT 1 FROM dedup WHERE k=? AND ts>=?', (str(key), time.time() - DEDUP_TTL))
        return cur.fetchone() is not None
    except Exception as _storage_error:
        _note_failure('db_dedup_seen', _storage_error)
        return False


# ═══════════════════ 小型 JSON 文档（docs 表） ═══════════════════

@_observed
def db_doc_set(name, obj):
    """保存/覆盖一个 JSON 文档。失败返回 False，从不抛异常（旧公共接口；关键状态通过 state_store 严格写入）。"""
    try:
        _execute(_DOC_UPSERT_SQL, (str(name), json.dumps(obj, ensure_ascii=False), time.time()))
        return True
    except Exception as _storage_error:
        _note_failure('db_doc_set', _storage_error)
        return False


@_observed
def db_doc_get(name, default=None):
    """读取 JSON 文档；不存在或损坏返回 default。"""
    try:
        row = _execute('SELECT payload FROM docs WHERE name=?', (str(name),)).fetchone()
        return json.loads(row[0]) if row else default
    except Exception as _storage_error:
        _note_failure('db_doc_get', _storage_error)
        return default


@_observed
def db_doc_mirror_file(name, path):
    if __package__:
        from . import state_store
    else:
        import state_store
    try:
        with state_store._access(path, 'state_migration') as (p, key, con):
            imported = state_store._prepare(con, p, key)
        state_store.prepare(path)
        return imported
    except state_store.StateStoreError as error:
        _note_failure('db_doc_mirror_file', error.__cause__ or error)
        return False


# ═══════════════════ 一次性迁移 ═══════════════════

# 旧入口兼容清单；实际一次迁移由 state_store 处理，包含 DATA_DIR 的 gov_auto。
_DOC_FILES = (
    ('wash_residuals', 'wash_residuals.json'),
    ('gov_latest', 'gov_latest.json'),
    ('manual_done', 'manual_done.json'),
)


@_observed
def db_migrate():
    try:
        from . import state_store, logger
    except ImportError:
        import state_store, logger
    count = state_store.migrate(_state_dir())
    return {'documents': count, 'audit': logger.migrate_legacy()}
