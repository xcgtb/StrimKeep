"""Single-flight cached Emby continue-watching snapshot for the dashboard."""
import hashlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from . import engine, state_store, storage

_LOCK = threading.RLock()
_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix='playback-overview')
_FUTURE = None
_RETRY = 0


def _path():
    return storage._diagnostic_dir() / 'playback_overview.json'


def _identity():
    return hashlib.sha256((str(engine.EMBY_HOST) + '\0' + str(engine.EMBY_KEY)).encode()).hexdigest()


def _fetch(identity):
    global _RETRY
    try:
        # Existing backend contract returns continue-watching facts, not an
        # invented duration/count derived from HTTP playback requests.
        result = engine.ACTIONS['played'](type('Args', (), {})())
        if result.get('status') != 'success':
            raise ValueError('playback facts unavailable')
        from . import overview_activity, morning
        names = overview_activity._index(morning.read_emby_lib_cache(max_age=None) or {})
        records = []
        for row in result.get('records', [])[:30]:
            seed = overview_activity._seed(names, row['type'], row['name'])
            records.append({**seed, **{k:row.get(k) for k in ('user','name','type','pct','s','e','text')}})
        with _LOCK:
            if identity == _identity():
                state_store.save(_path(), {'identity':identity, 'ts':time.time(), 'records':records,
                                          'alerts':result.get('alerts', [])[:30]})
    except Exception:
        with _LOCK:
            _RETRY = time.time() + 60
        # Exception strings can contain provider tokens. Keep a plain reason.
        return 'Emby 播放记录读取失败，保留上次快照；请核对连接。'
    return ''


def snapshot(force=False):
    global _FUTURE, _RETRY
    with _LOCK:
        identity = _identity()
        data = state_store.read(_path(), {})
        if data.get('identity') != identity:
            data = {}
        if not engine.EMBY_HOST or not engine.EMBY_KEY:
            return {'records':[], 'alerts':[], 'configured':False, 'refreshing':False,
                    'ts':None, 'error':'请先配置 Emby 连接，再读取续播记录。'}
        busy = _FUTURE is not None and not _FUTURE.done()
        error = _FUTURE.result() if _FUTURE is not None and _FUTURE.done() else ''
        stale = time.time() - data.get('ts', 0) >= 60
        if not busy and (force or stale) and time.time() >= _RETRY:
            _FUTURE = _POOL.submit(_fetch, identity)
            busy = True
        return {**{k:data.get(k, []) for k in ('records','alerts')}, 'ts':data.get('ts'),
                'configured':True, 'refreshing':busy, 'stale':stale, 'error':error or ''}
