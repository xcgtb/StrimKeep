"""Bounded MTProto reads. Sessions stay encrypted; cursors commit with inbox records."""
import asyncio
import hashlib
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from app.cloud import credentials, services, store
from app.cloud.errors import CloudError

DEFAULT = {"sealed": "", "pending": "", "expires": 0}
_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cloud-channels")
_future = None


def get(account_id):
    cfg = services.get(account_id, "telegram_reader", DEFAULT)
    return {"revision": cfg["revision"], "connected": bool(cfg["config"]["sealed"]),
            "pending": bool(cfg["config"]["pending"] and cfg["config"]["expires"] > time.time())}


def _client(secret):
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    return TelegramClient(StringSession(secret.get("session", "")), int(secret["api_id"]), secret["api_hash"],
                          connection_retries=0, request_retries=0, flood_sleep_threshold=0, timeout=12)


def _run(coro):
    try:
        return asyncio.run(asyncio.wait_for(coro, 45))
    except CloudError:
        raise
    except Exception:
        raise CloudError("Telegram 操作失败，请检查网络、登录信息或稍后重试；未自动重发验证码", 502) from None


async def _login(secret, action, code="", password=""):
    from telethon.errors import SessionPasswordNeededError
    client = _client(secret)
    try:
        await client.connect()
        if action == "code":
            sent = await client.send_code_request(secret["phone"])
            secret["phone_code_hash"] = sent.phone_code_hash
        elif action == "verify":
            try:
                await client.sign_in(phone=secret["phone"], code=code, phone_code_hash=secret["phone_code_hash"])
            except SessionPasswordNeededError:
                if not password:
                    return {**secret, "session": client.session.save(), "needs_password": True}
                await client.sign_in(password=password)
        elif action == "password":
            await client.sign_in(password=password)
        else:
            if not await client.is_user_authorized():
                raise CloudError("Telegram 会话未登录", 409)
        secret["session"] = client.session.save()
        return secret
    finally:
        await client.disconnect()


def login(account_id, body):
    action = body.get("action")
    expected = body.get("expected_revision")
    current = services.get(account_id, "telegram_reader", DEFAULT)
    if current["revision"] != expected:
        raise CloudError("Telegram 登录配置已变化，请刷新", 409)
    if action == "disconnect":
        patch = dict(DEFAULT)
    elif action in ("code", "import"):
        api_id, api_hash = str(body.get("api_id", "")), str(body.get("api_hash", ""))
        if not re.fullmatch(r"[1-9][0-9]{0,11}", api_id) or not re.fullmatch(r"[a-fA-F0-9]{32}", api_hash):
            raise CloudError("请填写 Telegram API ID 与32位 API Hash")
        secret = {"api_id": api_id, "api_hash": api_hash}
        if action == "code":
            phone = str(body.get("phone", ""))
            if not re.fullmatch(r"\+[0-9]{7,16}", phone):
                raise CloudError("手机号需包含国际区号，例如 +86…")
            secret["phone"] = phone
        else:
            session = body.get("session", "")
            if not isinstance(session, str) or not 100 < len(session) < 2000:
                raise CloudError("请填写有效的 Telethon StringSession")
            secret["session"] = session
        result = _run(_login(secret, "code" if action == "code" else "import"))
        patch = {"pending": credentials.seal(result), "expires": time.time()+600} if action == "code" else {"sealed": credentials.seal(result), "pending": "", "expires": 0}
    elif action == "verify":
        if not current["config"]["pending"] or current["config"]["expires"] < time.time():
            raise CloudError("验证码登录已过期，请重新获取", 409)
        secret = credentials.unseal(current["config"]["pending"])
        password, code = str(body.get("password", "")), str(body.get("code", ""))
        if len(password) > 256 or len(code) > 12:
            raise CloudError("验证码或两步验证密码过长")
        result = _run(_login(secret, "password" if secret.get("needs_password") else "verify", code, password))
        if result.get("needs_password") and not password:
            patch = {"pending": credentials.seal(result), "expires": time.time()+600}
        else:
            result.pop("needs_password", None)
            patch = {"sealed": credentials.seal(result), "pending": "", "expires": 0}
    else:
        raise CloudError("Telegram 登录操作无效")
    services.save(account_id, "telegram_reader", patch, expected, DEFAULT, lambda cfg, old: cfg)
    value = get(account_id)
    value["needs_password"] = bool(action == "verify" and result.get("needs_password") and not body.get("password"))
    return value


async def _messages(secret, rule, cursor, initialized):
    from telethon.utils import get_peer_id
    client = _client(secret)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise CloudError("Telegram 用户会话已失效，请重新登录", 409)
        address = rule["address"]
        entity = await client.get_entity(int(address) if address.startswith("-100") else address)
        if not getattr(entity, "broadcast", False):
            raise CloudError("目标不是可读频道", 409)
        # Initial zero means start at the current tip without importing old posts.
        options = {"limit": max(1, rule.get("history_limit", 0))} if not initialized else {"min_id": cursor, "reverse": True, "limit": 100}
        messages = [m async for m in client.iter_messages(entity, **options)]
        tip = max([cursor, *[m.id for m in messages]])
        if not initialized and not rule.get("history_limit", 0):
            return [], tip
        result = []
        for m in sorted(messages, key=lambda v: v.id):
            entities = [{"type": "text_link", "url": e.url} for e in (m.entities or []) if getattr(e, "url", None)]
            result.append({"message_id": m.id, "date": int(m.date.timestamp()), "chat": {"id": get_peer_id(entity)}, "text": m.message or "", "entities": entities})
        return result, tip
    finally:
        await client.disconnect()


def poll_one():
    from app.cloud import receiving
    from app import config
    now = time.time()
    with store.db() as con:
        row = con.execute("SELECT c.id,c.account_id,c.account_revision,c.revision,c.payload,s.cursor,s.initialized FROM cloud_channels c LEFT JOIN cloud_channel_cursors s ON s.id=c.id WHERE json_extract(c.payload,'$.enabled')=1 AND json_extract(c.payload,'$.mode')='user' AND COALESCE(s.next_poll,0)<=? ORDER BY COALESCE(s.next_poll,0) LIMIT 1", (now,)).fetchone()
    if not row:
        return
    rid, aid, arev, rev, raw, cursor, initialized = row
    rule = json.loads(raw)
    error = ""
    try:
        account = store.account(aid)
        cfg = services.get(aid, "telegram_reader", DEFAULT)
        if not account or account["revision"] != arev or cfg["account_revision"] != arev or not cfg["config"]["sealed"]:
            raise CloudError("账号授权或 Telegram 会话变化，请重新登录并保存频道规则", 409)
        messages, tip = _run(_messages(credentials.unseal(cfg["config"]["sealed"]), rule, cursor or 0, bool(initialized)))
        token_scope = hashlib.sha256((config.load_config().get("telegram_bot_token") or "").encode()).hexdigest()
        with store.db() as con:
            current = con.execute("SELECT revision,payload FROM cloud_channels WHERE id=?", (rid,)).fetchone()
            session = con.execute("SELECT revision FROM cloud_service_settings WHERE account_id=? AND kind='telegram_reader'", (aid,)).fetchone()
            account = con.execute("SELECT revision FROM cloud_accounts WHERE id=?", (aid,)).fetchone()
            if not current or current[0] != rev or not json.loads(current[1])["enabled"] or not session or session[0] != cfg["revision"] or not account or account[0] != arev:
                return
            for msg in messages:
                receiving.record_channel(con, rid, aid, arev, rev, rule, msg, token_scope)
            con.execute("INSERT INTO cloud_channel_cursors VALUES(?,?,1,?, '') ON CONFLICT(id) DO UPDATE SET cursor=excluded.cursor,initialized=1,next_poll=excluded.next_poll,last_error=''", (rid, tip, now+rule.get("poll_seconds", 60)))
        return
    except Exception as exc:
        error = str(exc) if isinstance(exc, CloudError) else "频道读取失败，请检查登录、权限和网络"
    with store.db() as con:
        con.execute("INSERT INTO cloud_channel_cursors VALUES(?,0,0,?,?) ON CONFLICT(id) DO UPDATE SET next_poll=excluded.next_poll,last_error=excluded.last_error", (rid, now+300, error))


def schedule():
    global _future
    if _future is None or _future.done():
        with store.db() as con:
            exists = con.execute("SELECT 1 FROM cloud_channels c LEFT JOIN cloud_channel_cursors s ON s.id=c.id WHERE json_extract(c.payload,'$.enabled')=1 AND json_extract(c.payload,'$.mode')='user' AND COALESCE(s.next_poll,0)<=? LIMIT 1", (time.time(),)).fetchone()
        if exists:
            _future = _POOL.submit(poll_one)
