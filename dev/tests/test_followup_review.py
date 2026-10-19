"""Follow-up review: terminal truth, explicit schedules and share-only handovers."""
import copy
import json
from pathlib import Path
from unittest.mock import Mock
import pytest
from dev.tests.test_cloud_workbench import cloud
from dev.tests.test_cloud_receiving import receiving
from dev.tests.test_share_library import shared, preview, execute, generated
from dev.tests.test_my_drive import client
from app.cloud import (directories, settings, store, jobs, strm, strm_scan, task_chain,
                       workflow, share_library as shares, emby_library, pipeline, playback)
from app.cloud.errors import CloudError


@pytest.mark.parametrize('state', ['cancelled', 'stopped'])
def test_cancelled_only_and_mixed_success_chain_never_report_success(cloud, state):
    root = jobs.enqueue('sync', {})
    child = jobs.enqueue('sync', {}, parent_id=root)
    with store.db() as con:
        con.execute("UPDATE cloud_jobs SET state='success' WHERE id=?", (root,))
        con.execute('UPDATE cloud_jobs SET state=? WHERE id=?', (state, child))
    assert task_chain.read(child)['state'] == 'stopped'
    with store.db() as con:
        con.execute('UPDATE cloud_jobs SET state=? WHERE id=?', (state, root))
    assert task_chain.read(root)['state'] == 'stopped'


def test_failed_or_stopped_recovery_does_not_resolve_original(cloud):
    root = jobs.enqueue('organize', {})
    child = jobs.enqueue('organize', {}, parent_id=root)
    with store.db() as con:
        con.execute("UPDATE cloud_jobs SET state='needs_review' WHERE id=?", (root,))
        con.execute("UPDATE cloud_jobs SET state='stopped' WHERE id=?", (child,))
        con.execute('INSERT INTO cloud_recovery_requests VALUES(?,?,?)', (root, child, 1))
    result = task_chain.read(root)
    assert result['state'] == 'needs_review'
    assert not result['jobs'][0]['resolved_by']


def test_explicit_schedule_modes_reenable_edit_and_tick_are_single_shot(receiving, monkeypatch):
    clock = [1_700_000_000.0]
    monkeypatch.setattr(settings.time, 'time', lambda: clock[0])
    directories.save(receiving.id, {'strm': '2'}, 1)
    settings.save({'strm_scan_cron': '0 0 * * *', 'strm_scan_start_mode': 'scheduled'})
    cfg = settings.get()
    scheduled = strm_scan.status(cfg, clock[0])['next_run']
    assert scheduled > clock[0] and not strm_scan.tick(cfg, clock[0])
    assert len(strm_scan.tick(cfg, scheduled)) == 1
    with store.db() as con:
        con.execute("UPDATE cloud_jobs SET state='success'")
    clock[0] = scheduled + 10
    settings.save({'strm_scan_cron': ''})
    assert not strm_scan.status(settings.get())['enabled']
    assert settings.get()['strm_scan_last_run'] == scheduled
    settings.save({'strm_scan_cron': '0 0 * * *'})
    assert not strm_scan.tick(settings.get(), clock[0])
    settings.save({'strm_scan_start_mode': 'immediate'})
    assert len(strm_scan.tick(settings.get(), clock[0])) == 1
    assert not strm_scan.tick(settings.get(), clock[0] + 1)
    stale = settings.get()
    clock[0] += 20
    settings.save({'strm_scan_timezone': 'UTC'})
    assert not settings.record_scan(stale, clock[0])
    assert settings.get()['strm_scan_pending']


def test_cleanup_can_be_disabled_and_is_frozen_at_enqueue(receiving):
    directories.save(receiving.id, {'strm': '1'}, 1)
    settings.save({'strm_reconcile_enabled': False, 'strm_scan_cron': '* * * * *'})
    queued = strm_scan.tick(settings.get())
    settings.save({'strm_reconcile_enabled': True})
    jobs.execute(queued[0]['job_id'])
    result = store.job(queued[0]['job_id'])['result']
    assert result['generated'] and result['cleanup_enabled'] is False
    assert 'reconciliation' not in result


def test_settings_records_are_server_owned(receiving, client):
    for key in ('strm_scan_last_run', 'strm_scan_last_enqueued', 'strm_scan_anchor', 'strm_scan_pending'):
        assert client.post('/api/cloud/settings', json={key: 0}).status_code == 400
    value = client.get('/api/cloud/settings').json()
    assert 'next_run' in value['schedule']


def test_receive_uses_saved_archive_and_rechecks_before_remote_write(receiving):
    p = workflow.preview_share(receiving.id, 'secretshare', '')
    with pytest.raises(CloudError, match='归档目录'):
        workflow.transfer_from_saved(p['preview_id'], ['999'], '3', True, '1')
    jid = workflow.transfer_from_saved(p['preview_id'], ['999'], '3', True, '2', directory_revision=1)
    directories.save(receiving.id, {'target': '3'}, 1)
    jobs.execute(jid)
    assert store.job(jid)['state'] == 'failed'
    assert not receiving.remote.writes


def enable_share(**patch):
    return settings.save({'share_quality_enabled': True, 'share_quality_mode': 'upgrade', **patch})


def test_archive_edit_after_verified_transfer_keeps_transfer_receipt(receiving, monkeypatch):
    p = workflow.preview_share(receiving.id, 'secretshare', '')
    jid = workflow.transfer_from_saved(p['preview_id'], ['999'], '3', True, '2', directory_revision=1)
    receive = receiving.remote.receive
    def changed(*args, **kwargs):
        receive(*args, **kwargs)
        directories.save(receiving.id, {'target': '3'}, 1)
    monkeypatch.setattr(receiving.remote, 'receive', changed)
    jobs.execute(jid)
    result = store.job(jid)
    assert result['state'] == 'failed' and result['result']['mapping']
    assert len([v for v in receiving.remote.writes if v[0] == 'receive']) == 1
    jobs.execute(jid)
    assert len([v for v in receiving.remote.writes if v[0] == 'receive']) == 1


def new_version(shared, name='Movie.2024.2160p.mkv', size=200):
    shared.shared['8'] = {**shared.shared['4'], 'id': '8', 'name': name, 'size': size, 'sha1': 'B'*40}
    return preview(shared, code='secondshare', ids=['8'])


def test_share_better_publishes_then_retires_owned_old_without_personal_mutation(shared):
    old = generated(shared)
    personal = strm.generate(shared.id, shared.remote.info('4'), old['payload']['media'])
    enable_share()
    plan = new_version(shared)
    assert plan['replacement_count'] == 1 and plan['comparisons'][0]['result'] == 1
    note = Path(old['local_path']).with_name('user-note.txt'); note.write_text('keep')
    result = execute(plan)
    assert result['state'] == 'success', result
    fresh = store.asset(result['result']['generated'][0]['asset_id'])
    assert fresh['state'] == 'active' and Path(fresh['local_path']).is_file()
    assert not Path(old['local_path']).exists() and store.asset(old['id'])['state'] == 'revoked'
    assert note.read_text() == 'keep'
    assert store.asset(personal['id'])['state'] == 'active' and Path(personal['local_path']).exists()
    assert shared.remote.writes == []
    jid = result['id']; jobs.execute(jid)
    assert store.job(jid)['result'] == result['result']


def test_share_same_path_rebind_keeps_signed_bytes_and_plays_new_source(shared):
    settings.save({'naming_style': 'template', 'movie_file_template': '{title} ({year}).{ext}'})
    old = generated(shared); content = Path(old['local_path']).read_bytes()
    enable_share()
    result = execute(new_version(shared))
    assert result['state'] == 'success', result
    fresh = store.asset(old['id'])
    assert fresh['file_id'] != old['file_id'] and fresh['nonce'] == old['nonce']
    assert fresh['payload']['source']['sha1'] == 'B'*40
    assert Path(old['local_path']).read_bytes() == content
    playback.resolve(fresh['id'], strm.signature(fresh['id'], fresh['nonce']), 'Player')
    assert shared.share_downloads[-1][2] == '8'
    assert result['result']['replaced'][0]['rebound'] and not shared.remote.writes


@pytest.mark.parametrize('foreign_subtitle', [False, True])
def test_same_path_share_replacement_refreshes_source_bound_subtitles(shared, monkeypatch, foreign_subtitle):
    settings.save({'naming_style':'template', 'movie_file_template':'{title} ({year}).{ext}'})
    shared.shared['9'] = {**shared.remote.info('1'), 'id':'9', 'parent_id':'0'}
    shared.shared['4']['parent_id'] = '9'
    shared.shared['5'] = {**shared.remote.info('5'), 'parent_id':'9'}
    monkeypatch.setattr(strm, 'fetch_sidecar', lambda view, row: ('subtitle-'+view.info(row['id'])['id']).encode())
    first = execute(preview(shared, ids=['9']))
    old = store.asset(first['result']['generated'][0]['asset_id'])
    subtitle = next(Path(v['path']) for v in old['payload']['owned'] if v.get('file_id'))
    assert subtitle.read_bytes() == b'subtitle-5'
    if foreign_subtitle:
        subtitle.write_bytes(b'user edited subtitle')
    shared.shared['11'] = {**shared.shared['9'], 'id':'11'}
    shared.shared['8'] = {**shared.shared['4'], 'id':'8', 'parent_id':'11', 'name':'Movie.2024.2160p.mkv', 'sha1':'B'*40}
    shared.shared['12'] = {**shared.shared['5'], 'id':'12', 'parent_id':'11', 'name':'Movie.2024.2160p.zh.srt'}
    enable_share()
    result = execute(preview(shared, code='secondshare', ids=['11']))
    assert result['state'] == 'success', result
    fresh = store.asset(old['id'])
    updated = next(Path(v['path']) for v in fresh['payload']['owned'] if v.get('file_id') == '12')
    assert updated.read_bytes() == b'subtitle-12'
    assert fresh['payload']['source']['id'] == '8'
    assert subtitle.exists() == foreign_subtitle
    if foreign_subtitle:
        assert subtitle.read_bytes() == b'user edited subtitle'
        assert str(subtitle) in result['result']['replaced'][0]['retained']
    assert not shared.remote.writes


@pytest.mark.parametrize('mode', ['worse', 'tie', 'preview'])
def test_share_holds_worse_tie_and_preview_versions_without_local_changes(shared, mode):
    old = generated(shared); before = Path(old['local_path']).read_bytes()
    enable_share(**({'share_quality_mode': 'preview'} if mode == 'preview' else {}))
    plan = new_version(shared, 'Movie.2024.720p.mkv' if mode == 'worse' else 'Movie.2024.1080p.mkv' if mode == 'tie' else 'Movie.2024.2160p.mkv', 100 if mode == 'tie' else 200)
    assert not plan['items'] and plan['skipped'] and plan['comparisons']
    assert Path(old['local_path']).read_bytes() == before
    assert store.asset(old['id'])['state'] == 'active' and not shared.remote.writes


@pytest.mark.parametrize('change', ['file', 'mount', 'config', 'source', 'stop'])
def test_share_rechecks_confirmation_before_changing_old_references(shared, change):
    old = generated(shared); enable_share(); plan = new_version(shared)
    if change == 'file': Path(old['local_path']).write_text('user edited')
    if change == 'mount':
        shared.share_root.rename(shared.share_root.with_name('previous')); shared.share_root.mkdir()
    if change == 'config': settings.save({'share_quality_coexist_dv': False})
    if change == 'source': shared.shared['8']['sha1'] = 'C'*40
    jid = shares.apply(plan['id'], True)
    if change == 'stop': jobs.request_stop(jid)
    jobs.execute(jid)
    assert store.job(jid)['state'] != 'success'
    assert store.asset(old['id'])['state'] == 'active'
    assert not list(shared.share_root.rglob('*2160p*.strm')) and not shared.remote.writes


def test_share_failed_new_publish_preserves_old_and_blocks_step_replay(shared, monkeypatch):
    old = generated(shared); enable_share(); plan = new_version(shared)
    monkeypatch.setattr(strm, 'publish', Mock(side_effect=CloudError('disk full', 409)))
    result = execute(plan)
    assert result['state'] == 'needs_review' and Path(old['local_path']).exists()
    assert store.asset(old['id'])['state'] == 'active'
    with store.db() as con:
        key = con.execute("SELECT step_key FROM cloud_steps WHERE job_id=? AND step_key LIKE 'share_replace:%'", (result['id'],)).fetchone()[0]
    with pytest.raises(CloudError, match='禁止重复执行'):
        jobs.step(result['id'], key, {}, lambda: pytest.fail('replayed'))


def test_share_batch_selects_best_before_generation_and_own_rules_are_independent(shared):
    shared.shared['8'] = {**shared.shared['4'], 'id':'8', 'name':'Movie.2024.2160p.mkv', 'sha1':'B'*40}
    enable_share()
    plan = preview(shared, ids=['4','8'])
    assert [i['source']['id'] for i in plan['items']] == ['8'] and plan['skipped']
    settings.save({'quality8_enabled': True})
    assert execute(plan)['state'] == 'success'  # Personal organizer rules are not share settings.


def test_share_coexistence_and_foreign_output_are_preserved(shared):
    enable_share()
    shared.shared['4']['name'] = 'Movie.2024.1080p.SDR.mkv'
    old = generated(shared)
    plan = new_version(shared, 'Movie.2024.2160p.Dolby.Vision.mkv')
    assert plan['items'] and not plan['replacement_count']
    assert execute(plan)['state'] == 'success' and Path(old['local_path']).exists()
    plan = new_version(shared, 'Movie.2024.4320p.mkv')
    if plan['items']:
        item = plan['items'][0]
        dest = shared.share_root/item['media']['directory']/Path(item['media']['name']).with_suffix('.strm')
        dest.parent.mkdir(parents=True, exist_ok=True); dest.write_text('foreign')
        assert execute(plan)['state'] != 'success' and dest.read_text() == 'foreign'
    assert not shared.remote.writes


def test_emby_overview_uses_connection_roots_and_warns_about_proxy_difference(shared):
    asset = generated(shared)
    emby_library.save(shared.id, {'expected_revision':0, 'connection': {'name':'Media', 'url':'http://emby.example', 'key':'private-key', 'enabled':True, 'personal_root':'/actual/local', 'share_root':'/actual/share'}})
    result = pipeline.status(shared.id)['emby_mappings']
    row = next(v for v in result['items'] if v['kind'] == 'share')
    assert row['sample_emby_path'] == '/actual/share/'+str(Path(asset['local_path']).relative_to(shared.share_root))
    assert row['emby_root'] == '/actual/share' and row['proxy_root'] == '/share' and not row['consistent']
    assert 'private-key' not in json.dumps(result)


@pytest.mark.parametrize('enabled', [False, True])
def test_unrelated_large_share_library_does_not_block_preview(shared, enabled):
    first = preview(shared)
    media = first['items'][0]['media']
    payload = {'source_kind':'share', 'output_root':str(shared.share_root), 'source':shared.shared['4'],
               'media':{**media, 'kind':'tv', 'season':2, 'episode':99}}
    with store.db() as con:
        con.executemany('INSERT INTO cloud_assets VALUES(?,?,?,?,?,?,?,?)', [
            (store.ident(), shared.id, 'share:unrelated:'+str(i), str(shared.share_root/('unrelated-'+str(i)+'.strm')), store.encode(payload), 'nonce', 'active', 1)
            for i in range(2001)])
    settings.save({'share_quality_enabled':enabled})
    assert preview(shared)['items']
