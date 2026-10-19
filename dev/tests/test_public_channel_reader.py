"""Deterministic public_web reader, no real Telegram/115 network calls."""
from __future__ import annotations

import html
import time
from unittest.mock import Mock

import httpx
import pytest

from dev.tests.test_cloud_receiving import receiving
from dev.tests.test_cloud_workbench import cloud
from dev.tests.test_my_drive import client
from app.cloud import public_channel_reader as web, receiving as intake, store, jobs
from app.cloud.errors import CloudError

CHANNEL = 'testmoviechannel'
SHARE = 'https://115.com/s/abcdef123?password=passSECRET'


def page(ids, *, channel=CHANNEL, url=SHARE):
    chunks = ['<html><div class="tgme_channel_info">Channel</div>']
    for mid in ids:
        chunks.append(f'''<div class="tgme_widget_message" data-post="{channel}/{mid}">
        <div class="tgme_widget_message_text">电影 {mid} &amp; 高清 <a href="{html.escape(url, quote=True)}">资源下载</a><br>提取码:passSECRET</div>
        <a class="tgme_widget_message_date"><time datetime="2026-10-19T06:00:00+00:00">Oct 19</time></a>
        </div>''')
    return (''.join(chunks)+'</html>').encode()


def pages(monkeypatch, newest=30):
    observed = []
    def load(channel, before=None, *, transport=None):
        observed.append(before)
        stop = min(newest, before-1 if before else newest)
        return page(range(max(1, stop-9), stop+1))
    monkeypatch.setattr(web, '_http_page', load)
    return observed


def rule(receiving, **overrides):
    config = dict(name='影视公开频道', address='https://t.me/'+CHANNEL,
        mode='public_web', target_cid='3', enabled=True, auto_transfer=True,
        poll_seconds=300, history_limit=20)
    config.update(overrides)
    return intake.save_channel(receiving.id, config)


def test_html_parser_decodes_hidden_link_and_timestamp():
    rows = web.parse_html(page([11]), CHANNEL)
    assert len(rows) == 1
    assert rows[0]['message_id'] == 11
    assert '& 高清' in rows[0]['text'] and '\n' in rows[0]['text']
    assert rows[0]['entities'] == [{'type': 'text_link', 'url': SHARE}]
    assert rows[0]['date'] == 1792389600


def test_parser_skips_foreign_channel_and_rejects_unrecognized_html():
    assert web.parse_html(page([11], channel='otherchannel'), CHANNEL) == []
    with pytest.raises(web.PublicPageError):
        web.parse_html(b'<html><title>Login required</title></html>', CHANNEL)


def test_history_exactly_twenty_and_incremental_catches_all_pages(monkeypatch):
    queried = pages(monkeypatch)
    initial, tip = web.fetch_messages(CHANNEL, history_limit=20)
    assert [x['message_id'] for x in initial] == list(range(11,31))
    assert tip == 30 and queried == [None,21]
    pages(monkeypatch, 47)
    newer, new_tip = web.fetch_messages(CHANNEL, cursor=tip, initialized=True)
    assert [x['message_id'] for x in newer] == list(range(31,48))
    assert new_tip == 47


def test_missing_old_cursor_fails_instead_of_skipping(monkeypatch):
    pages(monkeypatch, 500)
    with pytest.raises(web.PublicPageError, match='上限'):
        web.fetch_messages(CHANNEL, cursor=12, initialized=True)


def test_first_run_history_zero_means_no_backfill(monkeypatch):
    pages(monkeypatch)
    rows, tip = web.fetch_messages(CHANNEL, history_limit=0)
    assert not rows and tip == 30


def test_http_bounded_redirect_block_429_and_mime():
    for status, headers, body in [
        (302, {'location': 'https://evil.example/'}, b''),
        (429, {'Retry-After':'900'}, b''),
        (200, {'content-type':'application/json'}, b'{}'),
        (200, {'content-type':'text/html'}, b'x'* (2*1024*1024+1)),
    ]:
        transport = httpx.MockTransport(lambda req: httpx.Response(status, headers=headers, content=body))
        with pytest.raises(web.PublicPageError) as exc:
            web._http_page(CHANNEL, transport=transport)
        if status == 429:
            assert exc.value.retry_seconds >= 900


def test_preview_is_read_only_and_rejects_private_identifier(receiving, monkeypatch, client):
    pages(monkeypatch)
    result = client.post(f'/api/cloud/accounts/{receiving.id}/channels/public-preview',
                         json={'address':'https://t.me/'+CHANNEL, 'limit':20})
    assert result.status_code == 200, result.text
    assert result.json()['count'] == 20
    with store.db() as con:
        assert con.execute('SELECT count(*) FROM cloud_inbox').fetchone()[0] == 0
        assert con.execute('SELECT count(*) FROM cloud_channel_cursors').fetchone()[0] == 0
    with pytest.raises(CloudError, match='公开'):
        web.preview(receiving.id,'-10012345678')
    assert client.post(f'/api/cloud/accounts/{receiving.id}/channels/public-preview',
                       json={'address':'http://localhost/admin'}).status_code == 400


def test_public_rule_has_no_user_api_and_reuses_persistent_inbox(receiving, monkeypatch):
    rule(receiving)
    pages(monkeypatch, 5)
    assert web.poll_one() is True
    with store.db() as con:
        assert con.execute('SELECT cursor,initialized FROM cloud_channel_cursors').fetchone() == (5,1)
        assert con.execute('SELECT count(*) FROM cloud_inbox').fetchone()[0] == 5
    # Not queued again until poll interval.
    assert not web.poll_one()
    with store.db() as con:
        con.execute('UPDATE cloud_channel_cursors SET next_poll=0')
    assert web.poll_one()
    with store.db() as con:
        assert con.execute('SELECT count(*) FROM cloud_inbox').fetchone()[0] == 5
    intake.schedule()
    with store.db() as con:
        assert con.execute("SELECT count(*) FROM cloud_jobs WHERE kind='channel'").fetchone()[0] == 5


def test_disabled_auto_transfer_records_without_115_write(receiving, monkeypatch):
    rule(receiving, auto_transfer=False)
    pages(monkeypatch, 2)
    assert web.poll_one()
    intake.schedule()
    with store.db() as con:
        jids=[x[0] for x in con.execute("SELECT id FROM cloud_jobs WHERE kind='channel'")]
    assert len(jids)==2
    for jid in jids:
        jobs.execute(jid)
        result = store.job(jid)
        assert result['state']=='success' and result['result']['observed']
    assert not receiving.remote.writes
    assert receiving.remote.offline_add.call_count == 0


def test_public_autotransfer_115_share_into_target_not_organize(receiving, monkeypatch):
    rule(receiving)
    pages(monkeypatch, 1)
    assert web.poll_one()
    intake.schedule()
    with store.db() as con:
        jid=con.execute("SELECT id FROM cloud_jobs WHERE kind='channel'").fetchone()[0]
    jobs.execute(jid)
    result=store.job(jid)
    assert result['result']['accepted'][0]['target_cid'] == '3'
    assert result['result']['child_jobs']
    child_id=result['result']['child_jobs'][0]
    child=store.job(child_id)
    with store.db() as con:
        raw=con.execute('SELECT payload FROM cloud_jobs WHERE id=?',(child_id,)).fetchone()[0]
    import json
    assert child['kind']=='transfer' and json.loads(raw)['organize_target'] is None


def test_failure_never_advances_cursor_and_exponential_backoff(receiving, monkeypatch):
    rule(receiving)
    monkeypatch.setattr(web, 'fetch_messages', Mock(side_effect=web.PublicPageError('Blocked', 600)))
    assert web.poll_one()
    with store.db() as con:
        cursor,next_poll,last_error=con.execute('SELECT cursor,next_poll,last_error FROM cloud_channel_cursors').fetchone()
        assert cursor==0 and 'Blocked' in last_error and next_poll>time.time()+590
        assert con.execute('SELECT failures FROM cloud_public_channel_status').fetchone()[0] == 1


def test_changed_rule_before_commit_cannot_enqueue_stale_messages(receiving,monkeypatch):
    cfg = rule(receiving)
    real_fetch=web.fetch_messages
    def changed(*args,**kw):
        with store.db() as con:
            con.execute('UPDATE cloud_channels SET revision=revision+1 WHERE id=?',(cfg['id'],))
        return ([{'message_id':1,'date':1792389600,'text':SHARE,'entities':[]}],1)
    monkeypatch.setattr(web,'fetch_messages',changed)
    assert web.poll_one()
    with store.db() as con:
        assert con.execute('SELECT count(*) FROM cloud_inbox').fetchone()[0] == 0
        assert con.execute('SELECT count(*) FROM cloud_channel_cursors').fetchone()[0] == 0


def test_public_web_does_not_enqueue_non_share_links(receiving, monkeypatch):
    rule(receiving)
    magnet = 'magnet:?xt=urn:btih:' + 'a'*40
    monkeypatch.setattr(web, '_http_page', lambda channel, before=None, **kw: page([1] if before is None else [], url=magnet))
    assert web.poll_one()
    with store.db() as con:
        assert con.execute('SELECT count(*) FROM cloud_inbox').fetchone()[0] == 0
        assert con.execute('SELECT cursor FROM cloud_channel_cursors').fetchone()[0] == 1


def test_bad_timestamp_never_allows_valid_later_posts_to_skip_a_message():
    malformed=page([10,11]).replace(b'2026-10-19T06:00:00+00:00',b'bad-time',1)
    with pytest.raises(web.PublicPageError,match='时间'):
        web.parse_html(malformed,CHANNEL)


def test_public_preview_includes_plain_text_and_cdn_share_links(receiving, monkeypatch):
    link='https://www.115cdn.com/s/abcdef123?password=test'
    monkeypatch.setattr(web,'_http_page',lambda *a,**kw:page([1],url=link))
    assert web.preview(receiving.id,'@'+CHANNEL,1)['messages'][0]['links']==[link]


def test_universal_forward_captures_non_resource_public_text(receiving, monkeypatch):
    saved=rule(receiving,action='forward',forward_target='self',target_cid='',history_limit=1)
    monkeypatch.setattr(web,'_http_page',lambda *a,**kw:page([1],url='https://example.com'))
    assert web.poll_one()
    assert len(intake.inbox(receiving.id))==1
    assert not receiving.remote.writes


def test_resource_button_outside_post_text_is_preserved():
    raw=page([1],url='https://example.com').replace(b'<a class="tgme_widget_message_date">',('<div class="tgme_widget_message_inline_keyboard"><a href="'+SHARE+'">下载</a></div><a class="tgme_widget_message_date">').encode())
    messages=web.parse_html(raw,CHANNEL)
    assert SHARE in [e['url'] for e in messages[0]['entities']]
