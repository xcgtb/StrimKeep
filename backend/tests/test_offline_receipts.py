import asyncio
import time

import httpx

from strimkeep.notifications import Notifications
from strimkeep.store import Store


def make(tmp_path, **config):
    store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
    store.set_many({'notification_config': {
        'telegram_enabled': True, 'bot_token': 'test', 'chat_id': '42', **config}})
    return store


def response(method, status=200, description='', message_id=99):
    data = {'ok': status == 200, 'result': {'message_id': message_id}, 'description': description}
    return httpx.Response(status, json=data, request=httpx.Request('POST', 'https://example.invalid/' + method))


def test_photo_receipt_keeps_spoiler_and_original_link_through_updates(tmp_path):
    async def run():
        store = make(tmp_path)
        calls = []
        photo_reads = []
        async def send(token, method, payload, *, file=None):
            calls.append((method, payload, file))
            return response(method)
        async def photo(channel, message):
            photo_reads.append((channel, message))
            return b'original-image'

        link = 'https://t.me/c/1234567890/17'
        opts = {'spoiler': True, 'post_url': link, 'photo_source': {'channel': '-1001234567890', 'id': 17}}
        notes = Notifications(store, None)
        notes.send, notes.channel_photo = send, photo
        assert notes.stage_offline(436, 'submitted', '已提交', '等待下载', telegram_options=opts)
        await notes.flush_offline(436, 'submitted')
        assert calls[0][0] == 'sendPhoto' and calls[0][1]['has_spoiler']
        assert calls[0][2] == b'original-image'

        restarted = Notifications(store, None)
        restarted.send, restarted.channel_photo = send, photo
        for stage, title, message in [('stalled', '超时', '115 仍在下载'), ('completed', '已完成', 'film.mkv')]:
            assert restarted.stage_offline(436, stage, title, message)
            await restarted.flush_offline(436, stage)
        assert [c[0] for c in calls] == ['sendPhoto', 'editMessageCaption', 'editMessageCaption']
        for _, payload, file in calls[1:]:
            assert payload['message_id'] == 99 and payload['chat_id'] == '42'
            assert payload['reply_markup']['inline_keyboard'][0][0]['url'] == link
            assert file is None and 'has_spoiler' not in payload
        assert 'film.mkv' in calls[-1][1]['caption']
        assert photo_reads == [('-1001234567890', 17)]
        assert not restarted.stage_offline(436, 'stalled', '超时', '过期更新')
        assert store.db.execute("SELECT count(*) FROM notifications WHERE event_key='offline_receipt:436'").fetchone()[0] == 1
        store.close()
    asyncio.run(run())


def test_completed_receipt_supersedes_pending_submission_after_restart(tmp_path):
    async def run():
        store = make(tmp_path, telegram_enabled=False, chat_id='')
        notes = Notifications(store, None)
        async def offline(*args, **kwargs):
            raise httpx.ConnectError('offline')
        notes.send = offline
        assert notes.stage_offline(7, 'submitted', '已提交', '等待下载', chat_override='77')
        await notes.flush_offline(7, 'submitted')
        assert store.db.execute('SELECT telegram_state FROM notifications').fetchone()[0] == 'unknown'
        calls = []
        async def send(token, method, payload, *, file=None):
            calls.append((method, payload))
            return response(method)
        restarted = Notifications(store, None)
        restarted.send = send
        assert restarted.stage_offline(7, 'completed', '已完成', 'film.mkv')
        assert await restarted.retry() == 1
        assert await restarted.retry() == 0
        assert len(calls) == 1 and calls[0] == ('sendMessage', {
            'chat_id': '77', 'text': '已完成\nfilm.mkv', 'link_preview_options': {'is_disabled': True}})
        store.close()
    asyncio.run(run())


def test_text_fallback_is_edited_and_deleted_card_is_replaced_once(tmp_path):
    async def run():
        store = make(tmp_path)
        notes = Notifications(store, None)
        calls = []
        edit_result = 429
        async def send(token, method, payload, *, file=None):
            calls.append(method)
            if method == 'sendPhoto':
                return response(method, 400, 'invalid photo')
            if method == 'editMessageText':
                return response(method, edit_result, 'message to edit not found' if edit_result == 400 else '')
            return response(method, message_id=len(calls))

        notes.send = send
        assert notes.stage_offline(1, 'submitted', '已提交', '等待',
            photo='https://example.invalid/photo.jpg', telegram_options={'post_url': 'https://t.me/example/1'})
        await notes.flush_offline(1, 'submitted')
        assert store.db.execute('SELECT photo FROM telegram_message_refs').fetchone()[0] == 0
        assert notes.stage_offline(1, 'completed', '已完成', 'film.mkv')
        await notes.flush_offline(1, 'completed')
        assert calls == ['sendPhoto', 'sendMessage', 'editMessageText']
        edit_result = 400
        assert await notes.retry(now=time.time()+3600) == 1
        assert await notes.retry(now=time.time()+7200) == 0
        assert calls[-2:] == ['editMessageText', 'sendMessage']
        assert store.db.execute('SELECT message_id FROM telegram_message_refs').fetchone()[0] == 5
        store.close()
    asyncio.run(run())


def test_completion_staged_during_submission_send_updates_the_same_message(tmp_path):
    async def run():
        store = make(tmp_path)
        notes = Notifications(store, None)
        sending, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def send(token, method, payload, *, file=None):
            calls.append((method, payload))
            if method == 'sendMessage':
                sending.set()
                await release.wait()
            return response(method)
        notes.send = send
        assert notes.stage_offline(1, 'submitted', '已提交', '等待')
        started = asyncio.create_task(notes.flush_offline(1, 'submitted'))
        await asyncio.wait_for(sending.wait(), 2)
        assert notes.stage_offline(1, 'completed', '已完成', 'film.mkv')
        release.set()
        await started
        await notes.flush_offline(1, 'completed')
        assert [c[0] for c in calls] == ['sendMessage', 'editMessageText']
        assert calls[-1][1]['message_id'] == 99 and calls[-1][1]['text'] == '已完成\nfilm.mkv'
        store.close()
    asyncio.run(run())
