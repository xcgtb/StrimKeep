"""Initialization state, independent of database availability and remote APIs."""
import threading
import time

_LOCK = threading.Lock()
_STATE = {'phase': 'not_started', 'ready': False, 'message': '后台服务尚未初始化', 'ts': 0}


def set_state(phase, message=''):
    with _LOCK:
        _STATE.update(phase=phase, ready=phase == 'ready', message=message, ts=time.time())


def snapshot():
    with _LOCK:
        return dict(_STATE)


def failed():
    with _LOCK:
        return _STATE['phase'] == 'failed'
