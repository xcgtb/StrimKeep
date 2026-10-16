"""Reproduce zero-output skip reports and verify useful, read-only explanations."""
import json
import time
import pytest
from unittest.mock import Mock
from app import tg
from app.cloud import diagnostics, jobs, workflow, store, settings, file_filters, notifications
from app.cloud.errors import CloudError
from dev.tests.test_cloud_workbench import cloud
from dev.tests.test_my_drive import client
from dev.tests.test_cloud_task_activity import add_job


def test_zero_output_skip_has_file_reason_suggestion_and_one_notification_log(cloud, monkeypatch):
    settings.save({'notify_enabled': True, 'notify_organize': True})
    sent = []
    monkeypatch.setattr(tg, 'notify_telegram', lambda text: sent.append(text) or True)
    monkeypatch.setattr(workflow, 'recognize_source', Mock(side_effect=CloudError('未识别到对应作品，请核对 TMDB')))
    jid = jobs.enqueue('workflow', {'account_id': cloud.id, 'account_revision': store.account(cloud.id)['revision'], 'source_cid': '1', 'target_cid': '2'})
    jobs.execute(jid)
    job = jobs.details(jid)
    assert job['state'] == 'needs_review' and job['progress']['total'] == 0
    row = job['diagnostics']['items'][0]
    assert row['file_id'] == '4' and row['name'] == 'Movie.2024.1080p.mkv'
    assert 'TMDB' in row['reason'] and '重新预览' in row['suggestion']
    assert len(sent) == 1 and '整理结果' in sent[0] and '跳过：1 项' in sent[0]
    assert row['name'] in sent[0] and '原因：' in sent[0] and '处理：' in sent[0] and jid[:12] in sent[0]
    with store.db() as con:
        logs = con.execute('SELECT message,details FROM runtime_logs').fetchall()
    structured = [json.loads(data) for _, data in logs if data and json.loads(data).get('event') == 'diagnostic']
    assert structured[0]['file_id'] == '4' and structured[0]['suggestion']
    assert sum('任务通知已发送' in msg for msg, _ in logs) == 1
    assert not cloud.remote.writes


def test_historical_skips_are_read_without_reexecution_and_paginate(cloud, client):
    result = {'skipped': [{'file_id': str(i), 'name': '片名-'+str(i)+'.mkv', 'reason': '未识别到作品'} for i in range(43)],
              'filtered': [{'file_id': '99', 'name': 'sample.mkv', 'reason': '命中忽略关键词', 'rule': 'sample'}]}
    add_job('historic', 'needs_review', store.encode(result))
    report = jobs.details('historic')['diagnostics']
    assert report['total'] == 44 and report['has_more'] and len(report['items']) == 30
    response = client.get('/api/cloud/jobs/historic/diagnostics', params={'offset': 30}).json()['diagnostics']
    assert len(response['items']) == 14 and not response['has_more']
    filtered = client.get('/api/cloud/jobs/historic/diagnostics', params={'category': 'filtered', 'query': 'sample'}).json()['diagnostics']
    assert filtered['total'] == 1 and filtered['items'][0]['rule'] == 'sample'
    assert filtered['counts']['skipped'] == 43 and not cloud.remote.writes


def test_filter_only_run_is_successful_and_quiet_but_explained(cloud, monkeypatch):
    settings.save({'min_video_mb': 1, 'notify_enabled': True, 'notify_organize': True})
    send = Mock(return_value=True)
    monkeypatch.setattr(tg, 'notify_telegram', send)
    jid = jobs.enqueue('workflow', {'account_id': cloud.id, 'account_revision': store.account(cloud.id)['revision'], 'source_cid': '1', 'target_cid': '2'})
    jobs.execute(jid)
    job = jobs.details(jid)
    assert job['state'] == 'success' and not send.called
    row = job['diagnostics']['items'][0]
    assert row['category'] == 'filtered' and not row['attention']
    assert '最小 1 MB' in row['rule'] and '实际' in row['rule']
    assert not cloud.remote.writes


def test_filter_rule_keeps_exact_regex_keyword_and_extension(cloud):
    cfg = settings.get()
    row = cloud.remote.info('4')
    cfg['ignore_keywords'] = ['Movie']
    assert file_filters.select([row], cfg)[1][0]['rule'] == 'Movie'
    cfg.update(ignore_keywords=[], ignore_regex=['not-matching', r'\.1080p\.'])
    assert file_filters.select([row], cfg)[1][0]['rule'] == r'\.1080p\.'
    cfg.update(ignore_regex=[], video_extensions=['.mp4'])
    assert '.mkv' in file_filters.select([row], cfg)[1][0]['rule']


def test_normal_retention_and_legacy_explicit_filter_are_not_errors():
    value = {'skipped': [{'name': 'sample.mkv', 'reason': '命中忽略关键词', 'action': 'filtered'}],
             'quality_held': [{'file_id': '2', 'name': 'Old.mkv', 'reason': '已有版本更优', 'blocked': False}]}
    assert not diagnostics.requires_review(value)
    report = diagnostics.report(value)
    assert not report['attention'] and report['counts']['filtered'] == 1 and report['counts']['retained'] == 1
    value['quality_held'][0]['blocked'] = True
    assert diagnostics.requires_review(value)


def test_missing_historical_reason_is_explicit_not_guessed(cloud):
    add_job('empty-old', 'needs_review')
    row = jobs.details('empty-old')['diagnostics']['items'][0]
    assert row['label'] == '任务级异常' and '任务记录' in row['reason']
    entry = diagnostics.report({'skipped': [{'name': 'Missing.mkv'}]})['items'][0]
    assert '未保存具体原因' in entry['reason']


@pytest.mark.parametrize('error', [CloudError('移动结果未确认', uncertain=True), TypeError('Cookie=never-output')])
def test_step_failure_receipt_explains_file_and_never_replays(cloud, error):
    add_job('step-failure', 'needs_review')
    call = Mock(side_effect=error)
    with pytest.raises(type(error)):
        jobs.step('step-failure', 'move:4', {'file_id': '4', 'source_name': 'Movie.mkv'}, call)
    row = jobs.details('step-failure')['diagnostics']['items'][0]
    assert row['name'] == 'Movie.mkv' and row['stage'] == '移动归档'
    assert 'never-output' not in str(row)
    with pytest.raises(CloudError):
        jobs.step('step-failure', 'move:4', {}, call)
    assert call.call_count == 1


def test_summary_search_can_find_old_job_and_treats_wildcards_literally(cloud, client):
    add_job('old-task', 'needs_review')
    for i in range(55):
        add_job('new-'+str(i))
    assert all(v['id'] != 'old-task' for v in store.job_summaries())
    rows = client.get('/api/cloud/jobs', params={'query': 'old-task'}).json()['items']
    assert [v['id'] for v in rows] == ['old-task']
    assert client.get('/api/cloud/jobs', params={'query': '%'}).json()['items'] == []


def test_notification_details_are_escaped_redacted_and_bounded():
    result = {'skipped': [{'file_id': str(i), 'name': '<script>'*100, 'reason': 'Cookie=never-output '+('😀<&>'*300)} for i in range(15)]}
    text, _ = notifications.batch_result(result, 'needs_review', '部分项目需要处理', job_id='a'*32, kind='workflow')
    assert '<script>' not in text and 'never-output' not in text
    assert len(text.encode('utf-16-le'))//2 < 4096
    assert '任务：' in text and '处理清单' in text
