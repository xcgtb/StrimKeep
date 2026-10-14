"""Cached overview, explicit refresh and isolated TMDB recommendations.

Only derived overview/count caches are reset. Media files, comparison metadata,
manual completion, subscriptions and settings are never reset by this action.
"""
import copy
import threading
import time

from . import engine, morning, state_store, config, stats, tmdb

_lock = threading.RLock()
_memory = {'path': None, 'stamp': None, 'data': None, 'health_stamp': None}
_job = {'running': False, 'id': 0, 'started_at': 0, 'finished_at': 0,
        'phase': '', 'error': '', 'warnings': [], 'forced': False}
_RETRY_SECONDS = 60
# Five-minute dashboard refresh reads verified persisted facts, not 155k Episodes.
# Explicit refresh always rebuilds; automatic full verification runs at most daily.
_FULL_FACTS_INTERVAL = 24 * 3600
_STATS_RECHECK_INTERVAL = 3600


def _facts_reconcile_due():
    interval = config.get_ingest_cfg()['reconcile_hours'] * 3600 if config.load_config().get('ingest_mode') == 'webhook' else _FULL_FACTS_INTERVAL
    marker = state_store.read(engine.STATE_DIR / 'overview_full_facts.json', {})
    if marker.get('host') == engine.EMBY_HOST:
        if time.time() < float(marker.get('retry_after') or 0):
            return False
        if time.time() - float(marker.get('ts') or 0) < interval:
            return False
    # Existing releases already have valid full snapshots. Bootstrap the marker
    # from those facts rather than triggering a 155k-item reload on upgrade.
    if not marker:
        previous = morning._load_overview_disk()
        if previous and isinstance(previous.get('data'), dict):
            ts = float(previous.get('ts') or 0)
            if ts > 0 and time.time() - ts < interval:
                state_store.save(engine.STATE_DIR / 'overview_full_facts.json',
                                 {'ts': ts, 'host': engine.EMBY_HOST})
                return False
    return True


def _read_or_recompute_stats(forced):
    cached = state_store.read(engine.STATE_DIR / 'library_stats_cache.json', {})
    interval = config.get_ingest_cfg()['reconcile_hours'] * 3600 if config.load_config().get('ingest_mode') == 'webhook' else _STATS_RECHECK_INTERVAL
    if not forced and isinstance(cached.get('rows'), list) and time.time() - float(cached.get('verified_ts') or cached.get('ts') or 0) < interval:
        return cached
    with engine._strm_count_refreshing:
        return (engine._recompute_all_stats(allow_root_change=True) if forced
                else engine._recompute_all_stats())



def _path():
    return engine.STATE_DIR / 'media_overview.json'


def _health_stamp():
    return tuple(state_store.stamp(p) for p in (
        engine.LIBRARY_SNAPSHOT_FILE, engine.EMBY_LIB_CACHE_FILE, engine.MANUAL_DONE_FILE))


def _summary():
    health = engine.unified_health(max_age=1800)
    if not health:
        return None
    return {k: health.get(k) for k in ('ts', 'facts_ts', 'facts_version', 'stats',
                                      'episodes', 'source', 'stale', 'top_missing')}


def _read():
    path = _path()
    stamp = state_store.stamp(path)
    if _memory['path'] != path or _memory['stamp'] != stamp:
        data = state_store.read(path, {})
        if not isinstance(data, dict) or not isinstance(data.get('dashboard'), dict):
            data = None
        if data and data.get('emby_host') != engine.EMBY_HOST:
            data = None
        _memory.update(path=path, stamp=stamp, data=data, health_stamp=None)
    return _memory['data']


def _phase(text):
    with _lock:
        _job['phase'] = text


def _carry_metadata(fresh, previous):
    return morning._carry_metadata(fresh, previous)


def _refresh_facts():
    # Full rebuild only for manual requests, initial facts or daily reconciliation.
    # Do not retain the large raw Episode collection after snapshot materialization.
    with engine._overview_refresh_lock:
        # Do not hammer a disconnected Emby every five minutes after a failure.
        marker = state_store.read(engine.STATE_DIR / 'overview_full_facts.json', {})
        state_store.save(engine.STATE_DIR / 'overview_full_facts.json',
                         dict(marker, host=engine.EMBY_HOST, retry_after=time.time() + 3600))
        engine._ep_cache.update(ts=0, data=None)
        fresh = engine._build_emby_library_overview()
        if not isinstance(fresh, dict) or not isinstance(fresh.get('series'), list) or not isinstance(fresh.get('movies'), list):
            raise ValueError('片库数据不完整')
        # Read metadata AFTER the remote fetch so an intervening comparison is
        # carried forward rather than replaced by an older copy.
        fresh = _carry_metadata(fresh, morning.read_emby_lib_cache(max_age=None))
        state_store.save(engine.EMBY_LIB_CACHE_FILE, fresh)
        morning.save_library_snapshot(morning._health_snapshot(fresh))
        morning._save_overview_disk(fresh)
        engine._emby_lib_cache.update(ts=time.time(), data=fresh)
        engine._emby_index_cache.update(ts=0, data=None)
        # Only a successfully persisted full snapshot advances the reconciliation clock.
        state_store.save(engine.STATE_DIR / 'overview_full_facts.json',
                         {'ts': time.time(), 'host': engine.EMBY_HOST})
        try:
            from . import library_updates
            library_updates.publish(full=True)
        except Exception as error:
            engine.log.warning('片库完整事实版本通知失败: %s', error)


def _collect(lib_stats):
    from .routers.system import dashboard
    data = dashboard()
    if data.get('error'):
        raise RuntimeError('总览数据读取失败')
    data.pop('storage', None)
    data['localCount'] = f"{lib_stats['local_total']:,}"
    data['shareCount'] = f"{lib_stats['share_total']:,}"
    return {'schema': 1, 'ts': time.time(), 'emby_host': engine.EMBY_HOST,
            'dashboard': data, 'library_stats': lib_stats}


def _refresh_worker(forced):
    warnings = []
    try:
        _phase('正在更新双库统计')
        lib_stats = _read_or_recompute_stats(forced)
        if engine.EMBY_KEY:
            reconciled = False
            if forced or _facts_reconcile_due():
                _phase('正在校准片库数据')
                try:
                    _refresh_facts()
                    reconciled = True
                except Exception:
                    engine.log.exception('片库全量校准失败')
                    warnings.append('Emby 片库校准失败，保留上一份片库数据')
            if forced or reconciled:
                _phase('正在更新近期入库')
                try:
                    ingest = (engine.refresh_ingest_cache(full_reconcile=True)
                              if config.load_config().get('ingest_mode') == 'webhook'
                              else engine.refresh_ingest_cache())
                    if not ingest.get('ok', True) or ingest.get('stale_error'):
                        warnings.append('近期入库更新失败，保留原数据')
                except Exception:
                    warnings.append('近期入库更新失败，保留原数据')
        else:
            warnings.append('未配置 Emby，已更新 STRM 统计')
        _phase('正在保存最新总览')
        data = _collect(lib_stats)
        state_store.save(_path(), data)
        with _lock:
            _memory.update(path=_path(), stamp=state_store.stamp(_path()), data=data,
                           health_stamp=_health_stamp())
            _job.update(error='', warnings=warnings)
    except Exception:
        engine.log.exception('媒体总览刷新失败')
        with _lock:
            _job.update(error='总览刷新失败，请检查服务连接或数据目录后重试', warnings=warnings)
    finally:
        with _lock:
            _job.update(running=False, finished_at=time.time(), phase='')


def request_refresh(force=False):
    with _lock:
        data = _read()
        if _job['running']:
            return dict(_job)
        now = time.time()
        if not force and data and now - float(data.get('ts') or 0) < engine._CACHE_TTL:
            return dict(_job)
        if not force and now - _job['finished_at'] < _RETRY_SECONDS:
            return dict(_job)
        if force:
            # Reset derived overview documents and force a real recount, while
            # preserving the last verified counts until the replacement commits.
            for path in (_path(), engine._EMBY_OVERVIEW_CACHE_FILE):
                state_store.remove(path)
            stats.invalidate_stats_cache(preserve_persisted=True)
            _memory.update(path=_path(), stamp=state_store.stamp(_path()), data=data)
        _job.update(running=True, id=_job['id'] + 1, started_at=now,
                    finished_at=0, phase='正在更新双库统计', error='', warnings=[], forced=force)
        try:
            threading.Thread(target=_refresh_worker, args=(force,), daemon=True,
                             name='media-overview-refresh').start()
        except Exception:
            _job.update(running=False, finished_at=time.time(), error='无法启动刷新，请重试')
        return dict(_job)


def get_overview():
    with _lock:
        data = _read()
        # When opening for the first time after upgrade, expose existing count
        # and shared-fact caches immediately while the single worker refreshes.
        if data is None:
            count = stats._load_strm_count_disk()
            lib = state_store.read(engine.STATE_DIR / 'library_stats_cache.json', {})
            health = _summary()
            if count or health:
                l, s, ts = count or (0, 0, 0)
                data = {'ts': ts, 'dashboard': {'localCount': f'{l:,}', 'shareCount': f'{s:,}',
                         'libraryHealth': health}, 'library_stats': lib}
        result = copy.deepcopy(data) if data else {}
        if result and config.load_config().get('ingest_mode') == 'webhook':
            recent = engine.get_ingest()
            imported = recent.get('stats') or {}
            section = result.setdefault('dashboard', {}).setdefault('ingest', {})
            section.update(cache_ts=recent.get('ts', 0),
                           cache_age_sec=int(time.time() - recent['ts']) if recent.get('ts') else None,
                           movies=imported.get('movies', 0), series=imported.get('series', 0),
                           episodes=imported.get('episodes', 0))
        live_stats = state_store.read(engine.STATE_DIR / 'library_stats_cache.json', {})
        valid_counts = (isinstance(live_stats, dict) and isinstance(live_stats.get('rows'), list)
                        and all(isinstance(live_stats.get(k), int) and not isinstance(live_stats[k], bool)
                                and live_stats[k] >= 0 for k in ('local_total', 'share_total')))
        if result and valid_counts and live_stats.get('ts', 0) > (result.get('library_stats') or {}).get('ts', 0):
            result['library_stats'] = live_stats
            result['dashboard']['localCount'] = f"{live_stats['local_total']:,}"
            result['dashboard']['shareCount'] = f"{live_stats['share_total']:,}"
        # Local facts/manual-done changes should appear immediately, while a
        # repeated view does not deep-copy the whole library on every request.
        stamp = _health_stamp()
        if result and stamp != _memory['health_stamp']:
            health = _summary()
            if health:
                result['dashboard']['libraryHealth'] = health
                if _memory['data']:
                    _memory['data']['dashboard']['libraryHealth'] = health
            _memory['health_stamp'] = stamp
        result.update(status='success' if data else 'pending', refresh=dict(_job),
                      strategy=config.get_strategy(), ttl_sec=engine._CACHE_TTL,
                      stale=not data or time.time() - float(data.get('ts') or 0) >= engine._CACHE_TTL)
    health = (result.get('dashboard') or {}).get('libraryHealth')
    if health:
        health['stale'] = not health.get('ts') or time.time() - health['ts'] > 1800
    try:
        from . import overview_activity
        result['activity'] = overview_activity.snapshot(health)
    except Exception:
        engine.log.exception('总览动态读取失败')
        result['activity'] = {'status': 'error', 'message': '动态数据暂不可用，请稍后刷新'}
    request_refresh()
    with _lock:
        result['refresh'] = dict(_job)
    return result


def recommendations(media='movie', retry=False):
    if media not in ('movie', 'tv'):
        raise ValueError('无效推荐类型')
    page, meta = tmdb._explore_page('/trending/' + media + '/week', {'page': 1}, media, retry=retry)
    if page is None:
        return dict(meta, cards=[])
    cards = []
    for row in page['results'][:12]:
        cards.append({'type': media, 'tmdb_id': str(row['id']),
                      'title': row.get('title') or row.get('name') or '',
                      'year': str(row.get('release_date') or row.get('first_air_date') or '')[:4],
                      'rating': row.get('vote_average') or 0,
                      'poster': '/api/tmdb/poster' + row['poster_path'] if row.get('poster_path') else ''})
    try:
        status = tmdb._explore_library_status(cards)['cards']
        for card, facts in zip(cards, status):
            card.update(in_emby=facts.get('in_emby'), library_status=facts.get('library_status'))
    except Exception:
        pass
    return dict(meta, status='success', cards=cards)
