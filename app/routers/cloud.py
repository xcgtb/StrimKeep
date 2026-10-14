"""Cloud workbench API; same administrator session and CSRF as StrimKeep."""

import hashlib
import json
import time
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import Response as BinaryResponse
from pydantic import BaseModel, Field
from app.routers.deps import auth, WEB_PASSWORD, SESSION_COOKIE
from app.cloud import (
    accounts,
    store,
    settings,
    workflow,
    jobs,
    recognition,
    strm,
    emby_proxy,
)
from app.cloud.provider115 import for_account, numeric
from app.cloud.errors import CloudError


def require_password():
    if not WEB_PASSWORD:
        raise HTTPException(403, "网盘账号功能需要先配置管理密码")


router = APIRouter(
    prefix="/api/cloud", dependencies=[Depends(auth), Depends(require_password)]
)
play_router = APIRouter(prefix="/api/cloud")


def owner(request):
    value = (
        request.cookies.get(SESSION_COOKIE)
        or request.headers.get("authorization")
        or ""
    )
    return hashlib.sha256(value.encode()).hexdigest()


def call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except CloudError as error:
        raise HTTPException(error.status, str(error)) from None


class QRInput(BaseModel):
    device: str = Field(default="alipaymini", max_length=20)
    mode: Literal["cookie", "open"] = "cookie"
    account_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")


class AccountInput(BaseModel):
    account_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class ShareInput(AccountInput):
    link: str = Field(min_length=6, max_length=2000)
    password: str = Field(default="", max_length=12)
    cid: str = Field(default="0", pattern=r"^[0-9]{1,25}$")
    offset: int = Field(default=0, ge=0, le=100000)


class TransferInput(BaseModel):
    preview_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    file_ids: list[str] = Field(min_length=1, max_length=100)
    target_cid: str = Field(pattern=r"^[0-9]{1,25}$")
    organize_target: str | None = None
    generate: bool = True
    confirmed: bool = False


class MediaChoice(BaseModel):
    kind: Literal["movie", "tv"]
    tmdb_id: str = Field(pattern=r"^[0-9]{1,12}$")
    season: int | None = Field(default=None, ge=0, le=99)
    episode: int | None = Field(default=None, ge=1, le=999)


class PlanInput(AccountInput):
    source_cid: str = Field(pattern=r"^[0-9]{1,25}$")
    target_cid: str = Field(pattern=r"^[0-9]{1,25}$")
    overrides: dict[str, MediaChoice] = Field(default_factory=dict, max_length=2000)


class ApplyInput(BaseModel):
    plan_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    confirmed: bool = False
    generate: bool = True


class SyncInput(AccountInput):
    source_cid: str = Field(pattern=r"^[0-9]{1,25}$")
    confirmed: bool = False
    overrides: dict[str, MediaChoice] = Field(default_factory=dict, max_length=2000)


class RuleInput(PlanInput):
    name: str = Field(min_length=1, max_length=100)
    enabled: bool = False
    interval_seconds: int = Field(default=3600, ge=300, le=604800)


@router.get("/status")
def status(response: Response):
    response.headers["Cache-Control"] = "no-store"
    return {
        "status": "success",
        "accounts": call(accounts.describe),
        "proxy": call(emby_proxy.status),
        "pending": sum(
            x["state"] in ("queued", "running", "uncertain", "needs_review")
            for x in call(store.jobs)
        ),
    }


@router.post("/qr")
def start_qr(body: QRInput, request: Request):
    return {
        "status": "success",
        **call(accounts.start, owner(request), body.device, body.mode, body.account_id),
    }


@router.get("/qr/{flow_id}/image")
def qr_image(flow_id: str, request: Request):
    return BinaryResponse(
        call(accounts.image, flow_id, owner(request)),
        media_type="image/png",
        headers={"Cache-Control": "no-store"},
    )


@router.post("/qr/{flow_id}/poll")
def poll_qr(flow_id: str, request: Request):
    return {"status": "success", **call(accounts.poll, flow_id, owner(request))}


@router.delete("/qr/{flow_id}")
def cancel_qr(flow_id: str, request: Request):
    call(accounts.cancel, flow_id, owner(request))
    return {"status": "success"}


@router.post("/accounts/{account_id}/check")
def check_account(account_id: str):
    provider = call(for_account, account_id)
    return {"status": "success", "profile": call(provider.profile)}


@router.post("/accounts/{account_id}/open/check")
def check_open(account_id: str):
    from app.cloud import open115

    provider = call(open115.for_account, account_id)
    return {"status": "success", "profile": call(provider.profile)}


@router.delete("/accounts/{account_id}")
def disconnect(account_id: str):
    with store.db() as con:
        active = con.execute(
            "SELECT 1 FROM cloud_jobs j WHERE state IN ('queued','running') AND "
            "(json_extract(payload,'$.account_id')=? OR EXISTS (SELECT 1 FROM cloud_plans p "
            "WHERE p.id=json_extract(j.payload,'$.plan_id') AND p.account_id=?))",
            (account_id, account_id),
        ).fetchone()
        if active:
            raise HTTPException(409, "账号有未完成任务，请先处理任务")
        con.execute("DELETE FROM cloud_accounts WHERE id=?", (account_id,))
        con.execute(
            "UPDATE cloud_assets SET state='disconnected' WHERE account_id=?",
            (account_id,),
        )
        con.execute(
            "UPDATE cloud_rules SET enabled=0 WHERE json_extract(payload,'$.account_id')=?",
            (account_id,),
        )
    return {"status": "success"}


@router.get("/files")
def files(account_id: str, cid: str = "0", offset: int = Query(0, ge=0, le=100000)):
    provider = call(for_account, account_id)
    return {"status": "success", **call(provider.page, cid, offset)}


@router.post("/shares/preview")
def share_preview(body: ShareInput):
    return {
        "status": "success",
        **call(
            workflow.preview_share,
            body.account_id,
            body.link,
            body.password,
            body.cid,
            body.offset,
        ),
    }


@router.post("/transfers", status_code=202)
def transfer(body: TransferInput):
    job_id = call(
        workflow.transfer_submit,
        body.preview_id,
        body.file_ids,
        body.target_cid,
        body.confirmed,
        body.organize_target,
        body.generate,
    )
    return {"status": "success", "job_id": job_id}


@router.get("/tmdb")
def search(
    query: str = Query(min_length=2, max_length=120),
    kind: str = "movie",
    year: str = "",
):
    return {"status": "success", "items": call(recognition.search, query, kind, year)}


@router.post("/plans")
def create_plan(body: PlanInput):
    return {
        "status": "success",
        "plan": call(
            workflow.plan,
            body.account_id,
            body.source_cid,
            body.target_cid,
            body.model_dump()["overrides"],
        ),
    }


@router.post("/plans/apply", status_code=202)
def apply_plan(body: ApplyInput):
    return {
        "status": "success",
        "job_id": call(workflow.apply, body.plan_id, body.confirmed, body.generate),
    }


@router.post("/strm/sync", status_code=202)
def sync(body: SyncInput):
    if not body.confirmed:
        raise HTTPException(400, "请确认生成目录")
    account = call(store.account, body.account_id)
    if not account:
        raise HTTPException(404, "账号不存在")
    payload = body.model_dump()
    payload["account_revision"] = account["revision"]
    return {"status": "success", "job_id": call(jobs.enqueue, "sync", payload)}


@router.get("/assets")
def assets(limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0)):
    return {"status": "success", "items": call(strm.list_assets, limit, offset)}


@router.post("/assets/{asset_id}/revoke")
def revoke(asset_id: str):
    return {"status": "success", "revoked": call(strm.revoke, asset_id)}


@router.get("/assets/{asset_id}/source")
def asset_source(asset_id: str):
    from pathlib import Path
    from app.cloud import governance

    asset = call(store.asset, asset_id)
    if not asset:
        raise HTTPException(404, "播放引用不存在")
    sources = asset["payload"].get("companions", [])
    return {
        "status": "success",
        "source": call(
            governance.preview,
            asset["local_path"],
            Path(asset["payload"]["output_root"]),
        ),
        "deletions": [
            {
                "file_id": r["id"],
                "record": call(governance.record, asset["account_id"], r["id"]),
            }
            for r in [asset["payload"]["source"], *sources]
        ],
    }


def receipt_operation(fn, asset_id):
    from app import engine

    try:
        with engine.mutation_lock():
            return call(fn, asset_id)
    except engine.MutationBusy:
        raise HTTPException(409, "有文件操作正在执行，请完成后再核验") from None


@router.post("/assets/{asset_id}/deletion/verify")
def verify_deletion(asset_id: str):
    from app.cloud import governance

    return {
        "status": "success",
        "result": receipt_operation(governance.verify, asset_id),
    }


@router.post("/assets/{asset_id}/deletion/reset")
def reset_deletion(asset_id: str, body: dict):
    from app.cloud import governance

    if body.get("confirmed") is not True:
        raise HTTPException(400, "请确认重置被拒绝记录")
    return {
        "status": "success",
        "result": receipt_operation(governance.reset_rejected, asset_id),
    }


@router.get("/jobs")
def list_jobs():
    return {"status": "success", "items": call(store.jobs)}


@router.get("/jobs/{job_id}")
def job_details(job_id: str):
    return {"status": "success", "job": call(jobs.details, job_id)}


@router.post("/jobs/{job_id}/verify")
def verify_transfer(job_id: str):
    return {"status": "success", "result": call(jobs.verify_transfer, job_id)}


@router.post("/jobs/{job_id}/continue", status_code=202)
def continue_transfer(job_id: str, body: dict):
    return {
        "status": "success",
        **call(jobs.continue_transfer, job_id, body.get("confirmed", False)),
    }


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    with store.db() as con:
        changed = con.execute(
            "UPDATE cloud_jobs SET state='cancelled',updated=? WHERE id=? AND state='queued'",
            (time.time(), job_id),
        ).rowcount
    if not changed:
        raise HTTPException(409, "任务已开始，写入不能直接中断")
    return {"status": "success"}


@router.get("/settings")
def get_settings():
    return {"status": "success", "settings": call(settings.get)}


@router.post("/settings")
def save_settings(body: dict):
    previous = call(settings.get)
    result = call(settings.save, body)
    # Naming/authorization changes must not interrupt active playback listeners.
    if any(previous[k] != result[k] for k in ("proxy_enabled", "proxy_port")):
        emby_proxy.stop()
        emby_proxy.start()
    return {"status": "success", "settings": result, "proxy": emby_proxy.status()}


@router.post("/emby/refresh")
def refresh_emby():
    if not workflow.refresh_emby():
        raise HTTPException(502, "Emby 刷新失败，请检查现有服务地址和密钥")
    return {"status": "success"}


@router.get("/rules")
def rules():
    with store.db() as con:
        rows = con.execute(
            "SELECT id,payload,enabled,next_run FROM cloud_rules ORDER BY updated DESC"
        ).fetchall()
    return {
        "status": "success",
        "items": [
            {"id": r[0], **json.loads(r[1]), "enabled": bool(r[2]), "next_run": r[3]}
            for r in rows
        ],
    }


@router.post("/rules")
def save_rule(body: RuleInput):
    account = call(store.account, body.account_id)
    if not account:
        raise HTTPException(404, "账号不存在")
    provider = call(for_account, body.account_id)
    if hasattr(provider, "require"):
        call(provider.require, "organize")
    call(workflow._directory, provider, body.source_cid)
    call(workflow._directory, provider, body.target_cid)
    rid = store.ident()
    payload = body.model_dump()
    payload["account_revision"] = account["revision"]
    with store.db() as con:
        con.execute(
            "INSERT INTO cloud_rules VALUES(?,?,?,?,?)",
            (
                rid,
                store.encode(payload),
                int(body.enabled),
                time.time() + body.interval_seconds,
                time.time(),
            ),
        )
    return {"status": "success", "id": rid}


@router.delete("/rules/{rule_id}")
def delete_rule(rule_id: str):
    with store.db() as con:
        con.execute("DELETE FROM cloud_rules WHERE id=?", (rule_id,))
    return {"status": "success"}


@play_router.api_route("/play/{asset_id}", methods=["GET", "HEAD"])
async def play(
    asset_id: str, request: Request, sig: str = Query(min_length=64, max_length=64)
):
    from app.cloud import playback

    try:
        return await playback.serve(request, asset_id, sig)
    except CloudError as error:
        raise HTTPException(error.status, str(error)) from None
