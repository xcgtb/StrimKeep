"""Actual durable channel capture with bounded I/O substitutes; never touch live media."""
import json
from unittest.mock import Mock, MagicMock
import pytest
from app import config
from app.cloud import channel_network as net, channel_recovery as recovery, public_channel_reader as web
from app.cloud import receiving as intake, store, directories, jobs, credentials
from app.cloud.errors import CloudError
from dev.tests.test_cloud_receiving import receiving, channel, msg, run
from dev.tests.test_cloud_workbench import cloud
from dev.tests.test_my_drive import client
from dev.tests.test_public_channel_reader import pages, rule, SHARE


def test_explicit_proxy_includes_auth_but_status_does_not(monkeypatch):
    monkeypatch.setattr(config, 'load_config', lambda: {'http_proxy_enabled':'1', 'http_proxy_url':'http://127.0.0.1:7890', 'http_proxy_username':'a@b', 'http_proxy_password':'secret:pass'})
    assert net.http_options()['proxy'] == 'http://a%40b:secret%3Apass@127.0.0.1:7890'
    assert net.telegram_proxy() == {'proxy_type':'http','addr':'127.0.0.1','port':7890,'username':'a@b','password':'secret:pass','rdns':True}
    assert 'secret' not in json.dumps(net.describe()) and net.describe()['remote_dns']


def test_environment_and_no_proxy_route_are_consistent(monkeypatch):
    monkeypatch.setattr(config, 'load_config', lambda: {})
    monkeypatch.setattr(net, 'getproxies', lambda: {'https':'http://proxy.local:8888'})
    monkeypatch.setattr(net, 'proxy_bypass', lambda host: False)
    assert net.describe()['mode'] == 'environment'
    assert net.telegram_proxy()['addr'] == 'proxy.local'
    monkeypatch.setattr(net, 'proxy_bypass', lambda host: True)
    assert net.http_options()['proxy'] is None and net.telegram_proxy() is None


def test_http_proxy_does_not_require_local_telegram_dns(monkeypatch):
    monkeypatch.setattr(net, 'http_options', lambda: {'proxy':'http://proxy.local:7890','trust_env':False})
    monkeypatch.setattr(web, '_public_dns', Mock(side_effect=AssertionError('proxy resolves the fixed host')))
    client = MagicMock()
    response = Mock(status_code=200, headers={'Content-Type':'text/html'})
    response.iter_bytes.return_value = [b'<html></html>']
    client.return_value.__enter__.return_value.stream.return_value.__enter__.return_value = response
    monkeypatch.setattr(web.httpx, 'Client', client)
    assert web._http_page('channelname') == b'<html></html>'
    assert client.call_args.kwargs['proxy'] == 'http://proxy.local:7890'


def test_inbox_capture_rolls_back_with_outer_cursor_transaction(receiving):
    c = channel(receiving, mode='public_web')
    with pytest.raises(RuntimeError):
        with store.db() as con:
            con.execute("INSERT INTO cloud_channel_cursors VALUES(?,0,1,0,'')", (c['id'],))
            intake.record_channel(con, c['id'], receiving.id, 1, c['revision'], c, msg(SHARE, channel=True), 'test')
            import logging
            from app import runtime_logs
            runtime_logs.RuntimeHandler().handle(logging.LogRecord('strimkeep', logging.INFO, __file__, 0, 'channel transaction diagnostic', (), None))
            raise RuntimeError('failure before cursor commit')
    with store.db() as con:
        assert con.execute('SELECT count(*) FROM cloud_inbox').fetchone()[0] == 0
        assert con.execute('SELECT count(*) FROM cloud_channel_cursors').fetchone()[0] == 0


def test_large_interval_resumes_without_skipping_or_duplicate_inbox(receiving, monkeypatch):
    c = rule(receiving, history_limit=0)
    pages(monkeypatch, 5)
    web.poll_one()
    with store.db() as con:
        con.execute('UPDATE cloud_channel_cursors SET next_poll=0')
    pages(monkeypatch, 260)
    web.poll_one()
    with store.db() as con:
        assert con.execute('SELECT cursor FROM cloud_channel_cursors').fetchone()[0] == 5
        assert con.execute('SELECT count FROM cloud_channel_backfill').fetchone()[0] == 100
        assert con.execute('SELECT count(*) FROM cloud_inbox').fetchone()[0] == 0
        con.execute('UPDATE cloud_channel_cursors SET next_poll=0')
    web.poll_one()
    with store.db() as con:
        assert con.execute('SELECT cursor FROM cloud_channel_cursors').fetchone()[0] == 5
        con.execute('UPDATE cloud_channel_cursors SET next_poll=0')
    web.poll_one()
    with store.db() as con:
        assert con.execute('SELECT cursor FROM cloud_channel_cursors').fetchone()[0] == 260
        assert con.execute('SELECT count(*) FROM cloud_inbox').fetchone()[0] == 255
        assert con.execute('SELECT count(*) FROM cloud_channel_backfill').fetchone()[0] == 0
        con.execute('UPDATE cloud_channel_cursors SET next_poll=0')
    web.poll_one()
    with store.db() as con:
        assert con.execute('SELECT count(*) FROM cloud_inbox').fetchone()[0] == 255


def test_reset_needs_preview_paused_rule_and_matching_revisions(receiving, monkeypatch):
    c = rule(receiving, enabled=False)
    pages(monkeypatch, 30)
    before = recovery.preview(receiving.id, c['id'])
    with pytest.raises(CloudError):
        recovery.apply(receiving.id, c['id'], before['token'], False)
    result = recovery.apply(receiving.id, c['id'], before['token'], True)
    assert '仍暂停' in result['message']
    with pytest.raises(CloudError, match='变化'):
        recovery.apply(receiving.id, c['id'], before['token'], True)
    assert not intake.channels(receiving.id)[0]['enabled']
    with store.db() as con:
        assert con.execute('SELECT cursor FROM cloud_channel_cursors').fetchone()[0] == 30
        assert con.execute('SELECT count(*) FROM cloud_jobs').fetchone()[0] == 0


def test_channel_can_chain_transfer_organization_and_strm(receiving, monkeypatch):
    c = channel(receiving, mode='public_web', organize=True)
    pages(monkeypatch, 1)
    # Match helper pages' channel identity with our saved rule.
    monkeypatch.setattr(web, 'fetch_messages', lambda *a, **k: ([{**msg(SHARE, channel=True), 'entities':[]}], 1))
    web.poll_one(); intake.schedule()
    parent = intake.inbox(receiving.id)[0]['job_id']
    result = run(parent)
    assert result['state'] == 'success'
    child = result['result']['child_jobs'][0]
    with store.db() as con:
        payload = json.loads(con.execute('SELECT payload FROM cloud_jobs WHERE id=?', (child,)).fetchone()[0])
    assert payload['organize_target'] == '2'
    outcome = run(child)
    assert outcome['state'] == 'success', outcome
    assert outcome['result']['organization']['generated']
    assert any(w[0]=='move' for w in receiving.remote.writes)


def test_changed_archive_blocks_channel_before_transfer(receiving, monkeypatch):
    channel(receiving, mode='public_web', organize=True)
    monkeypatch.setattr(web, 'fetch_messages', lambda *a, **k: ([{**msg(SHARE, channel=True), 'entities':[]}], 1))
    web.poll_one(); intake.schedule()
    parent = intake.inbox(receiving.id)[0]['job_id']
    directories.save(receiving.id, {'target':'3'}, 1)
    result = run(parent)
    assert result['state'] == 'failed' and '目录已变化' in result['message']
    assert not receiving.remote.writes


def test_legacy_organize_flag_does_not_activate_new_pipeline(receiving):
    c = channel(receiving, organize=True)
    with store.db() as con:
        payload = json.loads(con.execute('SELECT payload FROM cloud_channels WHERE id=?', (c['id'],)).fetchone()[0])
        payload.pop('pipeline_version')
        con.execute('UPDATE cloud_channels SET payload=? WHERE id=?', (store.encode(payload), c['id']))
    assert not intake.channels(receiving.id)[0]['organize_effective']
    intake.capture(msg(SHARE, channel=True), 'token', channel=True)
    intake.schedule()
    result = run(intake.inbox(receiving.id)[0]['job_id'])
    child = result['result']['child_jobs'][0]
    with store.db() as con:
        child_payload = json.loads(con.execute('SELECT payload FROM cloud_jobs WHERE id=?', (child,)).fetchone()[0])
    assert child_payload['organize_target'] is None
    with store.db() as con:
        payload.update(mode='public_web', enabled=False)
        con.execute('UPDATE cloud_channels SET payload=? WHERE id=?', (store.encode(payload), c['id']))
    intake.set_channel_enabled(receiving.id, c['id'], True, c['revision'])
    assert not intake.channels(receiving.id)[0]['organize_effective']


def test_channel_network_wait_does_not_hold_database_or_block_web(receiving, monkeypatch, client):
    import threading
    from app import storage
    from dev.tests.test_public_channel_reader import page, CHANNEL
    rule(receiving)
    initial_ready = client.get('/api/ready')
    entered, release = threading.Event(), threading.Event()
    def slow_page(*args, **kwargs):
        entered.set()
        release.wait(3)
        return page([1])
    monkeypatch.setattr(web, '_http_page', slow_page)
    t = threading.Thread(target=web.poll_one, daemon=True)
    t.start()
    try:
        assert entered.wait(1)
        acquired = storage._LOCK.acquire(timeout=.5)
        assert acquired, 'channel HTTP wait holds the database lock'
        storage._LOCK.release()
        assert client.get('/').status_code == 200
        assert client.get('/api/ready').status_code == initial_ready.status_code
        assert client.get('/api/health').status_code == 200
        assert client.post('/api/login', json={'username':'admin','password':'test-pass-123'}).status_code == 200
        assert client.get('/api/runtime/status').status_code == 200
        assert client.get('/api/logs').status_code == 200
    finally:
        release.set()
        t.join(3)
    assert not t.is_alive()


def test_task_stage_counts_require_actual_receipts(receiving):
    from app.cloud import task_chain
    root = jobs.enqueue('transfer', {})
    with store.db() as con:
        con.executemany('INSERT INTO cloud_steps VALUES(?,?,?,?,?,?)', [
            (root,'receive','verified','{}','{}',1),
            (root,'rename:4','dispatched','{}','{}',1),
            (root,'publication:4','failed','{}','{}',1),
        ])
    stages = {s['key']:s for s in task_chain.read(root)['stages']}
    assert stages['transfer']['verified'] == 1
    assert stages['naming']['verified'] == 0 and stages['naming']['outstanding'] == 1
    assert stages['strm']['verified'] == 0 and stages['strm']['outstanding'] == 1
    assert stages['emby']['verified'] == 0 and '暂无入库回执' in stages['emby']['detail']
