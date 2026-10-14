"""Account-scoped directory selections, validated remotely before saving."""

import json
import time
from app.cloud import store
from app.cloud.provider115 import for_account, numeric
from app.cloud.errors import CloudError

ROLES = ("source", "target", "transfer", "unknown", "strm")


def get(account_id):
    if not store.account(account_id):
        raise CloudError("当前账号不存在", 404)
    with store.db() as con:
        row = con.execute("SELECT payload,revision FROM cloud_directory_settings WHERE account_id=?", (account_id,)).fetchone()
    return {"directories": {**dict.fromkeys(ROLES), **(json.loads(row[0]) if row else {})}, "revision": row[1] if row else 0}


def save(account_id, patch, expected_revision):
    if not isinstance(patch, dict) or not patch or set(patch) - set(ROLES):
        raise CloudError("目录配置字段无效")
    account = store.account(account_id)
    if not account:
        raise CloudError("当前账号不存在", 404)
    provider = for_account(account_id)
    provider.profile()
    verified = {}
    for role, value in patch.items():
        if value is None:
            verified[role] = None
            continue
        cid = numeric(value)
        if cid == "0":
            verified[role] = {"id": "0", "name": "根目录"}
        else:
            item = provider.info(cid)
            if not item["is_dir"] or item["id"] != cid:
                raise CloudError("请选择实际的 115 目录", 409)
            verified[role] = {"id": cid, "name": item["name"]}
    with store.db() as con:
        current = con.execute("SELECT uid,revision FROM cloud_accounts WHERE id=?", (account_id,)).fetchone()
        if not current or current[0] != account["uid"] or current[1] != account["revision"]:
            raise CloudError("账号凭证已变化，请重新选择目录", 409)
        row = con.execute("SELECT payload,revision FROM cloud_directory_settings WHERE account_id=?", (account_id,)).fetchone()
        revision = row[1] if row else 0
        if expected_revision != revision:
            raise CloudError("目录配置已在其他页面更新，请刷新后重试", 409)
        values = {**dict.fromkeys(ROLES), **(json.loads(row[0]) if row else {}), **verified}
        if values["source"] and values["target"] and values["source"]["id"] == values["target"]["id"]:
            raise CloudError("来源和归档不能是同一目录", 409)
        con.execute("INSERT INTO cloud_directory_settings VALUES(?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET payload=excluded.payload,revision=excluded.revision,updated=excluded.updated", (account_id,store.encode(values),revision+1,time.time()))
    return {"directories": values, "revision": revision+1}
