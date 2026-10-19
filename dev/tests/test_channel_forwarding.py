"""Durable forwarding, precise failures and compact cards; no live sends."""
import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import Mock, AsyncMock

import pytest
from dev.tests.test_cloud_workbench import cloud
from dev.tests.test_cloud_receiving import receiving, channel, msg, run
from dev.tests.test_share_library import shared
from app.cloud import receiving as r, jobs, store, workflow, provider115, channel_reader, channel_forwarding as relay, services, credentials, notifications, events
from app.cloud.errors import CloudError
from app.domain import telegram_intake, media_labels

SHARE = 'https://115.com/s/abcdef123?password=passSECRET'


@pytest.mark.parametrize('response,category,phrase', [
    ({'errno':4100010}, 'share_cancelled', '已取消'),
    ({'errno':4100026}, 'share_unavailable', '不存在或已删除'),
    ({'errno':17,'error':'分享链接已过期 https://secret.test/p?password=leak'}, 'share_expired', '已过期'),
    ({'errno':18,'error':'访问码错误 password=leak'}, 'share_password', '提取码错误'),
    ({'errno':99}, 'authorization', 'Cookie'),
    ({'errno':911}, 'verification_required', '安全验证'),
    ({'errno':45678,'error':'unrecognized password=leak'}, 'provider_rejected', '拒绝请求'),
])
def test_structured_sdk_failures_are_classified_without_leaks(response, category, phrase):
    provider = object.__new__(provider115.Provider115)
    provider.client = SimpleNamespace(share_snap=Mock(side_effect=OSError(5, {'state':False,**response})))
    with pytest.raises(CloudError) as e:
        provider.call('share_snap', {}, write=False)
    assert e.value.category == category and phrase in str(e.value)
    assert e.value.stage == 'read_share' and not e.value.uncertain
    assert 'leak' not in str(e.value) and 'secret.test' not in str(e.value)


def test_explicit_write_refusal_and_transport_unknown_are_different():
    provider = object.__new__(provider115.Provider115)
    provider.client = SimpleNamespace(share_receive=Mock(return_value={'state':False,'errno':4100010}))
    with pytest.raises(CloudError) as known:
        provider.call('share_receive', {}, write=True)
    assert not known.value.uncertain and known.value.stage == 'submit_transfer'
    provider.client.share_receive.side_effect = TimeoutError('secret Cookie=leak')
    with pytest.raises(CloudError) as unknown:
        provider.call('share_receive', {}, write=True)
    assert unknown.value.uncertain and 'leak' not in str(unknown.value)


def intake_message(text='/strm', **patch):
    return {**msg(text), **patch}


def test_reply_caption_and_hidden_links_use_current_sender_authorization(receiving, shared):
    r.save(shared.id, {'tg_enabled':True}, 0)
    message = intake_message(reply_to_message={'caption':'资源', 'caption_entities':[{'type':'text_link','url':SHARE}], 'from':{'id':999}})
    assert not r.capture(message, authorized=False)
    iid = r.capture(message, authorized=True)[0]
    assert r.capture(message, authorized=True) == [iid]
    r.schedule()
    jid = r.inbox(shared.id)[0]['job_id']
    result = run(jid)
    assert result['state'] == 'success' and '分享 STRM' in result['message']
    child = result['result']['child_jobs'][0]
    assert run(child)['state'] == 'success'
    assert list(shared.share_root.rglob('*.strm')) and not shared.remote.writes


@pytest.mark.parametrize('action,prefix', [('share_strm','/strm'), ('forward','/create_115share_strm')])
def test_channel_share_strm_does_not_require_transfer_directory_or_global_bot(receiving, shared, action, prefix, monkeypatch):
    saved = channel(shared, mode='public_web', action=action, command_prefix=prefix, target_cid='')
    monkeypatch.setattr(workflow, 'transfer_submit', Mock(side_effect=AssertionError('must not transfer')))
    with store.db() as con:
        r.record_channel(con,saved['id'],shared.id,1,saved['revision'],saved,msg(SHARE,channel=True),'public:test')
    r.schedule(); jid = r.inbox(shared.id)[0]['job_id']; result = run(jid)
    assert result['state'] == 'success', result
    assert run(result['result']['child_jobs'][0])['state'] == 'success'
    assert not shared.remote.writes and list(shared.share_root.rglob('*.strm'))


def test_disabled_auto_processing_is_observe_only_without_provider_calls(receiving, monkeypatch):
    saved = channel(receiving, mode='public_web', action='share_strm', target_cid='', auto_transfer=False)
    with store.db() as con:
        r.record_channel(con,saved['id'],receiving.id,1,saved['revision'],saved,msg(SHARE,channel=True),'p')
    r.schedule()
    monkeypatch.setattr(workflow,'verify_account',Mock(side_effect=AssertionError('observe must be local')))
    result = run(r.inbox(receiving.id)[0]['job_id'])
    assert result['state'] == 'success' and result['result']['observed'] and not result['result']['accepted']


def test_failure_stage_is_saved_in_inbox_job_logs_and_notification(receiving, monkeypatch):
    from app import tg
    from app.cloud import settings
    r.save(receiving.id,{'tg_enabled':True},0)
    provider = object.__new__(provider115.Provider115)
    provider.client = SimpleNamespace(share_snap=Mock(return_value={'state':False,'errno':4100010}))
    monkeypatch.setattr(receiving.remote,'share_page', lambda *a,**kw:provider.call('share_snap',{}))
    calls=[]
    monkeypatch.setattr(tg,'notify_telegram',lambda text:calls.append(text) or True)
    settings.save({'notify_enabled':True})
    iid = r.capture(msg('/receive '+SHARE), authorized=True)[0]
    r.schedule(); result = run(r.inbox(receiving.id)[0]['job_id'])
    assert result['state'] == 'failed'
    error = r.inbox(receiving.id)[0]['result']['errors'][0]
    assert error['stage'] == 'read_share' and error['code'] == '4100010'
    assert any('读取分享' in c and '分享已取消' in c for c in calls)
    with store.db() as con:
        logs = '\n'.join(str(row) for row in con.execute('SELECT message,details FROM runtime_logs'))
        raw = con.execute('SELECT payload FROM cloud_inbox WHERE id=?',(iid,)).fetchone()[0]
    assert '4100010' in logs and 'read_share' in logs
    assert 'passSECRET' not in logs+raw


def test_child_transfer_failure_is_visible_in_reception_history(receiving, monkeypatch):
    r.save(receiving.id,{'tg_enabled':True},0)
    r.capture(msg('/receive '+SHARE), authorized=True);r.schedule()
    parent = run(r.inbox(receiving.id)[0]['job_id'])
    child = parent['result']['child_jobs'][0]
    monkeypatch.setattr(receiving.remote,'receive',Mock(side_effect=CloudError('分享已取消',502,code='4100010',category='share_cancelled')))
    assert run(child)['state']=='failed'
    error = r.inbox(receiving.id)[0]['result']['child_errors'][0]
    assert error['stage']=='submit_transfer' and error['code']=='4100010'


def session(cloud):
    return services.save(cloud.id,'telegram_reader',{'sealed':credentials.seal({'api_id':'1234','api_hash':'a'*32,'session':'fixture'})},0,channel_reader.DEFAULT,lambda value,old:value)


def test_external_forward_is_once_and_does_not_claim_ingestion(receiving, monkeypatch):
    session(receiving)
    saved=channel(receiving,mode='public_web',action='forward',target_cid='',forward_target='@ExampleBot',command_prefix='/custom mode')
    sent=[]
    async def sender(secret,target,text,guard):
        guard();sent.append((target,text));return {'kind':'forward','message_id':19,'completed':True}
    monkeypatch.setattr(relay,'_send',sender)
    message=msg('无 115 链接的普通频道消息',channel=True)
    with store.db() as con:
        for _ in range(2):r.record_channel(con,saved['id'],receiving.id,1,saved['revision'],saved,message,'public:test')
    r.schedule();row=r.inbox(receiving.id)[0];result=run(row['job_id'])
    assert result['state']=='success' and '等待目标机器人' in result['message']
    assert sent==[('@ExampleBot','/custom mode\n无 115 链接的普通频道消息')]
    run(row['job_id']);r.schedule()
    assert len(sent)==1 and not receiving.remote.writes
    with store.db() as con:
        assert con.execute('SELECT state FROM cloud_steps WHERE job_id=? AND step_key=?',(row['job_id'],'telegram-forward')).fetchone()[0]=='verified'


def test_uncertain_relay_never_replays_and_paused_rule_blocks(receiving, monkeypatch):
    session(receiving)
    saved=channel(receiving,mode='public_web',action='forward',target_cid='',forward_target='@ExampleBot')
    payload={'account_id':receiving.id,'account_revision':1,'channel_id':saved['id'],'channel_revision':saved['revision']}
    jid=jobs.enqueue('channel',payload)
    calls=[]
    async def ambiguous(*args):
        calls.append(True);raise TimeoutError('secret token=leak')
    monkeypatch.setattr(relay,'_send',ambiguous)
    for _ in range(2):
        with pytest.raises(CloudError) as error:
            relay.run(jid,payload,saved,{'text':SHARE,'entities':[]},lambda:r.verify_source(payload))
        assert error.value.uncertain
    assert len(calls)==1
    r.save_channel(receiving.id,{k:v for k,v in {**saved,'enabled':False}.items() if k!='start_after'})
    with pytest.raises(CloudError):r.verify_source(payload)


def test_relay_refuses_non_bot_and_sends_plain_text_with_hidden_links(receiving, monkeypatch):
    client=SimpleNamespace(connect=AsyncMock(),disconnect=AsyncMock(),is_user_authorized=AsyncMock(return_value=True),get_entity=AsyncMock(return_value=SimpleNamespace(bot=False)),send_message=AsyncMock(return_value=SimpleNamespace(id=21)))
    monkeypatch.setattr(channel_reader,'_client',lambda secret:client)
    with pytest.raises(CloudError) as error:
        asyncio.run(relay._send({},'@ExampleBot','/strm',lambda:None))
    assert not error.value.uncertain and not client.send_message.called
    client.get_entity.return_value.bot=True
    text=telegram_intake.relay_text('/strm',{'text':'<b>电影</b>','entities':[{'url':SHARE}]})
    result=asyncio.run(relay._send({},'@ExampleBot',text,lambda:None))
    assert result['message_id']==21
    assert client.send_message.call_args.kwargs=={'parse_mode':None,'link_preview':False}
    assert SHARE in client.send_message.call_args.args[1]


@pytest.mark.parametrize('prefix,target',[('strm','self'),('/other','self'),('/strm\n/receive','self'),('/strm','https://evil.test'),('/x','@bad')])
def test_invalid_relay_configuration(prefix,target):
    with pytest.raises(ValueError):telegram_intake.validate_relay(prefix,target)


def test_long_relay_never_silently_truncates_links():
    with pytest.raises(ValueError):telegram_intake.relay_text('/strm',{'text':'😊'*3000,'entities':[]})


def test_notification_remux_is_evidence_based_and_subtitles_are_not_a_group():
    media={'kind':'movie','title':'A <Movie>','year':'0000','tmdb_id':44,'directory':'电影/欧美/A Movie','naming_fields':{'source':'Ultra HD Blu-ray','dynamic_range':'HDR10','resolution':'2160p','release_group':'简繁英字幕'}}
    items=[{'media':media,'source':{'name':'A.Clockwork.Orange.1971.2160p.BluRay.REMUX.H265.mkv','size':1024**3}}]
    text=notifications.caption(items,'share')
    assert 'BluRay Remux' in text and '简繁英字幕' not in text and '组名' not in text
    assert '0000' not in text and '&lt;Movie&gt;' in text and '已入库' not in text
    items[0]['source']['name']='Movie.BluRay.x265.mkv'
    assert 'Remux' not in notifications.caption(items)


def test_compact_summary_keeps_nonzero_failures_and_omits_zero_noise(cloud):
    text,_=notifications.batch_result({'errors':[{'reason':'分享已取消'}],'filtered':[{'reason':'未匹配'}]},'failed','读取分享失败',kind='share_strm')
    assert '处理异常：1 项' in text and '规则过滤：1 项' in text
    assert '元数据写入：0' not in text and '版本保留：0' not in text and '跳过：0' not in text


def test_failed_verification_is_unknown_and_does_not_resubmit(receiving, monkeypatch):
    r.save(receiving.id,{'tg_enabled':True},0)
    r.capture(msg('/receive '+SHARE), authorized=True);r.schedule()
    parent=run(r.inbox(receiving.id)[0]['job_id']);child=parent['result']['child_jobs'][0]
    monkeypatch.setattr(workflow,'transfer_receipt',Mock(side_effect=CloudError('核验读取失败',502)))
    result=run(child)
    assert result['state']=='uncertain' and result['result']['errors'][0]['stage']=='verify_transfer'
    assert len([w for w in receiving.remote.writes if w[0]=='receive'])==1
    run(child)
    assert len([w for w in receiving.remote.writes if w[0]=='receive'])==1


def test_user_reader_drops_stale_success_and_failure_without_resurrecting_cursor(receiving, monkeypatch):
    session(receiving)
    saved=channel(receiving,mode='user')
    async def removed(secret,rule,cursor,initialized):
        store.delete_channel(receiving.id,saved['id'])
        raise CloudError('旧会话读取失败',502)
    monkeypatch.setattr(channel_reader,'_messages',removed)
    assert channel_reader.poll_one()
    with store.db() as con:
        assert not con.execute('SELECT 1 FROM cloud_channel_cursors').fetchone()
        assert not con.execute('SELECT 1 FROM cloud_public_channel_status').fetchone()
    saved=channel(receiving,mode='user')
    async def advanced(secret,rule,cursor,initialized):
        with store.db() as con:con.execute('INSERT INTO cloud_channel_cursors VALUES(?,100,1,0,?)',(saved['id'],''))
        return [msg(SHARE,mid=20,channel=True)],20,False
    monkeypatch.setattr(channel_reader,'_messages',advanced)
    assert channel_reader.poll_one()
    with store.db() as con:
        assert con.execute('SELECT cursor FROM cloud_channel_cursors').fetchone()[0]==100
        assert not con.execute('SELECT 1 FROM cloud_inbox').fetchone()


def test_rule_forward_preview_has_no_writes(receiving):
    saved=channel(receiving,mode='public_web',action='forward',target_cid='')
    result=r.test_channel(receiving.id,saved['id'],SHARE)
    assert result['matched'] and result['preview_text']=='/strm\n'+SHARE
    with store.db() as con:
        assert not con.execute('SELECT 1 FROM cloud_inbox').fetchone()
        assert not con.execute('SELECT 1 FROM cloud_jobs').fetchone()
    assert not receiving.remote.writes


def test_inline_keyboard_share_is_received_without_visible_url():
    value=telegram_intake.content({'caption':'下载电影','reply_markup':{'inline_keyboard':[[{'text':'下载','url':SHARE}]]}})
    assert r.extract(value['text'],value['entities'])[0]['password']=='passSECRET'


def test_stop_forward_job_pauses_its_channel_and_stops_before_send(receiving):
    saved=channel(receiving,mode='public_web',action='forward',target_cid='')
    jid=jobs.enqueue('channel',{'account_id':receiving.id,'account_revision':1,'channel_id':saved['id'],'channel_revision':saved['revision']})
    stopped=jobs.request_stop(jid)
    assert stopped['rule_paused']
    assert not r.channels(receiving.id)[0]['enabled']
    assert r.channels(receiving.id)[0]['revision']==saved['revision']+1
    assert run(jid)['state']=='stopped' and not receiving.remote.writes


def test_relay_preserves_title_after_old_header():
    assert telegram_intake.relay_text('/strm',{'text':'/receive 电影标题\n'+SHARE,'entities':[]})=='/strm\n电影标题\n'+SHARE
