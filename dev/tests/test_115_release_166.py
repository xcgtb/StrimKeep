"""Reported 115 failures: real queues/SQLite, isolated providers, no external writes."""
import copy
import socket
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from dev.tests.test_cloud_workbench import cloud
from dev.tests.test_cloud_receiving import receiving, MAGNET
from dev.tests.test_share_library import shared
from dev.tests.test_my_drive_organize import Catalog
from app.cloud import automatic_runs, events, jobs, store, settings, workflow, receiving as intake
from app.cloud import media_probe, recognition, strm, share_library
from app.cloud.errors import CloudError


def test_each_share_uses_its_own_adjacent_password():
    rows = intake.extract('https://115.com/s/abcdef1 提取码：aaaa\nhttps://115.com/s/abcdef2 提取码：bbbb')
    assert [r['password'] for r in rows] == ['aaaa', 'bbbb']
    rows = intake.extract('https://115.com/s/abcdef1?password=zzzz 提取码：aaaa\nhttps://anxia.com/s/abcdef2 提取码：bbbb')
    assert [r['password'] for r in rows] == ['zzzz', 'bbbb']


def test_malformed_link_does_not_hide_valid_following_resource():
    errors = []
    rows = intake.extract('magnet:?xt=invalid\n'+MAGNET, errors=errors)
    assert len(rows) == 1 and rows[0]['kind'] == 'magnet'
    assert len(errors) == 1


@pytest.mark.parametrize('direct', [False, True])
def test_partial_parse_errors_survive_web_submission(receiving, direct):
    text = 'magnet:?xt=invalid\n'+MAGNET
    if direct:
        jid = intake.receive(receiving.id, text, 1, store.ident(), confirmed=True)
    else:
        plan = intake.preview(receiving.id, text)
        assert len(plan['errors']) == 1
        jid = intake.apply(plan['id'], True)
    jobs.execute(jid)
    job = store.job(jid)
    assert job['state'] == 'needs_review'
    assert len(job['result']['accepted']) == len(job['result']['errors']) == 1
    assert receiving.remote.offline_add.call_count == 1


def test_one_known_share_failure_does_not_block_later_resource(receiving, monkeypatch):
    monkeypatch.setattr(receiving.remote, 'share_page', Mock(side_effect=CloudError('分享失效', 404)))
    plan = intake.preview(receiving.id, 'https://115.com/s/abcdef1\n'+MAGNET)
    jid = intake.apply(plan['id'], True)
    jobs.execute(jid)
    job = store.job(jid)
    assert job['state'] == 'needs_review'
    assert len(job['result']['accepted']) == len(job['result']['errors']) == 1
    assert receiving.remote.offline_add.call_count == 1


def test_uncertain_write_stops_batch_and_is_never_replayed(receiving, monkeypatch):
    receiving.remote.offline_add.side_effect = CloudError('结果未知', 502, uncertain=True)
    plan = intake.preview(receiving.id, MAGNET+'\n'+MAGNET.replace('a'*40, 'b'*40))
    jid = intake.apply(plan['id'], True)
    jobs.execute(jid)
    jobs.execute(jid)
    assert store.job(jid)['state'] == 'uncertain'
    assert receiving.remote.offline_add.call_count == 1


def test_stop_is_not_swallowed_by_per_resource_error_handler(receiving):
    plan = intake.preview(receiving.id, MAGNET+'\n'+MAGNET.replace('a'*40, 'b'*40))
    jid = intake.apply(plan['id'], True)
    def submit(*args):
        jobs.request_stop(jid)
        return {'accepted': True}
    receiving.remote.offline_add.side_effect = submit
    jobs.execute(jid)
    assert store.job(jid)['state'] == 'stopped'
    assert receiving.remote.offline_add.call_count == 1


def test_equivalent_rules_share_one_active_execution(cloud):
    payload = {'account_id': cloud.id, 'account_revision': 1, 'source_cid': '1', 'target_cid': '2', 'interval_seconds': 300}
    jobs.enqueue('workflow', {**payload, 'rule_id': 'a', 'name': 'A'})
    with store.db() as con:
        assert automatic_runs.equivalent_active(con, 'workflow', {**payload, 'rule_id': 'b', 'name': 'B', 'interval_seconds': 600})
        assert not automatic_runs.equivalent_active(con, 'workflow', {**payload, 'target_cid': '3'})


def test_automatic_failure_cools_down_but_manual_preview_can_retry(cloud):
    row = cloud.remote.info('4')
    callback = Mock(side_effect=media_probe.probe_error('probe_http_403', '媒体读取 HTTP 403'))
    jid = jobs.enqueue('workflow', {'account_id': cloud.id, 'rule_id': 'rule'})
    token = events._JOB.set(jid)
    try:
        for _ in range(2):
            with pytest.raises(CloudError, match='403'):
                automatic_runs.recognize(cloud.id, row, {}, {}, callback)
        assert callback.call_count == 1
        with pytest.raises(CloudError):
            automatic_runs.recognize(cloud.id, {**row, 'sha1': 'B'*40}, {}, {}, callback)
        assert callback.call_count == 2
    finally:
        events._JOB.reset(token)
    assert automatic_runs.recognize(cloud.id, row, {}, {}, lambda: {'ok': True}) == {'ok': True}
    with store.db() as con:
        assert con.execute('SELECT count(*) FROM cloud_scan_failures').fetchone()[0] == 0


def test_repeat_notices_are_merged_but_changed_errors_and_recovery_are_not(cloud):
    payload = {'account_id': cloud.id, 'source_cid': '1', 'rule_id': 'a'}
    job = store.job(jobs.enqueue('workflow', payload))
    failed = {'skipped': [{'file_id': '4', 'reason': 'HTTP 403', 'sha1': 'A'*40}]}
    assert automatic_runs.claim_notice(job, 'workflow', 'needs_review', failed)
    assert automatic_runs.claim_notice(job, 'workflow', 'needs_review', failed)  # same-job delivery receipt owns retries
    next_job = store.job(jobs.enqueue('workflow', {**payload, 'rule_id': 'b'}))
    assert not automatic_runs.claim_notice(next_job, 'workflow', 'needs_review', failed)
    changed = {'skipped': failed['skipped']+[{'file_id': '5', 'reason': 'TMDB missing'}]}
    assert automatic_runs.claim_notice(next_job, 'workflow', 'needs_review', changed)
    assert automatic_runs.claim_notice(next_job, 'workflow', 'success', {})
    assert automatic_runs.claim_notice(store.job(jobs.enqueue('workflow', payload)), 'workflow', 'needs_review', changed)
    assert automatic_runs.claim_notice(store.job(jobs.enqueue('workflow', {})), 'workflow', 'needs_review', changed)


def test_changed_settings_cannot_be_overwritten_by_stale_draft(cloud):
    old = settings.get()['movie_folder']
    settings.save({'movie_folder': '影片'})
    with pytest.raises(CloudError, match='其他页面'):
        settings.save({'movie_folder': '电影库'}, {'movie_folder': old})
    assert settings.get()['movie_folder'] == '影片'


def test_old_pending_jobs_are_counted_and_can_be_filtered_and_paged(cloud):
    ids = [jobs.enqueue('sync', {'account_id': cloud.id}) for _ in range(55)]
    for _ in range(55):
        jid = jobs.enqueue('sync', {})
        with store.db() as con:
            con.execute("UPDATE cloud_jobs SET state='success' WHERE id=?", (jid,))
    assert store.pending_count() == 55
    first = store.job_page(group='active')
    second = store.job_page(group='active', offset=50)
    assert first['has_more'] and not second['has_more']
    assert {r['id'] for r in first['items']+second['items']} == set(ids)


def test_failed_post_write_verification_never_registers_active_asset(cloud, monkeypatch):
    settings.save({'nfo_enabled': False, 'artwork_enabled': False})
    original = strm.publish
    def tampered(path, data, *args, **kwargs):
        result = original(path, data, *args, **kwargs)
        path.write_bytes(b'changed externally')
        return result
    monkeypatch.setattr(strm, 'publish', tampered)
    row = cloud.remote.info('4')
    with pytest.raises(CloudError, match='核验失败'):
        strm.generate(cloud.id, row, recognition.recognize(row['name']))
    assert strm.list_assets()[0]['state'] == 'publishing'
    strm.recover()
    assert strm.list_assets()[0]['state'] == 'needs_review'


def test_sync_never_mixes_output_roots_in_one_run(cloud, monkeypatch, tmp_path):
    settings.save({'nfo_enabled': False, 'artwork_enabled': False})
    other = tmp_path/'other'
    other.mkdir()
    cloud.remote.files['6'] = {**cloud.remote.info('4'), 'id': '6', 'name': 'Movie.2024.720p.mkv'}
    original = strm.generate
    def change_after_first(*args, **kwargs):
        result = original(*args, **kwargs)
        settings.save({'output_root': str(other)})
        return result
    monkeypatch.setattr(strm, 'generate', change_after_first)
    jid = jobs.enqueue('sync', {'account_id': cloud.id, 'account_revision': store.account(cloud.id)['revision'], 'source_cid': '1', 'reconcile': False})
    jobs.execute(jid)
    result = store.job(jid)
    assert result['state'] == 'needs_review'
    assert len(result['result']['generated']) == 1
    assert len(result['result']['skipped']) == 1
    assert '变化' in result['result']['skipped'][0]['reason']
    assert not list(other.rglob('*.strm'))


def test_share_episode_inherits_share_directory_identity(shared, monkeypatch):
    monkeypatch.setattr(recognition, 'tmdb', lambda: Catalog())
    folder = {'id': '90', 'parent_id': '0', 'name': 'Series (2024) {tmdb-44}', 'is_dir': True, 'size': 0, 'sha1': '', 'pickcode': ''}
    season = {**folder, 'id': '91', 'parent_id': '90', 'name': 'Season 01'}
    video = {**shared.remote.info('4'), 'parent_id': '91', 'name': 'E01.mkv'}
    shared.shared = {'90': folder, '91': season, '4': video}
    first = workflow.preview_share(shared.id, 'https://115.com/s/abcdef1')
    result = share_library.preview(first['preview_id'], ['90'])
    assert len(result['items']) == 1 and not result['skipped']
    assert result['items'][0]['media']['season'] == 1
    assert not shared.remote.writes


def test_tmdb_missing_episode_has_distinct_identity_and_code(cloud, monkeypatch):
    catalog = Catalog()
    get = catalog.get
    catalog.get = lambda path, **params: None if '/episode/' in path else get(path, **params)
    monkeypatch.setattr(recognition, 'tmdb', lambda: catalog)
    with pytest.raises(CloudError) as caught:
        recognition.recognize('Series.2024.S01E01.mkv')
    assert caught.value.status == 404 and caught.value.code == 'tmdb_episode_missing'
    assert 'S01E01' in str(caught.value)


def test_probe_keeps_sanitized_diagnostics_without_exposing_link_or_cookie(cloud, monkeypatch):
    monkeypatch.setattr(media_probe.shutil, 'which', lambda _: '/fixture/ffprobe')
    monkeypatch.setattr(media_probe.socket, 'getaddrinfo', lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443))])
    def failed(command, stdout, stderr, **kwargs):
        stdout.write(b'{}')
        stderr.write(b'HTTP error https://download.115cdn.com/private-token Cookie=secret-value')
        return SimpleNamespace(returncode=1)
    monkeypatch.setattr(media_probe.subprocess, 'run', failed)
    recorded = []
    monkeypatch.setattr(events, 'emit', lambda *a, **kw: recorded.append((a, kw)))
    with pytest.raises(CloudError) as caught:
        media_probe.probe_url({'url': 'https://download.115cdn.com/private-token'}, 100)
    assert caught.value.code == 'probe_exit'
    assert 'private-token' not in str(recorded) and 'secret-value' not in str(recorded)
    assert recorded
