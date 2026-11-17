import asyncio
import json

import httpx

from strimkeep.webhook_notifier import WebhookNotifier


def test_webhook_notifier_posts_title_message_and_secret():
    seen = {}

    async def handler(request):
        seen['url'] = str(request.url)
        seen['headers'] = dict(request.headers)
        seen['json'] = json.loads(request.content)
        return httpx.Response(204, request=request)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            notifier = WebhookNotifier(http, 'https://example.test/hook', 's3cret')
            assert await notifier.send('标题', '内容') == ('sent', '204')
    asyncio.run(run())
    assert seen['json'] == {'title': '标题', 'message': '内容', 'source': 'StrimKeep'}
    assert seen['headers']['x-strimkeep-token'] == 's3cret'


def test_webhook_notifier_retries_5xx_but_not_4xx():
    codes = iter([503, 400])

    async def handler(request):
        return httpx.Response(next(codes), request=request)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            notifier = WebhookNotifier(http, 'https://example.test/hook')
            assert (await notifier.send('t', 'm'))[0] == 'unknown'
            assert (await notifier.send('t', 'm'))[0] == 'failed'
    asyncio.run(run())
