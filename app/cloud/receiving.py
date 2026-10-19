"""Typed link intake, durable Telegram inbox and Bot API channel rules."""
import hashlib
import json
import re
import time
from urllib.parse import urlsplit, parse_qs, unquote, urlencode
from app.cloud import services, credentials, store, workflow, jobs, directories, recognition, file_filters, events
from app.cloud.provider115 import numeric, parse_share
from app.cloud.errors import CloudError, diagnostic
from app.domain import telegram_intake

DEFAULT = {"tg_enabled": False, "organize_after_transfer": False, "start_after": 0}
LINK = re.compile(r"https://(?:www\.)?(?:115\.com|115cdn\.com)/s/[A-Za-z0-9-]+(?:\?[^\s<>\"']*)?|magnet:\?[^\s<>\"']+|ed2k://\|file\|[^\r\n<>]+?\|/|115://[^\r\n<>]+", re.I)


def parse(value):
    value = value.strip()
    if not value or len(value) > 4096 or any(ord(c) < 32 for c in value):
        raise CloudError("资源链接格式无效")
    if value.startswith("https://"):
        code, password = parse_share(value)
        return {"kind": "share", "url": value, "code": code, "password": password, "name": "115 分享"}
    if value.lower().startswith("magnet:?"):
        parsed = parse_qs(urlsplit(value).query)
        xt = next((v for v in parsed.get("xt", []) if re.fullmatch(r"urn:btih:(?:[a-fA-F0-9]{40}|[A-Z2-7a-z]{32})", v)), None)
        if not xt:
            raise CloudError("磁力链接缺少有效 BTIH")
        # Only the canonical info hash and display name are sent; tracker URLs are unnecessary.
        name = (parsed.get("dn") or ["磁力资源"])[0][:180]
        return {"kind": "magnet", "url": "magnet:?"+urlencode({"xt": xt.lower(), "dn": name}), "name": name}
    if value.lower().startswith("ed2k://"):
        match = re.fullmatch(r"ed2k://\|file\|([^|]+)\|(\d{1,16})\|([0-9a-fA-F]{32})\|/", value, re.I)
        if not match or int(match[2]) <= 0:
            raise CloudError("ed2k 链接格式无效")
        return {"kind": "ed2k", "url": value, "name": unquote(match[1])[:180]}
    if value.lower().startswith("115://"):
        parts = value[6:].split("|")
        if len(parts) != 4 or not parts[1].isdigit() or not 0 < int(parts[1]) < 10**16 or any(not re.fullmatch(r"[a-fA-F0-9]{40}", p) for p in parts[2:]):
            raise CloudError("秒传应为 115://文件名|大小|SHA1|前128KiB的SHA1")
        name = unquote(parts[0])
        if recognition.safe_name(name) != name:
            raise CloudError("秒传文件名无效")
        return {"kind": "instant", "url": value, "name": name, "size": int(parts[1]), "sha1": parts[2].upper(), "preid": parts[3].upper()}
    raise CloudError("仅支持 115 分享、磁力、ed2k 和 115:// 秒传链接")


def extract(text, entities=None):
    values = LINK.findall(str(text)[:16000])
    values += [e["url"] for e in (entities or []) if e.get("type") == "text_link" and isinstance(e.get("url"), str) and LINK.fullmatch(e["url"])]
    values=list(dict.fromkeys(values))
    if len(values)>20:
        raise CloudError("单次最多接收20条不同资源链接，请分批提交",413)
    result, seen = [], set()
    password = re.search(r"(?:提取码|访问码|密码)\s*[:：]?\s*([a-zA-Z0-9]{1,12})", str(text))
    for value in values:
        item = parse(value)
        if item["kind"] == "share" and not item["password"] and password:
            item["password"] = password[1]
        fingerprint = hashlib.sha256(store.encode(item).encode()).hexdigest()
        if fingerprint not in seen:
            seen.add(fingerprint);result.append(item)
    return result


def get(account_id):
    return services.get(account_id, "receiving", DEFAULT)


def save(account_id, patch, expected):
    if set(patch)-{"tg_enabled", "organize_after_transfer"}:
        raise CloudError("接收配置字段无效")
    def validate(config, old):
        if any(type(config[k]) is not bool for k in ("tg_enabled", "organize_after_transfer")):
            raise CloudError("接收开关必须为布尔值")
        if config["organize_after_transfer"] and not directories.get(account_id)["directories"]["target"]:
            raise CloudError("请先保存归档目录，再开启转存后自动整理", 409)
        if config["tg_enabled"]:
            from app import config as main_config
            global_cfg = main_config.load_config()
            if not global_cfg.get("telegram_bot_token") or not global_cfg.get("telegram_allowed_users"):
                raise CloudError("请先在规则设置保存 Bot Token 和机器人白名单", 409)
            if not directories.get(account_id)["directories"]["transfer"]:
                raise CloudError("请先选择分享转存目录", 409)
            with store.db() as con:
                other = con.execute("SELECT 1 FROM cloud_service_settings s JOIN cloud_accounts a ON a.id=s.account_id WHERE s.kind='receiving' AND s.account_id!=? AND json_extract(s.payload,'$.tg_enabled')=1", (account_id,)).fetchone()
            if other:
                raise CloudError("另一个账号已开启 TG 接收，请先关闭，避免多账号重复接收", 409)
            if not old["config"]["tg_enabled"]:
                config["start_after"] = int(time.time())
        return config
    return services.save(account_id, "receiving", patch, expected, DEFAULT, validate)


def make_payload(account_id, items, source="web", target=None, organize=False):
    account = store.account(account_id)
    if not account:
        raise CloudError("当前账号不存在", 404)
    cfg = directories.get(account_id)
    roots = cfg["directories"]
    selected = {}
    for item in items:
        role = "transfer" if item["kind"] == "share" else item["kind"]
        directory = {"id": numeric(target)} if target is not None else roots[role]
        if not directory:
            raise CloudError("请先选择 "+{"transfer":"分享转存", "bot":"机器人分享", "magnet":"磁力", "ed2k":"ed2k", "instant":"秒传"}[role]+" 保存目录", 409)
        selected[item["kind"]] = directory["id"]
    if organize and not roots["target"]:
        raise CloudError("请先选择整理归档目录", 409)
    if organize:
        from app.cloud import pipeline
        pipeline.prepare()
    return {"account_id": account_id, "account_revision": account["revision"], "directory_revision": cfg["revision"], "sealed": credentials.seal(items), "targets": selected, "organize_target": roots["target"]["id"] if organize else None, "origin": source}


def preview(account_id, text, organize=False):
    items = extract(text)
    if not items:
        raise CloudError("没有找到支持的资源链接")
    payload = make_payload(account_id, items, organize=organize)
    workflow.verify_account(payload)
    pid = store.ident()
    with store.db() as con:
        con.execute("INSERT INTO cloud_plans VALUES(?,?,?,?,?,?)", (pid, account_id, payload["account_revision"], "intake_pending", store.encode(payload), time.time()+600))
    return {"id": pid, "organize_after_transfer": bool(organize), "items": [{"kind": i["kind"], "name": i["name"], "target_cid": payload["targets"][i["kind"]]} for i in items]}


def apply(plan_id, confirmed):
    if confirmed is not True:
        raise CloudError("请确认资源接收")
    with store.db() as con:
        record = con.execute("SELECT payload,state,expires FROM cloud_plans WHERE id=?", (plan_id,)).fetchone()
        if not record or record[1] != "intake_pending" or record[2] < time.time():
            raise CloudError("资源预览过期或已使用", 409)
        data = json.loads(record[0])
        con.execute("UPDATE cloud_plans SET state='queued' WHERE id=?", (plan_id,))
        return jobs.enqueue("intake", data, con)


def channel_address(value):
    value = str(value).strip()
    if value.startswith("https://t.me/s/"):
        value = "@"+value.removeprefix("https://t.me/s/").rstrip("/")
    if value.startswith("https://t.me/"):
        value = "@"+value.removeprefix("https://t.me/").rstrip("/")
    if not re.fullmatch(r"@[A-Za-z][A-Za-z0-9_]{4,31}|-100[0-9]{5,16}", value):
        raise CloudError("频道地址应为 https://t.me/名称、@名称 或 -100 开头频道 ID")
    return value.lower()


def channels(account_id):
    with store.db() as con:
        rows = con.execute("SELECT id,revision,payload FROM cloud_channels WHERE account_id=? ORDER BY updated DESC", (account_id,)).fetchall()
    with store.db() as con:
        statuses={r[0]:r[1] for r in con.execute("SELECT id,last_error FROM cloud_channel_cursors")}
    with store.db() as con:
        public = {r[0]: {"last_success": r[1], "last_check": r[2], "last_count": r[3],
                         "failures": r[4], "cursor": r[5], "next_poll": r[6]}
                  for r in con.execute("SELECT p.channel_id,p.last_success,p.last_check,p.last_count,p.failures,"
                                       "COALESCE(c.cursor,0),COALESCE(c.next_poll,0) "
                                       "FROM cloud_public_channel_status p LEFT JOIN cloud_channel_cursors c ON c.id=p.channel_id")}
    return [{"id": r[0], "revision": r[1], **json.loads(r[2]), "reader_status":statuses.get(r[0], ""),
             "public_status": public.get(r[0], {})} for r in rows]


def save_channel(account_id, value):
    allowed = {"id", "revision", "name", "address", "target_cid", "enabled", "include", "exclude", "regex", "tmdb_ids", "kinds", "countries", "genres", "organize", "mode", "history_limit", "poll_seconds", "rule_cards", "auto_transfer", "action", "command_prefix", "forward_target"}
    if not isinstance(value, dict) or set(value)-allowed:
        raise CloudError("频道配置字段无效")
    data = {"name":"", "mode":"bot", "auto_transfer":True, "action":"transfer", "command_prefix":"/strm", "forward_target":"self", "history_limit":20 if value.get("mode") in ("user", "public_web") else 0, "poll_seconds":300, "enabled":False, "include":[], "exclude":[], "regex":[], "tmdb_ids":[], "kinds":[], "countries":[], "genres":[], "organize":False, "rule_cards":[], **value}
    cid = data.pop("id", None) or store.ident();expected = data.pop("revision", 0)
    data["address"] = channel_address(data.get("address", ""));data["target_cid"] = numeric(data.get("target_cid", "")) if data.get("target_cid") else ""
    if data["action"] not in ("transfer", "share_strm", "forward"):
        raise CloudError("频道处理动作无效")
    try:
        telegram_intake.validate_relay(data["command_prefix"], data["forward_target"])
    except ValueError as exc:
        raise CloudError(str(exc)) from None
    transfer_action = data["action"] == "transfer" or (data["action"] == "forward" and data["forward_target"] == "self" and telegram_intake.command(data["command_prefix"]) == "/receive")
    if transfer_action and not data["target_cid"]:
        raise CloudError("转存动作需选择接收目录")
    if type(data["organize"]) is not bool:
        raise CloudError("频道整理开关必须为布尔值")
    # Legacy field is accepted but channel intake and organizer remain independent.
    data["organize"] = False
    if type(expected) is not int or not re.fullmatch(r"[a-f0-9]{32}",cid) or not isinstance(data["name"], str) or not 1 <= len(data["name"]) <= 80 or any(type(data[k]) is not bool for k in ("enabled", "organize", "auto_transfer")):
        raise CloudError("请填写频道名称和有效开关")
    for k in ("include", "exclude", "regex", "tmdb_ids", "kinds", "countries", "genres"):
        if not isinstance(data[k], list) or len(data[k]) > 100 or any(not isinstance(v, str) or not 0 < len(v) <= 160 for v in data[k]):
            raise CloudError("频道过滤配置无效")
    if set(data["kinds"])-{"movie", "tv"} or any(not re.fullmatch(r"(?:movie|tv):[0-9]{1,12}", v) for v in data["tmdb_ids"]) or any(not re.fullmatch(r"[A-Z]{2}", v) for v in data["countries"]) or any(not v.isdigit() for v in data["genres"]):
        raise CloudError("TMDB 类型、作品、地区或类型 ID 无效")
    try:
        for pattern in data["regex"]:re.compile(pattern)
    except re.error:
        raise CloudError("频道正则语法错误") from None
    cards=data['rule_cards']
    if not isinstance(cards,list) or len(cards)>100:
        raise CloudError('影视规则卡片无效')
    for card in cards:
        if not isinstance(card,dict) or set(card)-{'id','label','year','poster_path','resolution','dynamic_range','keywords'}:
            raise CloudError('影视规则卡片字段无效')
        if card.get('id') not in data['tmdb_ids'] or any(not isinstance(v,str) or len(v)>300 for v in card.values()):
            raise CloudError('影视规则卡片作品或字段无效')
        if card.get('year','') and not re.fullmatch(r'[12][0-9]{3}',card['year']):
            raise CloudError('规则年份需为四位年份')
        if card.get('resolution','') not in ('','2160p','1080p','720p') or card.get('dynamic_range','') not in ('','DV','HDR','SDR'):
            raise CloudError('规则分辨率或效果无效')
        if card.get('poster_path','') and not re.fullmatch(r'/[A-Za-z0-9_.-]+',card['poster_path']):
            raise CloudError('规则海报路径无效')
    account = store.account(account_id)
    if not account:raise CloudError("当前账号不存在",404)
    if transfer_action:
        provider = workflow.verify_account({"account_id": account_id, "account_revision": account["revision"]})
        workflow._directory(provider, data["target_cid"])
    if data["enabled"] and data["action"] == "forward" and data["forward_target"] != "self":
        from app.cloud import channel_reader
        if not channel_reader.get(account_id)["connected"]:
            raise CloudError("转发到其他机器人需要先登录 Telegram 用户会话", 409)
    if data["organize"] and not directories.get(account_id)["directories"]["target"]:
        raise CloudError("自动整理需先保存整理归档目录",409)
    if data["mode"] not in ("bot", "user", "public_web") or type(data["history_limit"]) is not int or not 0 <= data["history_limit"] <= 1000 or type(data["poll_seconds"]) is not int or not 30 <= data["poll_seconds"] <= 3600:
        raise CloudError("频道模式或历史条数（0–1000）/轮询秒数（30–3600）无效")
    if data["mode"] == "public_web" and (not data["address"].startswith("@") or data["history_limit"] > 100 or data["poll_seconds"] < 300):
        raise CloudError("公开网页模式仅支持 @公开用户名、0–100 条历史消息、最短 300 秒轮询", 409)
    if data["mode"] == "bot" and data["history_limit"]:
        raise CloudError("历史读取需要选择用户会话模式并登录 Telegram", 409)
    if data["enabled"] and data["mode"] == "user":
        from app.cloud import channel_reader
        if not channel_reader.get(account_id)["connected"]:
            raise CloudError("请先登录 Telegram 用户会话", 409)
    if data["enabled"] and data["mode"] == "bot":
        from app import bot, config as main_config
        token=main_config.load_config().get("telegram_bot_token") or ""
        if not token:raise CloudError("请先在规则设置配置 Telegram Bot",409)
        try:
            response=bot._api(token,"getChat",{"chat_id":data["address"]},timeout=10)
            me=bot._api(token,"getMe",{},timeout=10)
            info=response.get("result") or {}
            if response.get("ok") is not True or info.get("type")!="channel" or not me.get("ok"):raise ValueError()
            membership=bot._api(token,"getChatMember",{"chat_id":info["id"],"user_id":me["result"]["id"]},timeout=10)
            if not membership.get("ok") or (membership.get("result") or {}).get("status") not in ("administrator","creator"):
                raise ValueError()
            data["chat_id"]=str(info["id"])
        except Exception:
            raise CloudError("频道不可读，请核对地址并将 Bot 添加为该频道管理员",409) from None
    with store.db() as con:
        owner=con.execute("SELECT account_id FROM cloud_channels WHERE id=?",(cid,)).fetchone()
        current_account=con.execute("SELECT revision FROM cloud_accounts WHERE id=?",(account_id,)).fetchone()
        if (owner and owner[0]!=account_id) or not current_account or current_account[0]!=account["revision"]:
            raise CloudError("账号或频道身份变化，请重新保存",409)
        old = con.execute("SELECT revision,payload FROM cloud_channels WHERE id=? AND account_id=?", (cid,account_id)).fetchone()
        if (old[0] if old else 0) != expected:
            raise CloudError("频道配置已变化，请刷新重试",409)
        if old and (json.loads(old[1]).get("address") != data["address"] or json.loads(old[1]).get("mode","bot") != data["mode"]):
            con.execute("DELETE FROM cloud_channel_cursors WHERE id=?",(cid,))
            con.execute("DELETE FROM cloud_public_channel_status WHERE channel_id=?",(cid,))
        data["start_after"] = json.loads(old[1]).get("start_after",0) if old and json.loads(old[1])["enabled"] and data["enabled"] else int(time.time())
        con.execute("INSERT INTO cloud_channels VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,payload=excluded.payload,account_revision=excluded.account_revision,updated=excluded.updated", (cid,account_id,account["revision"],expected+1,store.encode(data),time.time()))
    return {"id":cid,"revision":expected+1,**data}


def allowed_message(text, rule):
    folded = text.casefold()
    if rule["include"] and not any(v.casefold() in folded for v in rule["include"]):
        return False, "未命中包含关键词"
    if any(v.casefold() in folded for v in rule["exclude"]):
        return False, "命中排除关键词"
    if rule["regex"] and "message" not in file_filters.regex_matches(rule["regex"], [{"id":"message", "name":text[:8000]}]):
        return False, "未命中正则"
    if any(rule[k] for k in ("tmdb_ids", "kinds", "countries", "genres")):
        match = re.search(r"(?:themoviedb\.org/|tmdb[:： /-]*)(movie|tv)[/: -]*(\d{1,12})",text,re.I)
        if match:
            kind, mid = match[1].lower(), match[2]
        else:
            remaining=LINK.sub("", text).strip().splitlines()
            if not remaining:raise CloudError("消息缺少可识别作品名或 TMDB 链接，未接收",409)
            name = remaining[0][:180]
            media = recognition.recognize(name+".mkv")
            kind, mid = media["kind"], media["tmdb_id"]
        if rule["kinds"] and kind not in rule["kinds"]:return False,"作品类型未匹配"
        if rule["tmdb_ids"] and kind+":"+mid not in rule["tmdb_ids"]:return False,"TMDB 作品未匹配"
        cards=[c for c in rule.get('rule_cards',[]) if c['id']==kind+':'+mid]
        if cards and not any(card_matches(c,text) for c in cards):return False,'影视卡片年份、分辨率、效果或关键词未匹配'
        if rule["countries"] or rule["genres"]:
            try:
                detail = recognition.tmdb().get("/"+kind+"/"+mid)
            except Exception:
                raise CloudError("TMDB 筛选查询失败，未接收",502) from None
            countries = set(detail.get("origin_country") or []) | {r.get("iso_3166_1") for r in detail.get("production_countries",[])}
            genres = {str(r.get("id")) for r in detail.get("genres",[])}
            if rule["countries"] and not countries.intersection(rule["countries"]):return False,"地区未匹配"
            if rule["genres"] and not genres.intersection(rule["genres"]):return False,"影视类型未匹配"
    return True, "已匹配"


def card_matches(card,text):
    folded=text.casefold()
    if card.get('year') and not re.search(r'(?<![0-9])'+card['year']+r'(?![0-9])',text):return False
    resolutions={'2160p':r'2160p|(?<![a-z0-9])4k(?![a-z0-9])','1080p':r'1080[pi]','720p':r'720p'}
    if card.get('resolution') and not re.search(resolutions[card['resolution']],folded):return False
    effects={'DV':r'(?<![a-z0-9])(?:dv|dovi)(?![a-z0-9])|dolby[ ._-]*vision|杜比视界','HDR':r'hdr|hlg|杜比视界|dolby[ ._-]*vision|(?<![a-z0-9])(?:dv|dovi)(?![a-z0-9])','SDR':r'(?<![a-z0-9])sdr(?![a-z0-9])'}
    if card.get('dynamic_range') and not re.search(effects[card['dynamic_range']],folded):return False
    return all(v.casefold() in folded for v in card.get('keywords','').split())


def capture(msg, token_scope="", channel=False, authorized=False):
    """Fast durable capture before Bot offset advances; no remote I/O here."""
    if not isinstance(msg.get("message_id"),int) or not isinstance((msg.get("chat") or {}).get("id"),int) or type(msg.get("date")) is not int:return []
    normalized = telegram_intake.content(msg, allow_reply=not channel)
    text, entities = normalized["text"], normalized["entities"]
    has_resource = bool(LINK.search(text) or entities)
    if not channel and not has_resource:return []
    chat = msg.get("chat") or {};targets=[]
    with store.db() as con:
        if channel:
            rows=con.execute("SELECT c.id,c.account_id,c.account_revision,c.revision,c.payload FROM cloud_channels c JOIN cloud_accounts a ON a.id=c.account_id AND a.revision=c.account_revision WHERE json_extract(c.payload,'$.enabled')=1").fetchall()
            addresses={str(chat.get("id", "")),"@"+str(chat.get("username", "")).lower()}
            for rid,aid,arev,rev,raw in rows:
                cfg=json.loads(raw)
                if cfg.get("mode", "bot") != "bot":continue
                if not has_resource and not (cfg.get("action") == "forward" and text.strip()):continue
                if (cfg.get("chat_id")==str(chat.get("id")) if cfg.get("chat_id") else cfg["address"] in addresses) and msg.get("date",0)>=cfg["start_after"]:
                    targets.append((aid,arev,{"channel_id":rid,"channel_revision":rev,"rule":cfg},rid))
        elif authorized:
            rows=con.execute("SELECT s.account_id,s.account_revision,s.revision,s.payload FROM cloud_service_settings s JOIN cloud_accounts a ON a.id=s.account_id AND a.revision=s.account_revision WHERE s.kind='receiving' AND json_extract(s.payload,'$.tg_enabled')=1").fetchall()
            for aid,arev,rev,raw in rows:
                if msg.get("date",0)>=json.loads(raw).get("start_after",0):targets.append((aid,arev,{"receiving_revision":rev},"private"))
    sealed=credentials.seal(normalized) if targets else ""
    ids=[]
    with store.db() as con:
        for aid,arev,extra,scope in targets:
            account = con.execute("SELECT revision FROM cloud_accounts WHERE id=?", (aid,)).fetchone()
            if not account or account[0] != arev:
                continue
            key=hashlib.sha256(store.encode([token_scope,chat.get("id"),msg.get("message_id"),scope]).encode()).hexdigest()
            iid=store.ident()
            con.execute("INSERT OR IGNORE INTO cloud_inbox(id,event_key,account_id,source,payload,state,created) VALUES(?,?,?,?,?,'queued',?)",(iid,key,aid,"channel" if channel else "telegram",store.encode({"account_id":aid,"account_revision":arev,"sealed_message":sealed,**extra}),time.time()))
            row=con.execute("SELECT id FROM cloud_inbox WHERE event_key=?",(key,)).fetchone()
            ids.append(row[0])
    return ids


def schedule():
    with store.db() as con:
        con.execute("UPDATE cloud_inbox SET state='processed',result=? WHERE state='queued' "
                    "AND NOT EXISTS (SELECT 1 FROM cloud_accounts a WHERE a.id=cloud_inbox.account_id)",
                    (store.encode({'errors': [{'reason': '账号已断开，未提交接收任务'}]}),))
        rows=con.execute("SELECT id,account_id,source,payload FROM cloud_inbox WHERE state='queued' ORDER BY created LIMIT 20").fetchall()
        for iid,aid,source,raw in rows:
            jid=jobs.enqueue("channel" if source=="channel" else "intake",{**json.loads(raw),"inbox_id":iid},con)
            con.execute("UPDATE cloud_inbox SET state='submitted',job_id=? WHERE id=?",(jid,iid))


def inbox(account_id, offset=0):
    with store.db() as con:
        rows=con.execute("SELECT id,source,state,job_id,result,created FROM cloud_inbox WHERE account_id=? ORDER BY created DESC LIMIT 50 OFFSET ?",(account_id,offset)).fetchall()
    result = []
    for r in rows:
        value = json.loads(r[4])
        children = []
        for jid in value.get('child_jobs', [])[:100]:
            job = store.job(jid)
            if job and job['state'] in ('failed', 'uncertain', 'needs_review'):
                errors = job.get('result', {}).get('errors', [])
                children.extend({**e, 'job_id': jid} for e in errors if isinstance(e, dict))
                if not errors:
                    children.append({'job_id': jid, 'reason': job['message'], 'stage_label': '后续处理'})
        if children:
            value['child_errors'] = children[:100]
        result.append({'id':r[0], 'source':r[1], 'state':r[2], 'job_id':r[3], 'result':value,
                       'created':r[5], 'job':store.job(r[3]) if r[3] else None})
    return result


def verify_dispatch(payload,data):
    account=store.account(payload["account_id"])
    if not account or account["revision"]!=payload["account_revision"] or data["account_revision"]!=payload["account_revision"]:
        raise CloudError("账号凭据在接收前变化，已停止",409)
    if directories.get(data["account_id"])["revision"]!=data["directory_revision"]:
        raise CloudError("接收目录配置变化，已停止",409)
    if payload.get("channel_id"):
        with store.db() as con:row=con.execute("SELECT revision,payload FROM cloud_channels WHERE id=? AND account_id=?",(payload["channel_id"],payload["account_id"])).fetchone()
        if not row or row[0]!=payload["channel_revision"] or not json.loads(row[1])["enabled"]:
            raise CloudError("频道已暂停或规则变化，已停止接收",409)
    elif payload.get("receiving_revision") is not None:
        cfg=get(payload["account_id"])
        if cfg["revision"]!=payload["receiving_revision"] or not cfg["config"]["tg_enabled"]:
            raise CloudError("TG 接收配置变化，已停止",409)


def verify_source(payload):
    account = store.account(payload['account_id'])
    if not account or account['revision'] != payload['account_revision']:
        raise CloudError('账号授权已变化，已停止接收', 409)
    if payload.get('channel_id'):
        with store.db() as con:
            row = con.execute('SELECT revision,payload FROM cloud_channels WHERE id=? AND account_id=?', (payload['channel_id'], payload['account_id'])).fetchone()
        if not row or row[0] != payload['channel_revision'] or not json.loads(row[1])['enabled']:
            raise CloudError('频道已暂停或规则变化，已停止接收', 409)
    elif payload.get('receiving_revision') is not None:
        cfg = get(payload['account_id'])
        if cfg['revision'] != payload['receiving_revision'] or not cfg['config']['tg_enabled']:
            raise CloudError('TG 接收配置变化，已停止', 409)


def run(job_id,payload):
    result={"accepted":[],"errors":[],"child_jobs":[]}
    stage = "prepare"
    try:
        verify_source(payload)
        jobs.checkpoint(job_id, result)
        if payload.get("inbox_id"):
            message=credentials.unseal(payload["sealed_message"])
            if payload.get("channel_id"):
                with store.db() as con:rule=con.execute("SELECT revision,payload FROM cloud_channels WHERE id=? AND account_id=?",(payload["channel_id"],payload["account_id"])).fetchone()
                if not rule or rule[0]!=payload["channel_revision"] or not json.loads(rule[1])["enabled"]:raise CloudError("频道已暂停或规则变化，请人工检查原消息",409)
                cfg=json.loads(rule[1]);filter_text=message["text"]+"\n"+"\n".join(e.get("url","") for e in message["entities"] if "themoviedb.org/" in e.get("url",""));allowed,reason=allowed_message(filter_text,cfg)
                if not allowed:
                    result["filtered"]=[{"name":cfg["name"],"reason":reason}]
                    return result
                if not cfg.get("auto_transfer", True):
                    result["observed"] = [{"channel": cfg["name"], "message": events.clean(message["text"][:500]),
                                            "reason": "自动处理已关闭；仅记录消息"}]
                    return result
                source,target,organize="channel",cfg.get("target_cid"),False
                result['channel'] = cfg['name']
                result['action'] = cfg.get('action', 'transfer')
                if cfg.get('action') == 'forward' and cfg.get('forward_target', 'self') != 'self':
                    from app.cloud import channel_forwarding
                    stage = 'forward'
                    jobs.progress(job_id, 1, 0, '准备转发命令消息')
                    accepted = channel_forwarding.run(job_id, payload, cfg, message, lambda: verify_source(payload))
                    result['accepted'].append(accepted)
                    jobs.progress(job_id, 1, 1, '转发消息已确认发送')
                    return result
            else:
                cfg=get(payload["account_id"])
                if cfg["revision"]!=payload["receiving_revision"] or not cfg["config"]["tg_enabled"]:raise CloudError("TG 接收已关闭或配置变化",409)
                source,target,organize="telegram",None,bool(cfg["config"].get("organize_after_transfer", False))
            items=extract(message["text"],message["entities"])
            if payload.get("channel_id") and cfg.get("mode") == "public_web":
                ignored = [i for i in items if i["kind"] != "share"]
                if ignored:
                    result.setdefault("filtered", []).extend({"name":i["name"], "reason":"公开网页首版仅允许自动转存 115 分享"} for i in ignored)
                items = [i for i in items if i["kind"] == "share"]
                if not items:
                    return result
            if not items:raise CloudError("消息中没有支持的资源链接")
            direct_share = message.get('share_direct') if source == 'telegram' else cfg.get('action') == 'share_strm' or (cfg.get('action') == 'forward' and cfg.get('forward_target', 'self') == 'self' and telegram_intake.command(cfg.get('command_prefix', '/strm')) in telegram_intake.SHARE_COMMANDS)
            if direct_share:
                if any(i["kind"] != "share" for i in items):
                    raise CloudError("/strm 仅接受115分享链接，不提交转存或云下载")
                from app.cloud import share_library
                for index, item in enumerate(items):
                    jobs.checkpoint(job_id, result)
                    verify_source(payload)
                    stage = "read_share"
                    first=workflow.preview_share(payload["account_id"],item["code"],item["password"])
                    if first["has_more"] or not first["items"]:
                        raise CloudError("分享根目录为空或超过100项，请在Web分批选择",409)
                    stage = "generate_share_strm"
                    plan=share_library.preview(first["preview_id"],[r["id"] for r in first["items"]],{})
                    def direct(plan=plan):
                        verify_source(payload)
                        jobs.checkpoint(job_id, result)
                        return {"job_id":share_library.apply(plan["id"],True),"kind":"share_strm","name":"分享库免转存","completed":False}
                    accepted=jobs.step(job_id,"share-direct:"+str(index),{"kind":"share_strm"},direct)
                    result["child_jobs"].append(accepted["job_id"]);result["accepted"].append(accepted)
                    jobs.progress(job_id, len(items), index+1, "分享 STRM 任务已提交")
                return result
            data=make_payload(payload["account_id"],items,source,target,organize)
        else:
            data=payload;items=credentials.unseal(data["sealed"])
        provider=workflow.verify_account(payload)
        if directories.get(data["account_id"])["revision"]!=data["directory_revision"]:raise CloudError("保存目录已变化，请重新预览资源",409)
        for index,item in enumerate(items):
            jobs.checkpoint(job_id, result)
            stage = "prepare"
            verify_dispatch(payload,data)
            target=data["targets"][item["kind"]];workflow._directory(provider,target)
            if item["kind"]=="share":
                # Enqueue exactly once; the child owns receive receipts and optional organization.
                stage = "read_share"
                first=workflow.preview_share(data["account_id"],item["code"],item["password"])
                if first["has_more"] or not first["items"]:raise CloudError("分享根目录为空或超过100项，请在分享浏览器分批选择",409)
                def transfer():
                    verify_dispatch(payload,data)
                    jid=workflow.transfer_submit(first["preview_id"],[r["id"] for r in first["items"]],target,True,data["organize_target"],True)
                    return {"job_id":jid,"kind":"share","name":"115 分享","completed":False}
                stage = "submit_transfer"
                accepted=jobs.step(job_id,"resource:"+str(index),{"kind":"share","target_cid":target},transfer)
                result["child_jobs"].append(accepted["job_id"])
            elif item["kind"]=="instant":
                stage = "submit_transfer"
                accepted=jobs.step(job_id,"resource:"+str(index),{"kind":"instant","target_cid":target},lambda:provider.instant_receive(item,target))
            else:
                stage = "submit_transfer"
                accepted=jobs.step(job_id,"resource:"+str(index),{"kind":item["kind"],"target_cid":target},lambda:provider.offline_add(item["url"],target))
            result["accepted"].append({"kind":item["kind"],"name":item["name"],"target_cid":target,**accepted})
            jobs.progress(job_id,len(items),index+1,"资源请求已提交",item["name"])
        return result
    except jobs.JobStopped:
        result['stopped'] = True
        raise jobs.JobStopped(result)
    except CloudError as error:
        item = diagnostic(error, stage)
        result['errors'].append(item)
        error.stage = item['stage']
        error.partial_result=result
        events.emit('channel' if payload.get('channel_id') else 'transfer', item['stage_label']+'失败：'+item['reason'], job_id, level='WARNING', details=item)
        raise
    except Exception:
        error = CloudError('接收处理异常，请查看步骤记录；已提交操作不会自动重发', 502, stage=stage)
        result['errors'].append(diagnostic(error, stage))
        error.partial_result = result
        raise error from None
    finally:
        if payload.get("inbox_id"):
            with store.db() as con:con.execute("UPDATE cloud_inbox SET state='processed',result=? WHERE id=?",(store.encode(result),payload["inbox_id"]))


def record_channel(con, rid, aid, arev, rev, rule, msg, token_scope):
    message = telegram_intake.content(msg)
    text, entities = message["text"], message["entities"]
    if not LINK.search(text) and not any(e.get("type")=="text_link" for e in entities) and not (rule.get("action") == "forward" and text.strip()):
        return
    key = hashlib.sha256(store.encode([token_scope,msg["chat"]["id"],msg["message_id"],rid]).encode()).hexdigest()
    payload = {"account_id":aid,"account_revision":arev,"channel_id":rid,"channel_revision":rev,"rule":rule,
               "sealed_message":credentials.seal({"text":text,"entities":entities})}
    con.execute("INSERT OR IGNORE INTO cloud_inbox(id,event_key,account_id,source,payload,state,created) VALUES(?,?,?,?,?,'queued',?)", (store.ident(),key,aid,"channel",store.encode(payload),time.time()))


def receive(account_id, text, expected_revision, request_id, confirmed=False, organize=False):
    """One explicit submit, idempotent across retries; share/ed2k/magnet route independently."""
    if confirmed is not True:
        raise CloudError('请确认接收资源')
    if not re.fullmatch(r'[a-f0-9]{32}', request_id):
        raise CloudError('提交标识无效')
    if type(organize) is not bool:
        raise CloudError('自动整理开关无效')
    intent = {'kind':'resources','text':text,'revision':expected_revision}
    if organize:
        intent['organize'] = True
    def prior(con):
        row = con.execute('SELECT sealed,job_id FROM cloud_transfer_requests WHERE account_id=? AND request_id=?', (account_id, request_id)).fetchone()
        if row:
            if credentials.unseal(row[0]) != intent:
                raise CloudError('此提交标识已用于其他资源',409)
            return row[1]
    with store.db() as con:
        old = prior(con)
    if old:
        return old
    items = extract(text)
    if not items:
        raise CloudError('没有找到支持的资源链接')
    payload = make_payload(account_id,items,organize=organize)
    if payload['directory_revision'] != expected_revision:
        raise CloudError('保存目录已变化，请刷新后重新提交',409)
    workflow.verify_account(payload)
    with store.db() as con:
        old = prior(con)
        if old:
            return old
        directory = con.execute('SELECT revision FROM cloud_directory_settings WHERE account_id=?',(account_id,)).fetchone()
        account = con.execute('SELECT revision FROM cloud_accounts WHERE id=?',(account_id,)).fetchone()
        if not directory or directory[0] != expected_revision or not account or account[0] != payload['account_revision']:
            raise CloudError('账号或目录配置在提交期间变化，请重新提交',409)
        jid = jobs.enqueue('intake',payload,con)
        con.execute('INSERT INTO cloud_transfer_requests VALUES(?,?,?,?,?)',(account_id,request_id,credentials.seal(intent),jid,time.time()))
    return jid


def test_channel(account_id, channel_id, text):
    rule = next((r for r in channels(account_id) if r['id'] == channel_id), None)
    if not rule:
        raise CloudError('频道规则不存在', 404)
    matched, reason = allowed_message(text, rule)
    result = {'matched': matched, 'reason': reason, 'action': rule.get('action', 'transfer')}
    if rule.get('action') == 'forward':
        try:
            result['preview_text'] = telegram_intake.relay_text(rule.get('command_prefix', '/strm'), {'text':text,'entities':[]})
        except ValueError as error:
            result['matched'] = False
            result['reason'] = str(error)
        result['target'] = rule.get('forward_target', 'self')
        result['note'] = '仅预览，不发送消息，不生成或转存资源'
    return result
