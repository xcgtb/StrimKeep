import json

from test_sync_incremental import _client


def profile(name, mode, cid='100', frequency='daily'):
    return {'name':name, 'source_cid':cid, 'source_label':'目录'+cid, 'mode':mode,
            'enabled':True, 'frequency':frequency, 'weekday':1, 'time':'03:00'}


def test_profiles_run_and_schedule_independently(tmp_path, monkeypatch):
    app, client = _client(tmp_path, monkeypatch)
    store = app.state.store
    store.set_many({'schedule_config': {'sync_enabled': False, 'sync_source_cids':['999']}})
    a = client.post('/api/v1/sync/profiles', json=profile('AV 每天增量', 'incremental')).json()
    b = client.post('/api/v1/sync/profiles', json=profile('AV 每周全量', 'full', frequency='weekly')).json()
    c = client.post('/api/v1/sync/profiles', json=profile('影视增量', 'incremental', '200')).json()
    rows = client.get('/api/v1/sync/profiles').json()['items']
    assert len(rows) == 3 and all(p['next_due'] for p in rows)
    assert store.get('schedule_config')['sync_source_cids'] == ['999']
    for p in (a,b,c):
        result = client.post('/api/v1/sync/profiles/'+p['id']+'/run')
        assert result.status_code == 200
        config = json.loads(store.db.execute('SELECT config FROM organize_jobs WHERE id=?',
                                             (result.json()['id'],)).fetchone()[0])
        assert config['source_cids'] == [p['source_cid']]
        assert config['mode'] == p['mode'] and config['sync_profile_id'] == p['id']
        assert bool(config.get('_force_full')) == (p['mode'] == 'full')
    with store.db:
        store.db.execute('DELETE FROM organize_jobs')
        store.db.execute("DELETE FROM task_schedules WHERE kind LIKE 'sync_profile:%'")
    now = 1700000000
    app.state.organizer.tasks.tick_sync_profiles(now)
    next_times = {r['kind']:r['next_due'] for r in store.db.execute("SELECT * FROM task_schedules WHERE kind LIKE 'sync_profile:%'")}
    assert next_times['sync_profile:'+a['id']] == next_times['sync_profile:'+c['id']]
    due = next_times['sync_profile:'+a['id']]
    app.state.organizer.tasks.tick_sync_profiles(due)
    jobs = [json.loads(r['config']) for r in store.db.execute('SELECT config FROM organize_jobs')]
    assert {j['sync_profile_id'] for j in jobs} >= {a['id'],c['id']}
    app.state.organizer.tasks.tick_sync_profiles(next_times['sync_profile:'+b['id']])
    jobs = [json.loads(r['config']) for r in store.db.execute('SELECT config FROM organize_jobs')]
    assert {j['sync_profile_id'] for j in jobs} == {a['id'],b['id'],c['id']}
    assert any(j['sync_profile_id']==b['id'] and j['_force_full'] for j in jobs)
    assert client.delete('/api/v1/sync/profiles/'+b['id']).status_code == 200
    assert store.db.execute('SELECT 1 FROM task_schedules WHERE kind=?', ('sync_profile:'+b['id'],)).fetchone() is None
    assert len(client.get('/api/v1/sync/profiles').json()['items']) == 2
    client.__exit__(None, None, None)


def test_profile_validation_and_edit(tmp_path, monkeypatch):
    app, client = _client(tmp_path, monkeypatch)
    assert client.post('/api/v1/sync/profiles', json={**profile('x','full'), 'time':'25:00'}).status_code == 422
    assert client.post('/api/v1/sync/profiles', json={**profile('x','full'), 'source_cid':'0'}).status_code == 422
    created = client.post('/api/v1/sync/profiles', json=profile('AV','full')).json()
    changed = {**profile('AV 手动','incremental','200'), 'enabled':False}
    assert client.put('/api/v1/sync/profiles/'+created['id'], json=changed).status_code == 200
    result = client.post('/api/v1/sync/profiles/'+created['id']+'/run')
    config = json.loads(app.state.store.db.execute('SELECT config FROM organize_jobs WHERE id=?', (result.json()['id'],)).fetchone()[0])
    assert config['source_cids'] == ['200'] and config['mode'] == 'incremental'
    assert app.state.store.db.execute('SELECT 1 FROM task_schedules WHERE kind=?', ('sync_profile:'+created['id'],)).fetchone() is None
    client.__exit__(None, None, None)


def test_scheduled_jobs_do_not_merge_when_mode_changes(tmp_path, monkeypatch):
    app, client = _client(tmp_path, monkeypatch)
    created = client.post('/api/v1/sync/profiles', json=profile('AV','incremental')).json()
    tasks = app.state.organizer.tasks
    incremental = tasks.sync_profile_config(created)
    first, _ = tasks.enqueue(incremental, scheduled=True)
    full = tasks.sync_profile_config({**created, 'mode':'full'})
    second, merged = tasks.enqueue(full, scheduled=True)
    assert not merged and first != second
    client.__exit__(None, None, None)
