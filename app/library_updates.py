"""Small, bounded SQLite event journal for library UI invalidation.

Only completed authoritative cache changes are published. The journal contains IDs,
never media paths, credentials or large snapshots. Consumers falling behind reconcile.
"""
import threading
import time
from . import state_store

_LOCK = threading.RLock()
_HISTORY = 64
_MAX_IDS = 100


def _path():
    from . import engine
    return engine.STATE_DIR / 'library_ui_updates.json'


def publish(series_ids=(), *, movie_ids=(), full=False):
    ids = sorted({str(i) for i in series_ids if i})
    movies = sorted({str(i) for i in movie_ids if i})
    if len(ids) + len(movies) > _MAX_IDS:
        full = True
        ids = []
        movies = []
    with _LOCK:
        previous = state_store.read(_path(), {})
        if not isinstance(previous, dict):
            previous = {}
        version = int(previous.get('version') or 0) + 1
        history = list(previous.get('history') or [])[-(_HISTORY - 1):]
        history.append({'version': version, 'ids': [] if full else ids,
                        'movie_ids': [] if full else movies, 'full': bool(full)})
        state_store.save(_path(), {'version': version, 'ts': time.time(), 'history': history})
        return version


def changes(since):
    data = state_store.read(_path(), {})
    version = int(data.get('version') or 0) if isinstance(data, dict) else 0
    if since == version:
        return {'version': version, 'full': False, 'ids': [], 'movie_ids': []}
    history = (data.get('history') or []) if isinstance(data, dict) else []
    rows = [r for r in history if int(r.get('version') or 0) > since]
    # Never claim a delta if the client skipped a journal entry, has a future
    # revision, or is starting with an unknown revision.
    if (since < 0 or since > version or not rows or
            int(rows[0].get('version') or 0) != since + 1 or
            len(rows) != version - since or any(r.get('full') for r in rows)):
        return {'version': version, 'full': True, 'ids': [], 'movie_ids': []}
    ids = sorted({str(i) for r in rows for i in r.get('ids', []) if i})
    movies = sorted({str(i) for r in rows for i in r.get('movie_ids', []) if i})
    if len(ids) + len(movies) > _MAX_IDS:
        return {'version': version, 'full': True, 'ids': [], 'movie_ids': []}
    return {'version': version, 'full': False, 'ids': ids, 'movie_ids': movies}
