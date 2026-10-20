"""Bounded SQLite log stream; a small RAM queue remains readable during DB faults."""
import logging
import json
import re
import threading
from collections import deque
from datetime import datetime
if __package__:
    from . import storage
else:
    import storage

MAX_LOGS = 10000
_LOCAL = threading.local()
_PENDING = deque(maxlen=200)
# Log readers and handlers can run inside a cloud transaction. A separate
# log lock would invert the transaction -> logging order used by diagnostics.
_LOCK = storage._LOCK
_SEQ = 0


def redact(message):
    text = re.sub(r'(https?://api\.telegram\.org/bot)[^/\s]+', r'\1[hidden]', str(message))
    return re.sub(r'(?i)((?:api[_-]?key|token|password|emby_key|tmdb_key)=)[^\s&]+', r'\1[hidden]', text)[:8000]


def _insert(con, row):
    return con.execute('INSERT INTO runtime_logs(ts,level,source,message,details) VALUES(?,?,?,?,?)',
                       (row['ts'], row['level'], row['source'], row['message'], json.dumps(row.get('details', {}), ensure_ascii=False))).lastrowid


class RuntimeHandler(logging.Handler):
    def createLock(self):
        # logging.Handler normally adds another lock around emit(). It must
        # participate in the same order, including logs emitted inside a DB tx.
        self.lock = storage._LOCK

    def __init__(self, level=logging.INFO):
        super().__init__(level)
        self._strimkeep_runtime_handler = __name__
        self._state_directory = storage._state_dir

    def emit(self, record):
        global _SEQ
        if getattr(_LOCAL, 'writing', False): return
        # Package imports and direct CLI imports can coexist. Each application
        # gets its own stream, but only one handler writes a shared database.
        directory = storage._state_dir()
        for handler in logging.getLogger('strimkeep').handlers:
            if handler is self: break
            if (getattr(handler, '_strimkeep_runtime_handler', False)
                    and handler._state_directory() == directory):
                return
        _LOCAL.writing = True
        try:
            message = record.getMessage()
            details = {}
            if record.exc_info:
                error = record.exc_info[1]
                kind = type(error).__name__
                if kind == 'LibraryScanError':
                    message += '\n' + kind + ': ' + str(error)
                    details = {'exception_type': kind, 'path': redact(getattr(error, 'path', '')),
                               'reason': redact(str(error))}
                else:
                    message += '\n' + kind
            row = {'ts': datetime.fromtimestamp(record.created).isoformat(timespec='seconds'),
                   'level': record.levelname,
                   'source': record.module if record.name == 'strimkeep' else record.name.rsplit('.', 1)[-1],
                   'message': redact(message), 'details': details}
            with _LOCK:
                try:
                    # A log emitted inside a cloud transaction belongs to that
                    # transaction. Opening another context would commit it early.
                    active = storage._conn()
                    if active.in_transaction:
                        _insert(active, row)
                        return
                    with storage._transaction() as con:
                        for pending in _PENDING: _insert(con, pending)
                        rid = _insert(con, row)
                        if rid % 100 == 0: con.execute('DELETE FROM runtime_logs WHERE id <= ?', (rid - MAX_LOGS,))
                    _PENDING.clear()
                    storage._status.record_success(storage._state_dir(), 'runtime_logs_write')
                except Exception as error:
                    _SEQ += 1
                    row['id'] = 'memory-' + str(_SEQ)
                    _PENDING.append(row)
                    storage._status.record_failure(storage._state_dir(), 'runtime_logs_write', error)
        finally:
            _LOCAL.writing = False


def install():
    log = logging.getLogger('strimkeep')
    if not any(getattr(h, '_strimkeep_runtime_handler', None) == __name__ for h in log.handlers):
        handler = RuntimeHandler(logging.INFO)
        log.addHandler(handler)
    log.setLevel(logging.INFO)


def read(after=0, limit=200, source="", query="", level="", task=""):
    limit, after = max(1, min(500, int(limit))), max(0, int(after))
    with _LOCK:
        try:
            with storage._transaction() as con:
                clauses, params = [], []
                if source == 'cloud':
                    clauses.append("substr(source,1,6)='cloud_'")
                elif source:
                    clauses.append('source=?');params.append(source)
                if query:
                    clauses.append('(instr(lower(message),lower(?))>0 OR instr(lower(details),lower(?))>0)');params.extend([str(query)[:200]] * 2)
                if level:
                    clauses.append('level=?');params.append(level)
                if task:
                    clauses.append("(substr(json_extract(details,'$.job_id'),1,?)=? OR instr(message,?)>0)")
                    params.extend([len(task),task,'[任务 '+task])
                if after:
                    clauses.append('id>?');params.append(after)
                where = ' WHERE '+' AND '.join(clauses) if clauses else ''
                order = 'ASC' if after else 'DESC'
                rows=con.execute('SELECT id,ts,level,source,message,details FROM runtime_logs'+where+' ORDER BY id '+order+' LIMIT ?',(*params,limit)).fetchall()
                if not after: rows=rows[::-1]
                cursor=max([after]+[r[0] for r in rows])
                if len(rows)<limit:
                    cursor=max(cursor,con.execute('SELECT COALESCE(MAX(id),0) FROM runtime_logs').fetchone()[0])
            storage._status.record_success(storage._state_dir(), 'runtime_logs_read')
            return {'logs': [{**dict(zip(('id','ts','level','source','message'), r[:5])), 'details':json.loads(r[5])} for r in rows],
                    'cursor': cursor, 'degraded': False}
        except Exception as error:
            storage._status.record_failure(storage._state_dir(), 'runtime_logs_read', error)
            return {'logs': [r for r in _PENDING if (not source or (source=='cloud' and r['source'].startswith('cloud_')) or r['source']==source) and (not query or str(query).casefold() in r['message'].casefold()) and (not level or r['level']==level) and (not task or str(r.get('details',{}).get('job_id','')).startswith(task))], 'cursor': after, 'degraded': True}


def text(limit=MAX_LOGS):
    with storage._transaction() as con:
        rows = con.execute('SELECT ts,level,source,message,details FROM runtime_logs ORDER BY id DESC LIMIT ?', (max(1, min(MAX_LOGS, int(limit))),)).fetchall()[::-1]
    return '\n'.join('[%s] [%s] [%s] %s' % r[:4] + ('\n  '+r[4] if r[4] != '{}' else '') for r in rows)
