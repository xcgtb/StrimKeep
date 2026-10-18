"""Cron/timezones, independent rules, durable links and scoped empty-folder cleanup."""

import copy
import json
import time
from datetime import datetime, timezone
from unittest.mock import Mock
import pytest
from dev.tests.test_cloud_workbench import cloud
from dev.tests.test_my_drive import client
from app.cloud import schedules, settings, store, workflow, jobs, automation, provider115, directories, strm
from app.cloud.errors import CloudError


def stamp(value):
    return datetime.fromisoformat(value).timestamp()


@pytest.mark.parametrize('expression,after,expected', [
    ('0 3 * * *', '2026-10-14T19:00:00+00:00', '2026-10-15T19:00:00+00:00'),
    ('*/15 8-9 * * 1-5', '2026-10-15T00:14:01+00:00', '2026-10-15T00:15:00+00:00'),
    ('5,35 9 * * 7', '2026-10-15T00:00:00+00:00', '2026-10-18T01:05:00+00:00'),
    ('0 0 29 2 *', '2026-10-15T00:00:00+00:00', '2028-02-28T16:00:00+00:00'),
    ('0 0 1 * 1', '2026-10-14T16:00:00+00:00', '2026-10-18T16:00:00+00:00'),
])
def test_cron_next_numeric_calendar_and_day_or_semantics(expression, after, expected):
    assert schedules.cron_next(expression, 'Asia/Shanghai', stamp(after)) == stamp(expected)


@pytest.mark.parametrize('value', ['0 0 * *', '60 * * * *', '* 24 * * *', '*/0 * * * *', '0 3 31 2 *', '0 0 * * MON', '0 0 * * 8', '0 0 * 13 *', '0 0 * * 3-1', '0/61 * * * *', '@daily'])
def test_cron_invalid_and_impossible_dates_rejected(value):
    with pytest.raises(CloudError):
        schedules.cron_next(value, 'Asia/Shanghai', stamp('2026-01-01T00:00:00+00:00'))


def test_dst_skips_missing_time_and_uses_both_repeated_instants():
    assert schedules.cron_next('30 2 * * *', 'America/New_York', stamp('2026-03-08T06:00:00+00:00')) == stamp('2026-03-09T06:30:00+00:00')
    assert schedules.cron_next('30 1 * * *', 'America/New_York', stamp('2026-11-01T05:30:00+00:00')) == stamp('2026-11-01T06:30:00+00:00')
    with pytest.raises(CloudError):
        schedules.zone('Asia/Missing')


def test_old_interval_rule_is_compatible():
    assert schedules.next_run({'interval_seconds': 300}, 100) == 400


def test_rules_api_validates_cron_dirs_and_independent_sync(cloud, client, monkeypatch):
    from app.routers import cloud as router
    monkeypatch.setattr(router, 'for_account', lambda aid: cloud.remote)
    base = {'account_id': cloud.id, 'source_cid': '1', 'name': '同步', 'enabled': True, 'kind': 'sync', 'schedule_mode': 'cron', 'cron_expression': '0 3 * * *'}
    assert client.post('/api/cloud/rules', json={**base, 'cron_expression': 'bad'}).status_code == 400
    assert client.post('/api/cloud/rules', json={**base, 'timezone': 'Bad'}).status_code == 400
    assert client.post('/api/cloud/rules', json={**base, 'kind': 'workflow'}).status_code == 400
    r = client.post('/api/cloud/rules', json=base)
    assert r.status_code == 200 and r.json()['next_run'] > time.time()
    rid = r.json()['id']
    with store.db() as con:
        con.execute('UPDATE cloud_rules SET next_run=0 WHERE id=?', (rid,))
    jobs.schedule()
    jobs.schedule()
    with store.db() as con:
        rows = con.execute('SELECT id,kind,payload FROM cloud_jobs').fetchall()
    assert len(rows) == 1 and rows[0][1] == 'sync'
    assert json.loads(rows[0][2])['rule_id'] == rid
    jobs.execute(rows[0][0])
    assert store.job(rows[0][0])['state'] == 'success' and list(cloud.root.rglob('*.strm'))
    assert not cloud.remote.writes
    assert client.post(f'/api/cloud/rules/{rid}/enabled', json={'enabled': False}).status_code == 200
    assert client.post(f'/api/cloud/rules/{rid}/enabled', json={'enabled': True}).status_code == 200
    p = client.post('/api/cloud/schedules/preview', json={'schedule_mode': 'cron', 'cron_expression': '0 4 * * *'})
    assert p.status_code == 200 and p.json()['timezone'] == 'Asia/Shanghai'


def test_due_rule_coalesces_active_run_and_pauses_changed_credentials(cloud, client, monkeypatch):
    from app.routers import cloud as router
    monkeypatch.setattr(router, 'for_account', lambda aid: cloud.remote)
    body = {'account_id': cloud.id, 'source_cid': '1', 'target_cid': '2', 'name': '自动', 'enabled': True, 'interval_seconds': 300}
    rid = client.post('/api/cloud/rules', json=body).json()['id']
    with store.db() as con:
        con.execute('UPDATE cloud_rules SET next_run=0 WHERE id=?', (rid,))
    jobs.schedule()
    with store.db() as con:
        con.execute('UPDATE cloud_rules SET next_run=0 WHERE id=?', (rid,))
    jobs.schedule()
    assert len(store.jobs()) == 1
    with store.db() as con:
        con.execute('UPDATE cloud_rules SET next_run=0 WHERE id=?', (rid,))
        con.execute('UPDATE cloud_accounts SET revision=revision+1 WHERE id=?', (cloud.id,))
    jobs.schedule()
    rule = client.get('/api/cloud/rules').json()['items'][0]
    assert not rule['enabled'] and '授权变化' in rule['schedule_error']


def test_strm_directory_is_independent_and_old_accounts_migrate_without_reset(cloud, client):
    path = f'/api/cloud/accounts/{cloud.id}/directories'
    first = client.post(path, json={'directories': {'source': '1', 'target': '2'}, 'expected_revision': 0}).json()
    assert first['directories']['strm'] is None
    second = client.post(path, json={'directories': {'strm': '2'}, 'expected_revision': 1})
    assert second.status_code == 200
    saved = client.get(path).json()['directories']
    assert saved['source']['id'] == '1' and saved['target']['id'] == saved['strm']['id'] == '2'


def organized_delay(cloud):
    settings.save({'strm_delay_seconds': 60})
    plan = workflow.plan(cloud.id, '1', '2')
    jid = workflow.apply(plan['id'], True)
    jobs.execute(jid)
    origin = store.job(jid)
    assert origin['state'] == 'success' and not origin['result']['generated']
    assert not list(cloud.root.rglob('*.strm'))
    link = origin['result']['deferred'][0]
    return jid, link


def make_due(jid):
    with store.db() as con:
        con.execute('UPDATE cloud_job_due SET due=0 WHERE job_id=?', (jid,))


def test_delayed_publication_persists_idempotently_and_only_runs_when_due(cloud):
    jid, link = organized_delay(cloud)
    assert store.job(link['job_id'])['due'] > time.time() and store.job(link['job_id'])['origin_job'] == jid
    assert jobs.execute(link['job_id'])['status'] == 'error'
    assert store.job(link['job_id'])['state'] == 'queued'
    with store.db() as con:
        raw = con.execute('SELECT payload FROM cloud_jobs WHERE id=?', (link['job_id'],)).fetchone()[0]
    p = json.loads(raw)
    again = automation.defer(jid, p['account_id'], p['account_revision'], p['scope_cid'], p['source'], p['media'], p['companions'], 999)
    assert again == link
    make_due(link['job_id']);jobs.execute(link['job_id'])
    assert store.job(link['job_id'])['state'] == 'success' and len(list(cloud.root.rglob('*.strm'))) == 1
    assert jobs.execute(link['job_id'])['status'] == 'error'


@pytest.mark.parametrize('change', ['video', 'companion', 'output', 'credentials', 'ancestor'])
def test_delay_refuses_changed_inputs(cloud, change):
    _, link = organized_delay(cloud)
    if change == 'video':
        cloud.remote.files['4']['size'] += 1
    elif change == 'companion':
        cloud.remote.files['5']['name'] = 'Changed.srt'
    elif change == 'output':
        settings.save({'public_url': 'http://changed.example:8321'})
    elif change == 'credentials':
        with store.db() as con:
            con.execute('UPDATE cloud_accounts SET revision=revision+1 WHERE id=?', (cloud.id,))
    else:
        cloud.remote.files[cloud.remote.files['4']['parent_id']]['parent_id'] = '0'
    make_due(link['job_id']);jobs.execute(link['job_id'])
    assert store.job(link['job_id'])['state'] == 'failed'
    assert not list(cloud.root.rglob('*.strm'))


def test_cleanup_config_change_invalidates_plan(cloud):
    plan = workflow.plan(cloud.id, '1', '2')
    settings.save({'cleanup_empty_directories': True})
    with pytest.raises(CloudError, match='重新预览'):
        workflow.apply(plan['id'], True)


def nested_source(cloud, monkeypatch):
    monkeypatch.setattr(directories, 'for_account', lambda aid: cloud.remote)
    parent = cloud.remote.mkdir('1', '批次')
    leaf = cloud.remote.mkdir(parent, '来源作品')
    cloud.remote.move('4', leaf);cloud.remote.move('5', leaf)
    sibling = cloud.remote.mkdir('1', '原有空目录')
    def delete(fid):
        assert not cloud.remote.list(fid)
        cloud.remote.writes.append(('delete-empty', fid))
        del cloud.remote.files[fid]
    monkeypatch.setattr(cloud.remote, 'delete', delete, raising=False)
    monkeypatch.setattr(cloud.remote, 'removed_receipt', lambda fid, source, acknowledged=False: {'file_id': fid} if fid not in cloud.remote.files else None, raising=False)
    monkeypatch.setattr(cloud.remote, 'remove_empty_directory', lambda snapshot: provider115.Provider115.remove_empty_directory(cloud.remote, snapshot), raising=False)
    return parent, leaf, sibling


def test_cleanup_only_moved_empty_ancestors_bottom_up_never_roots_or_siblings(cloud, monkeypatch):
    parent, leaf, sibling = nested_source(cloud, monkeypatch)
    settings.save({'cleanup_empty_directories': True})
    plan = workflow.plan(cloud.id, '1', '2');jid = workflow.apply(plan['id'], True);jobs.execute(jid)
    job = store.job(jid)
    assert job['state'] == 'success'
    assert [i['id'] for i in job['result']['cleanup']['removed']] == [leaf, parent]
    assert all(fid in cloud.remote.files for fid in ('0', '1', '2', '3', sibling))


def test_cleanup_keeps_metadata_leftovers_and_protected_roles(cloud, monkeypatch):
    parent, leaf, sibling = nested_source(cloud, monkeypatch)
    cloud.remote.files['8'] = {**cloud.remote.info('5'), 'id': '8', 'parent_id': leaf, 'name': 'cover.jpg'}
    settings.save({'cleanup_empty_directories': True})
    plan = workflow.plan(cloud.id, '1', '2');jid = workflow.apply(plan['id'], True);jobs.execute(jid)
    assert leaf in cloud.remote.files and parent in cloud.remote.files
    assert not store.job(jid)['result']['cleanup']['removed']
    assert any(i['id'] == leaf for i in store.job(jid)['result']['cleanup']['kept'])


def test_cleanup_uncertain_delete_never_replayed_and_preserves_organize_result(cloud, monkeypatch):
    _, leaf, _ = nested_source(cloud, monkeypatch)
    settings.save({'cleanup_empty_directories': True})
    monkeypatch.setattr(cloud.remote, 'removed_receipt', lambda *a, **k: None)
    plan = workflow.plan(cloud.id, '1', '2');jid = workflow.apply(plan['id'], True);jobs.execute(jid)
    job = store.job(jid)
    assert job['state'] == 'uncertain' and job['result']['files'] and job['result']['generated']
    assert jobs.execute(jid)['status'] == 'error'
    assert len([w for w in cloud.remote.writes if w == ('delete-empty', leaf)]) == 1


def test_adapter_keeps_content_added_between_checks(cloud, monkeypatch):
    parent, leaf, _ = nested_source(cloud, monkeypatch)
    cloud.remote.move('4', '2');cloud.remote.move('5', '2')
    snapshot = cloud.remote.info(leaf)
    outer = cloud.remote.info(parent)
    calls = 0
    listing = cloud.remote.list
    def add_between_checks(fid):
        nonlocal calls
        if fid == leaf:
            calls += 1
            if calls == 2:
                cloud.remote.files['10'] = {**cloud.remote.info('4'), 'id': '10', 'parent_id': leaf, 'name': 'new.mkv'}
        return listing(fid)
    monkeypatch.setattr(cloud.remote, 'list', add_between_checks)
    plan = {'account_id': cloud.id, 'source_cid': '1', 'target_cid': '2', 'cleanup_candidates': [outer,snapshot]}
    result = automation.cleanup(cloud.remote, 'cleanup', plan, {leaf})
    assert result['kept'] and not result['removed']
    assert not [w for w in cloud.remote.writes if w[0] == 'delete-empty']


def test_cleanup_protects_strm_source_and_refuses_ancestor_moved_outside(cloud, monkeypatch):
    parent, leaf, _ = nested_source(cloud, monkeypatch)
    settings.save({'cleanup_empty_directories': True})
    directories.save(cloud.id, {'strm': leaf}, 0)
    plan = workflow.plan(cloud.id, '1', '2');jid = workflow.apply(plan['id'], True);jobs.execute(jid)
    assert leaf in cloud.remote.files and parent in cloud.remote.files
    assert not [w for w in cloud.remote.writes if w[0] == 'delete-empty']
    directories.save(cloud.id, {'strm': None}, 1)
    snapshot = cloud.remote.info(leaf);outer = cloud.remote.info(parent)
    cloud.remote.files[parent]['parent_id'] = '0'
    data = {'account_id': cloud.id, 'source_cid': '1', 'target_cid': '2', 'cleanup_candidates': [outer,snapshot]}
    result = automation.cleanup(cloud.remote, 'cleanup', data, {leaf})
    assert result['kept'] and not result['removed']


def test_stop_during_cleanup_does_not_delete_next_parent(cloud, monkeypatch):
    parent, leaf, _ = nested_source(cloud, monkeypatch)
    settings.save({'cleanup_empty_directories': True})
    plan = workflow.plan(cloud.id, '1', '2');jid = workflow.apply(plan['id'], True)
    delete = cloud.remote.delete
    def stop(fid):
        delete(fid);jobs.request_stop(jid)
    monkeypatch.setattr(cloud.remote, 'delete', stop)
    jobs.execute(jid)
    job = store.job(jid)
    assert job['state'] == 'stopped' and parent in cloud.remote.files and leaf not in cloud.remote.files
    assert job['result']['cleanup']['stopped'] and job['result']['generated']


def test_delayed_cancel_preserves_source_and_explicit_no_generate_creates_no_link(cloud, client):
    _, link = organized_delay(cloud)
    assert client.post(f"/api/cloud/jobs/{link['job_id']}/cancel", json={}).status_code == 200
    make_due(link['job_id']);jobs.execute(link['job_id'])
    assert store.job(link['job_id'])['state'] == 'stopped' and not list(cloud.root.rglob('*.strm'))
    assert cloud.remote.info('4')['parent_id'] != '1'
    cloud.remote.files['6'] = {**cloud.remote.info('4'), 'id': '6', 'parent_id': '1', 'name': 'Movie.2024.720p.mkv'}
    plan = workflow.plan(cloud.id, '1', '2');jid = workflow.apply(plan['id'], True, generate=False);jobs.execute(jid)
    assert not store.job(jid)['result'].get('deferred')


@pytest.mark.parametrize('patch', [{'strm_delay_seconds': True}, {'strm_delay_seconds': -1}, {'strm_delay_seconds': 86401}, {'strm_delay_seconds': float('nan')}, {'cleanup_empty_directories': 1}])
def test_invalid_automation_settings_rejected(cloud, patch):
    with pytest.raises(CloudError):
        settings.save(patch)


@pytest.mark.parametrize('has_next', [True, False])
def test_sync_stop_finishes_current_item_but_skips_reconciliation(cloud, monkeypatch, has_next):
    if has_next:
        cloud.remote.files['6'] = {**cloud.remote.info('4'), 'id': '6', 'name': 'Second.2024.mkv'}
    p = {'account_id': cloud.id, 'account_revision': store.account(cloud.id)['revision'], 'source_cid': '1'}
    jid = jobs.enqueue('sync', p)
    generate = strm.generate
    def stop(*a, **kw):
        asset = generate(*a, **kw)
        jobs.request_stop(jid)
        return asset
    monkeypatch.setattr(strm, 'generate', stop)
    monkeypatch.setattr(strm, 'reconcile', lambda *a, **k: pytest.fail('partial stop must not reconcile'))
    jobs.execute(jid)
    job = store.job(jid)
    assert job['state'] == 'stopped' and len(job['result']['generated']) == 1
    assert len(job['result'].get('remaining', [])) == int(has_next)
