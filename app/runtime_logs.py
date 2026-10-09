"""Bounded SQLite log stream; a small RAM queue remains readable during DB faults."""
import logging
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
_LOCK = threading.RLock()
_SEQ = 0


def redact(message):
    text = re.sub(r'(https?://api\.telegram\.org/bot)[^/\s]+', r'\1[hidden]', str(message))
    return re.sub(r'(?i)((?:api[_-]?key|token|password|emby_key|tmdb_key)=)[^\s&]+', r'\1[hidden]', text)[:8000]


def _insert(con, row):
    return con.execute('INSERT INTO runtime_logs(ts,level,source,message) VALUES(?,?,?,?)',
                       (row['ts'], row['level'], row['source'], row['message'])).lastrowid


class RuntimeHandler(logging.Handler):
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
            if record.exc_info: message += '\n' + type(record.exc_info[1]).__name__
            row = {'ts': datetime.fromtimestamp(record.created).isoformat(timespec='seconds'),
                   'level': record.levelname,
                   'source': record.module if record.name == 'strimkeep' else record.name.rsplit('.', 1)[-1],
                   'message': redact(message)}
            with _LOCK:
                try:
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


def read(after=0, limit=200):
    limit, after = max(1, min(500, int(limit))), max(0, int(after))
    with _LOCK:
        try:
            with storage._transaction() as con:
                if after:
                    rows = con.execute('SELECT id,ts,level,source,message FROM runtime_logs WHERE id>? ORDER BY id LIMIT ?', (after, limit)).fetchall()
                else:
                    rows = con.execute('SELECT id,ts,level,source,message FROM runtime_logs ORDER BY id DESC LIMIT ?', (limit,)).fetchall()[::-1]
            storage._status.record_success(storage._state_dir(), 'runtime_logs_read')
            return {'logs': [dict(zip(('id','ts','level','source','message'), r)) for r in rows],
                    'cursor': max([after] + [r[0] for r in rows]), 'degraded': False}
        except Exception as error:
            storage._status.record_failure(storage._state_dir(), 'runtime_logs_read', error)
            return {'logs': list(_PENDING), 'cursor': after, 'degraded': True}


def text(limit=MAX_LOGS):
    with storage._transaction() as con:
        rows = con.execute('SELECT ts,level,source,message FROM runtime_logs ORDER BY id DESC LIMIT ?', (max(1, min(MAX_LOGS, int(limit))),)).fetchall()[::-1]
    return '\n'.join('[%s] [%s] [%s] %s' % r for r in rows)
