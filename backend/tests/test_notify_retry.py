"""通知重试：临时失败要退避重发；对方明确拒收（4xx）不重发；有次数上限。"""
import asyncio
import time

import httpx

from strimkeep.notifications import Notifications, RETRY_LIMIT
from strimkeep.store import Store


class FakeHTTP:
    """按顺序吐出预设结果：ok / reject(400) / busy(429) / down(网络异常)。"""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    async def post(self, url, json=None, timeout=None):
        self.calls.append(json or {})
        outcome = self.outcomes.pop(0) if self.outcomes else 'ok'
        request = httpx.Request('POST', url)
        if outcome == 'ok':
            return httpx.Response(200, json={'ok': True, 'result': {'message_id': len(self.calls)}},
                                  request=request)
        if outcome == 'reject':
            return httpx.Response(400, json={'ok': False, 'description': 'bad chat'}, request=request)
        if outcome == 'busy':
            return httpx.Response(429, json={'ok': False}, request=request)
        raise httpx.ConnectError('boom', request=request)


def enable(store):
    store.set_many({'notification_config': {'telegram_enabled': True, 'bot_token': 'token', 'chat_id': '42'}})


def row_of(store, key):
    return store.db.execute('SELECT * FROM notifications WHERE event_key=?', (key,)).fetchone()


def test_transient_failure_is_retried_after_backoff(tmp_path):
    async def run():
        store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
        enable(store)
        http = FakeHTTP(['down', 'ok'])
        notes = Notifications(store, http)

        assert await notes.publish('k1', '标题', '正文') == 'unknown'
        row = row_of(store, 'k1')
        assert row['telegram_state'] == 'unknown' and row['attempts'] == 1
        assert row['next_try'] > time.time()

        assert await notes.retry(now=row['next_try'] - 1) == 0       # 没到点
        assert await notes.retry(now=row['next_try'] + 1) == 1       # 到点重发
        row = row_of(store, 'k1')
        assert row['telegram_state'] == 'sent' and row['attempts'] == 2
        assert row['next_try'] == 0
        store.close()
    asyncio.run(run())


def test_rejected_message_is_never_retried(tmp_path):
    async def run():
        store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
        enable(store)
        notes = Notifications(store, FakeHTTP(['reject']))
        assert await notes.publish('k2', '标题', '正文') == 'failed'
        row = row_of(store, 'k2')
        assert row['next_try'] == 0
        assert await notes.retry(now=time.time() + 86400) == 0
        store.close()
    asyncio.run(run())


def test_rate_limited_message_is_retryable(tmp_path):
    async def run():
        store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
        enable(store)
        notes = Notifications(store, FakeHTTP(['busy', 'ok']))
        assert await notes.publish('k3', '标题', '正文') == 'unknown'
        row = row_of(store, 'k3')
        assert await notes.retry(now=row['next_try'] + 1) == 1
        assert row_of(store, 'k3')['telegram_state'] == 'sent'
        store.close()
    asyncio.run(run())


def test_retry_stops_at_the_attempt_limit(tmp_path):
    async def run():
        store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
        enable(store)
        notes = Notifications(store, FakeHTTP(['down']))
        await notes.publish('k4', '标题', '正文')
        with store.db:
            store.db.execute("UPDATE notifications SET attempts=?,next_try=0 WHERE event_key='k4'", (RETRY_LIMIT,))
        assert await notes.retry(now=time.time() + 86400) == 0
        store.close()
    asyncio.run(run())


def test_photo_survives_retry(tmp_path):
    async def run():
        store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
        enable(store)
        http = FakeHTTP(['down', 'ok'])
        notes = Notifications(store, http)
        await notes.publish('k5', '标题', '正文', photo='https://img/x.jpg')
        row = row_of(store, 'k5')
        assert row['photo'] == 'https://img/x.jpg'
        assert await notes.retry(now=row['next_try'] + 1) == 1
        assert http.calls[-1]['photo'] == 'https://img/x.jpg'
        assert 'caption' in http.calls[-1]
        store.close()
    asyncio.run(run())


def test_channel_original_photo_uses_copy_or_spoiler_upload_and_keeps_link_on_retry(tmp_path):
    async def run():
        store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
        enable(store)
        http = FakeHTTP(['ok', 'down', 'ok'])
        notes = Notifications(store, http)
        methods = []
        async def send(token, method, payload, *, file=None):
            methods.append((method, payload, file))
            return await http.post('https://example.invalid/' + method, json=payload)
        async def photo(channel, message):
            assert (channel, message) == ('-1001234567890', 17)
            return b'mock-original-photo'
        notes.channel_photo = photo
        notes.send = send
        url = 'https://t.me/c/1234567890/17'
        copy = {'from_chat_id': '-1001234567890', 'message_id': 17}
        assert await notes.publish('channel:copy', '标题', '原帖', photo='https://cdn1.telesco.pe/x',
                                   telegram_options={'copy_source': copy, 'post_url': url}) == 'sent'
        assert methods[0][0] == 'copyMessage' and 'has_spoiler' not in methods[0][1]
        options = {'copy_source': copy, 'photo_source': {'channel': '-1001234567890', 'id': 17},
                   'spoiler': True, 'post_url': url}
        assert await notes.publish('channel:spoiler', '标题', '原帖', telegram_options=options) == 'unknown'
        assert await notes.retry(now=row_of(store, 'channel:spoiler')['next_try'] + 1) == 1
        assert methods[-1][0] == 'sendPhoto' and methods[-1][2] == b'mock-original-photo'
        assert methods[-1][1]['has_spoiler'] is True
        assert methods[-1][1]['reply_markup']['inline_keyboard'][0][0]['url'] == url
        store.close()
    asyncio.run(run())


def test_public_channel_uploads_the_poster_after_copy_rejection(tmp_path):
    async def run():
        store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
        enable(store)
        photo = 'https://cdn1.telesco.pe/card.jpg'
        link = 'https://t.me/example_public/7'
        jpeg = b'\xff\xd8mock-photo'
        sequence = []

        async def request(req):
            sequence.append(req.url.path.rsplit('/', 1)[-1])
            if req.method == 'GET':
                assert str(req.url) == photo
                return httpx.Response(200, content=jpeg)
            if req.url.path.endswith('/copyMessage'):
                return httpx.Response(400, json={'description': 'message to copy not found'})
            assert req.url.path.endswith('/sendPhoto')
            assert 'multipart/form-data' in req.headers['content-type']
            body = await req.aread()
            assert jpeg in body and link.encode() in body
            assert b'Original caption' in body
            assert (b'has_spoiler' in body) == ('copyMessage' not in sequence)
            return httpx.Response(200, json={'ok': True, 'result': {'message_id': 7}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(request)) as http:
            notes = Notifications(store, http)
            options = {'copy_source': {'from_chat_id': '@example_public', 'message_id': 7}, 'post_url': link}
            assert await notes.publish('channel:public', 'Result', 'Original caption', photo=photo,
                                       telegram_options=options) == 'sent'
            assert sequence == ['copyMessage', 'card.jpg', 'sendPhoto']
            sequence.clear()
            assert await notes.publish('channel:public-spoiler', 'Result', 'Original caption', photo=photo,
                                       telegram_options={**options, 'spoiler': True}) == 'sent'
            assert sequence == ['card.jpg', 'sendPhoto']
        store.close()
    asyncio.run(run())


def test_protected_channel_keeps_link_without_downloading_photo(tmp_path):
    async def run():
        store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
        enable(store)
        sequence = []

        async def request(req):
            method = req.url.path.rsplit('/', 1)[-1]
            sequence.append(method)
            assert req.method == 'POST'
            if method == 'copyMessage':
                return httpx.Response(400, json={'description': "message can't be forwarded"})
            assert method == 'sendMessage'
            return httpx.Response(200, json={'ok': True, 'result': {'message_id': 7}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(request)) as http:
            notes = Notifications(store, http)
            options = {'copy_source': {'from_chat_id': '@protected_example', 'message_id': 7},
                       'post_url': 'https://t.me/protected_example/7'}
            assert await notes.publish('channel:protected', 'Result', 'Text',
                                       photo='https://cdn1.telesco.pe/card.jpg', telegram_options=options) == 'sent'
            assert sequence == ['copyMessage', 'sendMessage']
            assert 'Telegram 拒绝复制原帖' in store.db.execute(
                "SELECT message FROM events WHERE kind='channel' ORDER BY id DESC LIMIT 1").fetchone()[0]
        store.close()
    asyncio.run(run())


def test_grouped_media_retry_keeps_poster_and_details_in_order_for_both_libraries(tmp_path):
    from test_organize import new_job
    async def run(kind):
        store=Store(tmp_path / kind, 'admin', 'test-password-1234')
        enable(store)
        http=FakeHTTP(['down','ok','ok','ok','ok'])
        notes=Notifications(store,http)
        async def send(token,method,payload): return await http.post('https://example.invalid/'+method,json=payload)
        notes.send=send
        jid=new_job(store,{'job_kind':kind})
        group='series'
        card='show_done:%s:%s'%(jid,group)
        assert await notes.publish(card,'海报','正文',photo='https://img/x.jpg',job_id=jid,category='media')=='unknown'
        items=[(i,{'relative_path':'library/series/S01E%02d.strm'%i,'strm_created':True}) for i in range(1,26)]
        await notes.group_strm(jid,group,items)
        rows=store.db.execute('SELECT event_key,telegram_state FROM notifications WHERE job_id=? ORDER BY id',(jid,)).fetchall()
        assert len(rows)>=4 and rows[0]['telegram_state']=='unknown'
        assert all(row['telegram_state']=='waiting' for row in rows[1:])
        assert len(http.calls)==1
        due=row_of(store,card)['next_try']
        assert await notes.retry(now=due+1)==len(rows)
        sent=[x.get('caption') or x.get('text') for x in http.calls[1:]]
        assert sent[0].startswith('海报') and 'STRM 批量结果' in sent[1]
        assert '文件明细 1/' in sent[2] and '文件明细 2/' in sent[3]
        assert all(call['reply_parameters']['message_id'] == 2 for call in http.calls[2:])
        assert all(row['telegram_state']=='sent' for row in store.db.execute('SELECT telegram_state FROM notifications WHERE job_id=?',(jid,)))
        store.close()
    for kind in ('organize','share_organize'):
        asyncio.run(run(kind))


def test_concurrent_media_batches_keep_each_poster_summary_and_pages_together(tmp_path):
    from test_organize import new_job

    async def run():
        store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
        enable(store)
        http = FakeHTTP([])
        notes = Notifications(store, http)
        first_started, release, second_queued = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def send(token, method, payload):
            if payload.get('caption', '').startswith('作品甲'):
                first_started.set()
                await release.wait()
            return await http.post('https://example.invalid/' + method, json=payload)
        notes.send = send

        async def group(kind, title):
            jid = new_job(store, {'job_kind': kind})
            items = [(i, {'relative_path': f'library/{title}/S01E{i:02d}.strm',
                          'strm_created': True, 'details': {'name': title}, 'hint': {'season': 1}})
                     for i in range(1, 24)]
            if kind == 'organize':
                second_queued.set()
            await notes.group_strm(jid, 'group:1,2', items,
                                   card=(title, '正文', 'https://img/x.jpg'))

        first = asyncio.create_task(group('share_organize', '作品甲'))
        await first_started.wait()
        Notifications(store, http)
        assert store.db.execute('SELECT telegram_state FROM notifications').fetchone()[0] == 'inflight'
        second = asyncio.create_task(group('organize', '作品乙'))
        await second_queued.wait()
        assert not http.calls
        release.set()
        await asyncio.gather(first, second)

        messages = [data.get('caption') or data['text'] for data in http.calls]
        assert len(messages) == 8
        for offset, name, source in [(0, '作品甲', '分享 STRM'), (4, '作品乙', '网盘整理')]:
            assert messages[offset].startswith(name)
            assert source in messages[offset + 1]
            assert '文件明细 1/2' in messages[offset + 2] and '文件明细 2/2' in messages[offset + 3]
            for idx in range(offset + 1, offset + 4):
                assert f'{name} · S01 · 任务 #' in messages[idx]
                assert http.calls[idx]['reply_parameters']['message_id'] == offset + 1
        store.close()
    asyncio.run(run())


def test_waiting_chain_survives_restart_and_a_second_failure(tmp_path):
    async def run():
        store=Store(tmp_path/'data','admin','test-password-1234');enable(store)
        http=FakeHTTP(['down','down','ok','ok'])
        notes=Notifications(store,http)
        async def send(token,method,payload): return await http.post('https://example.invalid/'+method,json=payload)
        notes.send=send
        await notes.publish('poster','海报','正文')
        assert await notes.publish('detail','明细','正文',depends_on='poster')=='waiting'
        due=row_of(store,'poster')['next_try']
        assert await notes.retry(now=due+1)==0
        assert row_of(store,'detail')['telegram_state']=='waiting' and len(http.calls)==2
        notes=Notifications(store,http);notes.send=send
        due=row_of(store,'poster')['next_try']
        assert await notes.retry(now=due+1)==2
        assert [x['text'].split('\n')[0] for x in http.calls]==['海报','海报','海报','明细']
        store.close()
    asyncio.run(run())


def test_failed_summary_blocks_later_pages_until_retry(tmp_path):
    from test_organize import new_job
    async def run():
        store=Store(tmp_path/'data','admin','test-password-1234');enable(store)
        http=FakeHTTP(['ok','down','ok','ok','ok'])
        notes=Notifications(store,http)
        async def send(token,method,payload): return await http.post('https://example.invalid/'+method,json=payload)
        notes.send=send
        jid=new_job(store,{'job_kind':'organize'});group='series'
        await notes.publish(f'show_done:{jid}:{group}','海报','正文',job_id=jid,category='media')
        items=[(i,{'relative_path':f'library/series/S01E{i:02}.strm','strm_created':True}) for i in range(1,26)]
        await notes.group_strm(jid,group,items)
        rows=store.db.execute('SELECT * FROM notifications WHERE job_id=? ORDER BY id',(jid,)).fetchall()
        assert rows[0]['telegram_state']=='sent' and rows[1]['telegram_state']=='unknown'
        assert all(row['telegram_state']=='waiting' for row in rows[2:]) and len(http.calls)==2
        assert await notes.retry(now=rows[1]['next_try']+1)==len(rows)-1
        delivered=[x.get('text','') for x in http.calls]
        assert 'STRM 批量结果' in delivered[2] and '文件明细 1/' in delivered[3] and '文件明细 2/' in delivered[4]
        store.close()
    asyncio.run(run())
