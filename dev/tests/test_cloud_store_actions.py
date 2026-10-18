# -*- coding: utf-8 -*-
"""契约：账号断开与频道删除必须走 store，且不能跨账号误伤。

这两件事是把 SQL 从路由搬进 store 层时最容易改坏的地方：
- 断开账号是级联删除，漏一张表就留下孤儿数据；
- 删频道必须带 account_id 条件，否则换个账号 id 就能删别人的频道。

测试用 state_scope 指向临时目录，得到独立的真实 SQLite（WAL），不碰运行数据。
"""
import json

import pytest

from app import storage
from app.cloud import store
from app.cloud.errors import CloudError


@pytest.fixture
def db(tmp_path):
    with storage.state_scope(tmp_path):
        yield


def row(con, sql, *args):
    return con.execute(sql, args).fetchone()


def seed_account(con, account_id='a1'):
    con.execute(
        "INSERT INTO cloud_accounts VALUES(?,?,?,?,?,?,?)",
        (account_id, '115', 'uid-' + account_id, 'name', 'sealed', 1, 1.0))
    con.execute(
        "INSERT INTO cloud_directory_settings VALUES(?,?,?,?)",
        (account_id, '{}', 1, 1.0))
    con.execute(
        "INSERT INTO cloud_media_probes VALUES(?,?,?,?,?)",
        (account_id, 'f1', 'fp', '{}', 1.0))
    # cloud_assets.local_path 全局唯一：不同账号必须落在不同 STRM 路径上。
    con.execute(
        "INSERT INTO cloud_assets VALUES(?,?,?,?,?,?,?,?)",
        ('as-' + account_id, account_id, 'f1', '/p-' + account_id, '{}', 'n', 'ok', 1.0))
    con.execute(
        "INSERT INTO cloud_rules VALUES(?,?,?,?,?)",
        ('r-' + account_id, store.encode({'account_id': account_id}), 1, 0.0, 1.0))


def test_disconnect_account_removes_every_cascade_table(db):
    with store.db() as con:
        seed_account(con)

    store.disconnect_account('a1')

    with store.db() as con:
        assert row(con, "SELECT COUNT(*) FROM cloud_accounts WHERE id='a1'")[0] == 0
        assert row(con, "SELECT COUNT(*) FROM cloud_directory_settings WHERE account_id='a1'")[0] == 0
        assert row(con, "SELECT COUNT(*) FROM cloud_media_probes WHERE account_id='a1'")[0] == 0


def test_disconnect_account_marks_assets_and_disables_rules(db):
    with store.db() as con:
        seed_account(con)

    store.disconnect_account('a1')

    with store.db() as con:
        assert row(con, "SELECT state FROM cloud_assets WHERE id='as-a1'")[0] == 'disconnected'
        assert row(con, "SELECT enabled FROM cloud_rules WHERE id='r-a1'")[0] == 0


def test_disconnect_account_keeps_job_history(db):
    """断开账号不能删历史任务——否则事后无法追溯做过什么。"""
    with store.db() as con:
        seed_account(con)
        con.execute(
            "INSERT INTO cloud_jobs VALUES(?,?,?,?,?,?,?,?)",
            ('j1', 'transfer', 'success', store.encode({'account_id': 'a1'}), '{}', '', 1.0, 1.0))

    store.disconnect_account('a1')

    with store.db() as con:
        assert row(con, "SELECT COUNT(*) FROM cloud_jobs WHERE id='j1'")[0] == 1


@pytest.mark.parametrize('state', ['queued', 'running'])
def test_disconnect_itself_rejects_active_work_and_keeps_account(db, state):
    with store.db() as con:
        seed_account(con)
        con.execute("INSERT INTO cloud_jobs VALUES(?,?,?,?,?,?,?,?)",
                    ('j1', 'transfer', state, store.encode({'account_id': 'a1'}), '{}', '', 1., 1.))
    with pytest.raises(CloudError) as error:
        store.disconnect_account('a1')
    assert error.value.status == 409
    with store.db() as con:
        assert row(con, "SELECT id FROM cloud_accounts WHERE id='a1'")
        assert row(con, "SELECT enabled FROM cloud_rules WHERE id='r-a1'")[0] == 1


def test_enable_rechecks_account_revision_in_write_transaction(db):
    payload = {'account_id': 'a1', 'account_revision': 1}
    with store.db() as con:
        seed_account(con)
        con.execute("UPDATE cloud_rules SET payload=?,enabled=0 WHERE id='r-a1'", (store.encode(payload),))
        con.execute("UPDATE cloud_accounts SET revision=2 WHERE id='a1'")
    with pytest.raises(CloudError) as error:
        store.update_rule_enabled('r-a1', True, 100, payload)
    assert error.value.status == 409
    with store.db() as con:
        assert row(con, "SELECT enabled FROM cloud_rules WHERE id='r-a1'")[0] == 0


def test_stale_rule_payload_cannot_overwrite_concurrent_edit(db):
    with store.db() as con:
        seed_account(con)
    with pytest.raises(CloudError) as error:
        store.update_rule_enabled('r-a1', False, 100, {'account_id': 'a1', 'stale': True})
    assert error.value.status == 409
    assert store.rule_payload('r-a1') == {'account_id': 'a1'}


@pytest.mark.parametrize('state,expected', [
    ('queued', True),
    ('running', True),
    ('success', False),
    ('failed', False),
    ('uncertain', False),
])
def test_active_jobs_detected_by_payload_account(db, state, expected):
    with store.db() as con:
        seed_account(con)
        con.execute(
            "INSERT INTO cloud_jobs VALUES(?,?,?,?,?,?,?,?)",
            ('j1', 'transfer', state, store.encode({'account_id': 'a1'}), '{}', '', 1.0, 1.0))

    assert store.account_has_active_jobs('a1') is expected


def test_active_jobs_detected_through_plan_owner(db):
    """任务 payload 里只有 plan_id 时，要能顺着计划找到账号。"""
    with store.db() as con:
        seed_account(con)
        con.execute(
            "INSERT INTO cloud_plans VALUES(?,?,?,?,?,?)",
            ('p1', 'a1', 1, 'queued', '{}', 9999999999.0))
        con.execute(
            "INSERT INTO cloud_jobs VALUES(?,?,?,?,?,?,?,?)",
            ('j1', 'transfer', 'queued', store.encode({'plan_id': 'p1'}), '{}', '', 1.0, 1.0))

    assert store.account_has_active_jobs('a1') is True


def test_no_active_jobs_for_other_account(db):
    with store.db() as con:
        seed_account(con)
        con.execute(
            "INSERT INTO cloud_jobs VALUES(?,?,?,?,?,?,?,?)",
            ('j1', 'transfer', 'running', store.encode({'account_id': 'a1'}), '{}', '', 1.0, 1.0))

    assert store.account_has_active_jobs('a2') is False


def test_delete_channel_is_scoped_to_owner(db):
    with store.db() as con:
        seed_account(con, 'a1')
        seed_account(con, 'a2')
        con.execute(
            "INSERT INTO cloud_channels VALUES(?,?,?,?,?,?)",
            ('c1', 'a2', 1, 1, '{}', 1.0))

    assert store.delete_channel('a1', 'c1') == 0
    with store.db() as con:
        assert row(con, "SELECT COUNT(*) FROM cloud_channels WHERE id='c1'")[0] == 1

    assert store.delete_channel('a2', 'c1') == 1
    with store.db() as con:
        assert row(con, "SELECT COUNT(*) FROM cloud_channels WHERE id='c1'")[0] == 0


def test_delete_channel_is_idempotent(db):
    with store.db() as con:
        seed_account(con, 'a1')

    assert store.delete_channel('a1', 'missing') == 0
    assert store.delete_channel('a1', 'missing') == 0
