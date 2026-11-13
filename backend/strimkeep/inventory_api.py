"""Inventory read-only API; no media mutation endpoints."""
import asyncio
import logging
import json
import re
import time
from datetime import date
from urllib.parse import quote, urlsplit

from fastapi import Depends, HTTPException, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal

from .inventory import missing_season, project, registered_rows, work_complete
from .inventory_fs import scan as filesystem_rows
from .inventory_audit import audit_roots
from .inventory_preview import governance_preview
from .inventory_revision import governance_revision
from .inventory_sources import source_fingerprints
from .library_mapping import NO_STORE
from .library_mapping import detail as mapping_detail
from .library_mapping import page as mapping_page
from .library_mapping import preview as mapping_preview
from .library_mapping import remember_status
from .organize_options import library_root
from .quality import public_policy


class PreviewRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    tie: Literal['cloud', 'share'] = 'cloud'


class ManualComplete(BaseModel):
    model_config = ConfigDict(extra='forbid')
    complete: bool


class InventoryConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')
    tie: Literal['cloud', 'share'] = 'cloud'
    single_quality: bool = False
    single_gaps: bool = False
    # 决策模型（对应1.2.12 口径 strategy.decision）：quality_first / keep_local / keep_share
    decision: Literal['quality_first', 'keep_local', 'keep_share'] = 'quality_first'
    # 特别篇策略：compare（比画质）/ delete（两库都删）/ ignore（不动）
    special_action: Literal['compare', 'delete', 'ignore'] = 'compare'
    # 多季保护：compare（不让任何一边被掏空）/ off（逐季独立择优）
    multi_season_protect: Literal['compare', 'off'] = 'compare'
    season_ratio: float = Field(default=0.9, ge=0.5, le=1.0, allow_inf_nan=False)
    quiet_minutes: int = Field(default=15, ge=0, le=1440)
    scan_schedule_enabled: bool = False
    scan_interval_hours: int = Field(default=6, ge=1, le=168)
    # 白名单（1.2.12 exempt_keywords）：作品名命中任一关键词就豁免，不参与清理判定
    exempt_keywords: list[str] = Field(default_factory=list, max_length=50)
    # 作品身份：title_year（剧名+年份，默认）/ tmdb（TMDB 优先；
    # 同一编号对应多个剧名或年份时自动回退到剧名+年份）
    match_strategy: Literal['title_year', 'tmdb'] = 'title_year'


def inventory_config_value(value):
    return InventoryConfig.model_validate(value or {}).model_dump()


def valid_official_episodes(episodes):
    if not isinstance(episodes, list) or not episodes or any(not isinstance(ep, dict) for ep in episodes):
        return False
    numbers = [ep.get('episode_number') for ep in episodes]
    return (all(type(number) is int and number > 0 for number in numbers)
            and len(set(numbers)) == len(numbers))


class StaleSelection(BaseModel):
    model_config = ConfigDict(extra='forbid')
    file_ids: list[str] = Field(min_length=1, max_length=100)


class OrphanItem(BaseModel):
    model_config = ConfigDict(extra='forbid')
    path: str = Field(min_length=1, max_length=4096)
    fingerprint: str = Field(pattern=r'^[a-f0-9]{64}$')
    kind: Literal['directory', 'file'] = 'directory'
    source: str = Field(default='', max_length=4096)


class OrphanSelection(BaseModel):
    model_config = ConfigDict(extra='forbid')
    items: list[OrphanItem] = Field(min_length=1, max_length=100)


def install_inventory(app, store, admin):
    mapping_cache = {'at': 0.0, 'snapshot': None, 'refreshing': False, 'generation': 0, 'error': ''}
    mapping_lock = asyncio.Lock()
    MAPPING_TTL = 60.0

    def invalidate_mapping_cache():
        """删除/手动完结后让片库映射缓存立刻失效，两个页面马上看到同一份库存。"""
        mapping_cache.update(snapshot=None, at=0.0, error='')
        mapping_cache['generation'] += 1

    app.state.inventory_invalidate = invalidate_mapping_cache

    async def refresh_mapping():
        """后台重扫：先让页面拿到上一份结果，扫完再替换（1.2.12 口径的 stale 语义）。"""
        async with mapping_lock:
            if mapping_cache['refreshing']:
                return
            mapping_cache['refreshing'] = True
            generation = mapping_cache['generation']
        try:
            snapshot = await asyncio.to_thread(scan)
            async with mapping_lock:
                if generation == mapping_cache['generation']:
                    mapping_cache.update(snapshot=snapshot, at=time.monotonic(), error='')
        except (OSError, ValueError):
            async with mapping_lock:
                if generation == mapping_cache['generation']:
                    mapping_cache['error'] = '后台库存刷新失败，请检查库目录。'
        finally:
            mapping_cache['refreshing'] = False

    def orphan_roots():
        config = store.get('organize_config', {}) or {}
        if not isinstance(config, dict):
            raise ValueError('整理配置无效')
        archive, labels = str(config.get('archive_cid') or ''), config.get('directory_labels') or {}
        if not config.get('library_dir_name') and (not isinstance(labels, dict) or not labels.get(archive)):
            raise ValueError('网盘库根目录名称未配置')
        return [library_root(config), '分享影视库']

    def registered_under(path):
        prefix = path.rstrip('/') + '/'
        return any(relative.startswith(prefix) for _, _, relative in registered_rows(store))

    @app.post('/api/v1/inventory/orphans/scan', dependencies=[Depends(admin)])
    async def scan_orphans():
        from .inventory_orphans import scan as scan_residues, sidecars_for_deleted
        from .internal_delete import recent
        try:
            async with app.state.organizer.publication_gate:
                roots = orphan_roots()
                result = await asyncio.to_thread(scan_residues, app.state.strms, roots)
                rows = registered_rows(store)
                registered = {relative for _, _, relative in rows}
                before = len(result['items'])
                result['items'] = [item for item in result['items'] if not any(
                    relative.startswith(item['path'].rstrip('/') + '/') for _, _, relative in rows)]
                result['skipped'] += before - len(result['items'])
                directories = [item['path'] + '/' for item in result['items']]
                seen = set()
                for source in sorted(recent(store)):
                    if source in registered or any(source.startswith(path) for path in directories):
                        continue
                    try:
                        candidates = await asyncio.to_thread(sidecars_for_deleted, app.state.strms, roots, source)
                    except (OSError, ValueError):
                        continue
                    for item in candidates:
                        if item['path'] not in seen:
                            result['items'].append(item)
                            seen.add(item['path'])
                            if len(result['items']) > 1000:
                                raise ValueError('候选超过 1000 项，请缩小扫描范围')
                return result
        except (OSError, ValueError) as exc:
            raise HTTPException(409, f'残留扫描失败：{exc}') from None

    @app.post('/api/v1/inventory/orphans/clean', dependencies=[Depends(admin)])
    async def clean_orphans(body: OrphanSelection):
        from .inventory_orphans import clean_one, clean_sidecar
        from .internal_delete import recent
        paths = [item.path for item in body.items]
        if len(set(paths)) != len(paths):
            raise HTTPException(422, '不能重复选择同一目录')
        result = []
        async with app.state.organizer.publication_gate:
            if store.db.execute("SELECT 1 FROM organize_jobs WHERE state='running' LIMIT 1").fetchone():
                raise HTTPException(409, '整理任务正在运行，请稍后重试')
            if store.db.execute('SELECT 1 FROM share_wash_intents LIMIT 1').fetchone():
                raise HTTPException(409, '分享整理存在未完成的文件操作')
            try:
                roots = orphan_roots()
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from None
            for item in body.items:
                try:
                    if item.kind == 'file':
                        if item.source not in recent(store) or any(path == item.source for _, _, path in registered_rows(store)):
                            raise ValueError('原 STRM 的删除记录或台账状态已变化')
                        row = await asyncio.to_thread(clean_sidecar, app.state.strms, roots, item.source, item.path,
                                                      item.fingerprint, store.root / 'cleanup' / 'orphans')
                    else:
                        if registered_under(item.path):
                            raise ValueError('目录仍有已登记的 STRM，已保留')
                        row = await asyncio.to_thread(clean_one, app.state.strms, roots, item.path,
                                                      item.fingerprint, store.root / 'cleanup' / 'orphans')
                except (OSError, ValueError) as exc:
                    row = {'path': item.path, 'files_removed': 0, 'dirs_removed': 0, 'errors': [str(exc)]}
                result.append(row)
        return {'items': result}
    def scan():
        settings = inventory_config_value(store.get('inventory_config', {}))
        config = store.get('organize_config', {}) or {}
        roots = {'cloud': library_root(config if isinstance(config, dict) else {}),
                 'share': '分享影视库'}
        # 库存 = 目录里真实存在的 STRM；台账只用来挂上来源编号。
        rows = filesystem_rows(app.state.strms, roots, store.db)
        snapshot = project(rows, app.state.strms, settings.get('match_strategy', 'title_year'))
        done = {row['key'] for row in store.db.execute("SELECT key FROM meta WHERE key LIKE 'inventory-manual-complete:tv:%' AND value='true'")}
        for item in snapshot['items']:
            item['manual_complete'] = f"inventory-manual-complete:tv:{item['tmdb_id']}" in done if item['kind'] == 'tv' else False
        return snapshot

    async def mapping_snapshot(force=False):
        snapshot = mapping_cache['snapshot']
        age = time.monotonic() - mapping_cache['at']
        if snapshot is not None and not force:
            if age > MAPPING_TTL:
                # 过期就先返回这份，再后台重扫：页面不再等一次全库遍历。
                asyncio.create_task(refresh_mapping())
            return snapshot
        async with mapping_lock:
            fresh = mapping_cache['snapshot'] is not None and time.monotonic() - mapping_cache['at'] < MAPPING_TTL
            if fresh and not force:
                return mapping_cache['snapshot']
            mapping_cache['generation'] += 1
            snapshot = await asyncio.to_thread(scan)
            mapping_cache.update(snapshot=snapshot, at=time.monotonic(), error='')
            return snapshot

    def mapping_meta():
        age = time.monotonic() - mapping_cache['at']
        return {'stale': age > MAPPING_TTL, 'age_sec': int(age),
                'refreshing': bool(mapping_cache['refreshing']), 'refresh_error': mapping_cache['error']}

    @app.get('/api/v1/inventory', dependencies=[Depends(admin)])
    async def inventory():
        try:
            snapshot = await mapping_snapshot(force=True)
            return snapshot
        except (OSError, ValueError):
            raise HTTPException(409, '库存读取失败，请检查 STRM 根目录。') from None

    from . import emby_libraries

    async def library_map(force=False):
        """Emby 媒体库 → 作品的映射（缓存 30 分钟）。读了失败也只影响分类来源。"""
        cached_value = emby_libraries.cached(store)
        if cached_value and not force:
            return cached_value
        connection = app.state.organizer.emby.connection()
        if not connection:
            return None
        try:
            value = await emby_libraries.fetch(app.state.http, *connection)
            emby_libraries.remember(store, value)
            return value
        except Exception as exc:  # noqa: BLE001 - Emby 不可用时退回目录分类
            emby_libraries.remember_failure(store, exc)
            return None

    @app.get('/api/v1/inventory/emby-libraries', dependencies=[Depends(admin)])
    async def inventory_emby_libraries(refresh: int = 0):
        value = await library_map(bool(refresh))
        if not value:
            return {'status': 'unavailable', 'source': 'path', 'libraries': [],
                    'message': '未连接 Emby 或读取失败，暂按目录关键词分类'}
        if value.get('error'):
            return {'status': 'error', 'source': 'path', 'libraries': [],
                    'message': str(value['error'])}
        return {'status': 'success' if value.get('libraries') else 'empty',
                'source': 'emby' if value.get('libraries') else 'path',
                'libraries': value.get('libraries') or [],
                'read_by': value.get('source') or '',
                'message': '；'.join(value.get('errors') or [])}

    @app.get('/api/v1/inventory/mapping/preview', dependencies=[Depends(admin)])
    async def inventory_mapping_preview(refresh: int = 0):
        """首屏：每类最多 48 张卡，不下发任何 STRM 路径（1.2.12 `preview`）。"""
        try:
            snapshot = await mapping_snapshot(bool(refresh))
        except (OSError, ValueError):
            raise HTTPException(409, '库存读取失败，请检查 STRM 根目录。') from None
        names = await library_map()
        data = await asyncio.to_thread(mapping_preview, snapshot, store,
                                       (names or {}).get('by_tmdb') if not (names or {}).get('error') else None)
        return JSONResponse({'status': 'success', 'preview': True,
                             'inventory_complete': snapshot['complete'],
                             'category_source': 'emby' if (names or {}).get('by_tmdb') else 'path',
                             **mapping_meta(), **data},
                            headers=dict(NO_STORE))

    @app.get('/api/v1/inventory/mapping/page', dependencies=[Depends(admin)])
    async def inventory_mapping_page(media: str = 'tv', status: str = 'all',
                                     scope: str = 'all', keyword: str = '', category: str = 'all', offset: int = 0,
                                     limit: int = 40, refresh: int = 0):
        """服务端分页：过滤/计数先算完再切片，返回全局总数（1.2.12 `page`）。"""
        try:
            snapshot = await mapping_snapshot(bool(refresh))
        except (OSError, ValueError):
            raise HTTPException(409, '库存读取失败，请检查 STRM 根目录。') from None
        try:
            names = await library_map()
            data = await asyncio.to_thread(mapping_page, snapshot, store, media=media,
                                           status=status, scope=scope, keyword=keyword, category=category,
                                           offset=offset, limit=limit,
                                           library_map=(names or {}).get('by_tmdb')
                                           if not (names or {}).get('error') else None)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        return JSONResponse({'status': 'success', 'inventory_complete': snapshot['complete'],
                             'issues': snapshot['issues'][:100], 'issue_count': len(snapshot['issues']),
                             'category_source': 'emby' if (names or {}).get('by_tmdb') else 'path',
                             **mapping_meta(), **data}, headers=dict(NO_STORE))

    @app.get('/api/v1/inventory/mapping/unidentified-poster/{item_id}', dependencies=[Depends(admin)])
    async def inventory_unidentified_poster(item_id: str):
        from .library_mapping import unidentified_cards
        from .inventory_unidentified_poster import read_unidentified_poster
        if not re.fullmatch(r'unmatched-[0-9a-f]{20}', item_id):
            raise HTTPException(404, '未找到海报。')
        snapshot = await mapping_snapshot()
        item = next((v for v in unidentified_cards(snapshot) if v['id'] == item_id), None)
        if item is None:
            raise HTTPException(404, '未找到作品。')
        data = await asyncio.to_thread(read_unidentified_poster, app.state.strms, item)
        if not data:
            raise HTTPException(404, '未找到海报。')
        return Response(content=data[0], media_type=data[1], headers=dict(NO_STORE))

    @app.get('/api/v1/inventory/mapping/item', dependencies=[Depends(admin)])
    async def inventory_mapping_item(kind: str, tmdb_id: int):
        """详情按 id 取单部作品（含完整文件清单），列表不再携带路径。"""
        try:
            snapshot = await mapping_snapshot()
        except (OSError, ValueError):
            raise HTTPException(409, '库存读取失败，请检查 STRM 根目录。') from None
        try:
            names = await library_map()
            item = await asyncio.to_thread(
                mapping_detail, snapshot, store, kind, tmdb_id,
                (names or {}).get('by_tmdb') if not (names or {}).get('error') else None)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        if item is None:
            raise HTTPException(404, '作品不在片库中。')
        return JSONResponse({'status': 'success', 'item': item,
                             'category_source': 'emby' if names and not names.get('error') else 'path'},
                            headers=dict(NO_STORE))

    def audit():
        config = store.get('organize_config', {}) or {}
        if not isinstance(config, dict):
            raise ValueError('整理配置无效')
        archive = str(config.get('archive_cid') or '')
        labels = config.get('directory_labels') or {}
        if not config.get('library_dir_name') and (not isinstance(labels, dict) or not labels.get(archive)):
            raise ValueError('网盘库根目录名称未配置')
        roots = {'cloud': library_root(config), 'share': '分享影视库'}
        return audit_roots(app.state.strms, roots, registered_rows(store))

    @app.post('/api/v1/inventory/audit', dependencies=[Depends(admin)])
    async def inventory_audit():
        try:
            return await asyncio.to_thread(audit)
        except (OSError, ValueError) as exc:
            raise HTTPException(409, f'库存核对未完成：{exc}') from None

    def stale_root():
        config = store.get('organize_config', {}) or {}
        if not isinstance(config, dict):
            raise ValueError('整理配置无效')
        archive, labels = str(config.get('archive_cid') or ''), config.get('directory_labels') or {}
        if not config.get('library_dir_name') and (not isinstance(labels, dict) or not labels.get(archive)):
            raise ValueError('网盘库根目录名称未配置')
        return library_root(config)

    @app.get('/api/v1/inventory/stale-records', dependencies=[Depends(admin)])
    async def stale_records():
        from .inventory_stale import candidates
        try:
            def read():
                return [{'file_id': row['file_id'], 'relative_path': row['relative_path']}
                        for row in candidates(store.db, app.state.strms, stale_root())][:100]
            return {'items': await asyncio.to_thread(read)}
        except (OSError, ValueError):
            raise HTTPException(409, '失效记录核查未完成。') from None

    @app.post('/api/v1/inventory/stale-records/clear', dependencies=[Depends(admin)])
    async def clear_stale_records(body: StaleSelection):
        from .inventory_stale import clear
        try:
            async with app.state.organizer.publication_gate:
                def run():
                    if store.db.execute("SELECT 1 FROM organize_jobs WHERE state='running' LIMIT 1").fetchone():
                        raise ValueError('整理任务正在运行')
                    return clear(store.db, app.state.strms, stale_root(), body.file_ids)
                return await asyncio.to_thread(run)
        except (OSError, ValueError) as exc:
            raise HTTPException(409, f'失效记录未清理：{exc}') from None

    @app.get('/api/v1/inventory/config', dependencies=[Depends(admin)])
    async def inventory_config():
        return inventory_config_value(store.get('inventory_config', {}))

    @app.put('/api/v1/inventory/config', dependencies=[Depends(admin)])
    async def inventory_config_save(body: InventoryConfig):
        # Older clients submit only their visible controls. Preserve every
        # other governance setting instead of silently resetting it.
        current = inventory_config_value(store.get('inventory_config', {}))
        value = inventory_config_value({**current, **body.model_dump(exclude_unset=True)})
        store.set_many({'inventory_config': value})
        return value

    scan_lock = asyncio.Lock()

    async def run_governance_scan(body: PreviewRequest):
        try:
            settings = inventory_config_value(store.get('inventory_config', {}))
            roots = {'cloud': stale_root(), 'share': '分享影视库'}
            try:
                account = app.state.organizer.index.account({})
            except Exception:
                # 没有 115 账号不再让整页治理不可用：库存照常展示，
                # 只是网盘侧文件核不上来源、进不了删除计划（分享侧不受影响）。
                account = ''
            from .inventory_partition import partition
            rows = await asyncio.to_thread(filesystem_rows, app.state.strms, roots, store.db)
            scoped = await asyncio.to_thread(partition, app.state.strms, roots, rows,
                                             settings.get('match_strategy', 'title_year'),
                                             store.db, account)
            snapshot, sources = scoped['projection'], scoped['sources']
            policy = public_policy(store.get('upgrade_policy'))
            result = await asyncio.to_thread(
                governance_preview, snapshot, policy, body.tie,
                settings['season_ratio'], settings['quiet_minutes'], None,
                settings['exempt_keywords'], settings['decision'],
                settings['special_action'], settings['multi_season_protect'])
            # 台账不再决定「库里有什么」：核不上来源的文件只记录，不隔离整部作品。
            result['isolated_works'] = 0
            result['isolated_records'] = len(scoped['unsupported']) + len(snapshot['issues'])
            result['audit'] = None
            result['scan_revision'] = governance_revision(snapshot, policy, {**settings, 'tie': body.tie}, sources)
            # 建计划时复用这次刚算出的库存，只重新核一遍「文件没变、来源没变」：
            # 以前 capture() 会把整库再扫两遍，一次扫描白等 2~3 秒。
            blocked_paths = {(item['side'], item['path']) for item in scoped['unsupported']}

            def recheck():
                fresh = filesystem_rows(app.state.strms, roots, store.db)
                supported = [row for row in fresh if (row[0], row[2]) not in blocked_paths]
                try:
                    now_sources = source_fingerprints(store.db, supported, account)
                except ValueError:
                    # 期间有文件变成「来源核不上」：退回完整分区，宁可慢也不猜。
                    again = partition(app.state.strms, roots, fresh,
                                      settings.get('match_strategy', 'title_year'), store.db, account)
                    return {'rows': again['rows'], 'sources': again['sources']}
                return {'rows': sorted(supported), 'sources': now_sources}

            result['_reuse'] = {'rows': scoped['rows'], 'scoped': scoped, 'recheck': recheck}
            return result
        except (OSError, ValueError) as exc:
            reason = str(exc)
            allowed = ('有异常路径无法确定作品归属', 'TMDB 匹配模式下存在缺少编号的异常文件', '异常记录过多')
            detail = reason if isinstance(exc, ValueError) and reason.startswith(allowed) else '库存或洗版配置无法核实'
            raise HTTPException(409, detail + '，未生成计划。') from None

    def scan_selections(result, settings):
        candidates = []
        for v in result.get('dual', []):
            if v.get('incomplete_sides') and not settings.get('single_gaps'):
                continue
            candidates.append({'kind': v['kind'], 'tmdb_id': v['tmdb_id'], 'season': v.get('season'),
                               'winner': v.get('winner'), 'action_kind': v.get('action_kind'),
                               'action_reason': v.get('action_reason'), '_type': 'dual',
                               '_side': 'cloud' if v.get('action_kind') == 'loc' else 'share'})
        for v in result.get('single', []):
            reason = v.get('reason')
            # 单库结果只剩「库内多版本」；缺集季统一走双库动作（残次品-删本地/删分享）。
            if reason != 'quality' or not settings['single_quality']:
                continue
            candidates.append({'kind': v['kind'], 'tmdb_id': v['tmdb_id'], 'season': v.get('season'),
                               'episode': v.get('episode'), 'side': v['side'], 'reason': reason,
                               '_type': 'single', '_side': v['side']})
        conflicted = set()
        groups = {}
        for i, row in enumerate(candidates):
            groups.setdefault((row['kind'], row['tmdb_id'], row['season']), []).append((i, row))
        for group in groups.values():
            for position, (i, a) in enumerate(group):
                for j, b in group[position+1:]:
                    pair_reasons = {a.get('action_reason'), b.get('action_reason')}
                    explicit_s00 = (a['kind'] == 'tv' and a['season'] == 0 and
                                    pair_reasons == {'special_delete_local', 'special_delete_share'})
                    # 1.2.12：两侧各自残次时，各删各的（文件互不相交），不算冲突。
                    residual_pair = pair_reasons == {'incomplete_delete_local',
                                                     'incomplete_delete_share'}
                    allowed_pair = explicit_s00 or residual_pair
                    # 同一季两侧同时进计划：只有「双方各自的残次品」和「S00 显式两库都删」
                    # 允许；其余（例如一侧删本地、另一侧删分享的洗版结论）视为冲突。
                    if not allowed_pair and a['_type'] == b['_type'] == 'dual' and a['_side'] != b['_side']:
                        conflicted.update((i, j))
        selected = [{k: v for k, v in row.items() if not k.startswith('_')}
                    for i, row in enumerate(candidates) if i not in conflicted]
        return selected, len(conflicted)

    async def scan_and_remember(body):
        from .inventory_operations_api import PlanRequest
        async with scan_lock:
            before = inventory_config_value(store.get('inventory_config', {}))
            result = await run_governance_scan(body)
            reuse = result.pop('_reuse', None)
            settings = inventory_config_value(store.get('inventory_config', {}))
            if before != settings:
                raise HTTPException(409, '扫描期间治理设置已改变，请重新扫描。')
            if body.tie != settings['tie']:
                return result
            result['plan'] = None
            if result.get('scan_complete') and result.get('scan_revision'):
                selected, skipped = scan_selections(result, settings)
                result['plan_skipped_conflicts'] = skipped
                if len(selected) > 10000:
                    result['plan_error'] = '可处理项目超过 10000 项，请分批选择。'
                elif selected:
                    try:
                        request = PlanRequest(mode='governance', selections=selected,
                                              scan_revision=result['scan_revision'])
                        result['plan'] = await app.state.inventory_build_delete_plan(
                            request, ttl=7200, reuse=reuse)
                    except HTTPException as exc:
                        result['plan_error'] = str(exc.detail)
            def save_result():
                with store.db:
                    store.db.execute('INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)',
                                     ('inventory-last-scan', json.dumps({'at': time.time(), 'settings': settings, 'result': result}, ensure_ascii=False)))
            await asyncio.to_thread(save_result)
            return result

    @app.post('/api/v1/inventory/preview', dependencies=[Depends(admin)])
    async def inventory_preview(body: PreviewRequest):
        return await scan_and_remember(body)

    @app.get('/api/v1/inventory/last-scan', dependencies=[Depends(admin)])
    async def inventory_last_scan():
        from .inventory_plans import _load, PlanError
        saved = await asyncio.to_thread(store.meta_json, 'inventory-last-scan', {})
        settings = inventory_config_value(store.get('inventory_config', {}))
        if not isinstance(saved, dict) or saved.get('settings') != settings:
            return {'result': None}
        result = saved.get('result')
        plan = result.get('plan') if isinstance(result, dict) else None
        if plan:
            try:
                state = _load(store.db, plan['plan_id'])
                if state.get('snapshot', {}).get('inventory', {}).get('policy', {}).get('guard_revision') != 4:
                    return {'result': None, 'reason': '删除保护规则已更新，请重新扫描'}
                if state.get('state') != 'planned' or time.time() >= state.get('expires', 0):
                    return {'result': None, 'reason': '计划已执行或过期'}
            except (PlanError, KeyError, TypeError):
                return {'result': None, 'reason': '计划不可用'}
        return {'at': saved.get('at'), 'result': result}

    async def scheduled_scans():
        while True:
            try:
                settings = inventory_config_value(store.get('inventory_config', {}))
                last = store.meta_json('inventory-last-scheduled-run', 0) or 0
                if (settings['scan_schedule_enabled'] and not scan_lock.locked()
                        and time.time() - float(last) >= settings['scan_interval_hours'] * 3600):
                    try:
                        await scan_and_remember(PreviewRequest(tie=settings['tie']))
                        store.event('inventory', '双库治理定时扫描及计划生成完成。')
                    except Exception as exc:
                        store.event('inventory', '双库治理定时扫描失败：%s。' % type(exc).__name__)
                    finally:
                        await asyncio.to_thread(store.set_meta, 'inventory-last-scheduled-run', time.time())
            except Exception as exc:
                logging.getLogger(__name__).warning('双库定时扫描检查失败：%s', type(exc).__name__)
            await asyncio.sleep(30)

    app.state.inventory_scan_loop = scheduled_scans

    @app.put('/api/v1/inventory/tv/{tmdb_id}/manual-complete', dependencies=[Depends(admin)])
    async def inventory_manual_complete(tmdb_id: int, body: ManualComplete):
        if tmdb_id <= 0:
            raise HTTPException(422, '剧集编号无效。')
        try:
            snapshot = await asyncio.to_thread(scan)
        except (OSError, ValueError):
            raise HTTPException(409, '库存读取失败，请检查 STRM 根目录。') from None
        if not any(x['kind'] == 'tv' and x['tmdb_id'] == tmdb_id for x in snapshot['items']):
            raise HTTPException(404, '未找到该剧集。')
        await asyncio.to_thread(store.set_meta, f'inventory-manual-complete:tv:{tmdb_id}', body.complete)
        async with mapping_lock:
            mapping_cache['generation'] += 1
            if mapping_cache['snapshot'] is not None:
                for entry in mapping_cache['snapshot']['items']:
                    if entry['kind'] == 'tv' and entry['tmdb_id'] == tmdb_id:
                        entry['manual_complete'] = body.complete
        return {'tmdb_id': tmdb_id, 'manual_complete': body.complete}

    @app.get('/api/v1/inventory/tv/{tmdb_id}/missing', dependencies=[Depends(admin)])
    async def inventory_missing(tmdb_id: int):
        if tmdb_id <= 0:
            raise HTTPException(422, '剧集编号无效。')
        try:
            snapshot = await mapping_snapshot()
        except (OSError, ValueError):
            raise HTTPException(409, '库存读取失败，请检查 STRM 根目录。') from None
        item = next((x for x in snapshot['items'] if x['kind'] == 'tv' and x['tmdb_id'] == tmdb_id), None)
        if item is None:
            raise HTTPException(404, '未找到该剧集。')
        details = await app.state.organizer.tmdb(f'tv/{tmdb_id}')
        seasons = details.get('seasons') if isinstance(details, dict) else None
        if not isinstance(seasons, list) or len(seasons) > 100:
            raise HTTPException(502, 'TMDB 季列表不完整或超出支持范围。')
        numbers, seen = [], set()
        for entry in seasons:
            number = entry.get('season_number') if isinstance(entry, dict) else None
            if type(number) is not int or number < 0 or number > 999 or number in seen:
                raise HTTPException(502, 'TMDB 季列表格式异常。')
            seen.add(number)
            if number > 0:
                numbers.append(number)
        sem = asyncio.Semaphore(4)
        async def fetch(number):
            async with sem:
                return await app.state.organizer.tmdb(f'tv/{tmdb_id}/season/{number}')
        values = await asyncio.gather(*(fetch(number) for number in numbers), return_exceptions=True)
        result, errors = [], []
        for number, value in zip(numbers, values):
            episodes = value.get('episodes') if isinstance(value, dict) else None
            if not valid_official_episodes(episodes):
                errors.append(number)
                continue
            compared = missing_season(episodes, item, number, date.today())
            combined = set(compared['cloud']['present']) | set(compared['share']['present'])
            official = {ep['episode_number'] for ep in episodes if type(ep.get('episode_number')) is int and ep['episode_number'] > 0}
            compared['combined_missing'] = sorted(set(compared['expected']) - combined)
            compared['combined_extra'] = sorted(combined - official)
            for side in ('cloud', 'share'):
                compared[side]['extra'] = sorted(set(compared[side]['present']) - official)
            result.append({'season': number, 'official_known': True, **compared})
        # Keep locally present seasons visible even when TMDB omits them. They
        # must not be presented as complete or silently disappear from details.
        local_seasons = {row['season'] for side in ('cloud', 'share') for row in item[side]
                         if type(row.get('season')) is int and row['season'] >= 0}
        unmatched_seasons = sorted(local_seasons - set(numbers))
        not_compared = sorted(local_seasons - {entry['season'] for entry in result})
        for number in not_compared:
            compared = missing_season([], item, number, date.today())
            result.append({'season': number, 'official_known': False, 'combined_missing': [], 'combined_extra': [], **compared})
        result.sort(key=lambda entry: entry['season'])
        reply = {'tmdb_id': tmdb_id, 'inventory_complete': work_complete(snapshot, tmdb_id),
                'inventory_global_complete': snapshot['complete'], 'seasons': result,
                'failed_seasons': errors, 'unmatched_seasons': unmatched_seasons,
                'tmdb_complete': not errors and not any(number > 0 for number in unmatched_seasons),
                'series_status': details.get('status') or '',
                'combined_missing': sum(len(s['combined_missing']) for s in result),
                'combined_extra': sum(len(s['combined_extra']) for s in result),
                'cloud_missing': sum(len(s['cloud']['missing']) for s in result),
                'share_missing': sum(len(s['share']['missing']) for s in result)}
        # 对照结果写进状态缓存：映射页分页/首屏直接读，不再为每张卡回源 TMDB。
        try:
            await asyncio.to_thread(remember_status, store, tmdb_id, reply, item)
        except Exception:
            pass
        return reply

    @app.get('/api/v1/inventory/{kind}/{tmdb_id}/emby', dependencies=[Depends(admin)])
    async def inventory_emby(kind: str, tmdb_id: int):
        if kind not in ('tv', 'movie') or tmdb_id <= 0:
            raise HTTPException(422, '作品类型或编号无效。')
        connection = app.state.organizer.emby.connection()
        url = str(store.get('emby_url') or '').strip().rstrip('/')
        parsed = urlsplit(url)
        if not connection or parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise HTTPException(409, '请先配置可访问的 Emby 地址。')
        from .network import emby_request
        api_url, key = connection
        try:
            response = await emby_request(app.state.http, 'GET', api_url + '/Items', headers={'X-Emby-Token': key},
                params={'Recursive': 'true', 'IncludeItemTypes': 'Series' if kind == 'tv' else 'Movie',
                        'Fields': 'ProviderIds', 'Limit': 20000}, timeout=15)
            response.raise_for_status()
            data = response.json()
            items, total = data.get('Items'), data.get('TotalRecordCount')
            if not isinstance(items, list) or type(total) is not int or total > len(items):
                raise ValueError('incomplete Emby response')
        except Exception:
            raise HTTPException(502, 'Emby 片库读取失败或数据不完整。') from None
        matches = []
        for item in items:
            if not isinstance(item, dict):
                continue
            providers, item_id = item.get('ProviderIds') or {}, str(item.get('Id') or '')
            if (isinstance(providers, dict) and str(providers.get('Tmdb') or '') == str(tmdb_id)
                    and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', item_id)):
                matches.append({'name': str(item.get('Name') or '')[:200],
                                'url': url + '/web/index.html#!/item?id=' + quote(item_id, safe='')})
        return {'items': matches}

    @app.get('/api/v1/inventory/tv/{tmdb_id}/season/{season}', dependencies=[Depends(admin)])
    async def inventory_season(tmdb_id: int, season: int):
        if tmdb_id <= 0 or not 1 <= season <= 999:
            raise HTTPException(422, '剧集或季号无效。')
        try:
            snapshot = await mapping_snapshot()
        except (OSError, ValueError):
            raise HTTPException(409, '库存读取失败，请检查 STRM 根目录。') from None
        item = next((x for x in snapshot['items'] if x['kind'] == 'tv' and x['tmdb_id'] == tmdb_id), None)
        if item is None:
            raise HTTPException(404, '未找到该剧集。')
        # The application's CloudError handler preserves TMDB error codes.
        data = await app.state.organizer.tmdb(f'tv/{tmdb_id}/season/{season}')
        episodes = data.get('episodes') if isinstance(data, dict) else None
        if not valid_official_episodes(episodes):
            raise HTTPException(502, 'TMDB 季集数据不完整。')
        return {'tmdb_id': tmdb_id, 'season': season, 'inventory_complete': work_complete(snapshot, tmdb_id),
                'inventory_global_complete': snapshot['complete'],
                **missing_season(episodes, item, season, date.today())}
