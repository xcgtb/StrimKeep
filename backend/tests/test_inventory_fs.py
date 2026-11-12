"""台账解耦：目录是库存事实，台账只管来源。

这些用例就是这次改造的验收条件——把 ``organized_files`` / ``share_strms``
清空之后，片库映射和双库治理仍然要能工作。
"""
import sqlite3

import pytest

from strimkeep.inventory import project
from strimkeep.inventory_fs import is_synthetic, scan, synthetic_id
from strimkeep.inventory_partition import partition
from strimkeep.inventory_preview import governance_preview
from strimkeep.inventory_sources import source_fingerprints
from strimkeep.quality import public_policy

ROOTS = {'cloud': 'cloud', 'share': '分享影视库'}
CLOUD = 'cloud/剧集/Show (2020) {tmdb-12}/Season 1/Show.S01E01.1080p.strm'
SHARE = '分享影视库/剧集/Show (2020) {tmdb-12}/Season 1/Show.S01E01.2160p.strm'


def empty_db():
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.executescript('CREATE TABLE organized_files(file_id TEXT,relative_path TEXT);'
                     'CREATE TABLE share_strms(id TEXT,strm_path TEXT,revoked INTEGER);')
    return db


def add(root, relative):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('http://example/play115/x', encoding='utf-8')
    return path


def test_empty_ledger_still_lists_every_work(tmp_path):
    add(tmp_path, CLOUD)
    add(tmp_path, SHARE)
    db = empty_db()
    rows = scan(tmp_path, ROOTS, db)
    assert [row[0] for row in rows] == ['cloud', 'share']
    assert all(is_synthetic(row[1]) for row in rows)
    snapshot = project(rows, tmp_path)
    item = snapshot['items'][0]
    assert snapshot['complete'] and item['tmdb_id'] == 12
    assert [row['registered'] for row in item['cloud'] + item['share']] == [False, False]


def test_registered_row_is_attached_to_the_same_file(tmp_path):
    add(tmp_path, CLOUD)
    db = empty_db()
    db.execute('INSERT INTO organized_files VALUES (?,?)', ('9001', CLOUD))
    db.commit()
    assert scan(tmp_path, ROOTS, db) == [('cloud', '9001', CLOUD)]


def test_revoked_or_foreign_rows_fall_back_to_file_identity(tmp_path):
    add(tmp_path, SHARE)
    db = empty_db()
    db.execute('INSERT INTO share_strms VALUES (?,?,?)', ('s9', SHARE, 1))
    db.commit()
    assert is_synthetic(scan(tmp_path, ROOTS, db)[0][1])


def test_source_evidence_allows_unregistered_share_but_not_cloud(tmp_path):
    add(tmp_path, CLOUD)
    add(tmp_path, SHARE)
    db = empty_db()
    share = source_fingerprints(db, [('share', synthetic_id('share', SHARE), SHARE)], 'account')
    assert len(share[0]['source_hash']) == 64
    with pytest.raises(ValueError, match='网盘源文件未登记'):
        source_fingerprints(db, [('cloud', synthetic_id('cloud', CLOUD), CLOUD)], 'account')


def test_partition_keeps_the_work_and_isolates_only_the_unverifiable_file(tmp_path):
    add(tmp_path, CLOUD)
    add(tmp_path, SHARE)
    db = empty_db()
    scoped = partition(tmp_path, ROOTS, scan(tmp_path, ROOTS, db), 'title_year', db, 'account')
    assert [row[0] for row in scoped['rows']] == ['share']       # 分享侧可进计划
    assert scoped['projection']['items'][0]['tmdb_id'] == 12     # 网盘文件照样展示
    assert [item['path'] for item in scoped['unsupported']] == [CLOUD]
    assert '网盘源文件未登记' in scoped['unsupported'][0]['reason']


def test_preview_has_no_global_gate_and_reports_unregistered(tmp_path):
    add(tmp_path, CLOUD)
    add(tmp_path, SHARE)
    db = empty_db()
    snapshot = project(scan(tmp_path, ROOTS, db), tmp_path)
    preview = governance_preview(snapshot, public_policy(), quiet_minutes=0)
    assert preview['scan_complete']
    assert not any(row.get('reason') == 'inventory_incomplete' for row in preview['blocked'])
    verdict = preview['dual'][0]
    assert verdict['unregistered'] == {'cloud': 1, 'share': 1}


def test_manual_actions_reject_unverifiable_files_only(tmp_path):
    from strimkeep.inventory_operations import manual_actions

    add(tmp_path, CLOUD)
    add(tmp_path, SHARE)
    db = empty_db()
    snapshot = project(scan(tmp_path, ROOTS, db), tmp_path)
    scoped = partition(tmp_path, ROOTS, scan(tmp_path, ROOTS, db), 'title_year', db, 'account')
    # 网盘侧没登记：明确拒绝，而不是删掉本地 STRM 留下 115 上的孤儿。
    with pytest.raises(ValueError, match='来源不可核实'):
        manual_actions(snapshot, [{'kind': 'tv', 'tmdb_id': 12, 'side': 'cloud'}],
                       scoped['unsupported'])
    # 分享侧只依赖本地文件：照常生成动作。
    actions = manual_actions(snapshot, [{'kind': 'tv', 'tmdb_id': 12, 'side': 'share'}],
                             scoped['unsupported'])
    assert len(actions) == 1 and actions[0]['side'] == 'share'
    assert actions[0]['id'].startswith('fs-')


def test_unattributable_file_is_reported_but_does_not_hide_works(tmp_path):
    add(tmp_path, CLOUD)
    add(tmp_path, 'cloud/剧集/散装文件.strm')   # 直接躺在分类目录里，认不出属于哪部作品
    db = empty_db()
    scoped = partition(tmp_path, ROOTS, scan(tmp_path, ROOTS, db), 'title_year', db, 'account')
    assert scoped['projection']['items'][0]['tmdb_id'] == 12
    reported = {item['path']: item['reason'] for item in scoped['unsupported']}
    assert '作品目录无法识别' in reported['cloud/剧集/散装文件.strm']
    assert '网盘源文件未登记' in reported[CLOUD]
