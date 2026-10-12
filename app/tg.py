# -*- coding: utf-8 -*-
"""Telegram 消息格式化与推送（从 engine.py 拆出）。

注意：RUNTIME_CFG 是运行时可变全局（reload_config() 会原地改 engine 里的那份），
所以这里用惰性访问，避免模块导入时快照旧配置。
"""
import datetime
import logging
import json
import re
import uuid
import urllib.parse
import urllib.request
import urllib.error
try:
    from . import network as _network
except ImportError:
    import network as _network

log = logging.getLogger('strimkeep')


def _engine_ns():
    """返回 engine 模块对象；import 失败则 None（脚本直跑等边角场景）。"""
    try:
        from . import engine as _e
        return _e
    except ImportError:
        try:
            import engine as _e
            return _e
        except ImportError:
            return None


def _runtime_cfg():
    eng = _engine_ns()
    if eng is not None and hasattr(eng, 'RUNTIME_CFG'):
        return eng.RUNTIME_CFG
    from . import config as _cfg
    return _cfg.load_config()


def tg_title(icon, title, sub=''):
    """消息标题：粗体标题 + 斜体副标题（不再用 ━━ 分隔线，宽度在不同字体下对不齐）"""
    return f'{icon} <b>{title}</b>' + (f'\n<i>{sub}</i>' if sub else '')


def tg_row(icon, label, value, note=''):
    return f'{icon} {label}　<b>{value}</b>' + (f'　<i>{note}</i>' if note else '')


def tg_stamp():
    n = datetime.datetime.now()
    return n.strftime('%m-%d %H:%M')


def fmt_scan_text(icon, title, loc, shr, keep, ex_cnt, ex_groups=0, sub=''):
    note = f'合并为 {ex_groups} 组' if ex_groups and ex_groups != ex_cnt else ''
    return '\n'.join([
        tg_title(icon, title, sub or tg_stamp()), '',
        tg_row('🧹', '待清理本地', loc),
        tg_row('📤', '待淘汰分享', shr),
        tg_row('🛡️', '受保护', keep),
        tg_row('🏷️', '白名单豁免', ex_cnt, note),
    ])


def split_telegram_html(text, limit=3800):
    """按行切分超长 Telegram HTML 消息；切分点若落在可折叠引用块 <blockquote expandable>
    里，会在上一段补 </blockquote>、下一段重新打开，保证每段都是合法的折叠块。"""
    if len(text) <= limit:
        return [text]
    bq_open = '<blockquote expandable>'
    chunks, cur, curlen, in_bq = [], [], 0, False
    for line in text.split('\n'):
        add = len(line) + 1
        if cur and curlen + add + 14 > limit:
            chunk = '\n'.join(cur) + ('\n</blockquote>' if in_bq else '')
            chunks.append(chunk)
            cur = []
            curlen = 0
            if in_bq:
                line = bq_open + line  # 和第一行拼在同一行，避免引用块开头多一个空行
                add = len(line) + 1
        cur.append(line); curlen += add
        if '<blockquote' in line:
            in_bq = '</blockquote>' not in line
        elif '</blockquote>' in line:
            in_bq = False
    if cur:
        chunks.append('\n'.join(cur))
    return chunks


def notify_telegram(text, chat_id=None):
    cfg = _runtime_cfg()
    token = (cfg.get('telegram_bot_token') or '').strip()
    cid = chat_id or (cfg.get('telegram_chat_id') or '').strip()
    if not token or not cid:
        return False
    ok_all = True
    for part in split_telegram_html(text):
        try:
            url = f'https://api.telegram.org/bot{token}/sendMessage'
            data = urllib.parse.urlencode({'chat_id': cid, 'text': part, 'parse_mode': 'HTML',
                                           'disable_web_page_preview': 'true'}).encode()
            req = urllib.request.Request(url, data=data, method='POST')
            with _network.open_external(req, timeout=10) as r:
                accepted = json.loads(r.read(65536)).get('ok') is True
                ok_all = ok_all and (200 <= r.status < 300) and accepted
        except Exception as e:
            # Request exceptions can contain the bot token in their URL.
            log.warning('Telegram 推送失败: %s', type(e).__name__)
            ok_all = False
    return ok_all


def _notification_poster(value):
    """Fetch only known poster routes; never fetch a subscription-supplied host."""
    if __package__:
        from . import posters
    else:
        import posters
    parsed = urllib.parse.urlsplit(str(value or ''))
    path = parsed.path
    if parsed.netloc:
        if parsed.scheme != 'https' or parsed.netloc != 'image.tmdb.org':
            raise ValueError('不支持的海报来源')
        match = re.fullmatch(r'/t/p/(?:w[0-9]{2,4}|original)/([^/]+)', path)
        if not match:
            raise ValueError('海报路径无效')
        return posters.get_tmdb_poster(match[1], 'w500')
    if parsed.scheme or path.startswith('//'):
        raise ValueError('海报路径无效')
    if path.startswith('/api/emby/poster/'):
        item = path.removeprefix('/api/emby/poster/')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,120}', item):
            raise ValueError('海报项目无效')
        cfg = _runtime_cfg()
        if not cfg.get('emby_host') or not cfg.get('emby_key'):
            raise ValueError('Emby 连接未配置')
        return posters.get_emby_poster(item, cfg['emby_host'], cfg['emby_key'])
    filename = path.removeprefix('/api/tmdb/poster/').lstrip('/')
    return posters.get_tmdb_poster(filename, 'w500')


def notify_telegram_card(text, poster='', chat_id=None, text_sender=None):
    """Photo + bounded HTML caption, with text fallback before uncertain sends.

    Upload cached image bytes so Telegram need not access an authenticated/local
    StrimKeep address. A timeout or server error remains queued for retry rather
    than sending a second, potentially duplicate, text notification.
    """
    def fallback():
        sender = text_sender or notify_telegram
        return sender(text, chat_id=chat_id) if chat_id is not None else sender(text)

    if not poster or len(text.encode('utf-16-le')) // 2 > 1024:
        return fallback()
    cfg = _runtime_cfg()
    token = (cfg.get('telegram_bot_token') or '').strip()
    cid = chat_id or (cfg.get('telegram_chat_id') or '').strip()
    if not token or not cid:
        return False
    try:
        photo, mime = _notification_poster(poster)
        if not photo or len(photo) > 2 * 1024 * 1024 or mime not in ('image/jpeg', 'image/png'):
            return fallback()
    except Exception as exc:
        log.info('追更海报不可用，发送文字详情: %s', type(exc).__name__)
        return fallback()
    boundary = 'strimkeep-' + uuid.uuid4().hex
    parts = []
    for name, value in {'chat_id': cid, 'caption': text, 'parse_mode': 'HTML'}.items():
        parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n').encode())
    extension = 'png' if mime == 'image/png' else 'jpg'
    parts += [(f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; filename="poster.{extension}"\r\nContent-Type: {mime}\r\n\r\n').encode(),
              photo, f'\r\n--{boundary}--\r\n'.encode()]
    req = urllib.request.Request(f'https://api.telegram.org/bot{token}/sendPhoto',
        data=b''.join(parts), headers={'Content-Type': 'multipart/form-data; boundary=' + boundary}, method='POST')
    try:
        with _network.open_external(req, timeout=10) as response:
            result = json.loads(response.read(65536))
            if result.get('ok') is False and result.get('error_code') in (400, 413):
                return fallback()
            return bool(200 <= response.status < 300 and result.get('ok') is True)
    except urllib.error.HTTPError as exc:
        # Only a definite photo/format rejection permits immediate fallback.
        # Rate limits/auth failures/5xx and unknown outcomes retain the queue.
        if exc.code in (400, 413):
            return fallback()
        log.warning('Telegram 海报推送失败: HTTP %s', exc.code)
        return False
    except Exception as exc:
        log.warning('Telegram 海报推送结果未确认: %s', type(exc).__name__)
        return False
