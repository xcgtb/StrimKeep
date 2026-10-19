"""Public Telegram channel *HTML preview* ingestion. No Telegram identity/API.

This is an optional, best-effort reader of public t.me/s/<username> pages,
NOT an official Telegram feed. It never follows redirects or bypasses challenges.
Read, parse and bound the full history interval before committing any cursor.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urlencode

import httpx

from app.cloud import events, receiving, store
from app.cloud.errors import CloudError

_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="public-channel")
_future = None
_HOST = "t.me"
_USERNAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{4,31}\Z")
_POST = re.compile(r"([A-Za-z][A-Za-z0-9_]{4,31})/([1-9][0-9]{0,18})\Z", re.I)
_VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"})
_MAX_BYTES = 2 * 1024 * 1024
_MAX_PAGES = 10
_MAX_MESSAGES = 200


class PublicPageError(CloudError):
    def __init__(self, reason: str, retry_seconds: int = 0):
        super().__init__(reason, 502)
        self.retry_seconds = min(86400, max(0, int(retry_seconds)))


class _PreviewHTML(HTMLParser):
    """Parse preview markup, preserving hidden anchor links and UTF-8 text.

    We only trust a message whose data-post has the *requested* channel name,
    a positive message ID and a machine-readable time tag.
    """

    def __init__(self, channel: str):
        super().__init__(convert_charrefs=True)
        self.channel = channel.casefold()
        self.messages = []
        self._stack = []
        self._active = None
        self._post_depth = 0
        self._text_depth = 0
        self._keyboard_depth = 0
        self.page_marker = False
        self.invalid_posts = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get("class", "").split()
        if "tgme_channel_info" in " ".join(classes) or "tgme_widget_message" in " ".join(classes):
            self.page_marker = True
        post = attrs.get("data-post")
        if post and self._active is None:
            match = _POST.fullmatch(post)
            if match and match.group(1).casefold() == self.channel:
                self._active = {"message_id": int(match.group(2)), "date": None, "parts": [], "entities": []}
                self._post_depth = len(self._stack) + 1
        if self._active is not None:
            if "tgme_widget_message_text" in classes or "tgme_widget_message_caption" in classes:
                self._text_depth = len(self._stack) + 1
            if "tgme_widget_message_inline_keyboard" in classes:
                self._keyboard_depth = len(self._stack) + 1
            if tag == "time" and attrs.get("datetime"):
                try:
                    dt = datetime.fromisoformat(attrs["datetime"].replace("Z", "+00:00"))
                    if dt.tzinfo is not None:
                        self._active["date"] = int(dt.timestamp())
                except (ValueError, OverflowError):
                    pass
            if (self._text_depth or self._keyboard_depth) and tag == "a":
                link = attrs.get("href", "")
                if link.startswith(("https://", "http://", "magnet:?", "ed2k://", "115://")) and len(link) <= 4096:
                    self._active["entities"].append({"type": "text_link", "url": link})
            if self._text_depth and tag == "br":
                self._active["parts"].append("\n")
        if tag not in _VOID:
            self._stack.append(tag)

    def handle_data(self, data):
        if self._active is not None and self._text_depth and len(self._active["parts"]) < 3000:
            self._active["parts"].append(data)

    def handle_endtag(self, tag):
        if tag in _VOID:
            return
        if not self._stack:
            return
        # Telegram's own HTML is normally balanced. Recover from stray HTML by
        # closing only up to the nearest matching tag, never executing markup.
        if tag not in self._stack:
            return
        count = len(self._stack) - 1 - self._stack[::-1].index(tag)
        self._stack = self._stack[:count]
        if self._active is None:
            return
        if self._keyboard_depth and len(self._stack) < self._keyboard_depth:
            self._keyboard_depth = 0
        if self._text_depth and len(self._stack) < self._text_depth:
            self._text_depth = 0
        if len(self._stack) < self._post_depth:
            item = self._active
            self._active = None
            self._post_depth = 0
            self._text_depth = 0
            if item["date"] is None:
                self.invalid_posts += 1
            if item["date"] is not None:
                self.messages.append({
                    "message_id": item["message_id"],
                    "date": item["date"],
                    "text": "".join(item["parts"]).strip()[:16000],
                    "entities": list({v["url"]: v for v in item["entities"]}.values()),
                })


def parse_html(raw: bytes, channel: str):
    parser = _PreviewHTML(channel)
    try:
        parser.feed(raw.decode("utf-8", errors="replace"))
        parser.close()
    except (ValueError, OverflowError) as exc:
        raise PublicPageError("公开频道 HTML 解析失败，游标未推进") from exc
    if parser.invalid_posts or parser._active is not None:
        raise PublicPageError("公开频道消息缺少有效时间或内容被截断，游标未推进")
    if not parser.messages and not parser.page_marker:
        raise PublicPageError("未找到公开频道预览标记；可能不存在、需要登录或网页结构已变化")
    ids = [m["message_id"] for m in parser.messages]
    if len(ids) != len(set(ids)):
        raise PublicPageError("公开网页有重复消息 ID，已停止本轮读取")
    return sorted(parser.messages, key=lambda m: m["message_id"])


def _public_dns():
    """Fixed hostname plus public DNS check; request client disables env proxies."""
    try:
        addrs = socket.getaddrinfo(_HOST, 443, type=socket.SOCK_STREAM)
        if not addrs or any(not ipaddress.ip_address(a[4][0]).is_global for a in addrs):
            raise PublicPageError("Telegram 域名未解析到公共网络地址，已拒绝请求")
    except socket.gaierror as exc:
        raise PublicPageError("无法解析 Telegram 公开网页域名") from exc


def _http_page(channel: str, before: int | None = None, *, transport=None) -> bytes:
    if not _USERNAME.fullmatch(channel):
        raise PublicPageError("公开频道用户名无效")
    # Never take a hostname or path from an arbitrary message / user URL.
    url = "https://t.me/s/" + channel + ("?" + urlencode({"before": before}) if before else "")
    if transport is None:
        _public_dns()
    try:
        with httpx.Client(timeout=httpx.Timeout(15, connect=5), follow_redirects=False,
                          trust_env=False, transport=transport, headers={"Accept": "text/html", "User-Agent": "StrimKeepPublicPreview/1.0"}) as client:
            with client.stream("GET", url) as response:
                if response.status_code == 429:
                    raw = response.headers.get("Retry-After", "")
                    wait = min(86400, max(60, int(raw))) if raw.isdigit() else 1800
                    raise PublicPageError("Telegram 公开网页请求受限（429），等待后重试", wait)
                if response.status_code in (401, 403, 404, 410) or 300 <= response.status_code < 400:
                    raise PublicPageError("公开频道不可读取（HTTP %s），未登录或预览受限" % response.status_code, 1800)
                if response.status_code != 200:
                    raise PublicPageError("公开频道网页暂不可用（HTTP %s）" % response.status_code, 300)
                if "text/html" not in response.headers.get("Content-Type", "").lower():
                    raise PublicPageError("公开频道响应不是 HTML，已拒绝解析")
                chunks = bytearray()
                for block in response.iter_bytes():
                    chunks.extend(block)
                    if len(chunks) > _MAX_BYTES:
                        raise PublicPageError("频道页面超过 2 MiB 限制，未推进游标")
                return bytes(chunks)
    except PublicPageError:
        raise
    except (httpx.TimeoutException, httpx.NetworkError, httpx.ProtocolError) as exc:
        raise PublicPageError("Telegram 公开网页连接失败或超时") from exc


def fetch_messages(channel: str, *, cursor: int = 0, initialized=False, history_limit=20, transport=None):
    """Return a *complete bounded* interval or fail without advancing cursor.

    If the newest content cannot be traced backwards to the old cursor within
    the page/message budget, raise rather than silently skipping messages.
    """
    if not _USERNAME.fullmatch(channel):
        raise PublicPageError("公开频道用户名无效")
    limit = min(100, max(0, int(history_limit)))
    found, before, seen = {}, None, set()
    reached = False
    for _ in range(_MAX_PAGES):
        page = parse_html(_http_page(channel, before, transport=transport), channel)
        if not page:
            if initialized and cursor > 0 and not reached:
                raise PublicPageError("公开预览缺少旧游标范围，已停止以避免漏单")
            if not found:  # genuinely empty preview, verified markup
                return [], 0 if not initialized else cursor
            reached = True  # no older page
            break
        oldest = min(m["message_id"] for m in page)
        if before is not None and oldest >= before:
            raise PublicPageError("频道历史分页没有前进；已暂停以避免漏单")
        fresh_page = 0
        for msg in page:
            mid = msg["message_id"]
            if mid in seen:
                continue
            seen.add(mid)
            if initialized and mid <= cursor:
                reached = True
            elif (not initialized and limit > 0) or initialized:
                found[mid] = msg
                fresh_page += 1
        if initialized and reached:
            break
        if not initialized and (limit == 0 or len(found) >= limit):
            break
        if len(seen) >= _MAX_MESSAGES or (initialized and len(found) >= _MAX_MESSAGES):
            raise PublicPageError("频道未读消息超过单轮 200 条上限，未推进游标，请缩短轮询周期")
        before = oldest
    else:
        raise PublicPageError("频道翻页超过 10 页上限，未推进游标以避免漏单")
    if initialized and cursor and not reached:
        # Public HTML can omit/deletes older posts. Never present this as a
        # lossless continuous feed; require manual review instead of skipping.
        raise PublicPageError("没有在公开预览窗口中找到上次消息游标，存在漏读风险；请人工检查频道")
    records = sorted(found.values(), key=lambda m: m["message_id"])
    if not initialized:
        records = records[-limit:] if limit else []
    tip = max([cursor, *seen]) if seen else cursor
    return records, tip


def preview(account_id, address: str, limit: int = 20, *, transport=None):
    if not store.account(account_id):
        raise CloudError("115 账号不存在", 404)
    channel = receiving.channel_address(address).lstrip("@")
    if not _USERNAME.fullmatch(channel):
        raise CloudError("免登录仅支持公开频道用户名，不支持 -100 ID 或邀请链接", 409)
    if type(limit) is not int or not 0 <= limit <= 100:
        raise CloudError("预览条数必须在 0–100 之间")
    messages, _ = fetch_messages(channel, history_limit=limit, transport=transport)
    def links(message):
        values = receiving.LINK.findall(message['text'])
        values.extend(e['url'] for e in message['entities'] if receiving.LINK.fullmatch(e['url']))
        return list(dict.fromkeys(v for v in values if v.lower().startswith(('https://115.com/s/', 'https://www.115.com/s/', 'https://115cdn.com/s/', 'https://www.115cdn.com/s/'))))[:20]
    return {"channel": channel, "mode": "public_web", "count": len(messages),
            "messages": [{"message_id": m["message_id"], "date": m["date"],
                          "text": m["text"][:1200], "links": links(m)} for m in messages]}


def _save_failure(rid, revision, account_revision, error, now):
    with store.db() as con:
        guard = con.execute("SELECT c.revision,a.revision,c.payload FROM cloud_channels c "
                            "JOIN cloud_accounts a ON a.id=c.account_id WHERE c.id=?", (rid,)).fetchone()
        if not guard or guard[0] != revision or guard[1] != account_revision or not json.loads(guard[2]).get("enabled") or json.loads(guard[2]).get("mode") != "public_web":
            return
        row = con.execute("SELECT failures FROM cloud_public_channel_status WHERE channel_id=?", (rid,)).fetchone()
        failures = (row[0] if row else 0) + 1
        delay = max(error.retry_seconds, min(3600, 300 * 2 ** min(failures - 1, 4)))
        con.execute("INSERT INTO cloud_channel_cursors(id,cursor,initialized,next_poll,last_error) "
                    "VALUES(?,0,0,?,?) ON CONFLICT(id) DO UPDATE SET next_poll=excluded.next_poll,last_error=excluded.last_error",
                    (rid, now+delay, str(error)))
        con.execute("INSERT INTO cloud_public_channel_status(channel_id,last_success,last_check,last_count,failures) "
                    "VALUES(?,0,?,0,?) ON CONFLICT(channel_id) DO UPDATE SET last_check=excluded.last_check,failures=excluded.failures",
                    (rid, now, failures))
    events.emit("channel", "公开频道抓取失败："+str(error), level="WARNING")


def poll_one():
    now = time.time()
    with store.db() as con:
        row = con.execute("SELECT c.id,c.account_id,c.account_revision,c.revision,c.payload,"
                          "COALESCE(s.cursor,0),COALESCE(s.initialized,0) "
                          "FROM cloud_channels c JOIN cloud_accounts a ON a.id=c.account_id AND a.revision=c.account_revision "
                          "LEFT JOIN cloud_channel_cursors s ON s.id=c.id "
                          "WHERE json_extract(c.payload,'$.enabled')=1 AND json_extract(c.payload,'$.mode')='public_web' "
                          "AND COALESCE(s.next_poll,0)<=? ORDER BY COALESCE(s.next_poll,0),c.id LIMIT 1", (now,)).fetchone()
    if not row:
        return False
    rid, aid, account_rev, rule_rev, raw, cursor, initialized = row
    rule = json.loads(raw)
    try:
        channel = rule["address"].removeprefix("@")
        rows, tip = fetch_messages(channel, cursor=int(cursor), initialized=bool(initialized),
                                   history_limit=rule["history_limit"])
        # Network and parsing are complete. Commit all messages and cursor as one
        # transaction; changed account/rule/cursor invalidate this result.
        with store.db() as con:
            current = con.execute("SELECT c.revision,c.payload,a.revision,COALESCE(s.cursor,0),"
                                  "COALESCE(s.initialized,0) FROM cloud_channels c JOIN cloud_accounts a "
                                  "ON a.id=c.account_id LEFT JOIN cloud_channel_cursors s ON s.id=c.id WHERE c.id=? AND c.account_id=?", (rid, aid)).fetchone()
            if not current or current[0] != rule_rev or current[2] != account_rev or current[3:] != (cursor, initialized) or not json.loads(current[1]).get("enabled") or json.loads(current[1]).get("mode") != "public_web":
                return True
            # Stable per-rule scope; no Bot token dependency or fake Bot identity.
            chat_id = -(10**12 + int(hashlib.sha256(channel.encode()).hexdigest()[:12], 16))
            for msg in rows:
                urls = receiving.LINK.findall(msg["text"])
                urls.extend(e["url"] for e in msg["entities"] if e.get("type") == "text_link"
                            and receiving.LINK.fullmatch(e["url"]))
                # Only resource-bearing posts go to the durable inbox. In v1
                # public-web mode only 115 share links are eligible to transfer.
                if rule.get("action") != "forward" and not any(u.lower().startswith(("https://115.com/s/", "https://www.115.com/s/", "https://115cdn.com/s/", "https://www.115cdn.com/s/")) for u in urls):
                    continue
                receiving.record_channel(con, rid, aid, account_rev, rule_rev, rule,
                    {**msg, "chat": {"id": chat_id}}, "public_web:"+channel)
            con.execute("INSERT INTO cloud_channel_cursors(id,cursor,initialized,next_poll,last_error) "
                        "VALUES(?,?,1,?,'') ON CONFLICT(id) DO UPDATE SET cursor=excluded.cursor,"
                        "initialized=1,next_poll=excluded.next_poll,last_error=''",
                        (rid, tip, time.time()+int(rule.get("poll_seconds", 300))))
            con.execute("INSERT INTO cloud_public_channel_status(channel_id,last_success,last_check,last_count,failures) "
                        "VALUES(?,?,?,?,0) ON CONFLICT(channel_id) DO UPDATE SET last_success=excluded.last_success,"
                        "last_check=excluded.last_check,last_count=excluded.last_count,failures=0",
                        (rid, time.time(), time.time(), len(rows)))
        events.emit("channel", "公开频道检查完成："+rule["name"]+" · 新消息 "+str(len(rows)))
    except PublicPageError as error:
        _save_failure(rid, rule_rev, account_rev, error, time.time())
    except Exception:
        _save_failure(rid, rule_rev, account_rev, PublicPageError("公开频道抓取出现异常，请检查网页结构与网络"), time.time())
    return True


def drain(limit=3):
    for _ in range(max(1, limit)):
        if not poll_one():
            break


def schedule():
    global _future
    if _future is not None and not _future.done():
        return
    with store.db() as con:
        due = con.execute("SELECT 1 FROM cloud_channels c JOIN cloud_accounts a ON a.id=c.account_id AND a.revision=c.account_revision "
                          "LEFT JOIN cloud_channel_cursors s ON s.id=c.id WHERE json_extract(c.payload,'$.enabled')=1 "
                          "AND json_extract(c.payload,'$.mode')='public_web' AND COALESCE(s.next_poll,0)<=? LIMIT 1", (time.time(),)).fetchone()
    if due:
        _future = _POOL.submit(drain)
