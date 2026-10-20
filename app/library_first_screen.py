# -*- coding: utf-8 -*-
"""Bounded, read-only first-screen projection for mapping's perceived load time.

The regular /api/emby/library remains authoritative for filters, details and
mutations. This endpoint never performs media scans or contacts Emby/TMDB.
"""
import threading
import heapq
import time
from functools import cmp_to_key
from . import engine, library_response

_lock = threading.RLock()
_cache = {'key': None, 'expires': 0.0, 'data': None}
_LIMIT = 48


def _series_cmp(a, b):
    """Mirror Mapping's status/difference priority, stable within a status."""
    prio = {'missing': 0, 'extra': 1, 'unmatched': 2,
            'no_tmdb': 2, 'ongoing': 3, 'aligned': 4}
    ai, bi = (a.get('tmdb_info') or {}), (b.get('tmdb_info') or {})
    aa, bb = prio.get(ai.get('match_status') or 'unmatched', 9), prio.get(bi.get('match_status') or 'unmatched', 9)
    if aa != bb:
        return -1 if aa < bb else 1
    def gap(value):
        try:
            return abs(int(value or 0))
        except (TypeError, ValueError, OverflowError):
            return 0
    ad, bd = gap(ai.get('diff')), gap(bi.get('diff'))
    if ad != bd:
        return -1 if ad > bd else 1
    # JS's localeCompare may choose a different order for some CJK titles.
    # The authoritative full load will reconcile those cases.
    an, bn = str(a.get('name') or '').casefold(), str(b.get('name') or '').casefold()
    return (an > bn) - (an < bn)


def source_labels(row):
    from .domain.media_sources import labels
    return labels(row.get('paths') or [row.get('path')], engine.EMBY_PATHS.entries())


def _series_card(src):
    info = src.get('tmdb_info') or {}
    return {key: src.get(key) for key in (
        'id', 'name', 'year', 'tmdb_id', 'has_image', 'have_eps',
        'total_episodes', 'total_seasons', 'local_eps', 'share_eps',
        'in_local', 'in_share', '_md')} | {'tmdb_info': {
            k: info.get(k) for k in ('poster', 'match_status', 'diff', 'declared_total', 'tmdb_total')
        }, 'source_labels': source_labels(src)}


def _movie_card(src):
    return {key: src.get(key) for key in (
        'id', 'name', 'year', 'tmdb_id', 'has_image',
        'poster_tmdb', 'in_local', 'in_share')} | {'source_labels': source_labels(src)}


def preview():
    """Return at most 48 cards per media type, with no raw disk/file paths."""
    # Same stamp key as full mapping response: updates to manual flags and
    # authoritative library source invalidate in the same request.
    with _lock:
        key = library_response._key()
        if key == _cache['key'] and _cache['data'] is not None and time.time() < _cache['expires']:
            return _cache['data']
        facts = engine.cached_library_view()
        if facts is None:
            _cache.update(key=None, expires=0, data=None)
            return {'status': 'nocache'}
        # Sorting is server-local and only once per source revision, not per view.
        ordered = heapq.nsmallest(_LIMIT, facts.get('series') or [], key=cmp_to_key(_series_cmp))
        result = {'status': 'success', 'preview': True,
                  'series': [_series_card(row) for row in ordered[:_LIMIT]],
                  'movies': [_movie_card(row) for row in (facts.get('movies') or [])[:_LIMIT]],
                  'stats': dict(facts.get('stats') or {}),
                  'facts_version': facts.get('facts_version'),
                  'facts_ts': facts.get('facts_ts'),
                  'stale': bool(facts.get('stale', True))}
        # Do not memoize a response built across a concurrent data change.
        if key == library_response._key():
            _cache.update(key=key, expires=time.time() + 60, data=result)
        else:
            _cache.update(key=None, expires=0, data=None)
        return result
