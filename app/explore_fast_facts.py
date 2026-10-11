# -*- coding: utf-8 -*-
"""Private per-revision TMDB TV lookup for Explore.

The authoritative snapshot still comes from engine.cached_library_view().
Only a small, read-only projection is retained; no paths, secrets, or episode
file names are cached here. File stamps and library events invalidate it.
"""
import threading
import time
from collections import defaultdict

_lock = threading.RLock()
_snapshot = {'key': None, 'created': 0., 'view': None}


def _revision(engine):
    try:
        from . import state_store, morning
    except ImportError:  # direct engine/CLI compatibility
        import state_store, morning
    if engine.cached_library_view is not morning.cached_library_view:
        raise AttributeError('provider has no persisted revision contract')
    return (
        tuple(state_store.stamp(path) for path in (
            engine.LIBRARY_SNAPSHOT_FILE,
            engine.EMBY_LIB_CACHE_FILE,
            engine.MANUAL_DONE_FILE,
            engine.STATE_DIR / 'library_ui_updates.json',
        )),
        id(engine.cached_library_view),
        id(engine.load_library_snapshot),
        id(morning.read_emby_lib_cache),
        id(morning.read_manual_done),
    )


def _project(facts):
    if not facts:
        return {'rows': {}, 'facts_version': None, 'facts_ts': None,
                'source_ts': None, 'original_stale': True}
    rows = defaultdict(list)
    for row in facts.get('series') or []:
        tid = str(row.get('tmdb_id') or '')
        if not tid:
            continue
        info = row.get('tmdb_info') or {}
        # Retain *all* matching rows to preserve the ambiguous-id guard.
        rows[tid].append({
            'id': row.get('id'),
            'have_eps': row.get('have_eps'),
            'total_episodes': row.get('total_episodes'),
            'local_eps': row.get('local_eps'),
            'share_eps': row.get('share_eps'),
            'in_local': row.get('in_local'),
            'in_share': row.get('in_share'),
            '_md': bool(row.get('_md')),
            'tmdb_info': {
                'declared_total': info.get('declared_total'),
                'tmdb_total': info.get('tmdb_total'),
                'match_status': info.get('match_status', 'unmatched'),
            },
        })
    return {'rows': dict(rows), 'facts_version': facts.get('facts_version'),
            'facts_ts': facts.get('facts_ts'),
            'source_ts': facts.get('ts'),
            'original_stale': facts.get('stale', True)}


def lookup(engine=None):
    """Return a current read-only projection, without re-copying the whole library.

    In normal operation the revision changes whenever a disk snapshot/manual
    flag or published library event changes. An untracked/test engine uses a
    no-cache fallback. Negative lookups are briefly retried for cold startup.
    """
    if engine is None:
        try:
            from . import engine
        except ImportError:  # direct engine/CLI compatibility
            import engine
    try:
        key = _revision(engine)
    except (AttributeError, KeyError, OSError, TypeError, ValueError):
        return _current(_project(engine.cached_library_view()))
    with _lock:
        now = time.time()
        if (_snapshot['key'] != key or _snapshot['view'] is None or
                (not _snapshot['view']['source_ts'] and now - _snapshot['created'] >= 5)):
            view = _project(engine.cached_library_view())
            # Do not retain a snapshot keyed to files that changed while read.
            try:
                stable = _revision(engine) == key
            except (AttributeError, KeyError, OSError, TypeError, ValueError):
                stable = False
            _snapshot.update(key=key if stable else None, created=now,
                             view=view if stable else None)
            return _current(view)
        return _current(_snapshot['view'])


def _current(view):
    # Do not freeze 'stale' when the index was first constructed.
    ts = view['source_ts']
    try:
        stale = time.time() - float(ts) > 1800 if ts else bool(view['original_stale'])
    except (TypeError, ValueError):
        stale = True
    return {'rows': view['rows'], 'facts_version': view['facts_version'],
            'facts_ts': view['facts_ts'], 'stale': stale}
