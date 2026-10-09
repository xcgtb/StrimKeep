"""Bounded in-memory storage diagnostics; never persist payloads or exception text."""
import copy
import errno
import logging
import sqlite3
import threading
import time
from collections import OrderedDict
from pathlib import Path

_LOCK = threading.RLock()
_STATES = OrderedDict()
MAX_STORES = 16
MAX_ISSUES = 64
LOG_INTERVAL = 300
_LOG = logging.getLogger('strimkeep.storage')


def _reason(error):
    if getattr(error, 'errno', None) in (errno.ENOSPC, errno.EDQUOT):
        return 'full', '存储空间或配额不足'
    if isinstance(error, PermissionError) or getattr(error, 'errno', None) in (errno.EACCES, errno.EPERM, errno.EROFS):
        return 'permission', '存储目录不可写或无访问权限'
    code = getattr(error, 'sqlite_errorcode', 0) or 0
    code &= 255
    if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
        return 'busy', '数据库正忙，稍后重试'
    if code == sqlite3.SQLITE_FULL:
        return 'full', '数据库存储空间不足'
    if code == sqlite3.SQLITE_READONLY:
        return 'permission', '数据库或其目录为只读，请检查挂载和权限'
    if code == sqlite3.SQLITE_CANTOPEN:
        return 'open', '无法打开数据库，请检查数据目录及挂载'
    if code == sqlite3.SQLITE_IOERR:
        return 'io', '数据库底层读写失败，请检查磁盘或挂载'
    if code in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB):
        return 'corrupt', '数据库无法解析，请检查数据文件'
    if isinstance(error, (ValueError, TypeError)):
        return 'invalid', '存储内容无法解析'
    if isinstance(error, sqlite3.Error):
        name = getattr(error, 'sqlite_errorname', '')
        return 'database', '数据库操作失败' + ('（%s）' % name if name.startswith('SQLITE_') else '')
    return 'io', '存储读写失败'


def record_failure(state_dir, operation, error):
    """Only operation identifiers and classified reasons enter diagnostics/logs."""
    now = time.time()
    key = str(state_dir)
    code, message = _reason(error)
    with _LOCK:
        state = _STATES.setdefault(key, {'issues': OrderedDict(), 'last_recovered_at': None})
        _STATES.move_to_end(key)
        while len(_STATES) > MAX_STORES:
            _STATES.popitem(last=False)
        issues = state['issues']
        old = issues.get(operation)
        issue = {'operation': operation, 'code': code, 'message': message,
                 'first_at': old['first_at'] if old else now,
                 'last_at': now, 'count': (old['count'] if old else 0) + 1,
                 '_logged_at': old['_logged_at'] if old else 0}
        if not old or old['code'] != code or now - issue['_logged_at'] >= LOG_INTERVAL:
            _LOG.warning('存储异常 [%s]: %s', operation, message)
            issue['_logged_at'] = now
        issues[operation] = issue
        issues.move_to_end(operation)
        while len(issues) > MAX_ISSUES:
            issues.popitem(last=False)


def record_success(state_dir, operation):
    with _LOCK:
        state = _STATES.get(str(state_dir))
        if state and state['issues'].pop(operation, None) is not None:
            state['last_recovered_at'] = time.time()


def snapshot(state_dir):
    """No filesystem probes, database queries, writes or full library scans."""
    with _LOCK:
        state = _STATES.get(str(state_dir), {})
        issues = [{k: v for k, v in issue.items() if not k.startswith('_')}
                  for issue in state.get('issues', {}).values()]
        return {'status': 'degraded' if issues else 'no_observed_error',
                'issues': copy.deepcopy(issues),
                'last_recovered_at': state.get('last_recovered_at'),
                'scope': 'current_process', 'checked_at': time.time()}


def write_json(path, value, operation, state_dir):
    from app import state_store
    return state_store.save(path, value)
