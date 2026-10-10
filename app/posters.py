# -*- coding: utf-8 -*-
"""Fixed TMDB CDN forwarding with bounded in-memory image cache."""
import re
import threading
import time
from collections import OrderedDict
from concurrent.futures import Future

from . import network

_cache = OrderedDict()
_inflight = {}  # filename -> Future: one outbound CDN request per identical poster
_lock = threading.Lock()
_MAX_IMAGE = 2 * 1024 * 1024
_MAX_CACHE = 16 * 1024 * 1024


def get_tmdb_poster(filename):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,120}\.(?:jpg|jpeg|png|webp)', filename):
        raise ValueError('海报路径无效')
    with _lock:
        hit = _cache.get(filename)
        if hit and time.time() - hit[0] < 86400:
            _cache.move_to_end(filename)
            return hit[1], hit[2]
        flight = _inflight.get(filename)
        leader = flight is None
        if leader:
            flight = Future()
            _inflight[filename] = flight
    if not leader:
        # Other cards requesting the same image reuse the leader's bytes/failure.
        # No lock is held while waiting for the network request.
        return flight.result()
    try:
        with network.open_external('https://image.tmdb.org/t/p/w500/' + filename, timeout=10) as response:
            content_type = response.headers.get('Content-Type', '').split(';')[0].strip().lower()
            if content_type not in ('image/jpeg', 'image/png', 'image/webp'):
                raise ValueError('海报服务返回非图片内容')
            data = response.read(_MAX_IMAGE + 1)
        if len(data) > _MAX_IMAGE:
            raise ValueError('海报大小超过限制')
        with _lock:
            _cache[filename] = (time.time(), data, content_type)
            _cache.move_to_end(filename)
            total = sum(len(item[1]) for item in _cache.values())
            while total > _MAX_CACHE or len(_cache) > 128:
                total -= len(_cache.popitem(last=False)[1][1])
        result = data, content_type
        flight.set_result(result)
        return result
    except BaseException as error:
        flight.set_exception(error)
        raise
    finally:
        with _lock:
            if _inflight.get(filename) is flight:
                _inflight.pop(filename, None)
