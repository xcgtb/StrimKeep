"""Redact Telegram credentials from error messages before they leave the process."""
import re

import httpx

_API_URL = re.compile(r"(?i)https?://api\.telegram\.org/bot[^\s\"\'<>]+")
_TOKEN = re.compile(r"(?<![0-9A-Za-z_])\d{5,}:[A-Za-z0-9_-]{15,}(?![A-Za-z0-9_-])")


def safe_telegram_error(exc, token=None):
    """Keep actionable HTTP status and scrub credentials before truncation."""
    if isinstance(exc, httpx.HTTPStatusError):
        return 'HTTP %s' % exc.response.status_code
    if isinstance(exc, httpx.HTTPError):
        return '网络请求未完成（%s）' % type(exc).__name__
    message = str(exc)
    if token:
        message = message.replace(str(token), '[已隐藏]')
    message = _API_URL.sub('[Telegram API 地址已隐藏]', message)
    return _TOKEN.sub('[已隐藏]', message) or type(exc).__name__
