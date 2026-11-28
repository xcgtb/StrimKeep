"""Bound management request bodies and password verification work."""
import asyncio
import time
from collections import OrderedDict, deque

from fastapi import HTTPException
from starlette.datastructures import Headers
from starlette.responses import JSONResponse


WRITE_METHODS = frozenset({'POST', 'PUT', 'PATCH', 'DELETE'})
MAX_BODY_BYTES = 64 * 1024


class RequestBodyLimit:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope['method'] not in WRITE_METHODS:
            await self.app(scope, receive, send)
            return

        declared = Headers(scope=scope).get('content-length', '').lstrip('0')
        maximum = str(MAX_BODY_BYTES)
        if declared.isascii() and declared.isdecimal() and (
                len(declared) > len(maximum) or len(declared) == len(maximum) and declared > maximum):
            await self.reject(scope, receive, send)
            return

        body = bytearray()
        while True:
            message = await receive()
            if message['type'] == 'http.disconnect':
                return
            chunk = message.get('body', b'')
            if len(body) + len(chunk) > MAX_BODY_BYTES:
                await self.reject(scope, receive, send)
                return
            body.extend(chunk)
            if not message.get('more_body', False):
                break

        async def replay():
            nonlocal body
            if body is None:
                return await receive()
            message = {'type': 'http.request', 'body': bytes(body), 'more_body': False}
            body = None
            return message

        await self.app(scope, replay, send)

    @staticmethod
    async def reject(scope, receive, send):
        response = JSONResponse(
            {'error': {'code': 'BODY_TOO_LARGE', 'message': '请求内容过大。'}},
            status_code=413,
        )
        await response(scope, receive, send)


class LoginGuard:
    FAILURE_LIMIT = 8
    FAILURE_WINDOW = 900
    PEER_COOLDOWN = 2
    MAX_PEERS = 1024
    MAX_ACTIVE = 2
    BURST = 8
    REFILL_PER_SECOND = 2

    def __init__(self):
        self.failures = OrderedDict()
        self.active = 0
        self.tokens = self.BURST
        self.refilled = time.monotonic()

    @staticmethod
    def reject(seconds):
        raise HTTPException(429, '登录请求过多，请稍后再试。', headers={'Retry-After': str(seconds)})

    async def verify(self, authenticate, peer, username, password):
        now = time.monotonic()
        attempts = self.failures.get(peer)
        if attempts is not None:
            while attempts and attempts[0] <= now - self.FAILURE_WINDOW:
                attempts.popleft()
            if len(attempts) == self.FAILURE_LIMIT and now < attempts[-1] + self.PEER_COOLDOWN:
                self.reject(self.PEER_COOLDOWN)
            if not attempts:
                self.failures.pop(peer)

        self.tokens = min(self.BURST, self.tokens + (now - self.refilled) * self.REFILL_PER_SECOND)
        self.refilled = now
        if self.active >= self.MAX_ACTIVE or self.tokens < 1:
            self.reject(1)
        self.tokens -= 1
        self.active += 1

        async def run():
            try:
                valid = await asyncio.to_thread(authenticate, username, password)
                if valid:
                    self.failures.pop(peer, None)
                    return True
                attempts = self.failures.setdefault(peer, deque(maxlen=self.FAILURE_LIMIT))
                attempts.append(time.monotonic())
                self.failures.move_to_end(peer)
                if len(self.failures) > self.MAX_PEERS:
                    self.failures.popitem(last=False)
                if len(attempts) == self.FAILURE_LIMIT:
                    self.reject(self.PEER_COOLDOWN)
                return False
            finally:
                self.active -= 1

        # A disconnected caller must not free a slot before scrypt actually finishes.
        task = asyncio.create_task(run())
        task.add_done_callback(self._observe)
        return await asyncio.shield(task)

    @staticmethod
    def _observe(task):
        if not task.cancelled():
            task.exception()
