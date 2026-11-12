import asyncio
import json
import sqlite3
from copy import deepcopy

import pytest

from strimkeep.inventory_executor import execute_simulated
from strimkeep.inventory_plans import PlanError, create_plan


def setup():
    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
    files = [{'side': 'cloud', 'id': '1', 'path': 'cloud/a', 'sha256': 'abc'},
             {'side': 'share', 'id': '2', 'path': 'share/a', 'sha256': 'def'},
             {'side': 'share', 'id': '3', 'path': 'share/b', 'sha256': 'ghi'}]
    sources = [{'side': f['side'], 'id': f['id'], 'path': f['path'], 'source_hash': 'a' * 64} for f in files]
    inventory = {'version': 1, 'identity': {'root': [1, 2]}, 'policy': {'tie': 'cloud'}, 'files': files, 'sources': sources}
    actions = [{'side': f['side'], 'id': f['id'], 'path': f['path']} for f in files[1:]]
    return db, inventory, actions


def state(db, plan):
    return json.loads(db.execute('SELECT value FROM meta WHERE key=?', ('inventory-plan:' + plan,)).fetchone()[0])


def test_simulated_batch_verifies_each_change_and_records_progress():
    db, inventory, actions = setup()
    plan = create_plan(db, {'inventory': inventory, 'actions': actions})
    current = deepcopy(inventory)
    async def capture(): return deepcopy(current)
    async def delete(file, source):
        current['files'] = [row for row in current['files'] if row['id'] != file['id']]
        current['sources'] = [row for row in current['sources'] if row['id'] != file['id']]
        return True
    updates = []
    result = asyncio.run(execute_simulated(db, plan, capture, delete, updates.append))
    assert result['state'] == 'completed' and result['done'] == actions
    assert state(db, plan)['progress'] == actions
    assert updates == [*actions, None]
    assert [row['id'] for row in current['files']] == ['1']
    with pytest.raises(PlanError):
        asyncio.run(execute_simulated(db, plan, capture, delete))


def test_snapshot_verification_is_not_doubled_per_file():
    """校验次数：开头一次 + 每次删除后一次（以前每个文件还有一次前置核对，纯白扫）。"""
    db, inventory, actions = setup()
    plan = create_plan(db, {'inventory': inventory, 'actions': actions})
    current, captures = deepcopy(inventory), []

    async def capture():
        captures.append(1)
        return deepcopy(current)

    async def delete(file, source):
        current['files'] = [row for row in current['files'] if row['id'] != file['id']]
        current['sources'] = [row for row in current['sources'] if row['id'] != file['id']]
        return True

    asyncio.run(execute_simulated(db, plan, capture, delete))
    assert len(captures) == 2 + len(actions)      # 旧实现是 1 + 2*len(actions)


def test_simulated_batch_stops_on_unrelated_change():
    db, inventory, actions = setup()
    plan = create_plan(db, {'inventory': inventory, 'actions': actions})
    current, called = deepcopy(inventory), []
    async def capture(): return deepcopy(current)
    async def delete(file, source):
        called.append(file['id'])
        current['files'] = [row for row in current['files'] if row['id'] != file['id']]
        current['sources'] = [row for row in current['sources'] if row['id'] != file['id']]
        current['files'][0]['sha256'] = 'changed-protected-file'
        return True
    with pytest.raises(PlanError, match='删除后库存'):
        asyncio.run(execute_simulated(db, plan, capture, delete))
    assert called == ['2'] and state(db, plan)['state'] == 'failed'


def test_unconfirmed_delete_stops_without_retry():
    db, inventory, actions = setup()
    plan = create_plan(db, {'inventory': inventory, 'actions': actions})
    async def capture(): return deepcopy(inventory)
    called = []
    async def delete(file, source):
        called.append(file['id'])
        return False
    with pytest.raises(PlanError, match='未确认'):
        asyncio.run(execute_simulated(db, plan, capture, delete))
    assert called == ['2'] and state(db, plan)['state'] == 'failed'


def test_rejects_duplicate_or_unlisted_targets_before_claim():
    db, inventory, actions = setup()
    plan = create_plan(db, {'inventory': inventory, 'actions': [actions[0], actions[0]]})
    async def unused(*_): raise AssertionError('must not be called')
    with pytest.raises(PlanError):
        asyncio.run(execute_simulated(db, plan, unused, unused))
    assert state(db, plan)['state'] == 'planned'


def test_scoped_execution_uses_one_global_snapshot_and_checks_each_target():
    db, inventory, actions = setup()
    plan = create_plan(db, {'inventory':inventory,'actions':actions})
    current = deepcopy(inventory); captures = []; verified = []
    async def capture(): captures.append(1); return deepcopy(current)
    async def verify(expected, action): verified.append((action['id'],len(expected['files']))); return True
    async def delete(file, source): return True
    assert asyncio.run(execute_simulated(db, plan, capture, delete, verify_work=verify))['state'] == 'completed'
    assert len(captures) == 1 and verified == [('2',3),('3',2)]


def test_scoped_execution_stops_if_retained_evidence_changes():
    db, inventory, actions = setup()
    plan = create_plan(db, {'inventory':inventory,'actions':actions})
    async def capture(): return deepcopy(inventory)
    called = []
    async def verify(expected, action): return not called
    async def delete(file, source): called.append(file['id']); return True
    with pytest.raises(PlanError, match='保留依据'):
        asyncio.run(execute_simulated(db, plan, capture, delete, verify_work=verify))
    assert called == ['2'] and state(db,plan)['state'] == 'failed'


def test_cloud_plan_is_deleted_in_one_batch():
    """网盘侧整批清理：一次 delete_many，而不是逐文件回收。"""
    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
    files = [{'side': 'cloud', 'id': '1', 'path': 'cloud/a'},
             {'side': 'cloud', 'id': '2', 'path': 'cloud/b'}]
    sources = [{'side': f['side'], 'id': f['id'], 'path': f['path'], 'source_hash': 'a' * 64}
               for f in files]
    inventory = {'version': 1, 'identity': {'root': [1, 2]}, 'policy': {'tie': 'cloud'},
                 'files': files, 'sources': sources}
    actions = [{'side': f['side'], 'id': f['id'], 'path': f['path']} for f in files]
    plan = create_plan(db, {'inventory': inventory, 'actions': actions})
    current = deepcopy(inventory)

    async def capture():
        return deepcopy(current)

    async def single(file, source):
        raise AssertionError('网盘侧不应逐条删除')

    calls = []

    async def many(evidences):
        calls.append(sorted(evidence['id'] for evidence in evidences))
        current['files'], current['sources'] = [], []
        return {(evidence['path'], evidence['id']): (True, 'ok') for evidence in evidences}

    updates = []
    result = asyncio.run(execute_simulated(db, plan, capture, single, updates.append, delete_many=many))
    assert result['state'] == 'completed'
    assert calls == [['1', '2']]
    assert state(db, plan)['progress'] == actions
    assert updates == [*actions, None]
