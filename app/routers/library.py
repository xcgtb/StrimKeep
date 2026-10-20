# -*- coding: utf-8 -*-
"""片库：入库 / 统计 / Emby / 搜索 / 日志 / 删除 / 探索"""
import time, json, threading, sqlite3, urllib.parse, urllib.request, asyncio, secrets, re
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from app import runtime_logs
from app import posters, tmdb, library_response, library_updates, morning, deletion_progress, library_first_screen, library_pagination, batch_library_delete, library_identity

try:
    from app.routers.deps import auth, engine, logger, Args
    from app.config import load_config, save_config, get_ingest_cfg
except ImportError:
    from routers.deps import auth, engine, logger, Args
    from config import load_config, save_config, get_ingest_cfg

router = APIRouter()


@router.post('/api/emby/webhook')
async def api_emby_webhook(request: Request):
    from app import emby_events
    token = request.headers.get('X-StrimKeep-Token') or request.query_params.get('token') or ''
    if not await asyncio.to_thread(emby_events.authorized, token):
        raise HTTPException(status_code=401, detail='回调令牌无效或监控已关闭')
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 256 * 1024:
            raise HTTPException(status_code=413, detail='回调内容过大')
    try:
        content_type = request.headers.get('content-type', '')
        if 'application/x-www-form-urlencoded' in content_type:
            payload = json.loads(urllib.parse.parse_qs(body.decode()).get('data', [''])[0])
        elif 'multipart/form-data' in content_type:
            # Request retains the bounded body for Starlette's form parser.
            request._body = bytes(body)
            form = await request.form(max_files=0, max_fields=8, max_part_size=256 * 1024)
            payload = json.loads(form.get('data') or '')
        else:
            payload = json.loads(body)
        result = await asyncio.to_thread(emby_events.accept, payload)
    except (ValueError, TypeError, UnicodeError) as error:
        raise HTTPException(status_code=400, detail='回调内容格式错误') from error
    return Response(content=json.dumps(result, ensure_ascii=False), media_type='application/json',
                    status_code=503 if result['status'] == 'busy' else 202)


@router.get('/api/ingest', dependencies=[Depends(auth)])
def api_ingest(full: int = 0, force: int = 0):
    """
    full=1 完整清单，force=1 强制立即刷新
    默认读缓存（10 分钟时效），过期自动刷新
    """
    try:
        kw = ('full ' if full else '') + ('force ' if force else '')
        return engine.action_stats(Args(kw=kw.strip()))
    except Exception as e:
        return {'status': 'error', 'message': str(e)}


@router.get('/api/ingest/status', dependencies=[Depends(auth)])
def api_ingest_status():
    """只读缓存状态，不触发刷新"""
    data = engine.read_ingest_cache()
    if not data:
        return {'status': 'success', 'has_cache': False}
    return {'status': 'success', 'has_cache': True,
            'ts': data.get('ts', 0),
            'age_sec': int(time.time() - data.get('ts', 0)),
            'ok': data.get('ok', True),
            'warning': data.get('stale_error') or ('' if data.get('ok', True) else data.get('error', '')),
            'stats': data.get('stats', {})}


@router.get('/api/stats', dependencies=[Depends(auth)])
def api_stats(full: int = 0, force: int = 0):
    kw = ('full ' if full else '') + ('force ' if force else '')
    return engine.ACTIONS['stats'](Args(kw=kw.strip()))


@router.get('/api/played', dependencies=[Depends(auth)])
def api_played():
    return engine.ACTIONS['played'](Args())


@router.get('/api/search', dependencies=[Depends(auth)])
def api_search(q: str):
    if not q.strip(): raise HTTPException(400, '关键词不能为空')
    return engine.ACTIONS['search'](Args(kw=q))


_HEAVY_META = ('share_paths', 'local_paths', 'members')


def _slim_record(rec):
    """列表接口不带逐项的文件路径/成员大列表（单条执行记录可能上百项），
    点开某一项详情时前端再按 id 调 /api/records/{id} 取完整快照。"""
    extra = rec.get('extra')
    if not isinstance(extra, dict) or not extra.get('items'):
        return rec
    slim_items = []
    for it in extra['items']:
        it = dict(it)
        meta = dict(it.get('meta') or {})
        for k in _HEAVY_META:
            if k in meta:
                meta['has_' + k] = bool(meta.pop(k))
        it['meta'] = meta
        slim_items.append(it)
    out = dict(rec)
    out['extra'] = dict(extra, items=slim_items)
    return out


@router.get('/api/records', dependencies=[Depends(auth)])
def api_records(n: int = 50):
    recs = [_slim_record(r) for r in logger.read_recent(limit=n)]
    try:
        snap = engine.daily_consistency_snapshot(False)
        return {'status': 'success', 'records': recs,
                'consistency': {'rule_sig': snap.get('rule_sig'),
                                'library_ts': (snap.get('library') or {}).get('ts', 0),
                                'ingest_ts': (snap.get('ingest') or {}).get('ts', 0),
                                'governance_ts': (snap.get('governance') or {}).get('ts', 0)}}
    except Exception:
        return {'status': 'success', 'records': recs, 'consistency': {}}


@router.get('/api/records/{rid}', dependencies=[Depends(auth)])
def api_record_detail(rid: int):
    """单条执行记录的完整快照（含每项的文件路径、画质对比依据等）。"""
    rec = engine.db_get_audit(rid)
    if rec is None:
        raise HTTPException(404, '记录不存在')
    return {'status': 'success', 'record': rec}


@router.get('/api/logs', dependencies=[Depends(auth)])
def api_logs(n: int = 200, after: int = 0, source: str = '', query: str = '', level: str = '', task: str = ''):
    if level and level not in ('DEBUG','INFO','WARNING','ERROR','CRITICAL'):
        raise HTTPException(400, '日志级别无效')
    if task and not re.fullmatch(r'[a-f0-9]{1,32}', task):
        raise HTTPException(400, '任务编号格式无效')
    return {'status': 'success', **runtime_logs.read(after=after, limit=n, source=source[:80], query=query[:200], level=level, task=task)}


@router.get('/api/activity/events', dependencies=[Depends(auth)])
async def api_activity_events(request: Request):
    from app.activity_stream import updates
    return StreamingResponse(updates(request), media_type='text/event-stream', headers={
        'Cache-Control': 'private, no-cache, no-transform',
        'X-Accel-Buffering': 'no', 'Vary': 'Cookie, Authorization'})


@router.get('/api/logs/download', dependencies=[Depends(auth)])
def api_logs_download():
    try:
        content = runtime_logs.text()
    except (OSError, sqlite3.Error):
        raise HTTPException(503, '日志数据库不可读取')
    return Response(content, media_type='text/plain; charset=utf-8',
                    headers={'Content-Disposition': 'attachment; filename="strimkeep-logs.txt"'})


@router.get('/api/emby/poster/{item_id}', dependencies=[Depends(auth)])
def api_emby_poster(item_id: str, size: str = 'w500'):
    if not engine.EMBY_KEY: raise HTTPException(500, '未配置 EMBY_KEY')
    try:
        data, ct = posters.get_emby_poster(item_id, engine.EMBY_HOST, engine.EMBY_KEY, size)
        return Response(content=data, media_type=ct, headers={
            'Cache-Control': 'private, max-age=3600',
            'Vary': 'Authorization',
            'X-Content-Type-Options': 'nosniff',
        })
    except posters.PosterBusyError as e:
        raise HTTPException(503, str(e), headers={'Retry-After': '1'})
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(404, str(e))


@router.get('/api/tmdb/poster/{filename}', dependencies=[Depends(auth)])
def api_tmdb_poster(filename: str, size: str = 'w500'):
    try:
        data, content_type = posters.get_tmdb_poster(filename, size)
        return Response(data, media_type=content_type, headers={
            'Cache-Control': 'private, max-age=86400', 'Vary': 'Authorization',
            'X-Content-Type-Options': 'nosniff'})
    except posters.PosterBusyError as error:
        raise HTTPException(503, str(error), headers={'Retry-After': '1'})
    except ValueError as error:
        raise HTTPException(400, str(error))
    except Exception:
        raise HTTPException(502, 'TMDB 海报加载失败，请检查网络或 HTTP 代理')


@router.get('/api/explore', dependencies=[Depends(auth)])
def api_explore(region: str = 'all', year: str = '', sort: str = 'popularity',
                media: str = 'movie', page: int = 1, q: str = '', genre: str = '', retry: int = 0):
    try:
        return engine.ACTIONS['explore'](Args(region=region, year=year,
                                              sort=sort, media=media,
                                              page=page, q=q, genre=genre, retry=bool(retry)))
    except Exception as e:
        return {'status': 'error', 'message': str(e)}


@router.post('/api/explore/library-status', dependencies=[Depends(auth)])
def api_explore_library_status(body: dict):
    cards = body.get('cards')
    if not isinstance(cards, list) or len(cards) > 200:
        raise HTTPException(400, '每次最多更新 200 张卡片')
    identities = []
    for card in cards:
        if not isinstance(card, dict) or card.get('type') not in ('movie', 'tv'):
            raise HTTPException(400, '无效媒体类型')
        tid = str(card.get('tmdb_id', ''))
        if not tid.isascii() or not tid.isdigit() or not 1 <= len(tid) <= 20:
            raise HTTPException(400, '无效 TMDB 编号')
        identities.append({'type': card['type'], 'tmdb_id': tid})
    if not identities:
        return {'status': 'success', 'cards': [], 'library_refreshing': False, 'library_error': None}
    result = tmdb._explore_library_status(identities)
    for card in result['cards']:
        card.pop('_emby_has_image', None)
    return result


@router.get('/api/tmdb/progress', dependencies=[Depends(auth)])
def api_tmdb_progress():
    return {'status': 'success', 'progress': morning.get_tmdb_scan_progress()}


@router.get('/api/library/changes', dependencies=[Depends(auth)])
def api_library_changes(since: int = 0):
    """Small patch set, never triggers Emby/TMDB scanning or exposes media paths."""
    if since < 0:
        raise HTTPException(400, '无效版本')
    change = library_updates.changes(since)
    result = {'status': 'success', 'version': change['version'],
              'full': change['full'], 'series': [], 'movies': []}
    if change['full'] or not (change['ids'] or change.get('movie_ids')):
        return result
    cached = morning.read_emby_lib_cache(max_age=None)
    if cached is None:
        result['full'] = True
        return result
    ids = set(change['ids'])
    found = set()
    for row in cached.get('series') or []:
        members = {str(x) for x in (row.get('series_ids') or [row.get('id')]) if x}
        if members & ids:
            result['series'].append(row)
            found.update(members & ids)
    # A deletion or concurrent full refresh must be reconciled, not silently
    # displayed as an unchanged badge.
    movie_ids = set(change.get('movie_ids') or [])
    found_movies = set()
    for row in cached.get('movies') or []:
        members = {str(x) for x in (row.get('ids') or [row.get('id')]) if x}
        if members & movie_ids:
            result['movies'].append(row)
            found_movies.update(members & movie_ids)
    if ids - found or movie_ids - found_movies:
        result['full'] = True
        result['series'] = []
        result['movies'] = []
        return result
    result['stats'] = cached.get('stats') or {}
    result['facts_ts'] = cached.get('facts_ts') or 0
    return result


@router.get('/api/library/events', dependencies=[Depends(auth)])
async def api_library_events(request: Request):
    """Authenticated SSE hints; clients read actual facts via /changes."""
    async def updates():
        last = (await asyncio.to_thread(library_updates.changes, 0))['version']
        yield 'retry: 5000\n\n'
        while not await request.is_disconnected():
            latest = (await asyncio.to_thread(library_updates.changes, last))['version']
            if latest != last:
                last = latest
                yield 'event: library\ndata: ' + str(last) + '\n\n'
            else:
                yield ': alive\n\n'
            await asyncio.sleep(1)
    return StreamingResponse(updates(), media_type='text/event-stream', headers={
        'Cache-Control': 'private, no-cache, no-transform',
        'X-Accel-Buffering': 'no', 'Vary': 'Cookie, Authorization'})


def _mapping_no_store(response: Response):
    # Even `status:nocache` is HTTP 200; prevent browser/FRP/CDN from retaining it.
    response.headers['Cache-Control'] = 'private, no-store, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Vary'] = 'Cookie, Authorization'


@router.get('/api/emby/library/preview', dependencies=[Depends(auth)])
def api_emby_library_preview(response: Response):
    _mapping_no_store(response)
    # Bounded read-only projection. No Emby/TMDB requests, paths or credentials.
    return library_first_screen.preview()


@router.get('/api/emby/library/page', dependencies=[Depends(auth)])
def api_emby_library_page(media: str = 'series', status: str = 'all', scope: str = 'all',
                          category: str = '', category_mode: str = 'seg', keyword: str = '',
                          offset: int = 0, limit: int = 40, response: Response = None):
    _mapping_no_store(response)
    try:
        return library_pagination.page(media=media, status=status, scope=scope,
                                       category=category, category_mode=category_mode,
                                       keyword=keyword, offset=offset, limit=limit)
    except ValueError as error:
        raise HTTPException(400, str(error))


@router.get('/api/emby/library/item', dependencies=[Depends(auth)])
def api_emby_library_item(media: str, id: str, response: Response):
    _mapping_no_store(response)
    try:
        item = library_pagination.detail(media, id)
    except ValueError as error:
        raise HTTPException(400, str(error))
    if item['status'] == 'notfound':
        raise HTTPException(404, '作品不在片库缓存中')
    return item


@router.get('/api/emby/library', dependencies=[Depends(auth)])
def api_emby_library(force: int = 0, with_tmdb: int = 1, cache_only: int = 0,
                     web: int = 0, request: Request = None):
    # 打开页面只读与健康汇总/探索相同的事实；陈旧缓存仍返回并标记。
    if cache_only or (with_tmdb and not force):
        if web:
            cached = library_response.cached_mapping_response(request.headers.get('accept-encoding', '') if request else '')
        else:
            cached = engine.cached_library_view()
        if cached is not None:
            return cached
        if cache_only:
            return {'status': 'nocache'}
    if not with_tmdb:
        return engine.action_emby_library(Args(force=False, with_tmdb=False))
    # force=1：启动后台任务，立即返回
    return engine.refresh_tmdb_scan(background=True)


@router.get('/api/library_stats', dependencies=[Depends(auth)])
def api_library_stats():
    try:
        return engine.action_library_stats(Args())
    except Exception as e:
        return {'status': 'error', 'message': str(e)}


@router.get('/api/ingest/settings', dependencies=[Depends(auth)])
def api_get_ingest_settings():
    from app import emby_events
    return {'status': 'success', 'settings': get_ingest_cfg(), 'webhook': emby_events.status()}


@router.post('/api/ingest/settings', dependencies=[Depends(auth)])
def api_set_ingest_settings(body: dict = None):
    body = body or {}
    cfg = load_config()
    previous_mode = cfg.get('ingest_mode', 'poll')
    if body.get('mode') not in (None, 'poll', 'webhook'):
        raise HTTPException(status_code=400, detail='无效监控模式')
    if body.get('generate_token') and body.get('reset_token'):
        raise HTTPException(status_code=400, detail='不能同时生成和重置回调令牌')
    if body.get('generate_token'):
        if cfg.get('ingest_webhook_token'):
            raise HTTPException(status_code=409, detail='已有回调令牌，首次生成不能覆盖，请使用重置操作')
        cfg['ingest_webhook_token'] = secrets.token_hex(24)
    if body.get('reset_token'):
        # 后端同样执行二次确认校验，拦截旧网页缓存中的误触请求。
        if body.get('confirm_reset') != 'RESET_WEBHOOK_TOKEN':
            raise HTTPException(status_code=400, detail='重置回调令牌需要二次确认')
        if not cfg.get('ingest_webhook_token'):
            raise HTTPException(status_code=400, detail='尚未生成回调令牌，请先首次生成')
        cfg['ingest_webhook_token'] = secrets.token_hex(24)
    if 'mode' in body:
        if body['mode'] == 'webhook' and not cfg.get('ingest_webhook_token'):
            raise HTTPException(status_code=400, detail='请先生成回调地址并在 Emby 中配置，再开启实时回调')
        cfg['ingest_mode'] = body['mode']
    if 'reconcile_hours' in body:
        try:
            hours = max(1, min(168, int(body['reconcile_hours'])))
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail='全量校准间隔无效')
        cfg['ingest_reconcile_hours'] = str(hours)
        cfg['tmdb_scan_interval_hours'] = str(hours)
    if 'enabled' in body:
        cfg['ingest_enabled'] = '1' if body['enabled'] else '0'
    if 'interval_min' in body:
        try:
            v = max(1, min(1440, int(body['interval_min'])))
        except (ValueError, TypeError):
            v = 5
        cfg['ingest_interval_min'] = str(v)
    save_config(cfg)
    engine.reload_config()
    from app import emby_events
    emby_events.kick()
    if previous_mode != 'webhook' and cfg.get('ingest_mode') == 'webhook':
        from app import overview
        overview.request_refresh(force=True)
    return {'status': 'success', 'settings': get_ingest_cfg()}


@router.get('/api/emby/series/{series_id}/episodes', dependencies=[Depends(auth)])
def api_emby_series_episodes(series_id: str):
    """返回某剧所有分集（含 Path），前端可自行分库统计"""
    try:
        data = engine.emby_request('/Items', {
            'ParentId': series_id,
            'Recursive': 'true',
            'IncludeItemTypes': 'Episode',
            'Fields': 'Path,ParentIndexNumber,IndexNumber',
            'Limit': 5000,
        }) or {}
        episodes = []
        for ep in data.get('Items', []):
            path = ep.get('Path', '') or ''
            lib = engine.emby_lib_of(path) or 'other'
            conv = engine.emby_path_to_container(path)
            exists = True if conv is None else conv.exists()   # None = 映射不上，无法核实
            episodes.append({
                'season': ep.get('ParentIndexNumber'),
                'episode': ep.get('IndexNumber'),
                'lib': lib,
                'path': path,
                'exists': exists,
            })
        return {'status': 'success', 'episodes': episodes}
    except Exception as e:
        return {'status': 'error', 'message': str(e)}


def _delete_title_files(files, target, title, media_type, series_id=None, tmdb_id=None, progress=None):
    """单剧/单片删除的公共执行段：拿跨入口文件锁 → 删文件（与双库治理同一条 fail-safe 删除链：
    本地库=删 strm + 联动 CD2 源文件；分享库=只删 strm）→ 立即修补缓存（海报马上撤下）
    → 后台精准通知 Emby → 写审计。"""
    root = engine.L_ROOT if target == 'local' else engine.S_ROOT
    cloud = engine.CLOUD_L_ROOT if target == 'local' else None
    try:
        with engine.mutation_lock():
            kwargs = {'progress': progress} if progress is not None else {}
            result = engine.safe_delete_files(files, root, cloud, dry_run=False, **kwargs)
    except engine.MutationBusy as e:
        return {'status': 'busy', 'message': str(e)}

    # 实际已删掉的文件（fail-safe 可能因云端源文件找不到而保留 strm）→ Emby 侧路径；空目录一并通知
    gone = [f for f in files if not f.exists()]
    emby_paths = set()
    for f in gone:
        emby_paths.add(engine.container_to_emby_path(f, target))
        for d in (f.parent, f.parent.parent):
            if not d.exists():
                emby_paths.add(engine.container_to_emby_path(d, target))

    engine.invalidate_media_caches(keep_emby_lib=True)
    series_removed, entry, movie_entries = False, None, None
    cache_sync_error = ''
    try:
        if series_id is not None:
            info = engine.patch_emby_lib_cache_after_series_delete(series_id, target)
            series_removed = info['remaining'] == 0
            entry = info['entry']
            if series_removed and info['series_path']:
                emby_paths.add(info['series_path'])   # 整部剧都没了：连剧目录一起通知
        elif tmdb_id is not None and gone:
            engine.patch_emby_lib_cache_after_movie_delete(tmdb_id, target)
            movie_entries = [row for row in (morning.read_emby_lib_cache(max_age=None) or {}).get('movies') or []
                             if str(row.get('tmdb_id') or '') == str(tmdb_id)]
    except Exception as e:
        engine.log.warning('删除后缓存修补失败: %s', e)
        cache_sync_error = '文件删除已执行，片库状态同步失败，请刷新总览核实：' + str(e)
        result.setdefault('errors', []).append(cache_sync_error)
    engine.notify_emby_deleted(emby_paths)    # 后台线程，不阻塞返回

    target_cn = '本地库' if target == 'local' else '分享库'
    cloud_note = '未处理云端源文件' if target == 'share' else ('删除 %d 个云端源文件' % result['cloud_removed'])
    engine.write_audit_log(
        '单剧删除',
        '《%s》删除%s：%d 个 strm' % (title, target_cn, result['strm_removed']),
        [
            '类型：%s' % media_type,
            '目标：%s' % target_cn,
            'strm 删除：%d 个' % result['strm_removed'],
            '云端源文件：%s' % cloud_note,
        ] + (['错误：%s' % e for e in result.get('errors', [])[:3]])
    )
    return {
        'status': 'success', 'dry_run': False, 'target': target,
        'count': len(files),
        'strm_removed': result['strm_removed'],
        'cloud_removed': result['cloud_removed'],
        'errors': result.get('errors', []),
        'series_removed': series_removed, 'entry': entry,
        'movie_entries': movie_entries, 'cache_sync_error': cache_sync_error,
        'movie_target_removed': tmdb_id is not None and len(gone) == len(files),
    }


@router.post('/api/emby/{media}/batch-delete/preview/start', dependencies=[Depends(auth)])
def api_batch_preview_start(media: str, body: dict = None):
    if media not in ('series', 'movies'):
        raise HTTPException(status_code=400, detail='无效媒体类型')
    return batch_library_delete.start_preview(dict(body or {}, media=media))


@router.get('/api/emby/{media}/batch-delete/preview/status', dependencies=[Depends(auth)])
def api_batch_preview_status(media: str, job_id: str):
    if media not in ('series', 'movies'):
        raise HTTPException(status_code=400, detail='无效媒体类型')
    return batch_library_delete.preview_status(job_id)


@router.post('/api/emby/series/batch-delete/preview', dependencies=[Depends(auth)])
def api_batch_series_preview(body: dict = None):
    return batch_library_delete.preview(dict(body or {}, media='series'))


@router.post('/api/emby/series/batch-delete/execute', dependencies=[Depends(auth)])
def api_batch_series_execute(body: dict = None):
    return batch_library_delete.execute(body, _delete_title_files)


@router.post('/api/emby/movies/batch-delete/preview', dependencies=[Depends(auth)])
def api_batch_movies_preview(body: dict = None):
    return batch_library_delete.preview(dict(body or {}, media='movies'))


@router.post('/api/emby/movies/batch-delete/execute', dependencies=[Depends(auth)])
def api_batch_movies_execute(body: dict = None):
    return batch_library_delete.execute(body, _delete_title_files)


@router.post('/api/emby/series/{series_id}/delete', dependencies=[Depends(auth)])
def api_emby_series_delete(series_id: str, body: dict = None):
    body = body or {}
    if body.get('async_delete') and not body.get('dry_run', True):
        identity = ('series', series_id, str(body.get('target', '')).lower())
        return deletion_progress.submit(body.get('operation_id'), identity,
            lambda progress: _series_delete(series_id, body, progress))
    return _series_delete(series_id, body)


def _series_delete(series_id, body, progress=None):
    """删除某剧的全部文件
    target: local | share
    dry_run: True -> 只返回数量，不真删
    """
    body = body or {}
    target = str(body.get('target', '')).lower()
    dry_run = bool(body.get('dry_run', True))
    series_name = str(body.get('series_name', '')).strip() or '(未命名)'
    media_type = str(body.get('media_type', '')).strip() or '剧集'

    if target not in ('local', 'share'):
        return {'status': 'error', 'message': 'target 必须是 local 或 share'}

    try:
        cached = morning.read_emby_lib_cache(max_age=None) or {}
        entry = next((row for row in cached.get('series') or []
                      if str(series_id) in {str(i) for i in row.get('series_ids') or [row.get('id')] if i}), None)
        row = dict(entry or {})
        row.update(members=library_identity.member_ids(entry or {'id': series_id}, 'series_ids'), target=target)
        paths, _ = library_identity.series_files(engine, row)
        from pathlib import Path
        files = [Path(path) for path in paths]

        if not files:
            return {'status': 'success', 'count': 0, 'target': target,
                    'message': '该库无此剧文件', 'ghost': True}
        from app import cloud_bridge
        root = engine.L_ROOT if target == 'local' else engine.S_ROOT
        if dry_run:
            return {'status': 'success', 'dry_run': True, 'count': len(files), 'target': target,
                    **cloud_bridge.capture(files, root, target == 'local')}
        cloud_bridge.confirm(body.get('source_preview_id'), files, root, target == 'local')
        return _delete_title_files(files, target, series_name, media_type, series_id=series_id, progress=progress)
    except Exception as e:
        return {'status': 'error', 'message': str(e)}


@router.post('/api/emby/series/{series_id}/resync', dependencies=[Depends(auth)])
def api_emby_series_resync(series_id: str):
    """不删任何文件：以磁盘真实文件为准，重算/移除片库映射缓存里的这部剧，并通知 Emby 刷新。
    用于清掉「文件已经没了但海报还在」的失效条目。"""
    try:
        info = engine.patch_emby_lib_cache_after_series_delete(series_id)
        engine.invalidate_media_caches(keep_emby_lib=True)
        if info['remaining'] == 0 and info['series_path']:
            engine.notify_emby_deleted([info['series_path']])
        return {'status': 'success', 'remaining': info['remaining'],
                'series_removed': info['remaining'] == 0, 'entry': info['entry']}
    except Exception as e:
        return {'status': 'error', 'message': str(e)}


@router.post('/api/emby/movie/delete_by_tmdb', dependencies=[Depends(auth)])
def api_movie_delete(body: dict = None):
    body = body or {}
    if body.get('async_delete') and not body.get('dry_run', True):
        identity = ('movie', str(body.get('tmdb_id', '')), str(body.get('target', '')).lower())
        return deletion_progress.submit(body.get('operation_id'), identity,
            lambda progress: _movie_delete(body, progress))
    return _movie_delete(body)


@router.get('/api/emby/delete/progress/{operation_id}', dependencies=[Depends(auth)])
def api_delete_progress(operation_id: str, response: Response = None):
    if response is not None:
        response.headers['Cache-Control'] = 'no-store'
    rec = deletion_progress.read(operation_id)
    if rec is None:
        raise HTTPException(404, '删除记录不存在或容器已重启，请同步片库核实结果，不要重复提交')
    rec.pop('identity', None)
    rec.pop('updated', None)
    return {'status': 'success', **rec}


def _movie_delete(body, progress=None):
    """按 tmdb_id 删除电影（扫目录名，不依赖 Emby 索引）"""
    body = body or {}
    tmdb_id = str(body.get('tmdb_id', '')).strip()
    target = str(body.get('target', '')).lower()
    dry_run = bool(body.get('dry_run', True))
    movie_name = str(body.get('name', '')).strip() or '(未命名)'

    if not tmdb_id.isdigit():
        return {'status': 'error', 'message': '缺少 tmdb_id'}
    if target not in ('local', 'share'):
        return {'status': 'error', 'message': 'target 必须是 local 或 share'}

    root = engine.L_ROOT if target == 'local' else engine.S_ROOT
    files = engine.find_movie_strms_by_tmdb(root, tmdb_id)
    if not files:
        return {'status': 'success', 'count': 0, 'target': target}
    from app import cloud_bridge
    try:
        if dry_run:
            return {'status': 'success', 'dry_run': True, 'count': len(files), 'target': target,
                    **cloud_bridge.capture(files, root, target == 'local')}
        cloud_bridge.confirm(body.get('source_preview_id'), files, root, target == 'local')
    except ValueError as error:
        return {'status':'error', 'message':str(error)}
    return _delete_title_files(files, target, movie_name, '电影', tmdb_id=tmdb_id, progress=progress)
