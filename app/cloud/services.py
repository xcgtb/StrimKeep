"""CAS-protected per-account service configuration; secrets are never returned."""
import json
import time
from app.cloud import store, workflow
from app.cloud.errors import CloudError


def get(account_id, kind, defaults):
    if not store.account(account_id):
        raise CloudError("当前账号不存在", 404)
    with store.db() as con:
        row = con.execute("SELECT payload,revision,account_revision FROM cloud_service_settings WHERE account_id=? AND kind=?", (account_id, kind)).fetchone()
    return {"config": {**defaults, **(json.loads(row[0]) if row else {})}, "revision": row[1] if row else 0, "account_revision": row[2] if row else store.account(account_id)["revision"]}


def save(account_id, kind, config, expected, defaults, validate):
    original = get(account_id, kind, defaults)
    if original["revision"] != expected:
        raise CloudError("配置已在其他页面更新，请刷新后重试", 409)
    account = store.account(account_id)
    workflow.verify_account({"account_id": account_id, "account_revision": account["revision"]})
    result = validate({**original["config"], **config}, original)
    with store.db() as con:
        row = con.execute("SELECT revision FROM cloud_service_settings WHERE account_id=? AND kind=?", (account_id, kind)).fetchone()
        current = con.execute("SELECT revision FROM cloud_accounts WHERE id=?", (account_id,)).fetchone()
        if (row[0] if row else 0) != expected or not current or current[0] != account["revision"]:
            raise CloudError("账号或配置变化，请刷新后重试", 409)
        if kind == "receiving" and result.get("tg_enabled") and con.execute("SELECT 1 FROM cloud_service_settings WHERE kind='receiving' AND account_id!=? AND json_extract(payload,'$.tg_enabled')=1",(account_id,)).fetchone():
            raise CloudError("另一个账号已开启 TG 接收，请先关闭",409)
        con.execute("INSERT INTO cloud_service_settings VALUES(?,?,?,?,?,?) ON CONFLICT(account_id,kind) DO UPDATE SET payload=excluded.payload,revision=excluded.revision,account_revision=excluded.account_revision,updated=excluded.updated", (account_id, kind, store.encode(result), expected+1, account["revision"], time.time()))
    return get(account_id, kind, defaults)
