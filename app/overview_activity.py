"""Bounded dashboard activity from persisted facts; never poll external services."""
import copy
import threading
import time
from collections import defaultdict
from . import config, engine, morning, state_store, subscribe

_lock = threading.RLock()
_cache = {'key': None, 'data': None}


def _number(value):
    try:
        return max(0, int(value)) if value is not None else None
    except (TypeError, ValueError):
        return None


def _poster(row):
    value = row.get('poster_tmdb') or (row.get('tmdb_info') or {}).get('poster')
    if value:
        if str(value).startswith('/api/tmdb/poster/'):
            return str(value)
        return '/api/tmdb/poster/' + str(value).lstrip('/')
    if row.get('has_image') and row.get('id'):
        return '/api/emby/poster/' + str(row['id'])
    return ''


def _index(cache):
    names = defaultdict(list)
    for kind, field in (('tv', 'series'), ('movie', 'movies')):
        for row in cache.get(field) or []:
            names[(kind, str(row.get('name') or ''))].append(row)
    return names


def _seed(names, kind, title, tid=''):
    rows = names.get((kind, str(title)), [])
    if tid:
        rows = [row for row in rows if str(row.get('tmdb_id') or '') == str(tid)]
    identities = {str(row.get('tmdb_id') or '') for row in rows}
    known = bool(rows and len(identities) == 1 and '' not in identities)
    row = rows[0] if known else {}
    return {'title': str(title or '')[:200], 'media_type': kind,
            'tmdb_id': str(row.get('tmdb_id') or tid or ''), 'year': row.get('year'),
            'poster': _poster(row), 'identity_known': known or bool(tid)}


def _ingest_ranges(episodes):
    # The subscription formatter intentionally excludes S00; ingest also
    # includes specials, so retain their exact season without changing it.
    groups = defaultdict(set)
    for season, episode in episodes:
        groups[season].add(episode)
    parts = []
    for season, values in sorted(groups.items()):
        start = last = None
        for episode in sorted(values) + [None]:
            if start is not None and episode != last + 1:
                parts.append(f'S{season:02d}E{start:02d}' +
                             (f'–E{last:02d}' if last != start else ''))
                start = None
            if episode is not None:
                if start is None:
                    start = episode
                last = episode
    return ' · '.join(parts)


def _recent(ingest, names, now):
    rows = []
    tree = ingest.get('tree') or {}
    for library, categories in (tree.get('tv_detail') or {}).items():
        for category, shows in categories.items():
            for title, info in shows.items():
                ts = float(info.get('last_ts') or 0)
                if ts and ts < now - 86400:
                    continue
                episodes = set()
                for season, detail in (info.get('seasons') or {}).items():
                    if str(season).isdigit():
                        episodes.update((int(season), int(ep)) for ep in detail.get('episodes') or []
                                        if str(ep).isdigit() and int(ep) > 0)
                rows.append(dict(_seed(names, 'tv', title), library=library, category=category,
                                 ts=ts, count=_number(info.get('count')),
                                 episodes=_ingest_ranges(episodes) if episodes else '',
                                 episode_count=len(episodes) if episodes else None))
    for library, categories in (tree.get('mov_detail') or {}).items():
        for category, entries in categories.items():
            for title, ts in entries.items():
                ts = float(ts or 0)
                if ts and ts < now - 86400:
                    continue
                rows.append(dict(_seed(names, 'movie', title), library=library,
                                 category=category, ts=ts, count=1, episodes=''))
    return sorted(rows, key=lambda row: row['ts'], reverse=True)[:6]


def build(health, cache, ingest, subscriptions, report, state, notification, morning_cfg, now):
    """Pure projection. Unknown identity/season/count remains unknown."""
    names = _index(cache)
    following = []
    for update in (report or {}).get('updates') or []:
        seed = _seed(names, 'tv', update.get('name'), update.get('tmdb_id'))
        seed['poster'] = seed['poster'] or update.get('poster') or ''
        labels = []
        for field, label in (('new_eps', '新增'), ('refilled', '补齐'), ('newly_missing', '新增缺集')):
            if update.get(field):
                labels.append(label + ' ' + engine._fmt_ep_ranges(engine._keys_to_eps(update[field])))
        if labels:
            following.append(dict(seed, detail=' · '.join(labels), ts=report.get('ts')))
    watching = []
    for sub in subscriptions:
        if not sub.get('enabled', True):
            continue
        sid = str(sub.get('id') or sub.get('tmdb_id') or sub.get('name') or '')
        seed = _seed(names, 'tv', sub.get('name'), sub.get('tmdb_id'))
        seed['poster'] = seed['poster'] or sub.get('poster') or ''
        history = state.get(sid) or {}
        watching.append(dict(seed, detail=('最近通知 ' + str(history['latest_ep'])
                                           if history.get('latest_ep') else '尚无通知记录')))
    missing = [dict(_seed(names, 'tv', row.get('name')), diff=_number(row.get('diff')),
                    have=_number(row.get('have')), total=_number(row.get('tot')))
               for row in (health or {}).get('top_missing') or []]
    return {'status': 'success', 'recent': _recent(ingest or {}, names, now),
            'ingest_ts': (ingest or {}).get('ts'),
            'ingest_error': bool((ingest or {}).get('stale_error') or (ingest or {}).get('ok') is False),
            'following': following[:3], 'following_ts': (report or {}).get('ts'),
            'watching': watching[:2], 'missing': missing[:3],
            'missing_stale': bool((health or {}).get('stale')),
            'missing_ts': (health or {}).get('ts'),
            'subscriptions': {'total': len(subscriptions),
                              'enabled': sum(bool(sub.get('enabled', True)) for sub in subscriptions),
                              'pending': _number((notification or {}).get('pending'))},
            'morning': {'enabled': morning_cfg.get('enabled'),
                        'time': f"{morning_cfg.get('hour', 9):02d}:{morning_cfg.get('minute', 0):02d}",
                        'last_date': morning_cfg.get('last_date') or ''}}


def snapshot(health=None):
    paths = [engine.EMBY_LIB_CACHE_FILE, engine.INGEST_CACHE_FILE, config.CONFIG_FILE,
             engine.SUB_STATE_FILE, engine.STATE_DIR / 'subscription_report.json',
             subscribe._notification_file().with_name(subscribe._notification_file().stem + '_status.json')]
    now = time.time()
    key = (engine.EMBY_HOST, tuple(state_store.stamp(path) for path in paths),
           (health or {}).get('facts_version'), (health or {}).get('stale'),
           (health or {}).get('ts'), repr((health or {}).get('top_missing')))
    with _lock:
        if key == _cache['key']:
            result = copy.deepcopy(_cache['data'])
            result['recent'] = [row for row in result['recent'] if not row['ts'] or row['ts'] >= now - 86400]
            return result
        result = build(health or {}, morning.read_emby_lib_cache(max_age=None) or {},
                       engine.read_ingest_cache() or {}, config.get_subscriptions(),
                       engine._load_subscription_report() or {}, engine._load_sub_state(),
                       engine.load_subscription_notification_status(), config.get_morning_report(), now)
        _cache.update(key=key, data=result)
        return copy.deepcopy(result)
