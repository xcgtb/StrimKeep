"""调度防抖：等待登录合并、空转退避、登录恢复只继续一次。"""
import asyncio
import json
import time

import httpx

from strimkeep.organizer import Organizer
from strimkeep.store import Store
from test_organize import MemoryCloud


def make_engine(tmp_path):
    store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
    store.set_many({'cookie': 'UID=1; CID=t; SEID=t', 'tmdb_key': 'k',
                    'playback_base': 'http://nas:9000'})
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})))
    return store, Organizer(store, MemoryCloud(store), http, tmp_path / 'strm'), http


CONFIG = {'job_kind': 'organize', 'account_id': '1', 'source_cids': ['100'], 'scheduled': True}


def test_scheduled_enqueue_merges_into_waiting_login(tmp_path):
    store, engine, http = make_engine(tmp_path)
    first, merged = engine.tasks.enqueue(dict(CONFIG), scheduled=True)
    assert not merged
    with store.db:
        store.db.execute("UPDATE organize_jobs SET state='waiting_login' WHERE id=?", (first,))
    second, merged = engine.tasks.enqueue(dict(CONFIG), scheduled=True)
    assert merged and second == first
    assert store.db.execute('SELECT count(*) FROM organize_jobs').fetchone()[0] == 1
    asyncio.run(http.aclose())
    store.close()


def test_login_ready_keeps_only_one_job(tmp_path):
    store, engine, http = make_engine(tmp_path)
    with store.db:
        for _ in range(4):
            store.db.execute("INSERT INTO organize_jobs(state,config,created,updated) VALUES('waiting_login',?,1,1)",
                             (json.dumps(CONFIG),))
    engine.login_ready()
    rows = store.db.execute('SELECT state,notify_state FROM organize_jobs ORDER BY id').fetchall()
    assert [r['state'] for r in rows] == ['queued', 'cancelled', 'cancelled', 'cancelled']
    assert all(r['notify_state'] == 'not_needed' for r in rows[1:])
    asyncio.run(http.aclose())
    store.close()


def test_scan_interval_backs_off_when_idle_then_wakes(tmp_path):
    store, engine, http = make_engine(tmp_path)
    store.set_many({'schedule_config': {'scan_enabled': True, 'sync_enabled': False,
                                        'scan_mode': 'interval', 'scan_interval_minutes': 5}})
    now = time.time()
    with store.db:
        store.db.execute("INSERT OR REPLACE INTO meta VALUES('scan_idle_streak','2')")
    engine.tasks.tick(now)
    due = store.db.execute("SELECT next_due FROM task_schedules WHERE kind='organize'").fetchone()[0]
    # 5 分钟基数，连续 2 轮空转后放宽到 5 * 2^1 = 10 分钟
    assert 550 <= due - now <= 610
    with store.db:
        store.db.execute("UPDATE organize_jobs SET state='cancelled'")
        store.db.execute("INSERT OR REPLACE INTO meta VALUES('scan_idle_streak','9')")
        store.db.execute("DELETE FROM task_schedules WHERE kind='organize'")
    engine.tasks.tick(now)
    capped = store.db.execute("SELECT next_due FROM task_schedules WHERE kind='organize'").fetchone()[0]
    # 退避上限 15 分钟，避免新内容被压住太久
    assert capped - now <= 900
    engine.tasks.wake('organize')
    woken = store.db.execute("SELECT next_due FROM task_schedules WHERE kind='organize'").fetchone()[0]
    assert woken - time.time() <= 300
    asyncio.run(http.aclose())
    store.close()


def test_idle_streak_resets_and_pulls_next_scan_forward(tmp_path):
    store, engine, http = make_engine(tmp_path)
    store.set_many({'schedule_config': {'scan_enabled': True, 'sync_enabled': False,
                                        'scan_mode': 'interval', 'scan_interval_minutes': 5}})
    with store.db:
        store.db.execute("INSERT OR REPLACE INTO meta VALUES('scan_idle_streak','4')")
        store.db.execute("INSERT OR REPLACE INTO task_schedules(kind,next_due) VALUES('organize',?)",
                         (time.time() + 3600,))
    engine.note_scan_result(dict(CONFIG), False)
    assert store.meta('scan_idle_streak') == '5'
    engine.note_scan_result(dict(CONFIG), True)
    assert store.meta('scan_idle_streak') == '0'
    due = store.db.execute("SELECT next_due FROM task_schedules WHERE kind='organize'").fetchone()[0]
    assert due - time.time() <= 300
    asyncio.run(http.aclose())
    store.close()


def test_interval_waits_for_completion_without_building_another_scan(tmp_path):
    store, engine, http = make_engine(tmp_path)
    store.set_many({'organize_config': {'source_cids': ['100']},
                    'schedule_config': {'scan_enabled': True, 'scan_mode': 'interval', 'scan_interval_minutes': 5}})
    engine.tasks.tick(1000)
    engine.tasks.tick(1300)
    row = store.db.execute("SELECT id FROM organize_jobs").fetchone()
    assert row is not None
    with store.db:
        store.db.execute("UPDATE organize_jobs SET state='running',updated=1300 WHERE id=?", (row['id'],))
    engine.tasks.tick(1600)
    engine.tasks.tick(1900)
    assert store.db.execute('SELECT count(*) FROM organize_jobs').fetchone()[0] == 1
    with store.db:
        store.db.execute("UPDATE organize_jobs SET state='completed',updated=1900 WHERE id=?", (row['id'],))
    engine.tasks.tick(1900)
    assert store.db.execute("SELECT next_due FROM task_schedules WHERE kind='organize'").fetchone()[0] == 2200
    engine.tasks.tick(2199)
    assert store.db.execute('SELECT count(*) FROM organize_jobs').fetchone()[0] == 1
    engine.tasks.tick(2200)
    assert store.db.execute('SELECT count(*) FROM organize_jobs').fetchone()[0] == 2
    asyncio.run(http.aclose()); store.close()


def test_idle_backoff_never_shortens_user_interval(tmp_path):
    store, engine, http = make_engine(tmp_path)
    store.set_many({'schedule_config': {'scan_enabled': True, 'scan_mode': 'interval', 'scan_interval_minutes': 60}})
    with store.db:
        store.db.execute("INSERT OR REPLACE INTO meta VALUES('scan_idle_streak','3')")
    engine.tasks.tick(1000)
    assert store.db.execute("SELECT next_due FROM task_schedules WHERE kind='organize'").fetchone()[0] >= 4600
    asyncio.run(http.aclose()); store.close()


def test_identical_manual_request_merges_and_promotes_scheduled_job(tmp_path):
    store, engine, http = make_engine(tmp_path)
    config = {'job_kind': 'organize', 'account_id': '1', 'source_cids': ['100']}
    first, _ = engine.tasks.enqueue(dict(config), scheduled=True)
    second, merged = engine.tasks.enqueue(dict(config))
    assert merged and second == first
    assert json.loads(engine.job(first)['config'])['scheduled'] is False
    other, merged = engine.tasks.enqueue({**config, 'source_cids': ['200']})
    assert not merged and other != first
    with store.db:
        store.db.execute("UPDATE organize_jobs SET state='running' WHERE id=?", (first,))
    followup, merged = engine.tasks.enqueue(dict(config))
    repeated, repeated_merged = engine.tasks.enqueue(dict(config))
    assert followup != first and not merged and repeated == followup and repeated_merged
    asyncio.run(http.aclose()); store.close()


def test_interval_does_not_add_scans_while_login_is_required(tmp_path):
    store, engine, http = make_engine(tmp_path)
    store.set_many({'organize_config': {'source_cids': ['100']},
                    'schedule_config': {'scan_enabled': True, 'scan_mode': 'interval', 'scan_interval_minutes': 5}})
    engine.tasks.tick(1000); engine.tasks.tick(1300)
    with store.db:
        store.db.execute("UPDATE organize_jobs SET state='waiting_login'")
    engine.tasks.tick(5000); engine.tasks.tick(10000)
    assert store.db.execute('SELECT count(*) FROM organize_jobs').fetchone()[0] == 1
    asyncio.run(http.aclose()); store.close()


def test_failed_round_also_waits_before_next_interval(tmp_path):
    store, engine, http = make_engine(tmp_path)
    store.set_many({'organize_config': {'source_cids': ['100']},
                    'schedule_config': {'scan_enabled': True, 'scan_mode': 'interval', 'scan_interval_minutes': 5}})
    engine.tasks.tick(1000); engine.tasks.tick(1300)
    with store.db:
        store.db.execute("UPDATE organize_jobs SET state='needs_review',updated=1900")
    engine.tasks.tick(1900)
    assert store.db.execute("SELECT next_due FROM task_schedules WHERE kind='organize'").fetchone()[0] == 2200
    assert store.db.execute('SELECT count(*) FROM organize_jobs').fetchone()[0] == 1
    asyncio.run(http.aclose()); store.close()


def test_cron_keeps_at_most_one_followup_during_running_job(tmp_path):
    store, engine, http = make_engine(tmp_path)
    store.set_many({'organize_config': {'source_cids': ['100']},
                    'schedule_config': {'scan_enabled': True, 'scan_mode': 'cron', 'scan_cron': '* * * * *', 'timezone': 'UTC'}})
    first, _ = engine.tasks.enqueue(dict(CONFIG), scheduled=True, now=1000)
    with store.db:
        store.db.execute("UPDATE organize_jobs SET state='running' WHERE id=?", (first,))
    engine.tasks.tick(1000); engine.tasks.tick(1060); engine.tasks.tick(1120); engine.tasks.tick(1180)
    rows = store.db.execute('SELECT state FROM organize_jobs ORDER BY id').fetchall()
    assert [r['state'] for r in rows] == ['running', 'queued']
    asyncio.run(http.aclose()); store.close()
