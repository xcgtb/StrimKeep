# -*- coding: utf-8 -*-
"""Telegram 消息格式化与推送（从 engine.py 拆出）。

注意：RUNTIME_CFG 是运行时可变全局（reload_config() 会原地改 engine 里的那份），
所以这里用惰性访问，避免模块导入时快照旧配置。
"""
import datetime
import logging
import urllib.parse
import urllib.request

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
            with urllib.request.urlopen(req, timeout=10) as r:
                ok_all = ok_all and (200 <= r.status < 300)
        except Exception as e:
            log.warning('Telegram 推送失败: %s', e)
            ok_all = False
    return ok_all
