# -*- coding: utf-8 -*-
"""Read-only, bounded, source-revision keyed Mapping pagination.

The authoritative Emby/TMDB facts still come from cached_library_view(). Every
filter is evaluated before offset/limit, so counts and sorting are global. The
legacy whole-library endpoint is unchanged for other clients.
"""
import threading
import time
from collections import OrderedDict
from functools import cmp_to_key
from . import engine, library_response, morning
from .library_first_screen import _series_cmp, _series_card, _movie_card, source_labels

_lock = threading.RLock()
_cache = {'key': None, 'until': 0., 'series': (), 'movies': (), 'stats': {},
          'manual_done': {}, 'meta': {}, 'filters': OrderedDict(), 'series_by_id': {}, 'movies_by_id': {}}
_MAX_FILTERS = 8
_TTL = 60
_CATEGORY = {
    '儿童节目': ('儿童',), '演唱会': ('演唱会',), '综艺剧': ('综艺',),
    '动漫剧': ('动漫',), '动画电影': ('动画电影',), '纪录片': ('纪录',),
    '国产剧': ('国产剧集', '国产剧'),
    '欧美剧': ('欧美剧集', '欧美剧', '其他剧集'),
    '日韩剧': ('日韩剧集', '日韩剧'),
    '华语电影': ('国产电影', '华语电影'),
    '外语电影': ('欧美电影', '日韩电影', '外语电影', '其他电影'),
}
_EXCLUDE = {'name', 'title', 'overview', 'desc', 'tmdb_info', 'id'}


def _category_of(row):
    # Same first-matching-segment order as static/js/mapping.js CAT_KW.
    for segment in str(row.get('path') or '').replace('\\', '/').split('/'):
        for label, keywords in _CATEGORY.items():
            if any(word in segment for word in keywords):
                return label
    return ''


def _category_match(row, category, mode):
    if not category:
        return True
    if category in _CATEGORY:
        return _category_of(row) == category
    if mode == 'sub':
        fields = []
        for key, value in row.items():
            if key in _EXCLUDE:
                continue
            if isinstance(value, str):
                fields.append(value)
            elif isinstance(value, list):
                fields.extend(part for part in value if isinstance(part, str))
        return category in '|'.join(fields)
    clean = lambda s: ''.join(c for c in s if ('\u4e00' <= c <= '\u9fa5') or ('A' <= c <= 'Z') or ('a' <= c <= 'z') or ('0' <= c <= '9'))
    return any(clean(part) == category for part in str(row.get('path') or '').replace('\\', '/').split('/'))


def _read():
    """Single catalogue per facts revision; discard if change races snapshot."""
    now = time.monotonic()
    key = library_response._key()
    if _cache['key'] == key and now < _cache['until']:
        return True
    # A webhook may commit a new source revision while we read. Retry once
    # against a fresh revision instead of falsely reporting a missing cache.
    facts = None
    for attempt in range(2):
        key = library_response._key()
        candidate = engine.cached_library_view()
        if candidate is not None and key == library_response._key():
            facts = candidate
            break
        if candidate is None:
            break
    if facts is None:
        _cache.update(key=None, until=0)
        return False
    series = tuple(sorted(facts.get('series') or (), key=cmp_to_key(_series_cmp)))
    movies = tuple(facts.get('movies') or ())
    _cache.update(key=key, until=now + _TTL, series=series, movies=movies,
                  stats=dict(facts.get('stats') or {}), manual_done=morning.read_manual_done(),
                  meta={k: facts.get(k) for k in ('facts_version', 'facts_ts', 'stale', 'source', 'ts')},
                  series_by_id={str(row['id']): row for row in series if row.get('id')},
                  movies_by_id={str(row['id']): row for row in movies if row.get('id')},
                  filters=OrderedDict())
    return True


def _filtered(media, status, scope, category, category_mode, keyword):
    key = (media, status, scope, category, category_mode, keyword)
    filters = _cache['filters']
    if key in filters:
        filters.move_to_end(key)
        return filters[key]
    rows = _cache['series'] if media == 'series' else _cache['movies']
    result = []
    for row in rows:
        if media == 'series' and status != 'all':
            state = (row.get('tmdb_info') or {}).get('match_status') or 'unmatched'
            if status == 'unmatched':
                if state not in ('unmatched', 'no_tmdb'):
                    continue
            elif state != status:
                continue
        if scope == 'local' and not row.get('in_local'):
            continue
        if scope == 'share' and not row.get('in_share'):
            continue
        if category and not _category_match(row, category, category_mode):
            continue
        if keyword and keyword not in str(row.get('name') or '').lower():
            continue
        result.append(row)
    result = tuple(result)
    filters[key] = result
    if len(filters) > _MAX_FILTERS:
        filters.popitem(last=False)
    return result


def page(*, media='series', status='all', scope='all', category='', category_mode='seg', keyword='', offset=0, limit=40):
    if (media not in ('series', 'movies') or status not in ('all', 'aligned', 'missing', 'extra', 'ongoing', 'unmatched', 'no_tmdb', 'pending')
            or scope not in ('all', 'local', 'share') or category_mode not in ('seg', 'sub')
            or len(category) > 80 or len(keyword) > 120 or not 0 <= offset <= 500000 or not 1 <= limit <= 80):
        raise ValueError('无效片库分页参数')
    keyword = keyword.strip().lower()
    with _lock:
        if not _read():
            return {'status': 'nocache'}
        rows = _filtered(media, status, scope, category, category_mode, keyword)
        selected = rows[offset:offset + limit]
        project = _series_card if media == 'series' else _movie_card
        # Read-only card projection never leaks STRM paths, filenames or secret tokens.
        return dict(_cache['meta'], status='success', media=media, offset=offset, limit=limit,
                    total=len(rows), has_more=offset + len(selected) < len(rows),
                    next_offset=offset + len(selected), rows=[project(row) for row in selected],
                    stats=dict(_cache['stats']), manual_done=dict(_cache['manual_done']),
                    emby_host=engine.EMBY_HOST)


def detail(media, item_id):
    if media not in ('series', 'movies') or not item_id or len(item_id) > 160:
        raise ValueError('无效片库条目')
    with _lock:
        if not _read():
            return {'status': 'nocache'}
        collection = _cache['series_by_id'] if media == 'series' else _cache['movies_by_id']
        row = collection.get(item_id)
        if row is None:
            return {'status': 'notfound'}
        item = dict(row, source_labels=source_labels(row))
        if media == 'movies':
            paths = set(item.get('paths') or ([item['path']] if item.get('path') else []))
            item['local_versions'] = sum(engine.EMBY_PATHS.lib_of(p, fallback=False) == 'local' for p in paths)
            item['share_versions'] = sum(engine.EMBY_PATHS.lib_of(p, fallback=False) == 'share' for p in paths)
        return {'status': 'success', 'media': media, 'item': item, **_cache['meta']}
