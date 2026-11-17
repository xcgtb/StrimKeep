"""通用 Webhook 通知通道。只发 title/message，不依赖 Telegram 消息模型。"""
import httpx

from .ports import Notifier


class WebhookNotifier(Notifier):
    name = 'webhook'

    def __init__(self, http: httpx.AsyncClient, url: str, secret: str = ''):
        self.http = http
        self.url = url
        self.secret = secret

    @staticmethod
    def enabled(config: dict) -> bool:
        return bool(config.get('webhook_enabled') and config.get('webhook_url'))

    async def send(self, title: str, message: str, attachments: list[str] | None = None) -> tuple[str, str]:
        if not self.url:
            return 'disabled', 'missing url'
        payload = {'title': title, 'message': message, 'source': 'StrimKeep'}
        headers = {'Content-Type': 'application/json'}
        if self.secret:
            headers['X-StrimKeep-Token'] = self.secret
        try:
            response = await self.http.post(self.url, json=payload, headers=headers, timeout=15)
            response.raise_for_status()
            return 'sent', str(response.status_code)
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            return ('unknown' if code >= 500 or code == 429 else 'failed'), f'HTTP {code}'
        except httpx.HTTPError:
            return 'unknown', 'network error'
