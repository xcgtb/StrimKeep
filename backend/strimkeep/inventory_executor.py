"""Snapshot-bound inventory executor, using a verified deletion adapter."""
import asyncio
from copy import deepcopy
from functools import partial

from .inventory_plans import (
    _load, begin_action, claim_plan, confirm_action, finish_plan, pending_action, record_progress,
    snapshot_hash, PlanError,
)


def _validate(plan):
    if not isinstance(plan, dict) or set(plan) != {'inventory', 'actions'}:
        raise PlanError('执行计划格式无效')
    inventory, actions = plan['inventory'], plan['actions']
    if not isinstance(inventory, dict) or not isinstance(inventory.get('files'), list) or not isinstance(inventory.get('sources'), list):
        raise PlanError('库存快照格式无效')
    if not isinstance(actions, list) or not 1 <= len(actions) <= 10000:
        raise PlanError('计划条目数量无效')
    index = {}
    for item in inventory['files']:
        if not isinstance(item, dict) or not all(isinstance(item.get(k), str) and item[k] for k in ('side', 'id', 'path')):
            raise PlanError('库存文件证据无效')
        key = (item['side'], item['id'], item['path'])
        if key in index:
            raise PlanError('库存文件证据重复')
        index[key] = item
    source_keys = set()
    sources = {}
    for source in inventory['sources']:
        if (not isinstance(source, dict) or set(source) != {'side', 'id', 'path', 'source_hash'}
                or not all(isinstance(source.get(k), str) and source[k] for k in ('side', 'id', 'path'))
                or not isinstance(source.get('source_hash'), str) or len(source['source_hash']) != 64
                or any(c not in '0123456789abcdef' for c in source['source_hash'])):
            raise PlanError('来源身份快照无效')
        key = (source['side'], source['id'], source['path'])
        if key in source_keys:
            raise PlanError('来源身份快照重复')
        source_keys.add(key)
        sources[key] = source
    if source_keys != set(index):
        raise PlanError('来源身份快照不完整')
    selected = set()
    for action in actions:
        if not isinstance(action, dict) or set(action) != {'side', 'id', 'path'} or action.get('side') not in ('cloud', 'share'):
            raise PlanError('计划条目无效')
        if not all(isinstance(action.get(k), str) and action[k] for k in ('id', 'path')):
            raise PlanError('计划文件身份无效')
        key = (action['side'], action['id'], action['path'])
        if key not in index or key in selected:
            raise PlanError('计划文件不在快照中或重复')
        selected.add(key)
    return index, sources


async def execute_simulated(db, plan_id, capture, delete, progress=None, verify_work=None,
                            delete_many=None, *, dispatched=False, recover_missing=None):
    """Run with the caller's deletion adapter.

    capture() checks the full snapshot at claim. When verify_work is supplied,
    revalidate the affected work and retained files before each deletion; the
    fallback preserves full-snapshot checks for callers without scoped guards.
    delete() must confirm deletion and ledger update before returning True.
    """
    value = _load(db, plan_id)
    plan = value.get('snapshot')
    index, sources = _validate(plan)
    if dispatched and (verify_work is None or recover_missing is None or delete_many is not None):
        raise PlanError('后台清理需要逐项核对')
    resumed = dispatched and bool(value.get('claimed'))
    current = None if resumed else {'inventory': await capture(), 'actions': plan['actions']}
    claim = claim_plan(db, plan_id, current, dispatched=dispatched)
    expected = deepcopy(plan['inventory'])
    done = []
    actions = plan['actions']

    try:
        if dispatched:
            done = list(value.get('progress') or [])
            if done != actions[:len(done)]:
                raise PlanError('清理进度与计划不一致')
            pending, confirmed = pending_action(db, plan_id)
            if pending is not None and (len(done) == len(actions) or pending != actions[len(done)]):
                raise PlanError('待核实文件与计划不一致')
            for action in done:
                _exclude(expected, action)
        else:
            pending, confirmed = None, False
        # Full snapshot guards legacy callers. Production verifies only the
        # affected work, including retained files, before each mutation.
        if verify_work is None and snapshot_hash(await capture()) != snapshot_hash(expected):
            raise PlanError('库存发生变化，已停止执行')
        remaining = list(actions[len(done):])
        while remaining:
            # 连续的网盘动作合成一批：按 115 父目录一次回收，比逐文件快数倍。
            if delete_many is not None and remaining[0]['side'] == 'cloud':
                group = []
                while remaining and remaining[0]['side'] == 'cloud' and len(group) < 200:
                    group.append(remaining.pop(0))
                for action in group:
                    if verify_work is not None and await verify_work(expected, action) is not True:
                        raise PlanError('作品文件或保留依据已变化，已停止执行')
                evidences = [deepcopy(index[(action['side'], action['id'], action['path'])])
                             for action in group]
                outcome = await delete_many(evidences)
                failures = []
                for action in group:
                    key = (action['side'], action['id'], action['path'])
                    ok, message = outcome.get((action['path'], str(action['id'])),
                                              (False, '删除未返回结果'))
                    if not ok:
                        failures.append('%s（%s）' % (message or '删除未确认，已停止执行', action['path']))
                        continue
                    if progress:
                        progress(deepcopy(action))
                    expected['files'] = [item for item in expected['files']
                                         if (item['side'], item['id'], item['path']) != key]
                    expected['sources'] = [item for item in expected['sources']
                                           if (item['side'], item['id'], item['path']) != key]
                    record_progress(db, plan_id, claim, action)
                    done.append(action)
                if failures:
                    raise PlanError(failures[0])
                continue
            action = remaining.pop(0)
            key = (action['side'], action['id'], action['path'])
            if dispatched:
                checkpoint = partial(confirm_action, db, plan_id)
                if pending == action:
                    if not confirmed:
                        recovered = await recover_missing(expected, index[key], sources[key], checkpoint)
                        if recovered is False:
                            raise PlanError('中断文件的来源或回收结果待核实')
                        confirmed = recovered is True
                    pending = None
                    if confirmed:
                        record_progress(db, plan_id, claim, action, dispatched=True)
                        _exclude(expected, action)
                        done.append(action)
                        confirmed = False
                        continue
                else:
                    checkpoint = None
            if verify_work is not None and await verify_work(expected, action) is not True:
                raise PlanError('作品文件或保留依据已变化，已停止执行')
            if progress:
                progress(deepcopy(action))
            file, source = deepcopy(index[key]), deepcopy(sources[key])
            if dispatched:
                if checkpoint is None:
                    begin_action(db, plan_id, action)
                    checkpoint = partial(confirm_action, db, plan_id)
                removed = await delete(file, source, checkpoint)
            else:
                removed = await delete(file, source)
            if removed is not True:
                raise PlanError('删除未确认，已停止执行（%s）' % action['path'])
            _exclude(expected, action)
            if verify_work is None and snapshot_hash(await capture()) != snapshot_hash(expected):
                raise PlanError('删除后库存与预期不一致，已停止执行')
            record_progress(db, plan_id, claim, action, dispatched=dispatched)
            done.append(action)
        finish_plan(db, plan_id, claim, 'completed', dispatched=dispatched)
        if progress:
            progress(None)
        return {'state': 'completed', 'done': done}
    except BaseException as exc:
        if not (dispatched and isinstance(exc, asyncio.CancelledError)):
            state = 'needs_review' if dispatched and pending_action(db, plan_id)[0] else 'failed'
            finish_plan(db, plan_id, claim, state, message=str(exc), dispatched=dispatched)
        raise


def _exclude(expected, action):
    key = (action['side'], action['id'], action['path'])
    for field in ('files', 'sources'):
        expected[field] = [item for item in expected[field]
                           if (item['side'], item['id'], item['path']) != key]
