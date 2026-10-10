"""Bounded encoded mapping responses; no network or media scans on cache reads."""
import gzip
import json
import threading
import time

from fastapi.responses import Response
from . import engine, state_store, morning

_lock = threading.RLock()
_cache = {'key': None, 'expires': 0, 'plain': None, 'gzip': None}
_MAX_BYTES = 32 * 1024 * 1024


def _key():
    return (tuple(state_store.stamp(p) for p in
                  (engine.LIBRARY_SNAPSHOT_FILE, engine.EMBY_LIB_CACHE_FILE, engine.MANUAL_DONE_FILE)),
            engine.EMBY_HOST, id(engine.cached_library_view), id(morning.read_manual_done))


def _gzip_accepted(encoding):
    for part in encoding.lower().split(','):
        pieces = part.strip().split(';')
        if pieces[0] != 'gzip':
            continue
        quality = 1.0
        for parameter in pieces[1:]:
            if parameter.strip().startswith('q='):
                try:
                    quality = float(parameter.strip()[2:])
                except ValueError:
                    quality = 0.0
        return quality > 0
    return False


def _response(plain, packed, encoding):
    use_gzip = packed is not None and _gzip_accepted(encoding)
    headers = {'Cache-Control': 'private, no-store', 'Vary': 'Accept-Encoding, Authorization'}
    if use_gzip:
        headers['Content-Encoding'] = 'gzip'
    return Response(packed if use_gzip else plain, media_type='application/json', headers=headers)


def cached_mapping_response(encoding=''):
    """Reuse one current body; edits invalidate immediately, age fields expire in 15s."""
    with _lock:
        key = _key()
        now = time.time()
        if _cache['key'] == key and now < _cache['expires']:
            return _response(_cache['plain'], _cache['gzip'], encoding)
        data = engine.cached_library_view()
        if data is None:
            _cache.update(key=None, expires=0, plain=None, gzip=None)
            return None
        data = dict(data, manual_done=morning.read_manual_done())
        plain = json.dumps(data, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
        packed = gzip.compress(plain, compresslevel=1, mtime=0) if len(plain) >= 1024 else None
        # Do not retain an obsolete document or keep arbitrarily large full-library bodies.
        if key == _key() and len(plain) + len(packed or b'') <= _MAX_BYTES:
            lifetime = 15.0
            ts = float(data.get('ts') or 0)
            if ts and not data.get('stale'):
                lifetime = min(lifetime, max(0.01, ts + 1800 - time.time()))
            _cache.update(key=key, expires=time.time() + lifetime, plain=plain, gzip=packed)
        else:
            _cache.update(key=None, expires=0, plain=None, gzip=None)
        return _response(plain, packed, encoding)
