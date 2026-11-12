import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from strimkeep.inventory_plans import PlanError, claim_plan, create_plan, finish_plan, snapshot_hash


@pytest.fixture
def database(tmp_path):
    path = tmp_path / 'plans.sqlite'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)')
    return path


def connect(path):
    return sqlite3.connect(path, timeout=5)


def snapshot():
    return {'rules': ['resolution'], 'roots': ['cloud', 'share'],
            'protected': [{'path': 'cloud/a.strm', 'sha256': 'aaa'}],
            'targets': [{'path': 'share/a.strm', 'sha256': 'bbb'}]}


def test_only_one_independent_connection_can_claim(database):
    with connect(database) as db:
        plan = create_plan(db, snapshot(), now=100)
    barrier = Barrier(2)
    def attempt():
        with connect(database) as db:
            barrier.wait()
            try:
                return claim_plan(db, plan, snapshot(), now=101)
            except PlanError:
                return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: attempt(), range(2)))
    assert len([x for x in results if x]) == 1
    with connect(database) as db:
        with pytest.raises(PlanError):
            claim_plan(db, plan, snapshot(), now=102)


def test_changed_snapshot_expiry_and_clock_rollback_leave_plan_unclaimed(database):
    with connect(database) as db:
        plan = create_plan(db, snapshot(), now=100, ttl=60)
        changed = snapshot()
        changed['protected'][0]['sha256'] = 'different'
        for evidence, now in ((changed, 101), (snapshot(), 160), (snapshot(), 99)):
            with pytest.raises(PlanError):
                claim_plan(db, plan, evidence, now=now)
        assert claim_plan(db, plan, snapshot(), now=102)


def test_completion_requires_owner_and_never_replays(database):
    with connect(database) as db:
        plan = create_plan(db, snapshot(), now=100)
        claim = claim_plan(db, plan, snapshot(), now=101)
        with pytest.raises(PlanError):
            finish_plan(db, plan, 'wrong', 'completed')
        finish_plan(db, plan, claim, 'failed', now=103)
        with pytest.raises(PlanError):
            claim_plan(db, plan, snapshot(), now=104)
        with pytest.raises(PlanError):
            finish_plan(db, plan, claim, 'completed')


def test_corrupt_plan_and_existing_transaction_fail_closed(database):
    with connect(database) as db:
        plan = create_plan(db, snapshot(), now=100)
        db.execute('UPDATE meta SET value=? WHERE key=?', (json.dumps({'state': 'planned'}), 'inventory-plan:' + plan))
        db.commit()
        with pytest.raises(PlanError):
            claim_plan(db, plan, snapshot(), now=101)
        db.execute('BEGIN')
        with pytest.raises(PlanError):
            create_plan(db, snapshot(), now=100)
        db.rollback()
    with pytest.raises(PlanError):
        snapshot_hash({'invalid': float('nan')})
