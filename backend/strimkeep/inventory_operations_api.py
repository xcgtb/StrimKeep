"""Explicit, snapshot-bound inventory deletion plans."""
import asyncio
import json
from typing import Literal

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .inventory import project, registered_rows
from .inventory_api import inventory_config_value
from .inventory_delete_adapter import delete_many, delete_one
from .inventory_executor import execute_simulated
from .inventory_operations import dual_actions, manual_actions, single_actions
from .inventory_plans import PlanError, _load, create_plan
from .inventory_preview import governance_preview
from .inventory_revision import governance_revision
from .inventory_snapshot import capture_snapshot
from .inventory_sources import source_fingerprints
from .organize_options import library_root
from .quality import public_policy


class Selection(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: Literal['tv', 'movie']
    tmdb_id: int = Field(gt=0)
    side: Literal['cloud', 'share', 'both'] = 'both'
    season: int | None = Field(default=None, ge=0, le=999)
    episode: int | None = Field(default=None, ge=1, le=9999)
    reason: Literal['quality'] | None = None
    winner: Literal['cloud', 'share'] | None = None
    action_kind: Literal['loc', 'shr'] | None = None
    action_reason: str | None = Field(default=None, max_length=100)


class PlanRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    mode: Literal['manual', 'dual', 'single', 'governance']
    selections: list[Selection] = Field(min_length=1, max_length=10000)
    scan_revision: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')


def install_inventory_operations(app, store, admin):
    active, errors, current_actions = {}, {}, {}

    def capture(reuse=None):
        engine = app.state.organizer
        if store.db.execute("SELECT 1 FROM organize_jobs WHERE state='running' LIMIT 1").fetchone():
            raise ValueError('整理任务正在运行，请等待任务结束')
        if store.db.execute('SELECT 1 FROM share_wash_intents LIMIT 1').fetchone():
            raise ValueError('分享整理存在未完成的文件操作')
        config = store.get('organize_config', {}) or {}
        if not isinstance(config, dict):
            raise ValueError('整理配置无效')
        archive, labels = str(config.get('archive_cid') or ''), config.get('directory_labels') or {}
        if not config.get('library_dir_name') and (not isinstance(labels, dict) or not labels.get(archive)):
            raise ValueError('网盘库根目录名称未配置')
        roots = {'cloud': library_root(config), 'share': '分享影视库'}
        account = engine.index.account({})
        tie_config = inventory_config_value(store.get('inventory_config', {}))
        policy = {'upgrade': public_policy(store.get('upgrade_policy')), 'inventory': tie_config, 'guard_revision': 4}
        from .inventory_fs import scan as filesystem_rows
        from .inventory_partition import partition
        if reuse:
            # 预览刚扫过：复用那次的库存，只让快照自己再核一遍「文件/来源有没有变」。
            selected, scope_reader = reuse['scoped'], reuse['recheck']
        else:
            def scoped():
                # 目录是库存事实：每次核对都重新扫目录，台账只用来挂来源编号。
                rows = filesystem_rows(app.state.strms, roots, store.db)
                return partition(app.state.strms, roots, rows,
                                 tie_config.get('match_strategy', 'title_year'), store.db, account)
            selected, scope_reader = scoped(), scoped
        rows = selected['rows']
        def sources():
            return source_fingerprints(store.db, rows, account)
        snapshot = capture_snapshot(app.state.strms, roots, rows, policy,
                                    source_reader=sources, scope_reader=scope_reader,
                                    unsupported=selected['unsupported'])
        return snapshot, rows

    async def build_delete_plan(body: PlanRequest, ttl=900, reuse=None):
        engine = app.state.organizer
        try:
            async with engine.publication_gate:
                snapshot, rows = await asyncio.to_thread(capture, reuse)
                projection = (reuse['scoped']['projection'] if reuse
                              else await asyncio.to_thread(
                                  project, rows, app.state.strms,
                                  snapshot['policy']['inventory'].get('match_strategy', 'title_year')))
                selections = [v.model_dump() for v in body.selections]
                if body.mode == 'manual':
                    actions = manual_actions(projection, selections, snapshot['unsupported'])
                else:
                    current_revision = governance_revision(projection, snapshot['policy']['upgrade'], snapshot['policy']['inventory'], snapshot['sources'])
                    if not body.scan_revision or body.scan_revision != current_revision:
                        raise ValueError('库存或规则已变化，请重新扫描后再执行')
                    tie = snapshot['policy']['inventory'].get('tie', 'cloud')
                    settings = snapshot['policy']['inventory']
                    preview = governance_preview(
                        projection, snapshot['policy']['upgrade'], tie,
                        threshold=settings['season_ratio'],
                        quiet_minutes=settings['quiet_minutes'],
                        exempt_keywords=settings.get('exempt_keywords') or (),
                        decision=settings.get('decision', 'quality_first'),
                        special_action=settings.get('special_action', 'compare'),
                        multi_season_protect=settings.get('multi_season_protect', 'compare'))
                    if not settings.get('single_gaps'):
                        selected_dual = {(v.kind, v.tmdb_id, v.season, v.action_kind or ('loc' if v.winner == 'share' else 'shr' if v.winner == 'cloud' else None)) for v in body.selections if v.action_kind or v.winner}
                        if any(v.get('incomplete_sides') and (v['kind'], v['tmdb_id'], v.get('season'), v.get('action_kind')) in selected_dual for v in preview['dual']):
                            raise ValueError('该季存在单库缺集，请先启用缺集季清理')
                    singles = [v for v in body.selections if v.reason is not None]
                    duals = [v for v in body.selections if v.action_kind is not None]
                    if body.mode == 'governance' and (len(singles) + len(duals) != len(body.selections)
                                                       or any(v.reason is not None and v.action_kind is not None for v in body.selections)):
                        raise ValueError('治理项目类型无效')
                    if body.mode in ('single', 'governance'):
                        settings = snapshot['policy']['inventory']
                        if any(v.reason == 'quality' and settings.get('single_quality') is not True
                               for v in (singles if body.mode == 'governance' else body.selections)):
                            raise ValueError('请先启用单库多版本清理')
                    if body.mode == 'governance':
                        actions = []
                        if singles:
                            actions.extend(single_actions(projection, preview, [v.model_dump() for v in singles],
                                                          snapshot['unsupported']))
                        if duals:
                            actions.extend(dual_actions(projection, preview, [v.model_dump() for v in duals],
                                                        snapshot['unsupported']))
                        keys = [(v['side'], v['id'], v['path']) for v in actions]
                        if not actions or len(actions) > 10000 or len(set(keys)) != len(keys):
                            raise ValueError('治理目标为空、重复或超过 10000 条')
                    else:
                        actions = (dual_actions(projection, preview, selections, snapshot['unsupported'])
                                   if body.mode == 'dual'
                                   else single_actions(projection, preview, selections, snapshot['unsupported']))
                plan_id = create_plan(store.db, {'inventory': snapshot, 'actions': actions}, ttl=ttl)
                # 计划类型单独存一条 meta：_validate 只认 inventory/actions，别塞进去。
                if hasattr(store, 'set_meta'):
                    store.set_meta('inventory-plan-mode:' + plan_id, {'mode': body.mode})
                return {'plan_id': plan_id, 'actions': actions, 'expires_in': ttl, 'mode': body.mode}
        except (OSError, ValueError, PlanError) as exc:
            raise HTTPException(409, f'无法创建删除计划：{exc}') from None

    @app.post('/api/v1/inventory/delete/plan', dependencies=[Depends(admin)])
    async def plan_delete(body: PlanRequest):
        return await build_delete_plan(body)

    app.state.inventory_build_delete_plan = build_delete_plan

    async def run(plan_id):
        engine = app.state.organizer
        try:
            async with engine.publication_gate:
                async def current():
                    result, _ = await asyncio.to_thread(capture)
                    return result
                from .inventory_scope import verify_work
                work_paths = None
                async def verify(expected, action):
                    nonlocal work_paths
                    if work_paths is None:
                        # 固定计划生成时的实际配对，避免同名作品不同 TMDB 漏查保留库。
                        rows = [(v['side'], v['id'], v['path']) for v in expected['files']]
                        projected = await asyncio.to_thread(
                            project, rows, app.state.strms,
                            expected['policy']['inventory'].get('match_strategy', 'title_year'))
                        if not projected['complete']:
                            raise ValueError('计划作品无法完整核实')
                        work_paths = {}
                        for item in projected['items']:
                            paths = {v['path'] for side in ('cloud', 'share') for v in item[side]}
                            for path in paths:
                                work_paths[path] = paths
                    paths = work_paths.get(action['path'])
                    if not paths:
                        return False
                    return await asyncio.to_thread(verify_work, engine, expected, action, paths)
                # 逐项核查和提交，避免异步批量过程中保留依据失效或部分结果漏记。
                await execute_simulated(store.db, plan_id, current,
                                        lambda file, source: delete_one(engine, file, source),
                                        lambda action: current_actions.update({plan_id: action}),
                                        verify_work=verify)
        except Exception as exc:
            errors[plan_id] = str(exc)[:180]
        finally:
            # 删除会改变两库内容：让片库映射的缓存立刻失效，两个页面马上一致。
            invalidate = getattr(app.state, 'inventory_invalidate', None)
            if callable(invalidate):
                try:
                    await asyncio.to_thread(invalidate)
                except Exception:
                    pass
            active.pop(plan_id, None)
            current_actions.pop(plan_id, None)

    @app.post('/api/v1/inventory/delete/{plan_id}/execute', dependencies=[Depends(admin)])
    async def start_delete(plan_id: str):
        try:
            plan = _load(store.db, plan_id)
            if plan.get('snapshot', {}).get('inventory', {}).get('policy', {}).get('guard_revision') != 4:
                raise PlanError('删除保护规则已更新，请重新扫描生成计划')
            if plan.get('state') != 'planned' or plan_id in active:
                raise PlanError('计划已在执行或已经结束')
            errors.pop(plan_id, None)
            active[plan_id] = asyncio.create_task(run(plan_id))
            return {'state': 'starting', 'plan_id': plan_id}
        except PlanError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.get('/api/v1/inventory/delete/{plan_id}', dependencies=[Depends(admin)])
    async def delete_status(plan_id: str):
        try:
            value = _load(store.db, plan_id)
            actions = value.get('snapshot', {}).get('actions', [])
            return {'plan_id': plan_id, 'state': 'starting' if plan_id in active and value.get('state') == 'planned' else 'failed' if plan_id in errors and value.get('state') == 'planned' else value.get('state'),
                    'count': len(actions), 'progress': value.get('progress', []), 'error': errors.get(plan_id, '') or value.get('message', ''),
                    'current': current_actions.get(plan_id),
                    'created': value.get('created'), 'expires': value.get('expires')}
        except PlanError as exc:
            raise HTTPException(404, str(exc)) from None

    def sweep_stale_plans():
        """进程重启/超时后卡在 executing 的计划：标为失败，页面不再显示"执行中"。"""
        import time as _time
        now = _time.time()
        for row in store.db.execute("SELECT key,value FROM meta WHERE key LIKE 'inventory-plan:%'"):
            try:
                value = json.loads(row['value'])
            except (TypeError, ValueError):
                continue
            if not isinstance(value, dict) or value.get('state') != 'executing':
                continue
            pid = row['key'].split(':', 1)[1]
            if pid in active and not active[pid].done():
                continue
            claimed = value.get('claimed') or value.get('created') or 0
            if not isinstance(claimed, (int, float)) or now - claimed < 900:
                continue
            value.update(state='failed',
                         message='执行中断（进程重启或超时），未自动重试；已确认的文件不会重复删除。',
                         finished=now)
            with store.db:
                store.db.execute('UPDATE meta SET value=? WHERE key=?',
                                 (json.dumps(value, ensure_ascii=False, separators=(',', ':')), row['key']))

    @app.get('/api/v1/inventory-plans', dependencies=[Depends(admin)])
    async def plan_history():
        sweep_stale_plans()
        result = []
        for row in store.db.execute("SELECT key,value FROM meta WHERE key LIKE 'inventory-plan:%'"):
            try:
                value = json.loads(row['value'])
                if not isinstance(value, dict) or not isinstance(value.get('snapshot'), dict):
                    continue
                pid = row['key'].split(':', 1)[1]
                value = _load(store.db, pid)
                progress = value.get('progress', [])
                actions = value['snapshot'].get('actions', [])
                mode_row = store.db.execute('SELECT value FROM meta WHERE key=?',
                                            ('inventory-plan-mode:' + pid,)).fetchone()
                mode = ''
                if mode_row:
                    try:
                        mode = str((json.loads(mode_row['value']) or {}).get('mode') or '')
                    except (TypeError, ValueError):
                        mode = ''
                labels = {'manual': '手动清理', 'dual': '双库洗版', 'single': '单库清理',
                          'governance': '双库治理'}
                message = str(value.get('message') or '')
                if 'too many values to unpack' in message:
                    # 旧版本核查异常（已修复），别让用户看到这种看不懂的报错。
                    message = '旧版本核查异常（已在 0.9.136 修复），请重新生成计划。'
                result.append({'plan_id': pid, 'state': value.get('state', 'unknown'), 'mode': mode,
                               'mode_label': labels.get(mode, '旧计划'),
                               'count': len(actions) if isinstance(actions, list) else 0,
                               'done': len(progress) if isinstance(progress, list) else 0,
                               'created': value.get('created', 0), 'error': message})
            except (ValueError, TypeError, KeyError):
                continue
        result.sort(key=lambda item: item['created'] if isinstance(item['created'], (int, float)) else 0, reverse=True)
        return {'items': result[:20]}
