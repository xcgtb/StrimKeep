"""Resource-acquisition subscriptions: cache events drive import notifications.

No Emby/TMDB polling, no media writes. A bounded durable queue is delivered by
the existing scheduler notification worker. Telegram has at-least-once semantics.
"""
import html
import threading
import time
from . import config, state_store

_lock = threading.RLock()
_sender = threading.Lock()


def _path():
    return config.DATA_DIR / 'state' / 'resource_import_notifications.json'


def _load():
    data = state_store.read(_path(), {})
    return data if isinstance(data, dict) else {}


def status():
    with _lock:
        data = _load()
        return {'count': len(data.get('watches', {})), 'pending': len(data.get('pending', [])),
                'items': [{'title': w['title'], 'media_type': w['media'], 'tmdb_id': w['tmdb_id']} for w in data.get('watches', {}).values()]}


def _facts(rows, media, tid):
    matches = [r for r in rows if str(r.get('tmdb_id') or '') == tid]
    if not matches:
        return {'count': 0, 'local': False, 'share': False}
    if len(matches) != 1:
        return None
    row = matches[0]
    if media == 'tv':
        count = row.get('have_eps')
        if count is None:
            count = row.get('total_episodes')
        if not isinstance(count, int):
            return None
    else:
        count = len(row.get('ids') or ([row['id']] if row.get('id') else []))
    return {'count': count, 'local': bool(row.get('in_local')), 'share': bool(row.get('in_share'))}


def current(payload):
    from . import morning
    cache = morning.read_emby_lib_cache() or {}
    return _facts(cache.get('series' if payload['media_type'] == 'tv' else 'movies') or [], payload['media_type'], str(payload['tmdb_id']))


def register(payload, baseline=None):
    media, tid = payload['media_type'], str(payload['tmdb_id'])
    baseline = current(payload) if baseline is None else baseline
    key = media + ':' + tid
    with _lock:
        data = _load()
        watches = data.setdefault('watches', {})
        if key not in watches and len(watches) >= 100:
            raise ValueError('资源入库通知订阅已达 100 部上限')
        if key not in watches:
            watches[key] = {'title': payload['title'][:200], 'media': media, 'tmdb_id': tid,
                'last': baseline or {'count': 0, 'local': False, 'share': False}}
        state_store.save(_path(), data)


def changed():
    from . import morning
    with _lock:
        if not _load().get('watches'):
            return
    cache = morning.read_emby_lib_cache()
    if not cache:
        return
    regular = {str(s.get('tmdb_id') or '') for s in config.get_subscriptions() if s.get('enabled', True)}
    with _lock:
        data = _load()
        queue = data.setdefault('pending', [])
        for key, watch in data.get('watches', {}).items():
            facts = _facts(cache.get('series' if watch['media'] == 'tv' else 'movies') or [], watch['media'], watch['tmdb_id'])
            if facts is None:
                continue  # Ambiguous identity never proves an import.
            old = watch['last']
            added = (facts['count'] > old['count'] or facts['local'] and not old['local'] or facts['share'] and not old['share'])
            if added and not (watch['media'] == 'tv' and watch['tmdb_id'] in regular):
                if len(queue) >= 100:
                    continue  # Leave old facts to recover on the next committed library event.
                queue.append({'key': key, 'title': watch['title'], 'facts': facts, 'created': time.time(), 'retry': 0})
            watch['last'] = facts
        state_store.save(_path(), data)


def drain(limit=2):
    from . import tg
    if config.load_config().get('subscribe_enabled', '1') != '1':
        return
    if not _sender.acquire(blocking=False):
        return
    try:
        for _ in range(limit):
            with _lock:
                data = _load()
                event = next((e for e in data.get('pending', []) if e.get('retry', 0) <= time.time()), None)
                if event is None:
                    return
                event = dict(event)
            facts = event['facts']
            text = '🎬 <b>订阅资源已入库</b>\n' + html.escape(event['title']) + '\n' + ('本地库 ' if facts['local'] else '') + ('分享库 ' if facts['share'] else '') + '\n当前入库数量：' + str(facts['count']) + '\n已由 Emby 片库事实确认。'
            ok = tg.notify_telegram(text)
            with _lock:
                data = _load()
                queue = data.get('pending', [])
                for item in queue:
                    if item['key'] == event['key'] and item['created'] == event['created']:
                        if ok:
                            queue.remove(item)
                        else:
                            item['retry'] = time.time() + 60
                        break
                state_store.save(_path(), data)
            if not ok:
                return
    finally:
        _sender.release()
