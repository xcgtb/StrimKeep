"""Real SQLite workflow receipts, stream hints and read-only playback diagnostics."""
import asyncio
import json
from pathlib import Path
import pytest
from dev.tests.test_cloud_workbench import cloud  # noqa: F401
from app import activity_stream, task_history, tasks
from app.cloud import settings, strm, recognition, playback, store, events, jobs
from app.cloud.errors import CloudError


def asset_for(cloud):
    row = cloud.remote.info('4')
    return strm.generate(cloud.id, row, recognition.recognize(row['name']), '1')


def test_playback_check_resolves_actual_source_without_returning_provider_secrets(cloud, monkeypatch):
    asset = asset_for(cloud)
    settings.save({'external_url': 'https://play.example.test/movies'})
    calls = []
    def download(*args):
        calls.append(args)
        return {'url':'https://download.115.com/a?token=secret', 'headers':{'Cookie':'secret'}}
    cloud.remote.download = download
    result = playback.check(asset['id'])
    assert result['checked'] and result['mode'] == 'proxy'
    assert result['external_link'].startswith('https://play.example.test/movies/api/cloud/play/')
    assert calls
    assert 'Cookie' not in json.dumps(result) and 'token=secret' not in json.dumps(result)
    assert Path(asset['local_path']).read_text().startswith('http://nas.example:8321/')
    playback.check(asset['id'])
    assert len(calls) == 2  # Diagnostic checks revalidate source, bypassing old cache.


def test_playback_check_rejects_modified_strm_before_network(cloud):
    asset = asset_for(cloud)
    Path(asset['local_path']).write_text('foreign content')
    cloud.remote.download = lambda *a: pytest.fail('must not request a changed file')
    with pytest.raises(CloudError, match='文件已变化'):
        playback.check(asset['id'])


@pytest.mark.parametrize('value', ['https://u:p@example.test', 'file:///tmp', 'https://example.test/?secret=x', 'https://example.test:bad'])
def test_external_playback_address_validation(cloud, value):
    with pytest.raises(CloudError):
        settings.save({'external_url':value})


@pytest.mark.parametrize('patch', [{'ingest_retry_seconds':9}, {'ingest_max_attempts':0}, {'ingest_max_attempts':True}])
def test_ingestion_retry_limits(cloud, patch):
    with pytest.raises(CloudError):
        settings.save(patch)


def test_job_timeline_is_scoped_and_contains_delayed_child(cloud):
    parent = jobs.enqueue('sync', {'account_id': cloud.id})
    child = jobs.enqueue('publish', {'account_id': cloud.id})
    other = jobs.enqueue('sync', {'account_id': cloud.id})
    with store.db() as con:
        con.execute('INSERT INTO cloud_job_due VALUES(?,?,?,?,?)',(child, 100, parent, '4', 1))
    events.emit('naming','文件命名完成',parent,details={'elapsed_ms':12})
    events.emit('strm','延时生成完成',child)
    events.emit('strm','其他任务不能混入',other)
    result = jobs.timeline(parent)
    assert [r['message'].split('] ')[-1] for r in result['items']][-2:] == ['文件命名完成','延时生成完成']
    assert not any('其他任务不能混入' in r['message'] for r in result['items'])


def test_activity_stream_emits_revision_and_closes_on_disconnect(cloud):
    class Request:
        calls = 0
        async def is_disconnected(self):
            self.calls += 1
            return self.calls > 2
    async def run():
        stream = activity_stream.updates(Request(), interval=0)
        assert 'retry:' in await anext(stream)
        assert 'event: activity' in await anext(stream)
        events.emit('strm','新文件已生成')
        assert 'event: activity' in await anext(stream)
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
    asyncio.run(run())


def test_task_metadata_survives_manager_restart_without_replaying(cloud):
    manager = tasks.TaskManager()
    task = manager.spawn('stats', lambda: {'status':'success'})
    assert task.done.wait(3)
    archived = task_history.archived(task.id)
    assert archived['status'] == 'success' and archived['archived']
    assert 'error' not in archived and archived['archive_note']
    assert any(r['id'] == task.id for r in tasks.TaskManager().recent())
    running = tasks.Task('inter_check')
    task_history.record(running)
    assert task_history.archived(running.id)['status'] == 'needs_review'
    assert task_history.archived(running.id)['cancellable'] is False


def test_new_management_endpoints_require_session():
    from fastapi.testclient import TestClient
    from app import main
    client = TestClient(main.app)
    for path in ('/api/activity/events','/api/tasks/recent','/api/overview/playback','/api/cloud/jobs/private/timeline'):
        assert client.get(path).status_code == 401
    assert client.post('/api/cloud/assets/private/playback/check').status_code == 401


def test_playback_overview_caches_actual_records_and_hides_previous_connection(cloud, monkeypatch):
    from app import playback_snapshot, engine, state_store, morning
    monkeypatch.setattr(engine, 'EMBY_HOST', 'http://emby.local')
    monkeypatch.setattr(engine, 'EMBY_KEY', 'private-key')
    monkeypatch.setattr(morning, 'read_emby_lib_cache', lambda **kwargs: {})
    monkeypatch.setitem(engine.ACTIONS, 'played', lambda args: {'status':'success','records':[{'type':'movie','name':'Movie','user':'viewer','pct':42}],'alerts':[]})
    assert playback_snapshot._fetch(playback_snapshot._identity()) == ''
    monkeypatch.setattr(playback_snapshot, '_FUTURE', None)
    result = playback_snapshot.snapshot()
    assert result['records'][0]['pct'] == 42
    assert result['refreshing'] is False
    monkeypatch.setattr(engine, 'EMBY_KEY', '')
    result = playback_snapshot.snapshot()
    assert result['records'] == [] and result['configured'] is False
    assert 'private-key' not in json.dumps(result)
