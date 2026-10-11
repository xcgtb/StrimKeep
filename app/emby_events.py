"""Authenticated hints, one durable bounded worker, authoritative scoped reads.

Events never authorize filesystem mutation. Acknowledgement follows persistence;
failed reads keep their job and old facts. No periodic Emby discovery in webhook mode.
"""
import copy
import hashlib
import hmac
import json
import re
import threading
import time
from . import config, engine, ingest, morning, state_store, library_updates, tmdb

_lock = threading.RLock()
_wake = threading.Event()
_worker = None
MAX_PENDING = 512
_ID = re.compile(r'^[A-Za-z0-9_-]{1,128}$')


def _path():
    return engine.STATE_DIR / 'emby_callback_queue.json'


def _load():
    data = state_store.read(_path(), {})
    if data.get('host') != engine.EMBY_HOST:
        return {'host': engine.EMBY_HOST, 'jobs': {}, 'seen': [], 'items': {}}
    return data


def authorized(token):
    cfg = config.load_config()
    expected = cfg.get('ingest_webhook_token') or ''
    return bool(expected and cfg.get('ingest_enabled', '1') == '1'
                and isinstance(token, str) and hmac.compare_digest(expected, token))


def accept(payload):
    if not isinstance(payload, dict):
        raise ValueError('回调必须是 JSON 对象')
    event = str(payload.get('Event') or '').lower()
    if event not in ('library.new', 'library.deleted', 'library.updated'):
        with _lock:
            data = _load()
            data['probe_at'] = time.time()
            state_store.save(_path(), data)
        return {'status': 'ignored', 'message': '测试或非媒体事件，已收到，不修改片库'}
    item = payload.get('Item') or {}
    if not isinstance(item, dict):
        raise ValueError('Item 格式错误')
    kind, ident = item.get('Type'), str(item.get('Id') or '')
    if kind not in ('Episode', 'Series', 'Movie'):
        return {'status': 'ignored', 'message': '仅处理电影、剧集和分集'}
    if not _ID.fullmatch(ident):
        raise ValueError('无效媒体编号')
    sid = str(item.get('SeriesId') or '')
    if sid and not _ID.fullmatch(sid):
        raise ValueError('无效剧集编号')
    hint = {'id': ident, 'type': kind, 'sid': sid,
            'path': str(item.get('Path') or '')[:2048], 'event': event}
    digest = hashlib.sha256(json.dumps([hint, payload.get('Date')], sort_keys=True).encode()).hexdigest()
    with _lock:
        data = _load()
        jobs = data.setdefault('jobs', {})
        key = kind + ':' + ident
        if jobs.get(key, {}).get('digest') == digest:
            return {'status': 'accepted', 'duplicate': True}
        if key not in jobs and len(jobs) >= MAX_PENDING:
            return {'status': 'busy', 'message': '回调队列已满，请稍后重试'}
        generation = int(jobs.get(key, {}).get('generation', 0)) + 1
        jobs[key] = dict(hint, digest=digest, generation=generation, received=time.time(),
                         due=time.time() + 2, attempts=0)
        data['received_at'] = time.time()
        state_store.save(_path(), data)
    kick()
    return {'status': 'accepted'}


def status():
    with _lock:
        data = _load()
        return {key: data.get(key) for key in ('received_at', 'processed_at', 'probe_at', 'error') } | {
            'pending': len(data.get('jobs') or {}),
            'running': _worker is not None and _worker.is_alive()}


def kick():
    global _worker
    if config.load_config().get('ingest_enabled', '1') != '1':
        return
    with _lock:
        _wake.set()
        if _worker is not None and _worker.is_alive():
            return
        if not _load().get('jobs'):
            return
        _worker = threading.Thread(target=_run, daemon=True, name='emby-callback')
        _worker.start()


def _exact_many(jobs):
    expected = {job['id']: job['type'] for job in jobs}
    response = engine.emby_request('/Items', {'Ids': ','.join(expected), 'Recursive': 'true',
        'IncludeItemTypes': ','.join(sorted(set(expected.values()))),
        'Fields': 'Path,SeriesId,SeriesName,ProviderIds,ProductionYear,Genres,ImageTags,DateCreated,ParentIndexNumber,IndexNumber,IndexNumberEnd,CommunityRating',
        'Limit': len(expected) + 1}, timeout=15, retries=0)
    if not isinstance(response, dict) or not isinstance(response.get('Items'), list):
        raise RuntimeError('Emby 精准详情读取失败')
    rows = response['Items']
    if response.get('TotalRecordCount') is None or int(response['TotalRecordCount']) != len(rows):
        raise RuntimeError('Emby 精准详情不完整')
    found = {}
    for row in rows:
        ident = str(row.get('Id') or '')
        if ident not in expected or ident in found or row.get('Type') != expected[ident]:
            raise RuntimeError('Emby 精准详情身份不匹配')
        found[ident] = row
    return found


def _live(sid):
    response = engine.emby_request('/Items', {'ParentId': sid, 'Recursive': 'true',
        'IncludeItemTypes': 'Episode', 'Fields': 'Path,SeriesId,ParentIndexNumber,IndexNumber,IndexNumberEnd',
        'Limit': 5000}, timeout=15, retries=0)
    if not isinstance(response, dict) or not isinstance(response.get('Items'), list):
        raise RuntimeError('Emby 单剧分集读取失败')
    rows = response['Items']
    if response.get('TotalRecordCount') is None or int(response['TotalRecordCount']) != len(rows) or len(rows) >= 5000:
        raise RuntimeError('Emby 单剧分集结果不完整')
    for row in rows:
        if str(row.get('SeriesId') or sid) != sid:
            raise RuntimeError('Emby 忽略了单剧筛选')
        path = row.get('Path') or ''
        if engine.EMBY_PATHS.lib_of(path) not in ('local', 'share'):
            raise RuntimeError('分集路径无法确认所属库')
        local = engine.emby_path_to_container(path)
        if local is None or not local.is_file():
            raise RuntimeError('STRM 挂载不可见或 Emby 尚未完成同步，保留旧数据')
    return rows


def _commit(kind, ids, entry):
    def patch(data):
        rows = data.setdefault(kind, [])
        field = 'series_ids' if kind == 'series' else 'ids'
        rows[:] = [row for row in rows if not ids.intersection(
            str(x) for x in (row.get(field) or [row.get('id')]) if x)]
        if entry is not None:
            rows.append(copy.deepcopy(entry))
        morning._recompute_mapping_stats(data)
        data['facts_ts'] = time.time()
        return True
    morning._patch_all_caches(patch)
    cache = morning.read_emby_lib_cache()
    if cache:
        morning.save_library_snapshot(morning._health_snapshot(cache))
    engine._emby_index_cache.pop('callback_revision', None)
    library_updates.publish(ids if kind == 'series' else (), movie_ids=ids if kind == 'movies' else ())


def _recent(items):
    with ingest._ingest_refresh_lock:
        index = state_store.read(ingest._ingest_index_path(), {})
        if index.get('host') != engine.EMBY_HOST:
            index = {'host': engine.EMBY_HOST, 'hours': 24, 'movies': [], 'episodes': []}
        for job, meta in items:
            if job['type'] == 'Series':
                continue
            field = 'movies' if job['type'] == 'Movie' else 'episodes'
            rows = [r for r in index.get(field, []) if str(r.get('Id')) != job['id']]
            if meta:
                rows.append(meta)
            index[field] = rows
        data = ingest._fetch_ingest(source_items={'movies': index.get('movies', []), 'episodes': index.get('episodes', [])})
        if not data.get('ok', True):
            raise RuntimeError('近期入库投影更新失败')
        source = data.pop('_recent_source', {})
        index.update(ts=time.time(), movies=source.get('movies', []), episodes=source.get('episodes', []))
        state_store.save_many([(ingest._ingest_index_path(), index), (engine.INGEST_CACHE_FILE, data)])


def process_batch(jobs):
    """No file deletes; resolve hints against exact IDs, then query each title once."""
    cache = morning.read_emby_lib_cache()
    baseline = state_store.stamp(engine.EMBY_LIB_CACHE_FILE)
    if not cache:
        raise RuntimeError('请先完成一次片库全量初始化')
    with _lock:
        known = copy.deepcopy(_load().get('items') or {})
    resolved, groups, paths = [], {}, set()
    metadata = _exact_many(jobs)
    for job in jobs:
        meta = metadata.get(job['id'])
        # Event ordering is not authoritative. A late delete after re-import
        # must project the current item, never remove it from verified facts.
        if job['event'] != 'library.deleted' and meta is None:
            raise RuntimeError('Emby 入库尚未可见，稍后核实')
        remembered = known.get(job['id']) or {}
        old_path = remembered.get('path')
        if old_path:
            old_local = engine.emby_path_to_container(old_path)
            if old_local is not None:
                paths.add(str(old_local.parent))
        path = (meta or {}).get('Path') or job.get('path') or remembered.get('path') or ''
        if path:
            local = engine.emby_path_to_container(path)
            if local is not None:
                paths.add(str(local.parent))
                if job['type'] == 'Series':
                    ledger = state_store.read(engine.STATE_DIR / 'strm_directory_counts.json', {})
                    affected = [p for p in ledger.get('directories', {})
                                if p == str(local) or p.startswith(str(local) + '/')]
                    if len(affected) > 1000:
                        raise RuntimeError('剧目录过大，等待全量统计校准')
                    paths.update(affected)
            if meta and job['type'] != 'Series' and (local is None or not local.is_file()):
                raise RuntimeError('入库 STRM 尚未可见，保留旧事实并重试')
        sid = str((meta or {}).get('SeriesId') or job.get('sid') or remembered.get('sid') or '')
        if job['type'] == 'Series':
            sid = job['id']
        if job['type'] == 'Episode' and not sid:
            raise RuntimeError('删除回调缺少剧集身份，等待全量校准；保留原数据')
        kind = 'movies' if job['type'] == 'Movie' else 'series'
        identity = job['id'] if kind == 'movies' else sid
        groups.setdefault((kind, identity), []).append((job, meta))
        known[job['id']] = {'sid': sid, 'path': path}
        resolved.append((job, meta))
    with _lock:
        data = _load()
        data['items'] = dict(list(known.items())[-4096:])
        state_store.save(_path(), data)
    facts, comparison_errors = [], []
    for (kind, identity), events in groups.items():
        field = 'ids' if kind == 'movies' else 'series_ids'
        original = next((r for r in cache.get(kind, []) if identity in
            {str(i) for i in (r.get(field) or [r.get('id')]) if i}), None)
        ids = {str(i) for i in (original.get(field) or [original.get('id')]) if i} if original else {identity}
        if kind == 'movies':
            if original is None:
                meta = next((m for _, m in events if m), None)
                tid = str(((meta or {}).get('ProviderIds') or {}).get('Tmdb') or '')
                if tid.isdecimal():
                    original = next((row for row in cache.get('movies', [])
                        if str(row.get('tmdb_id') or '') == tid
                        and str(row.get('name') or '').casefold() == str(meta.get('Name') or '').casefold()
                        and str(row.get('year') or '') == str(meta.get('ProductionYear') or '')), None)
                    if original:
                        ids |= {str(i) for i in (original.get('ids') or [original.get('id')]) if i}
            removed_ids = {j['id'] for j, m in events if m is None}
            remaining = ids - removed_ids
            if not remaining:
                entry = None
            else:
                fresh = [morning._movie_metadata_by_id(i) for i in sorted(remaining)]
                if any(engine.emby_path_to_container(r['Path']) is None or
                       not engine.emby_path_to_container(r['Path']).is_file() for r in fresh):
                    raise RuntimeError('电影 STRM 挂载不可见，保留旧事实')
                if any(str(r.get('Name') or '').casefold() != str(fresh[0].get('Name') or '').casefold()
                       or str(r.get('ProductionYear') or '') != str(fresh[0].get('ProductionYear') or '')
                       or str((r.get('ProviderIds') or {}).get('Tmdb') or '') != str((fresh[0].get('ProviderIds') or {}).get('Tmdb') or '') for r in fresh):
                    raise RuntimeError('跨库电影身份冲突，保留旧事实')
                libs = {engine.EMBY_PATHS.lib_of(r['Path']) for r in fresh}
                entry = copy.deepcopy(original) if original else {}
                first = fresh[0]
                entry.update(id=str(first['Id']), ids=sorted(remaining), name=first['Name'],
                    tmdb_id=(first.get('ProviderIds') or {}).get('Tmdb'), year=first.get('ProductionYear'),
                    genres=first.get('Genres') or [], path=first['Path'], paths=[r['Path'] for r in fresh],
                    in_local='local' in libs, in_share='share' in libs,
                    has_image=any('Primary' in (r.get('ImageTags') or {}) for r in fresh))
        else:
            gone = {j['id'] for j, m in events if j['type'] == 'Series' and m is None}
            remaining = ids - gone
            live = [ep for sid in sorted(remaining) for ep in _live(sid)]
            if not live:
                if any(m is not None for _, m in events):
                    raise RuntimeError('新增剧集尚无可见分集，等待同步')
                entry = None
            else:
                if original is None:
                    candidate = morning._new_series_after_ingest(identity, live, cache)
                    if candidate is None:
                        raise RuntimeError('新增剧集身份尚未完整')
                    original = candidate[0]
                    original = copy.deepcopy(original)
                    original['paths'] = list(dict.fromkeys(
                        [p for p in (original.get('paths') or [original.get('path')]) if p] + [candidate[1]]))
                    merged = {str(i) for i in original.get('series_ids', []) if i}
                    for sid in sorted(merged - remaining):
                        live.extend(_live(sid))
                    ids |= merged
                    remaining |= merged
                entry = copy.deepcopy(original)
                entry['series_ids'] = sorted(remaining)
                if str(entry.get('id')) not in remaining:
                    entry['id'] = sorted(remaining)[0]
                morning._resync_series_entry(entry, live)
                tid = entry.get('tmdb_id')
                # Publish Emby facts before any slow TMDB request.
                if state_store.stamp(engine.EMBY_LIB_CACHE_FILE) != baseline:
                    raise RuntimeError('片库已发生其他变化，重新核实本次回调')
                if any(not engine.emby_path_to_container(ep['Path']).is_file() for ep in live):
                    raise RuntimeError('STRM 已变化，重新核实本次回调')
                _commit(kind, ids, entry)
                baseline = state_store.stamp(engine.EMBY_LIB_CACHE_FILE)
                if tid and engine.RUNTIME_CFG.get('tmdb_key'):
                    try:
                        client = engine.Tmdb()
                        info = client.get('/tv/' + str(tid), ttl=engine.TMDB_INFO_TTL)
                        client.save()
                        if not isinstance(info, dict) or not isinstance(info.get('seasons'), list):
                            raise RuntimeError('单剧 TMDB 对照失败')
                        result = engine.classify_series_by_tmdb(entry.get('seasons') or [], info)
                        if info.get('poster_path'):
                            result['poster'] = info['poster_path']
                        entry['tmdb_info'] = result
                    except Exception:
                        comparison_errors.append('入库事实已同步；单剧 TMDB 对照等待重试')
                facts.append(entry)
        if state_store.stamp(engine.EMBY_LIB_CACHE_FILE) != baseline:
            raise RuntimeError('片库已发生其他变化，重新核实本次回调')
        _commit(kind, ids, entry)
        baseline = state_store.stamp(engine.EMBY_LIB_CACHE_FILE)
    # Notification handoff depends on confirmed episode facts, not directory
    # counters or import-window projection succeeding in the same attempt.
    if facts:
        result = engine.enqueue_ingest_notifications({'series_facts': facts})
        if isinstance(result, dict) and result.get('status') == 'error':
            comparison_errors.append('入库事实已同步；订阅通知等待重试')
        from . import scheduler
        scheduler._request_notification_delivery()
    _recent(resolved)
    from . import stats
    if paths and not stats.refresh_event_directories(paths):
        comparison_errors.append('入库事实已同步；STRM 统计等待全量初始化')
    with _lock:
        data = _load()
        data['items'] = dict(list(known.items())[-4096:])
        state_store.save(_path(), data)
    # Publish after recent import and STRM totals are committed as well.
    library_updates.publish([sid for kind, sid in groups if kind == 'series'],
                            movie_ids=[sid for kind, sid in groups if kind == 'movies'])
    if comparison_errors:
        raise RuntimeError(comparison_errors[0])


def _run():
    global _worker
    while True:
        _wake.wait(2)
        _wake.clear()
        with _lock:
            data = _load()
            ready = [(key, dict(job)) for key, job in data.get('jobs', {}).items()
                     if job['due'] <= time.time()][:40]
            if not data.get('jobs') or config.load_config().get('ingest_enabled', '1') != '1':
                _worker = None
                return
        if not ready:
            continue
        # A bad title cannot suppress notification or processing of other titles.
        groups = {}
        for key, job in ready:
            group = ('series', job.get('sid') or job['id']) if job['type'] != 'Movie' else ('movie', job['id'])
            groups.setdefault(group, []).append((key, job))
        errors = {}
        for members in groups.values():
            try:
                with engine._overview_refresh_lock:
                    process_batch([job for _, job in members])
                error = ''
            except Exception as exc:
                engine.log.warning('Emby 回调保留待处理作品: %s', exc)
                error = str(exc)[:300]
            for key, _ in members:
                errors[key] = error
        with _lock:
            data = _load()
            for key, job in ready:
                error = errors[key]
                current = data.get('jobs', {}).get(key)
                if not current or current['generation'] != job['generation']:
                    continue
                if not error:
                    del data['jobs'][key]
                else:
                    current['attempts'] += 1
                    current['due'] = time.time() + min(300, 5 * 2 ** min(current['attempts'], 6))
            data['error'] = next((e for e in errors.values() if e), '')
            if any(not e for e in errors.values()):
                data['processed_at'] = time.time()
            state_store.save(_path(), data)
