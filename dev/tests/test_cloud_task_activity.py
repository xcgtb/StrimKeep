"""Task polling stays small and includes durable follow-up work."""
import time
from app.cloud import store, jobs, strm
from dev.tests.test_cloud_workbench import cloud
from dev.tests.test_my_drive import client


def add_job(job_id, state='success', result='{}', kind='organize'):
    now = time.time()
    with store.db() as con:
        con.execute('INSERT INTO cloud_jobs VALUES(?,?,?,?,?,?,?,?)',
                    (job_id, kind, state, '{}', result, '任务记录', now, now))


def step(job_id, key, state):
    with store.db() as con:
        con.execute('INSERT INTO cloud_steps VALUES(?,?,?,?,?,?)',
                    (job_id, key, state, '{}', '{}', time.time()))


def test_list_does_not_decode_job_or_step_results(cloud, monkeypatch):
    add_job('large', result=store.encode({'files': ['large-file-name' * 100] * 1000}))
    step('large', 'publication:4', 'dispatched')
    with store.db() as con:
        con.execute('INSERT INTO cloud_progress VALUES(?,?,?,?,?,?)', ('large', 1, 1, '移动归档', '电影.mkv', time.time()))
    monkeypatch.setattr(store, 'job', lambda *_: (_ for _ in ()).throw(AssertionError('full job read')))
    monkeypatch.setattr(store.json, 'loads', lambda *_: (_ for _ in ()).throw(AssertionError('JSON decode')))
    values = store.job_summaries()
    assert len(values) == 1 and 'result' not in values[0]
    assert values[0]['progress']['processed'] == 1
    assert values[0]['activity']['active'] is True
    assert values[0]['activity']['label'] == 'STRM 后续处理中'


def test_parent_tracks_delayed_children_without_double_count(cloud):
    add_job('parent')
    add_job('later', 'queued', kind='publish')
    with store.db() as con:
        con.execute('INSERT INTO cloud_job_due VALUES(?,?,?,?,?)', ('later', time.time()+60, 'parent', '4', time.time()))
    step('parent', 'poster:movie', 'verified')
    step('later', 'publication:4', 'queued')
    values = {v['id']: v for v in store.job_summaries()}
    assert values['parent']['activity']['deferred'] == {'queued': 1}
    assert values['parent']['activity']['publication'] == {'queued': 1}
    assert values['later']['activity']['publication'] == {'queued': 1}
    assert values['parent']['activity']['notification'] == {'verified': 1}
    assert values['parent']['activity']['active']
    assert jobs.details('parent')['activity'] == values['parent']['activity']


def test_uncertain_notification_remains_attention_without_replay(cloud):
    add_job('notice')
    step('notice', 'notification', 'unknown')
    step('notice', 'rename:4', 'unknown')  # unrelated remote steps aren't publication counters
    a = store.job_summaries()[0]['activity']
    assert a['needs_attention'] and not a['active']
    assert a['notification'] == {'unknown': 1}
    assert a['publication'] == {}
    assert a['label'] == '后续结果待核实'
    with store.db() as con:
        assert con.execute('SELECT state FROM cloud_steps WHERE step_key=?', ('notification',)).fetchone()[0] == 'unknown'


def test_success_only_after_children_finish_and_failed_child_visible(cloud):
    add_job('parent')
    add_job('child', 'failed', kind='publish')
    with store.db() as con:
        con.execute('INSERT INTO cloud_job_due VALUES(?,?,?,?,?)', ('child', 1, 'parent', '4', 1))
    a = {v['id']: v['activity'] for v in store.job_summaries()}['parent']
    assert not a['active'] and a['needs_attention']
    with store.db() as con:
        con.execute("UPDATE cloud_jobs SET state='success' WHERE id='child'")
    a = {v['id']: v['activity'] for v in store.job_summaries()}['parent']
    assert a['label'] == '已完成' and not a['needs_attention']
    with store.db() as con:
        con.execute("UPDATE cloud_jobs SET state='cancelled' WHERE id='child'")
    a = {v['id']: v['activity'] for v in store.job_summaries()}['parent']
    assert a['needs_attention'] and a['label'] == '后续生成已取消或停止'


def test_assets_filter_precedes_limit_and_offset(cloud, client):
    with store.db() as con:
        for i, account in enumerate(['other'] * 110 + [cloud.id] * 3):
            con.execute('INSERT INTO cloud_assets VALUES(?,?,?,?,?,?,?,?)',
                        (str(i), account, str(i), '/fixture/'+str(i)+'.strm', '{}', 'nonce', 'active', 1000-i))
    assert len(strm.list_assets()) == 100
    rows = strm.list_assets(1, 1, cloud.id)
    assert len(rows) == 1 and rows[0]['account_id'] == cloud.id and rows[0]['id'] == '111'
    data = client.get('/api/cloud/assets', params={'account_id': cloud.id, 'limit': 2}).json()
    assert len(data['items']) == 2 and all(r['account_id'] == cloud.id for r in data['items'])


def test_list_api_excludes_full_result_and_has_active_stage(cloud, client):
    add_job('job', 'running', result='{"private_large_result":"unused"}')
    step('job', 'publication:4', 'queued')
    rows = client.get('/api/cloud/jobs').json()['items']
    assert rows[0]['id'] == 'job' and 'result' not in rows[0]
    assert rows[0]['activity']['label'] == '整理与 STRM 生成中'
