import asyncio
from types import SimpleNamespace

import httpx

from strimkeep.notifications import Notifications
from strimkeep.telegram_bot import TelegramBot
from strimkeep.telegram_security import safe_telegram_error

# Synthetic nonfunctional credential for redaction tests only.
TOKEN = '123456789' + ':' + 'TESTTOKEN_FOR_REDACTION_ONLY'
URL = 'https://api.telegram.org/bot' + TOKEN + '/getUpdates'


class StubStore:
    def __init__(self):
        self.events = []
        self.values = {}
    def get(self, key, default=None):
        if key == 'notification_config':
            return {'bot_token': TOKEN}
        return default
    def event(self, kind, message):
        self.events.append((kind, message))
    def set_many(self, values):
        self.values.update(values)


def test_redacts_urls_tokens_before_truncation():
    error = ValueError('prefix ' + URL + ' token=' + TOKEN)
    detail = safe_telegram_error(error, TOKEN)
    assert TOKEN not in detail
    assert 'api.telegram.org' not in detail
    assert 'prefix' in detail


def test_poll_error_records_status_without_credentials():
    async def scenario():
        store = StubStore()
        bot = TelegramBot(SimpleNamespace(store=store, http=None))
        request = httpx.Request('POST', URL)
        response = httpx.Response(502, request=request)
        async def failed_poll():
            raise httpx.HTTPStatusError('failure ' + URL, request=request, response=response)
        bot.poll = failed_poll
        task = asyncio.create_task(bot.loop())
        for _ in range(100):
            if store.events: break
            await asyncio.sleep(0.001)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        value = repr((store.events, store.values))
        assert TOKEN not in value and 'api.telegram.org' not in value
        assert 'HTTP 502' in value
    asyncio.run(scenario())


def test_bot_error_reply_and_event_redact_secrets():
    async def scenario():
        store = StubStore()
        delivered = []
        async def publish(*args, **kwargs): delivered.append(args)
        bot = TelegramBot(SimpleNamespace(store=store, http=None, notifications=SimpleNamespace(publish=publish)))
        async def fail(*args): raise ValueError('problem ' + URL + ' ' + TOKEN)
        bot.handle = fail
        assert not await bot._safe_handle({'update_id': 2, 'message': {'chat': {'id': 123}}}, {}, 'i')
        combined = repr((store.events, delivered))
        assert TOKEN not in combined and 'api.telegram.org' not in combined
        assert 'problem' in combined
    asyncio.run(scenario())


def test_notification_error_detail_does_not_expose_url():
    async def scenario():
        notices = Notifications.__new__(Notifications)
        async def fail(*args): raise httpx.RequestError('failed ' + URL, request=httpx.Request('POST', URL))
        notices.send = fail
        state, detail = await notices._deliver('title', 'message', None, '1', TOKEN)
        assert state == 'unknown'
        assert TOKEN not in detail and 'api.telegram.org' not in detail
    asyncio.run(scenario())


def test_notification_parse_error_also_scrubs_secret():
    async def scenario():
        notices = Notifications.__new__(Notifications)
        async def fail(*args): raise ValueError('invalid: ' + URL)
        notices.send = fail
        state, detail = await notices._deliver('title', 'message', None, '1', TOKEN)
        assert state == 'unknown'
        assert TOKEN not in detail and 'api.telegram.org' not in detail
    asyncio.run(scenario())
