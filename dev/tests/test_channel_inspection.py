"""Inspection must explain the actual route without queueing or moving cursors."""
from unittest.mock import Mock

import pytest

from dev.tests.test_cloud_workbench import cloud
from dev.tests.test_my_drive import client
from dev.tests.test_cloud_receiving import receiving, channel, msg
from app.cloud import channel_inspection as inspection, receiving as intake, store, public_channel_reader, channel_reader
from app.cloud.errors import CloudError

SHARE = 'https://115.com/s/abcdef123?password=abcd'


def unchanged_tables():
    with store.db() as con:
        return {table: list(con.execute('SELECT * FROM ' + table)) for table in
                ('cloud_inbox', 'cloud_jobs', 'cloud_channel_cursors', 'cloud_public_channel_status')}


def test_public_inspection_includes_hidden_share_and_does_not_consume_it(receiving, monkeypatch):
    rule = channel(receiving, mode='public_web', action='forward', target_cid='')
    source = msg('资源见按钮', channel=True)
    source['entities'] = [{'type': 'text_link', 'url': SHARE}]
    fetch = Mock(return_value=([source], 1))
    monkeypatch.setattr(public_channel_reader, 'fetch_messages', fetch)
    before = unchanged_tables()
    result = inspection.inspect(receiving.id, rule['id'])
    assert result['preview_only'] and not result['read_error']
    sample = result['messages'][0]
    assert sample['matched'] and sample['resource_count'] == 1
    assert sample['preview_text'] == '/strm\n资源见按钮\n' + SHARE
    assert '内置生成' in sample['would_do']
    assert before == unchanged_tables() and not receiving.remote.writes
    assert fetch.call_args.kwargs['history_limit'] == 5


def test_public_network_failure_is_visible_without_advancing_cursor(receiving, monkeypatch):
    rule = channel(receiving, mode='public_web')
    monkeypatch.setattr(public_channel_reader, 'fetch_messages', Mock(side_effect=CloudError('公开预览不可用', 502)))
    before = unchanged_tables()
    result = inspection.inspect(receiving.id, rule['id'])
    assert result['read_error'] == '公开预览不可用'
    assert any(item['ok'] is False and item['label'] == '读取频道' for item in result['checks'])
    assert before == unchanged_tables()


def test_inspection_of_paused_rule_does_not_enable_it(receiving, monkeypatch):
    rule = channel(receiving, mode='public_web', enabled=False)
    monkeypatch.setattr(public_channel_reader, 'fetch_messages', lambda *a, **k: ([msg(SHARE)], 1))
    result = inspection.inspect(receiving.id, rule['id'])
    assert '暂停' in result['messages'][0]['would_do']
    assert not intake.channels(receiving.id)[0]['enabled']


def test_inspection_does_not_suggest_processing_keyword_match_without_resource(receiving):
    rule = channel(receiving, mode='public_web', include=['电影'])
    result = intake.test_channel(receiving.id, rule['id'], '电影上新')
    assert result['filter_matched'] and not result['matched']
    assert '资源链接' in result['reason']


def test_non_share_is_explained_in_public_mode(receiving):
    from dev.tests.test_cloud_receiving import MAGNET
    rule = channel(receiving, mode='public_web')
    result = intake.test_channel(receiving.id, rule['id'], MAGNET)
    assert not result['matched'] and '115 分享' in result['reason']


def test_external_relay_accepts_text_and_explains_no_session(receiving, monkeypatch):
    rule = channel(receiving, mode='public_web', enabled=False, action='forward', target_cid='', forward_target='@targetrobot', command_prefix='/custom')
    monkeypatch.setattr(public_channel_reader, 'fetch_messages', lambda *a, **k: ([msg('普通公告')], 1))
    result = inspection.inspect(receiving.id, rule['id'])
    assert result['messages'][0]['matched']
    assert result['messages'][0]['preview_text'] == '/custom\n普通公告'
    assert any(c['label'] == 'Telegram 用户会话' and c['ok'] is False for c in result['checks'])
    assert not receiving.remote.writes


def test_bot_inspection_explains_realtime_only(receiving, monkeypatch):
    rule = channel(receiving)
    monkeypatch.setattr(public_channel_reader, 'fetch_messages', Mock(side_effect=AssertionError('must not fetch web')))
    monkeypatch.setattr(channel_reader, '_run', Mock(side_effect=AssertionError('must not read history')))
    before = unchanged_tables()
    result = inspection.inspect(receiving.id, rule['id'])
    assert not result['messages'] and any('只能接收后续' in c['detail'] for c in result['checks'])
    assert before == unchanged_tables()


def test_changed_rule_during_inspection_is_not_presented_as_current(receiving, monkeypatch):
    rule = channel(receiving, mode='public_web')
    def fetch(*args, **kwargs):
        with store.db() as con:
            con.execute('UPDATE cloud_channels SET revision=revision+1 WHERE id=?', (rule['id'],))
        return [msg(SHARE)], 1
    monkeypatch.setattr(public_channel_reader, 'fetch_messages', fetch)
    with pytest.raises(CloudError, match='已变化'):
        inspection.inspect(receiving.id, rule['id'])
    assert not receiving.remote.writes


def test_user_session_inspection_reads_without_consuming_cursor(receiving, monkeypatch):
    rule = channel(receiving, mode='user', enabled=False)
    from app.cloud import services, credentials
    services.save(receiving.id, 'telegram_reader', {'sealed': credentials.seal({'session':'test', 'api_id':'1', 'api_hash':'a'*32})}, 0, channel_reader.DEFAULT, lambda cfg, old: cfg)
    async def messages(secret, read_rule, cursor, initialized):
        assert read_rule['history_limit'] == 5 and cursor == 0 and not initialized
        return [msg(SHARE)], 1, False
    monkeypatch.setattr(channel_reader, '_messages', messages)
    before = unchanged_tables()
    result = inspection.inspect(receiving.id, rule['id'])
    assert result['messages'][0]['matched'] and not result['read_error']
    assert before == unchanged_tables()


def test_pausing_does_not_require_any_remote_connection(receiving, monkeypatch):
    rule = channel(receiving, mode='public_web')
    from app.cloud import workflow
    monkeypatch.setattr(workflow, 'verify_account', Mock(side_effect=AssertionError('pause must be local')))
    result = intake.set_channel_enabled(receiving.id, rule['id'], False, rule['revision'])
    assert result['enabled'] is False and result['revision'] == rule['revision'] + 1
    with pytest.raises(CloudError, match='已变化'):
        intake.set_channel_enabled(receiving.id, rule['id'], False, rule['revision'])


def test_enable_still_validates_remote_target(receiving, monkeypatch):
    rule = channel(receiving, mode='public_web', enabled=False)
    from app.cloud import workflow
    monkeypatch.setattr(workflow, 'verify_account', Mock(side_effect=CloudError('账号需要验证', 409)))
    with pytest.raises(CloudError, match='账号需要验证'):
        intake.set_channel_enabled(receiving.id, rule['id'], True, rule['revision'])
    assert not intake.channels(receiving.id)[0]['enabled']


def test_inspection_and_switch_routes_validate_scope_and_auth(receiving, client, monkeypatch):
    rule = channel(receiving, mode='public_web')
    monkeypatch.setattr(public_channel_reader, 'fetch_messages', lambda *a, **k: ([msg(SHARE)], 1))
    base = '/api/cloud/accounts/' + receiving.id + '/channels/' + rule['id']
    before = unchanged_tables()
    response = client.post(base + '/inspect')
    assert response.status_code == 200 and response.json()['preview_only']
    assert before == unchanged_tables()
    assert client.post(base + '/enabled', json={'enabled': False}).status_code == 400
    response = client.post(base + '/enabled', json={'enabled': False, 'revision': rule['revision']})
    assert response.status_code == 200 and not response.json()['channel']['enabled']
    assert client.post(base + '/enabled', json={'enabled': True, 'revision': rule['revision']}).status_code == 409
    client.cookies.clear()
    assert client.post(base + '/inspect').status_code == 401
    assert client.post(base + '/enabled', json={'enabled': False, 'revision': 2}).status_code == 401
