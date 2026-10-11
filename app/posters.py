# -*- coding: utf-8 -*-
"""Demand-only thumbnails, shared bounded caches and six upstream image slots."""
import hashlib
import re
import threading
import time
import urllib.parse
import urllib.request
from collections import OrderedDict
from concurrent.futures import Future

from . import network, poster_store

_cache = OrderedDict()
_inflight = {}
_lock = threading.Lock()
_upstream_slots = threading.BoundedSemaphore(6)
_MAX_IMAGE = 2 * 1024 * 1024
_MAX_CACHE = 16 * 1024 * 1024
_TTL_TMDB = 86400
_TTL_EMBY = 3600


class PosterBusyError(Exception):
    pass


def _size(size):
    if size not in ('w342', 'w500'):
        raise ValueError('海报尺寸无效')
    return size


def _read_image(response, default_type=''):
    content_type = response.headers.get('Content-Type', default_type).split(';')[0].strip().lower()
    if content_type not in poster_store.CONTENT_TYPES:
        raise ValueError('海报服务返回非图片内容')
    data = response.read(_MAX_IMAGE + 1)
    if not 0 < len(data) <= _MAX_IMAGE:
        raise ValueError('海报为空或大小超过限制')
    return data, content_type


def _remember(key, result, stamp=None):
    with _lock:
        _cache[key] = (time.time() if stamp is None else stamp, *result)
        _cache.move_to_end(key)
        total = sum(len(item[1]) for item in _cache.values())
        while total > _MAX_CACHE or len(_cache) > 128:
            total -= len(_cache.popitem(last=False)[1][1])


def _get(key, loader, ttl):
    with _lock:
        hit = _cache.get(key)
        if hit and 0 <= time.time() - hit[0] < ttl:
            _cache.move_to_end(key)
            return hit[1], hit[2]
        flight = _inflight.get(key)
    if flight is not None:
        return flight.result(timeout=20)
    # Disk hits need no outbound slot, even while six unrelated downloads stall.
    stored = poster_store.read(key, ttl, with_timestamp=True)
    if stored is not None:
        _remember(key, stored[:2], stored[2])
        return stored[:2]
    with _lock:
        hit = _cache.get(key)
        if hit and 0 <= time.time() - hit[0] < ttl:
            _cache.move_to_end(key)
            return hit[1], hit[2]
        flight = _inflight.get(key)
        leader = flight is None
        if leader:
            # Reject overload promptly instead of occupying all API worker threads.
            if len(_inflight) >= 6:
                raise PosterBusyError('海报请求繁忙，请稍后重试')
            flight = Future()
            _inflight[key] = flight
    if not leader:
        # Other cards requesting the same image reuse the leader's bytes/failure.
        # No lock is held while waiting for the network request.
        return flight.result(timeout=20)
    try:
        if not _upstream_slots.acquire(blocking=False):
            raise PosterBusyError('海报请求繁忙，请稍后重试')
        try:
            result = loader()
        finally:
            _upstream_slots.release()
        _remember(key, result)
        # Followers can display without waiting for a best-effort disk write.
        flight.set_result(result)
        poster_store.write(key, *result)
        return result
    except BaseException as error:
        if not flight.done():
            flight.set_exception(error)
        raise
    finally:
        with _lock:
            if _inflight.get(key) is flight:
                _inflight.pop(key, None)


def get_tmdb_poster(filename, size='w500'):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,120}\.(?:jpg|jpeg|png|webp)', filename):
        raise ValueError('海报路径无效')
    size = _size(size)
    key = 'tmdb:' + size + ':' + filename

    def load():
        with network.open_external('https://image.tmdb.org/t/p/' + size + '/' + filename, timeout=10) as response:
            return _read_image(response)
    return _get(key, load, _TTL_TMDB)


def get_emby_poster(item_id, host, token, size='w500'):
    size = _size(size)
    if not item_id or len(item_id) > 120:
        raise ValueError('海报项目无效')
    # Host/key changes cannot reuse another Emby server's image with the same ID.
    scope = hashlib.sha256((host.rstrip('/') + '\0' + token).encode()).hexdigest()
    key = 'emby:' + scope + ':' + size + ':' + item_id
    url = host.rstrip('/') + '/Items/' + urllib.parse.quote(item_id, safe='') + '/Images/Primary?'
    url += urllib.parse.urlencode({'MaxWidth': int(size[1:]), 'Quality': 90})

    def load():
        req = urllib.request.Request(url, headers={'X-Emby-Token': token})
        # Local Emby keeps its original route; never use the external Web proxy.
        with urllib.request.urlopen(req, timeout=15) as response:
            return _read_image(response, 'image/jpeg')
    return _get(key, load, _TTL_EMBY)
