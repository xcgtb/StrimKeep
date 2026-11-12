import sqlite3

import pytest

from strimkeep.inventory_plans import PlanError, claim_plan, create_plan
from strimkeep.inventory_snapshot import capture_snapshot


def setup_library(root):
    (root / 'cloud').mkdir()
    (root / 'share').mkdir()
    (root / 'cloud' / 'a.strm').write_text('cloud link', encoding='utf-8')
    (root / 'share' / 'a.strm').write_text('share link', encoding='utf-8')
    return {'cloud': 'cloud', 'share': 'share'}, [('cloud', '1', 'cloud/a.strm'), ('share', '2', 'share/a.strm')]


def reader(rows):
    return lambda: [{'side': side, 'id': str(record_id), 'path': path, 'source_hash': 'a' * 64}
                    for side, record_id, path in sorted(rows)]


def test_content_or_policy_change_invalidates_plan(tmp_path):
    roots, rows = setup_library(tmp_path)
    policy = {'tie': 'cloud', 'rules': ['resolution']}
    with sqlite3.connect(':memory:') as db:
        db.execute('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
        initial = capture_snapshot(tmp_path, roots, rows, policy, source_reader=reader(rows))
        plan = create_plan(db, initial, now=100)
        (tmp_path / 'share' / 'a.strm').write_text('new link', encoding='utf-8')
        changed = capture_snapshot(tmp_path, roots, rows, policy, source_reader=reader(rows))
        with pytest.raises(PlanError):
            claim_plan(db, plan, changed, now=101)
        changed_policy = capture_snapshot(tmp_path, roots, rows, {'tie': 'share', 'rules': ['resolution']}, source_reader=reader(rows))
        assert changed_policy != changed


def test_unregistered_files_are_recorded_but_oversize_blocks(tmp_path):
    roots, rows = setup_library(tmp_path)
    extra = tmp_path / 'share' / 'unregistered.strm'
    extra.write_text('extra', encoding='utf-8')
    # 目录里有、台账里没有：不再一票否决，只作为「不可进计划」记进快照
    # （快照因此仍能发现它在计划期间增删）。
    snapshot = capture_snapshot(tmp_path, roots, rows, {}, source_reader=reader(rows),
                                unsupported=[{'side': 'share', 'path': 'share/unregistered.strm',
                                              'reason': '未登记'}])
    assert snapshot['unsupported'] == [{'side': 'share', 'path': 'share/unregistered.strm',
                                        'reason': '未登记'}]
    assert snapshot['version'] == 2
    (tmp_path / 'share' / 'a.strm').write_bytes(b'x' * (64 * 1024 + 1))
    with pytest.raises(ValueError):
        capture_snapshot(tmp_path, roots, rows, {}, source_reader=reader(rows))


def test_source_change_during_capture_is_rejected(tmp_path):
    roots, rows = setup_library(tmp_path)
    calls = 0
    def changing():
        nonlocal calls
        calls += 1
        data = reader(rows)()
        if calls > 1:
            data[0]['source_hash'] = 'b' * 64
        return data
    with pytest.raises(ValueError):
        capture_snapshot(tmp_path, roots, rows, {}, source_reader=changing)
