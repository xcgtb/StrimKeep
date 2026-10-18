# -*- coding: utf-8 -*-
"""契约：云盘域的幂等键必须是数据库约束，不能只靠"先查后插"。

这些断言挡的是一类最贵的回归：某个新功能把 UNIQUE 去掉，或者改表时漏了约束，
于是重复消息、重复转存、重复删除在并发下悄悄发生——而单元测试看不出来。

约定：每条用例的第二行都与第一行「业务键相同、其他列不同」。
这样 IntegrityError 一定来自业务键，而不是主键撞车。
"""
import sqlite3

import pytest

from app.cloud import store


def fresh():
    con = sqlite3.connect(':memory:')
    for statement in store.SCHEMA.split(';'):
        if statement.strip():
            con.execute(statement)
    return con


def insert(con, table, columns, values):
    sql = 'INSERT INTO {} ({}) VALUES ({})'.format(
        table, ','.join(columns), ','.join('?' * len(columns)))
    return con.execute(sql, values)


def insert_or_ignore(con, table, columns, values):
    sql = 'INSERT OR IGNORE INTO {} ({}) VALUES ({})'.format(
        table, ','.join(columns), ','.join('?' * len(columns)))
    return con.execute(sql, values)


# (表, 列, 首行, 重复业务键的另一行)
CASES = [
    ('cloud_accounts',
     ('id', 'provider', 'uid', 'name', 'sealed', 'revision', 'updated'),
     ('a1', '115', 'u1', 'n1', 's1', 1, 1.0),
     ('a2', '115', 'u1', 'n2', 's2', 1, 2.0)),

    ('cloud_files',
     ('account_id', 'file_id', 'parent_id', 'name', 'size', 'sha1', 'pickcode', 'is_dir', 'observed'),
     ('a', 'f1', 'p1', 'n1', 1, 'sha1', 'pc1', 0, 1.0),
     ('a', 'f1', 'p2', 'n2', 2, 'sha2', 'pc2', 1, 2.0)),

    ('cloud_media_probes',
     ('account_id', 'file_id', 'fingerprint', 'payload', 'updated'),
     ('a', 'f1', 'fp1', '{}', 1.0),
     ('a', 'f1', 'fp2', '{"x":1}', 2.0)),

    ('cloud_steps',
     ('job_id', 'step_key', 'state', 'payload', 'result', 'updated'),
     ('j1', 's1', 'queued', '{}', '{}', 1.0),
     ('j1', 's1', 'running', '{}', '{}', 2.0)),

    ('cloud_job_due',
     ('job_id', 'due', 'origin_job', 'file_id', 'created'),
     ('c1', 1.0, 'origin', 'f1', 1.0),
     ('c2', 2.0, 'origin', 'f1', 2.0)),

    ('cloud_inbox',
     ('id', 'event_key', 'account_id', 'source', 'payload', 'state', 'created'),
     ('i1', 'ev1', 'a', 'web', '{}', 'queued', 1.0),
     ('i2', 'ev1', 'a', 'web', '{}', 'queued', 2.0)),

    ('cloud_transfer_requests',
     ('account_id', 'request_id', 'sealed', 'job_id', 'created'),
     ('a', 'r1', 's1', 'j1', 1.0),
     ('a', 'r1', 's2', 'j2', 2.0)),

    ('cloud_assets',
     ('id', 'account_id', 'file_id', 'local_path', 'payload', 'nonce', 'state', 'updated'),
     ('as1', 'a', 'f1', '/p1', '{}', 'n1', 'ok', 1.0),
     ('as2', 'a', 'f2', '/p1', '{}', 'n2', 'ok', 2.0)),

    ('cloud_ingestion',
     ('id', 'asset_id', 'connection_id', 'revision', 'payload', 'state',
      'next_check', 'attempts', 'result', 'updated'),
     ('g1', 'as1', 'c1', '1', '{}', 'pending', 0, 0, '{}', 1.0),
     ('g2', 'as1', 'c1', '1', '{}', 'done', 0, 1, '{}', 2.0)),

    ('cloud_upgrades',
     ('id', 'job_id', 'new_asset', 'old_asset', 'state', 'payload', 'result', 'updated'),
     ('u1', 'j1', 'new1', 'old1', 'pending', '{}', '{}', 1.0),
     ('u2', 'j1', 'new2', 'old1', 'pending', '{}', '{}', 2.0)),

    ('cloud_share_sources',
     ('id', 'account_id', 'code_hash', 'sealed', 'revision', 'updated'),
     ('sh1', 'a', 'h1', 's1', 1, 1.0),
     ('sh2', 'a', 'h1', 's2', 1, 2.0)),

    ('cloud_deletions',
     ('account_id', 'file_id', 'asset_id', 'state', 'payload', 'result', 'updated'),
     ('a', 'f1', 'as1', 'ok', '{}', '{}', 1.0),
     ('a', 'f1', 'as2', 'no', '{}', '{}', 2.0)),

    ('cloud_service_settings',
     ('account_id', 'kind', 'payload', 'revision', 'account_revision', 'updated'),
     ('a', 'k1', '{}', 1, 1, 1.0),
     ('a', 'k1', '{"v":1}', 2, 1, 2.0)),
]


@pytest.mark.parametrize('table,columns,first,duplicate', CASES,
                         ids=[case[0] for case in CASES])
def test_duplicate_business_key_is_rejected(table, columns, first, duplicate):
    con = fresh()
    insert(con, table, columns, first)
    with pytest.raises(sqlite3.IntegrityError):
        insert(con, table, columns, duplicate)


@pytest.mark.parametrize('table,columns,first,duplicate', CASES,
                         ids=[case[0] for case in CASES])
def test_insert_or_ignore_absorbs_duplicate(table, columns, first, duplicate):
    """项目里到处用 INSERT OR IGNORE 做幂等，这一条保证它真的会"忽略"。"""
    con = fresh()
    assert insert_or_ignore(con, table, columns, first).rowcount == 1
    assert insert_or_ignore(con, table, columns, duplicate).rowcount == 0


def test_schema_is_reentrant():
    """建表语句必须可以重复执行（每次事务都会跑一遍 SCHEMA）。"""
    con = fresh()
    for statement in store.SCHEMA.split(';'):
        if statement.strip():
            con.execute(statement)


def test_cloud_jobs_state_column_has_no_hidden_enum():
    """cloud_jobs.state 是文本列，合法取值由 app/cloud/states.py 约束。

    这条测的是"没有在数据库层偷偷加 CHECK"：加 CHECK 会让状态扩展变成一次迁移。
    """
    con = fresh()
    sql = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='cloud_jobs'").fetchone()[0]
    assert 'CHECK' not in sql.upper()
