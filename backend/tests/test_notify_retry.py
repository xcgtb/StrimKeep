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
            return httpx.Response(200, json={'ok': True}, request=request)
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
