"""Explicit, snapshot-bound inventory deletion plans."""
import asyncio
import json
from typing import Literal

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .inventory import project, registered_rows
from .inventory_api import inventory_config_value
from .inventory_delete_adapter import delete_many, delete_one, recover_missing_delete
from .inventory_executor import execute_simulated
from .inventory_operations import dual_actions, manual_actions, single_actions
from .inventory_plans import (
    PlanError, _load, create_plan, fail_queued_plan, pending_action, queue_plan,
    next_queued_plan, recover_dispatched_plans, work_groups,
)
from .inventory_preview import governance_preview
from .inventory_revision import governance_revision
from .inventory_snapshot import capture_snapshot
from .inventory_sources import optional_account, source_fingerprints
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


async def inventory_delete_loop(store, run, *, interval=0.5):
    await asyncio.to_thread(lambda: recover_dispatched_plans(store.db))
    while True:
        plan_id = await asyncio.to_thread(lambda: next_queued_plan(store.db))
        if plan_id is None:
            await asyncio.sleep(interval)
        else:
            await run(plan_id)


def install_inventory_operations(app, store, admin):
    def capture(reuse=None):
        engine = app.state.organizer
        if store.db.execute("SELECT 1 FROM organize_jobs WHERE state='running' LIMIT 1").fetchone():
            raise ValueError('整理任务正在运行，请等待任务结束')
        if store.db.execute('SELECT 1 FROM share_wash_intents LIMIT 1').fetchone():
            raise ValueError('分享整理存在未完成的文件操作')
        config = store.get('organize_config', {}) or {}
        if not isinstance(config, dict):
            raise ValueError('整理配置无效')
        from .inventory_fs import check_missing_registrations, resolve_roots, scan as filesystem_rows
        roots = resolve_roots(app.state.strms, store)
        check_missing_registrations(app.state.strms, roots, store.db)
        account = optional_account(engine)
        tie_config = inventory_config_value(store.get('inventory_config', {}))
        policy = {'upgrade': public_policy(store.get('upgrade_policy')), 'inventory': tie_config, 'guard_revision': 4}
        from .inventory_partition import partition
        if reuse:
            if reuse['roots'] != roots:
                raise ValueError('库目录发生变化，请重新扫描')
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
                preview = None
                if body.mode == 'manual':
                    actions = await asyncio.to_thread(
                        manual_actions, projection, selections, snapshot['unsupported'])
                else:
                    current_revision = await asyncio.to_thread(
                        governance_revision, projection, snapshot['policy']['upgrade'],
                        snapshot['policy']['inventory'], snapshot['sources'])
                    if not body.scan_revision or body.scan_revision != current_revision:
                        raise ValueError('库存或规则已变化，请重新扫描后再执行')
                    tie = snapshot['policy']['inventory'].get('tie', 'cloud')
                    settings = snapshot['policy']['inventory']
                    preview = await asyncio.to_thread(
                        governance_preview,
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
                            actions.extend(await asyncio.to_thread(
                                single_actions, projection, preview,
                                [v.model_dump() for v in singles], snapshot['unsupported']))
                        if duals:
                            actions.extend(await asyncio.to_thread(
                                dual_actions, projection, preview,
                                [v.model_dump() for v in duals], snapshot['unsupported']))
                        keys = [(v['side'], v['id'], v['path']) for v in actions]
                        if not actions or len(actions) > 10000 or len(set(keys)) != len(keys):
                            raise ValueError('治理目标为空、重复或超过 10000 条')
                    else:
                        actions = await asyncio.to_thread(
                            dual_actions if body.mode == 'dual' else single_actions,
                            projection, preview, selections, snapshot['unsupported'])
                plan_id = await asyncio.to_thread(
                    lambda: create_plan(store.db, {'inventory': snapshot, 'actions': actions}, ttl=ttl))
                # 计划上下文：沿用治理生成的判定（为什么删 / 平局 / 决胜维度 / 达标率）。
                by_key = {(item.get('kind'), item.get('tmdb_id')): item
                          for item in projection.get('items') or []}
                preview_rows = {}
                for row in list((preview or {}).get('dual') or []) + list((preview or {}).get('single') or []):
                    preview_rows.setdefault((row.get('kind'), row.get('tmdb_id'), row.get('season')),
                                            []).append(row)

                def common_dir(paths):
                    groups = [str(path).split('/')[:-1] for path in paths if str(path)]
                    if not groups:
                        return ''
                    shared = []
                    for column in zip(*groups):
                        if len(set(column)) != 1:
                            break
                        shared.append(column[0])
                    return '/'.join(shared)

                def pick_row(key, season, selection):
                    candidates = preview_rows.get((key[0], key[1], season)) or []
                    for row in candidates:
                        if selection.get('action_kind') and row.get('action_kind') != selection.get('action_kind'):
                            continue
                        if selection.get('reason') and row.get('reason') != selection.get('reason'):
                            continue
                        return row
                    return candidates[0] if candidates else None

                entries = []
                for selection in selections:
                    key = (selection.get('kind'), selection.get('tmdb_id'))
                    season = selection.get('season')
                    item = by_key.get(key) or {}
                    prefixes = {}
                    for side in ('cloud', 'share'):
                        paths = [row.get('path') for row in item.get(side) or []
                                 if season is None or row.get('season') == season]
                        if paths:
                            prefixes[side] = common_dir(paths)
                    row = pick_row(key, season, selection)
                    entry = {'kind': key[0], 'tmdb_id': key[1], 'title': item.get('title') or '',
                             'season': season, 'side': selection.get('side'),
                             'prefixes': prefixes, 'files': []}
                    if row:
                        entry.update({
                            'reason': row.get('reason'), 'reason_label': row.get('reason_label'),
                            'decisive': row.get('decisive') or '', 'tie': bool(row.get('tie')),
                            'decision': row.get('decision') or '', 'outcome': row.get('outcome') or '',
                            'gap': row.get('gap') or '', 'ratio': row.get('ratio'),
                            'threshold': row.get('threshold'),
                            'episodes': {'share': len(row.get('share_episodes') or row.get('share_files') or []),
                                         'cloud': len(row.get('cloud_episodes') or row.get('cloud_files') or [])},
                            'dup_episodes': list(row.get('dup_episodes') or [])[:50],
                            'lost_episodes': list(row.get('lost_episodes') or [])[:50],
                            'single_side': bool(row.get('single_side')),
                            'manual': bool(row.get('manual_choice'))})
                    elif body.mode == 'manual':
                        entry['reason_label'] = '手动清理'
                    entries.append(entry)
                used = [False] * len(actions)
                for entry in entries:
                    for index, action in enumerate(actions):
                        if used[index]:
                            continue
                        prefix = (entry.get('prefixes') or {}).get(action.get('side'))
                        path = str(action.get('path') or '')
                        if prefix and (path == prefix or path.startswith(prefix + '/')):
                            used[index] = True
                            entry['files'].append({'side': action.get('side'),
                                                   'id': str(action.get('id')),
                                                   'path': path})
                others = [{'side': action.get('side'), 'id': str(action.get('id')),
                           'path': action.get('path')}
                          for index, action in enumerate(actions) if not used[index]]
                if hasattr(store, 'set_meta'):
                    store.set_meta('inventory-plan-context:' + plan_id,
                                   {'mode': body.mode, 'entries': entries[:500],
                                    'others': others[:2000]})
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

    async def execute(plan_id):
        engine = app.state.organizer
        try:
            if store.db.execute('SELECT 1 FROM share_wash_intents LIMIT 1').fetchone():
                raise PlanError('分享整理存在未完成的文件操作')
            async def current():
                result, _ = await asyncio.to_thread(capture)
                return result

            from .inventory_scope import verify_work
            work_paths = None

            async def verify(expected, action, *, missing_target=False):
                nonlocal work_paths
                if work_paths is None:
                    groups = work_groups(store.db, plan_id)
                    if groups is None:
                        # Persist the original pairing so a resumed plan checks the same keeper files.
                        rows = [(v['side'], v['id'], v['path']) for v in expected['files']]
                        projected = await asyncio.to_thread(
                            project, rows, app.state.strms,
                            expected['policy']['inventory'].get('match_strategy', 'title_year'))
                        if not projected['complete']:
                            raise ValueError('计划作品无法完整核实')
                        targets = {item['path'] for item in
                                   _load(store.db, plan_id)['snapshot']['actions']}
                        groups = [sorted({v['path'] for side in ('cloud', 'share') for v in item[side]})
                                  for item in projected['items']]
                        groups = [group for group in groups if targets.intersection(group)]
                        work_groups(store.db, plan_id, groups)
                    work_paths = {}
                    for group in groups:
                        paths = set(group)
                        for path in paths:
                            work_paths[path] = paths
                paths = work_paths.get(action['path'])
                if not paths:
                    return False
                if missing_target:
                    return await asyncio.to_thread(verify_work, engine, expected, action, paths, missing_target=True)
                return await asyncio.to_thread(verify_work, engine, expected, action, paths)

            async def recover(expected, file, source, on_confirm):
                return await recover_missing_delete(
                    engine, file, source, on_confirm, lambda: verify(expected, file, missing_target=True))

            outcome = await execute_simulated(
                store.db, plan_id, current,
                lambda file, source, on_confirm: delete_one(engine, file, source, on_confirm),
                verify_work=verify, dispatched=True, recover_missing=recover)
            return outcome['done']
        except Exception as exc:
            fail_queued_plan(store.db, plan_id, str(exc))
            return []
        finally:
            invalidate = getattr(app.state, 'inventory_invalidate', None)
            if callable(invalidate):
                invalidate()

    async def run(plan_id):
        engine = app.state.organizer
        while True:
            async with engine.publication_gate:
                # Jobs also claim this gate. Wait through an already running job without
                # holding the gate it still needs to publish its remaining output.
                if not store.db.execute("SELECT 1 FROM organize_jobs WHERE state='running' LIMIT 1").fetchone():
                    done = await execute(plan_id)
                    break
            await asyncio.sleep(.25)
        notifications = getattr(engine, 'notifications', None)
        if done and notifications is not None:
            lines = ['成功 %d 条' % len(done)]
            for action in done[:10]:
                icon = '☁️ ' if action['side'] == 'cloud' else '📄 '
                lines.append(icon + action['path'])
            if len(done) > 10:
                lines.append('… 共 %d 条' % len(done))
            try:
                await notifications.publish(
                    'inventory_delete:%s' % plan_id, '🧹 片库清理完成',
                    '\n'.join(lines), 'success', category='general')
            except Exception:
                pass

    app.state.inventory_run_delete = run

    @app.post('/api/v1/inventory/delete/{plan_id}/execute', dependencies=[Depends(admin)])
    async def start_delete(plan_id: str):
        try:
            await asyncio.to_thread(lambda: queue_plan(store.db, plan_id))
            return {'state': 'starting', 'plan_id': plan_id}
        except PlanError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.get('/api/v1/inventory/delete/{plan_id}', dependencies=[Depends(admin)])
    async def delete_status(plan_id: str):
        def read():
            value = _load(store.db, plan_id)
            current, confirmed = pending_action(store.db, plan_id)
            actions = value.get('snapshot', {}).get('actions', [])
            return {'plan_id': plan_id, 'state': value.get('state'), 'count': len(actions),
                    'progress': value.get('progress', []), 'error': value.get('message', ''),
                    'current': current, 'current_confirmed': confirmed,
                    'created': value.get('created'), 'expires': value.get('expires')}
        try:
            return await asyncio.to_thread(read)
        except PlanError as exc:
            raise HTTPException(404, str(exc)) from None

    def sweep_stale_plans():
        """进程重启/超时后卡在 executing 的计划：标为失败，页面不再显示"执行中"。"""
        import time as _time
        now = _time.time()
        # 只挑「执行中」的计划：以前是把每一条计划的 JSON 都解析一遍。
        for row in store.db.execute(
                "SELECT key,value FROM meta WHERE key LIKE 'inventory-plan:%' "
                "AND json_valid(value) AND json_extract(value,'$.state')='executing' "
                "AND NOT EXISTS (SELECT 1 FROM inventory_plan_dispatch d WHERE key='inventory-plan:' || d.plan_id)"):
            try:
                value = json.loads(row['value'])
            except (TypeError, ValueError):
                continue
            if not isinstance(value, dict) or value.get('state') != 'executing':
                continue
            claimed = value.get('claimed') or value.get('created') or 0
            if not isinstance(claimed, (int, float)) or now - claimed < 900:
                continue
            value.update(state='failed',
                         message='执行中断（进程重启或超时），未自动重试；已确认的文件不会重复删除。',
                         finished=now)
            with store.db:
                store.db.execute("UPDATE meta SET value=? WHERE key=? AND json_extract(value,'$.state')='executing' "
                                 "AND NOT EXISTS (SELECT 1 FROM inventory_plan_dispatch d WHERE key='inventory-plan:' || d.plan_id)",
                                 (json.dumps(value, ensure_ascii=False, separators=(',', ':')), row['key']))
        # 过期没执行的计划：以前一直显示「待执行」，历史里越堆越多，看着像没收拾干净。
        for row in store.db.execute(
                "SELECT key,value FROM meta WHERE key LIKE 'inventory-plan:%' "
                "AND json_valid(value) AND json_extract(value,'$.state')='planned'"):
            try:
                value = json.loads(row['value'])
            except (TypeError, ValueError):
                continue
            expires = value.get('expires')
            if not isinstance(expires, (int, float)) or now <= expires:
                continue
            value.update(state='expired', finished=now,
                         message='计划已过期（超过有效期未执行），请重新扫描后生成新计划。')
            with store.db:
                store.db.execute("UPDATE meta SET value=? WHERE key=? AND json_extract(value,'$.state')='planned'",
                                 (json.dumps(value, ensure_ascii=False, separators=(',', ':')), row['key']))

    @app.get('/api/v1/inventory/plans/{plan_id}/detail', dependencies=[Depends(admin)])
    async def plan_detail(plan_id: str):
        """历史计划详情：治理条目、文件清单与逐条执行结果。"""
        try:
            def read():
                return _load(store.db, plan_id), pending_action(store.db, plan_id)
            value, (current, confirmed) = await asyncio.to_thread(read)
        except PlanError as exc:
            raise HTTPException(404, str(exc)) from None
        actions = value.get('snapshot', {}).get('actions') or []
        done = value.get('progress') or []
        finished = {(item.get('side'), str(item.get('id')), item.get('path')) for item in done if isinstance(item, dict)}
        try:
            row = store.db.execute('SELECT value FROM meta WHERE key=?',
                                   ('inventory-plan-context:' + plan_id,)).fetchone()
            context = json.loads(row[0]) if row and row[0] else {}
        except (TypeError, ValueError):
            context = {}
        if not isinstance(context, dict):
            context = {}
        def marked(items):
            results = []
            for item in items or []:
                key = item.get('side'), str(item.get('id')), item.get('path')
                row = {'side': key[0], 'path': key[2], 'done': key in finished}
                if current and item == current:
                    row['checkpoint'] = 'confirmed' if confirmed else 'needs_review'
                results.append(row)
            return results

        files = marked(actions)
        entries = []
        for entry in context.get('entries') or []:
            grouped = marked(entry.get('files'))
            entries.append({**{key: item for key, item in entry.items()
                                if key not in ('files', 'prefixes')},
                            'files': grouped,
                            'done': sum(1 for file in grouped if file['done'])})
        return {'plan_id': plan_id, 'state': value.get('state'),
                'message': value.get('message', ''), 'created': value.get('created'),
                'finished': value.get('finished'), 'mode': context.get('mode', ''),
                'entries': entries, 'others': marked(context.get('others')),
                'items': context.get('items') or [], 'count': len(files),
                'done': sum(1 for file in files if file['done']), 'files': files}

    @app.get('/api/v1/inventory-plans', dependencies=[Depends(admin)])
    async def plan_history():
        def build():
            sweep_stale_plans()
            rows = store.db.execute(
                "SELECT key, "
                " json_extract(value, '$.created') AS created, "
                " json_extract(value, '$.finished') AS finished, "
                " json_extract(value, '$.state') AS state, "
                " json_extract(value, '$.message') AS message, "
                " COALESCE(json_array_length(json_extract(value, '$.snapshot.actions')), 0) AS total, "
                " COALESCE(json_array_length(json_extract(value, '$.progress')), 0) AS done "
                "FROM meta WHERE key LIKE 'inventory-plan:%' "
                "AND json_valid(value) AND json_type(value, '$.snapshot') = 'object' "
                "ORDER BY COALESCE(json_extract(value, '$.created'), 0) DESC LIMIT 20").fetchall()
            modes = {}
            for row in store.db.execute("SELECT key,value FROM meta WHERE key LIKE 'inventory-plan-mode:%'"):
                try:
                    modes[row['key'].split(':', 1)[1]] = str((json.loads(row['value']) or {}).get('mode') or '')
                except (TypeError, ValueError):
                    continue
            # 进度明细另存表时按计划汇总一次；以前是每条计划单独查一遍。
            extra_progress = {}
            if store.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                                "AND name='inventory_plan_progress'").fetchone():
                for row in store.db.execute('SELECT plan_id, count(*) FROM inventory_plan_progress '
                                            'GROUP BY plan_id'):
                    extra_progress[row[0]] = row[1]
            labels = {'manual': '手动清理', 'dual': '双库洗版', 'single': '单库清理',
                      'governance': '双库治理'}
            result = []
            for row in rows:
                pid = row['key'].split(':', 1)[1]
                # 与 _load 的口径一致：非 32 位十六进制编号不是合法计划，列表里不出现。
                if len(pid) != 32 or any(char not in '0123456789abcdef' for char in pid):
                    continue
                mode = modes.get(pid, '')
                message = str(row['message'] or '')
                if 'too many values to unpack' in message:
                    # 旧版本核查异常（已修复），别让用户看到这种看不懂的报错。
                    message = '旧版本核查异常（已在 0.9.136 修复），请重新生成计划。'
                created = row['created'] if isinstance(row['created'], (int, float)) else 0
                finished = row['finished'] if isinstance(row['finished'], (int, float)) else 0
                result.append({'plan_id': pid, 'state': row['state'] or 'unknown', 'mode': mode,
                               'finished': finished,
                               'mode_label': labels.get(mode, '旧计划'),
                               'count': int(row['total'] or 0),
                               'done': int(row['done'] or 0) + extra_progress.get(pid, 0),
                               'created': created, 'error': message})
            return {'items': result}
        return await asyncio.to_thread(build)
