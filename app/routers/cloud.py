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
    directories,
    naming,
    categories,
    schedules,
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


class DirectoryInput(BaseModel):
    directories: dict[str, str | None] = Field(max_length=10)
    expected_revision: int = Field(ge=0)


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
    directory_revision: int | None = None
    generate: bool = True
    confirmed: bool = False


class MediaChoice(BaseModel):
    kind: Literal["movie", "tv"]
    tmdb_id: str = Field(pattern=r"^[0-9]{1,12}$")
    season: int | None = Field(default=None, ge=0, le=99)
    episode: int | None = Field(default=None, ge=1, le=999)
    category: str = Field(default="", max_length=80)


class PlanInput(AccountInput):
    source_cid: str = Field(pattern=r"^[0-9]{1,25}$")
    target_cid: str = Field(pattern=r"^[0-9]{1,25}$")
    overrides: dict[str, MediaChoice] = Field(default_factory=dict, max_length=2000)
    unknown_cid: str | None = Field(default=None, pattern=r"^[0-9]{1,25}$")


class CategoryInput(AccountInput):
    target_cid: str = Field(pattern=r"^[0-9]{1,25}$")
    confirmed: bool = False


class ApplyInput(BaseModel):
    plan_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    confirmed: bool = False
    generate: bool = True


class ShareStrmInput(BaseModel):
    preview_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    file_ids: list[str] = Field(min_length=1, max_length=100)
    overrides: dict[str, MediaChoice] = Field(default_factory=dict, max_length=2000)


class SyncInput(AccountInput):
    source_cid: str = Field(pattern=r"^[0-9]{1,25}$")
    confirmed: bool = False
    overrides: dict[str, MediaChoice] = Field(default_factory=dict, max_length=2000)
    reconcile: bool | None = None


class ScheduleInput(BaseModel):
    schedule_mode: Literal["interval", "cron"] = "interval"
    interval_seconds: int = Field(default=3600, ge=300, le=604800)
    cron_expression: str = Field(default="0 3 * * *", max_length=120)
    timezone: str = Field(default="Asia/Shanghai", max_length=80)


class RuleInput(AccountInput, ScheduleInput):
    kind: Literal["workflow", "sync"] = "workflow"
    source_cid: str = Field(pattern=r"^[0-9]{1,25}$")
    target_cid: str | None = Field(default=None, pattern=r"^[0-9]{1,25}$")
    unknown_cid: str | None = Field(default=None, pattern=r"^[0-9]{1,25}$")
    overrides: dict[str, MediaChoice] = Field(default_factory=dict, max_length=2000)
    name: str = Field(min_length=1, max_length=100)
    enabled: bool = False


@router.get("/status")
def status(response: Response):
    response.headers["Cache-Control"] = "no-store"
    return {
        "status": "success",
        "accounts": call(accounts.describe),
        "devices": [{"id": k, "name": v} for k,v in accounts.DEVICE_LABELS.items()],
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


@router.post("/accounts/cookie")
def import_cookie(body: dict, response: Response):
    response.headers["Cache-Control"] = "no-store"
    import re
    account_id = body.get("account_id")
    if set(body) - {"cookie", "account_id"} or (account_id is not None and (not isinstance(account_id, str) or not re.fullmatch(r"[a-f0-9]{32}", account_id))):
        raise HTTPException(400, "登录请求格式无效")
    return {"status": "success", "account_id": call(accounts.save_cookie, body.get("cookie"), account_id)}


@router.get("/accounts/{account_id}/directories")
def directory_settings(account_id: str, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return {"status": "success", **call(directories.get, account_id)}


@router.post("/accounts/{account_id}/directories")
def save_directories(account_id: str, body: DirectoryInput):
    return {"status": "success", **call(directories.save, account_id, body.directories, body.expected_revision)}


@router.post("/accounts/{account_id}/open/check")
def check_open(account_id: str):
    from app.cloud import open115

    provider = call(open115.for_account, account_id)
    return {"status": "success", "profile": call(provider.profile)}


@router.delete("/accounts/{account_id}")
def disconnect(account_id: str):
    call(store.disconnect_account, account_id)
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
        workflow.transfer_from_saved,
        body.preview_id,
        body.file_ids,
        body.target_cid,
        body.confirmed,
        body.organize_target,
        body.generate,
        body.directory_revision,
    )
    return {"status": "success", "job_id": job_id}


@router.post("/shares/strm/preview")
def preview_share_strm(body: ShareStrmInput):
    from app.cloud import share_library
    plan = call(share_library.preview, body.preview_id, body.file_ids,
                {k: v.model_dump(exclude_none=True) for k, v in body.overrides.items()})
    return {"status": "success", "plan": plan}


@router.get("/accounts/{account_id}/receiving")
def receiving_config(account_id: str):
    from app.cloud import receiving
    return {"status":"success",**call(receiving.get,account_id)}


@router.post("/accounts/{account_id}/receiving")
def receiving_save(account_id: str,body:dict):
    from app.cloud import receiving
    if set(body)!={"config","expected_revision"} or not isinstance(body["config"],dict) or type(body["expected_revision"]) is not int:raise HTTPException(400,"接收配置格式无效")
    return {"status":"success",**call(receiving.save,account_id,body["config"],body["expected_revision"])}


@router.post("/resources/preview")
def resource_preview(body:dict):
    from app.cloud import receiving
    if not isinstance(body,dict) or set(body)-{"account_id","text","organize"} or not {"account_id","text"}.issubset(body) or not isinstance(body["text"],str) or len(body["text"])>16000 or type(body.get("organize",False)) is not bool:raise HTTPException(400,"资源预览格式无效")
    return {"status":"success","plan":call(receiving.preview,body["account_id"],body["text"],body.get("organize",False))}


@router.post("/resources/apply",status_code=202)
def resource_apply(body:ApplyInput):
    from app.cloud import receiving
    return {"status":"success","job_id":call(receiving.apply,body.plan_id,body.confirmed)}


@router.get("/resources/offline")
def offline_resources(account_id:str,page:int=Query(1,ge=1,le=1000)):
    provider=call(for_account,account_id)
    call(provider.profile)
    return {"status":"success",**call(provider.offline_tasks,page)}


@router.get("/resources/catalog")
def receiving_catalog():
    from app import engine
    return {"status":"success","genres":[{"name":name,"ids":list(dict.fromkeys(str(v) for v in ids.values() if v))} for name,ids in engine.GENRE_MAP.items()],"countries":[{"name":name,"id":cid} for name,cid in (("中国大陆","CN"),("中国香港","HK"),("中国台湾","TW"),("韩国","KR"),("日本","JP"),("美国","US"),("英国","GB"),("法国","FR"),("德国","DE"),("加拿大","CA"),("澳大利亚","AU"),("印度","IN"))]}


@router.post('/accounts/{account_id}/inventory/scan', status_code=202)
def inventory_scan(account_id: str, body: dict):
    if set(body) - {'root_cid', 'kind_hint', 'enrich'}:
        raise HTTPException(400, '库存扫描参数无效')
    from app.cloud import inventory
    return {'status': 'success', 'job_id': call(inventory.start, account_id, body.get('root_cid', '0'), body.get('kind_hint', 'auto'), body.get('enrich', True))}


@router.get('/accounts/{account_id}/inventory')
def inventory_list(account_id: str, root_cid: str = '0', kind: Literal['', 'movie', 'tv', 'unknown'] = '', keyword: str = Query('', max_length=100), offset: int = Query(0, ge=0, le=100000), limit: int = Query(48, ge=1, le=100)):
    from app.cloud import inventory
    return {'status': 'success', **call(inventory.listing, account_id, root_cid, kind, keyword, offset, limit)}


@router.get("/accounts/{account_id}/channels")
def channel_list(account_id:str):
    from app.cloud import receiving
    return {"status":"success","items":call(receiving.channels,account_id)}


@router.post("/accounts/{account_id}/channels")
def channel_save(account_id:str,body:dict):
    from app.cloud import receiving
    return {"status":"success","channel":call(receiving.save_channel,account_id,body)}


@router.post("/accounts/{account_id}/channels/public-preview")
def public_channel_preview(account_id: str, body: dict):
    """Read-only network preview: no inbox, cursor or 115 remote writes."""
    if set(body) - {"address", "limit"} or not isinstance(body.get("address"), str):
        raise HTTPException(400, "请填写公开频道地址")
    from app.cloud import public_channel_reader
    return {"status": "success", **call(public_channel_reader.preview, account_id,
                                           body["address"], body.get("limit", 20))}


@router.delete("/accounts/{account_id}/channels/{channel_id}")
def channel_delete(account_id:str,channel_id:str):
    store.delete_channel(account_id, channel_id)
    return {"status":"success"}


@router.post("/accounts/{account_id}/channels/{channel_id}/test")
def channel_filter_test(account_id:str,channel_id:str,body:dict):
    from app.cloud import receiving
    if set(body)!={"text"} or not isinstance(body["text"],str) or len(body["text"])>8000:raise HTTPException(400,"请填写不超过8000字的测试消息")
    return {"status":"success", **call(receiving.test_channel, account_id, channel_id, body["text"])}


@router.get("/accounts/{account_id}/inbox")
def inbox_history(account_id:str,offset:int=Query(0,ge=0,le=100000)):
    from app.cloud import receiving
    return {"status":"success","items":call(receiving.inbox,account_id,offset)}


@router.post("/accounts/{account_id}/channels/{channel_id}/inspect")
def channel_inspect(account_id: str, channel_id: str):
    from app.cloud import channel_inspection
    return {"status": "success", **call(channel_inspection.inspect, account_id, channel_id)}


@router.post("/accounts/{account_id}/channels/{channel_id}/enabled")
def channel_enabled(account_id: str, channel_id: str, body: dict):
    if set(body) != {'enabled', 'revision'}:
        raise HTTPException(400, '请提供频道开关和配置版本')
    from app.cloud import receiving
    return {"status": "success", "channel": call(receiving.set_channel_enabled, account_id, channel_id, body['enabled'], body['revision'])}


@router.get("/accounts/{account_id}/maintenance")
def maintenance_config(account_id:str):
    from app.cloud import maintenance
    return {"status":"success",**call(maintenance.get,account_id)}


@router.post("/accounts/{account_id}/maintenance")
def maintenance_save(account_id:str,body:dict):
    from app.cloud import maintenance
    if set(body)-{"config","expected_revision","confirmed"} or not isinstance(body.get("config"),dict) or type(body.get("expected_revision")) is not int:raise HTTPException(400,"维护配置格式无效")
    return {"status":"success",**call(maintenance.save,account_id,body["config"],body["expected_revision"],body.get("confirmed",False))}


@router.post("/accounts/{account_id}/maintenance/run",status_code=202)
def maintenance_run(account_id:str,body:dict):
    from app.cloud import maintenance
    if set(body)!={"action","confirmed"}:raise HTTPException(400,"维护操作格式无效")
    return {"status":"success","job_id":call(maintenance.enqueue,account_id,body["action"],body["confirmed"])}


@router.post("/jobs/{job_id}/maintenance/verify")
def maintenance_verify(job_id:str):
    from app.cloud import maintenance
    return {"status":"success",**call(maintenance.verify,job_id)}


@router.post("/shares/strm/apply", status_code=202)
def apply_share_strm(body: ApplyInput):
    from app.cloud import share_library
    return {"status": "success", "job_id": call(share_library.apply, body.plan_id, body.confirmed)}


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
            body.unknown_cid,
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
    from app.cloud import pipeline
    call(pipeline.output_ready)
    payload = body.model_dump()
    payload["account_revision"] = account["revision"]
    payload['reconcile'] = body.reconcile if body.reconcile is not None else settings.get()['strm_reconcile_enabled']
    return {"status": "success", "job_id": call(jobs.enqueue, "sync", payload)}


@router.get("/assets")
def assets(limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0), account_id: str | None = None):
    return {"status": "success", "items": call(strm.list_assets, limit, offset, account_id)}


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
    if asset["payload"].get("source_kind") == "share":
        media, source = asset["payload"]["media"], asset["payload"]["source"]
        return {"status": "success", "source": {
            "source_kind": "share", "uid": asset["payload"]["uid"], "file_id": source["id"],
            "effect": "分享免转存：仅清理 NAS 分享库引用，保留分享和 115 源文件",
            "enabled": True, "media_identity": {"tmdb_id": media["tmdb_id"]},
            "source_fingerprints": [{k: source[k] for k in ("id", "name", "size", "sha1")}],
        }, "deletions": []}
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

    asset = call(store.asset, asset_id)
    if asset and asset["payload"].get("source_kind") == "share":
        raise HTTPException(409, "分享免转存来源仅清理 NAS 引用，没有源删除回执")
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
def list_jobs(query: str = Query('', max_length=200)):
    return {"status": "success", "items": call(store.job_summaries, 50, query)}


@router.get("/jobs/{job_id}")
def job_details(job_id: str):
    return {"status": "success", "job": call(jobs.details, job_id)}


@router.get("/jobs/{job_id}/timeline")
def job_timeline(job_id: str, response: Response):
    response.headers["Cache-Control"] = "private, no-store"
    return {"status": "success", **call(jobs.timeline, job_id)}


@router.get("/jobs/{job_id}/chain")
def job_chain(job_id: str, response: Response):
    from app.cloud import task_chain
    response.headers["Cache-Control"] = "private, no-store"
    return {"status":"success", **call(task_chain.read, job_id)}


@router.post("/jobs/{job_id}/recovery/preview")
def recovery_preview(job_id: str):
    from app.cloud import recovery
    return {"status":"success", **call(recovery.preview, job_id)}


@router.post("/jobs/{job_id}/recovery/apply", status_code=202)
def recovery_apply(job_id: str, body: dict):
    from app.cloud import recovery
    return {"status":"success", **call(recovery.apply, job_id, body.get('preview_id',''), body.get('confirmed',False))}


@router.post("/assets/{asset_id}/playback/check")
def check_playback(asset_id: str):
    from app.cloud import playback
    return {"status": "success", **call(playback.check, asset_id)}


@router.get("/jobs/{job_id}/diagnostics")
def job_diagnostics(job_id: str, offset: int = Query(0, ge=0), limit: int = Query(30, ge=1, le=100),
                    category: str = Query('', max_length=30), query: str = Query('', max_length=200)):
    from app.cloud import diagnostics
    value = call(store.job, job_id)
    if value is None:
        raise HTTPException(404, "任务不存在")
    return {"status": "success", "diagnostics": call(diagnostics.for_job, value, offset, limit, category, query)}


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
    changed = store.cancel_queued_job(job_id)
    if not changed:
        raise HTTPException(409, "任务已开始，写入不能直接中断")
    return {"status": "success"}


@router.post("/jobs/{job_id}/stop")
def stop_organizing(job_id: str, body: dict):
    if set(body) != {"confirmed"} or body.get("confirmed") is not True:
        raise HTTPException(400, "请确认在当前文件组完成后停止；关联定时规则同时暂停")
    return {"status": "success", **call(jobs.request_stop, job_id)}


@router.get("/output-directories")
def browse_output_directories(path: str = Query('', max_length=4096)):
    from app.cloud import local_paths
    return {"status": "success", **call(local_paths.browse, path)}


@router.get("/settings")
def get_settings():
    from app.cloud import media_probe, strm_scan
    config = call(settings.get)
    return {"status": "success", "settings": config, "schedule": call(strm_scan.status, config), "capabilities": {"ffprobe_available": media_probe.available()}}


@router.get("/accounts/{account_id}/pipeline")
def pipeline_status(account_id: str, response: Response):
    from app.cloud import pipeline
    response.headers["Cache-Control"] = "no-store"
    return {"status": "success", **call(pipeline.status, account_id)}


@router.post("/settings")
def save_settings(body: dict):
    if set(body) & {'strm_scan_last_run','strm_scan_last_enqueued','strm_scan_anchor','strm_scan_pending'}:
        raise HTTPException(400, '排期执行记录由调度器维护，请仅修改 Cron、时区与启动方式')
    previous = call(settings.get)
    result = call(settings.save, body)
    # Naming/authorization changes must not interrupt active playback listeners.
    if any(previous[k] != result[k] for k in ("proxy_enabled", "proxy_port")):
        emby_proxy.stop()
        emby_proxy.start()
    from app.cloud import strm_scan
    return {"status": "success", "settings": result, "schedule": call(strm_scan.status, result), "proxy": emby_proxy.status()}


@router.post("/naming/preview")
def naming_preview(body: dict):
    if set(body) - {"settings"} or not isinstance(body.get("settings", {}), dict):
        raise HTTPException(400, "模板预览配置格式无效")
    patch = body.get("settings", {})
    if set(patch) - set(workflow.NAMING_KEYS):
        raise HTTPException(400, "模板预览字段无效")
    config = call(settings.validate, {**settings.get(), **patch})
    return {"status": "success", "items": [call(naming.preview, {**config, "naming_style": "template"}, kind) for kind in ("movie", "tv")], "fields": sorted(naming.FIELDS)}


@router.post('/naming/details')
def naming_details(body: dict):
    if set(body) - {'settings'} or not isinstance(body.get('settings', {}), dict):
        raise HTTPException(400, '模板详情配置格式无效')
    patch = body.get('settings', {})
    if set(patch) - set(workflow.NAMING_KEYS):
        raise HTTPException(400, '模板详情字段无效')
    config = call(settings.validate, {**settings.get(), **patch})
    return {'status':'success', 'template':call(naming.describe, config)}


@router.post("/categories/create", status_code=202)
def create_categories(body: CategoryInput):
    if not body.confirmed:
        raise HTTPException(400, "请确认创建分类目录")
    account = call(store.account, body.account_id)
    if not account:
        raise HTTPException(404, "账号不存在")
    config = settings.get()
    names = categories.directory_paths(config)
    if not names:
        raise HTTPException(400, "请先选择并保存分类")
    return {"status": "success", "job_id": call(jobs.enqueue, "categories", {"account_id": body.account_id, "account_revision": account["revision"], "target_cid": body.target_cid, "categories": names})}


@router.post("/emby/refresh")
def refresh_emby():
    if not workflow.refresh_emby():
        raise HTTPException(502, "Emby 刷新失败，请检查现有服务地址和密钥")
    return {"status": "success"}


@router.get("/rules")
def rules():
    return {"status": "success", "items": store.list_rules()}


@router.post('/jobs/{job_id}/upgrades/continue')
def continue_upgrades(job_id: str, body: dict):
    from app.cloud import replacement
    if body != {'confirmed': True} or body.get('confirmed') is not True:
        raise HTTPException(400, '请确认核实并继续洗版')
    job = call(store.job, job_id)
    if not job or job['state'] in ('queued', 'running') or not replacement.summary(job_id):
        raise HTTPException(409, '请等待原任务结束并核对洗版记录')
    result = call(replacement.complete, job_id)
    refresh_pending = not workflow.refresh_emby()
    store.append_job_result(job_id, {'upgrades': result, 'refresh_pending': refresh_pending})
    return {'status': 'success', 'upgrades': result, 'refresh_pending': refresh_pending}


@router.post("/rules")
def save_rule(body: RuleInput):
    call(schedules.zone, body.timezone)
    due = call(schedules.next_run, body.model_dump(), time.time())
    if body.kind == "workflow" and (body.target_cid is None or body.target_cid == body.source_cid):
        raise HTTPException(400, "整理规则需选择不同的来源和归档目录")
    account = call(store.account, body.account_id)
    if not account:
        raise HTTPException(404, "账号不存在")
    provider = call(for_account, body.account_id)
    if hasattr(provider, "require"):
        call(provider.require, "organize" if body.kind == "workflow" else "read")
    call(workflow._directory, provider, body.source_cid)
    if body.kind == "workflow":
        call(workflow._directory, provider, body.target_cid)
        if body.unknown_cid:
            call(workflow._directory, provider, body.unknown_cid)
            if body.unknown_cid == body.target_cid:
                raise HTTPException(400, "未识别目录不能与归档相同")
    rid = store.ident()
    payload = body.model_dump()
    payload["account_revision"] = account["revision"]
    store.save_rule(rid, payload, body.enabled, due)
    return {"status": "success", "id": rid, "next_run": due}


@router.post("/schedules/preview")
def preview_schedule(body: ScheduleInput):
    call(schedules.zone, body.timezone)
    due = call(schedules.next_run, body.model_dump(), time.time())
    return {"status": "success", "next_run": due, "timezone": body.timezone}


@router.delete("/rules/{rule_id}")
def delete_rule(rule_id: str):
    store.delete_rule(rule_id)
    return {"status": "success"}


@router.post("/rules/{rule_id}/enabled")
def enable_rule(rule_id: str, body: dict):
    if set(body) != {"enabled"} or not isinstance(body["enabled"], bool):
        raise HTTPException(400, "请填写 enabled 布尔值")
    payload = store.rule_payload(rule_id)
    if payload is None:
        raise HTTPException(404, "规则不存在")
    revision = store.account_revision(payload["account_id"])
    if body["enabled"] and revision != payload["account_revision"]:
        raise HTTPException(409, "账号授权已变化，请重新创建规则")
    due = call(schedules.next_run, payload, time.time()) if body["enabled"] else time.time()
    payload.pop("schedule_error", None)
    call(store.update_rule_enabled, rule_id, body["enabled"], due, payload)
    return {"status": "success", "enabled": body["enabled"]}


# 可选 UA 拦截；该 UA 不是客户端身份凭证，默认关闭并仅完整匹配。
FEINIU_PROBE_UA = 'Lavf/60.3.100'


def is_probe_request(user_agent, enabled=True):
    """是否命中用户开启的探测 UA 规则；不推断客户端身份。"""
    return enabled is True and FEINIU_PROBE_UA.casefold() == (user_agent or '').strip().casefold()


@play_router.api_route("/play/{asset_id}", methods=["GET", "HEAD"])
async def play(
    asset_id: str, request: Request, sig: str = Query(min_length=64, max_length=64)
):
    from app.cloud import playback, settings as cloud_settings

    # 只拦这一个直连播放端点；Emby 反代不经过这里，不受影响。
    if is_probe_request(request.headers.get('user-agent'),
                        cloud_settings.get().get('strm_block_feiniu_ua', False)):
        raise HTTPException(403, '探测请求已拦截')
    try:
        return await playback.serve(request, asset_id, sig)
    except CloudError as error:
        raise HTTPException(error.status, str(error)) from None


@router.get("/accounts/{account_id}/telegram-reader")
def telegram_reader_get(account_id: str):
    from app.cloud import channel_reader
    return {"status":"success", **call(channel_reader.get, account_id)}


@router.post("/accounts/{account_id}/telegram-reader")
def telegram_reader_login(account_id: str, body: dict):
    from app.cloud import channel_reader
    return {"status":"success", **call(channel_reader.login, account_id, body)}


@router.get("/accounts/{account_id}/emby-connections")
def emby_connections(account_id: str):
    from app.cloud import emby_library
    return {"status":"success", **call(emby_library.get,account_id)}


@router.post("/accounts/{account_id}/emby-connections")
def save_emby_connection(account_id: str, body: dict):
    from app.cloud import emby_library
    return {"status":"success", **call(emby_library.save,account_id,body)}


@router.post("/accounts/{account_id}/emby-connections/{connection_id}/test")
def test_emby_connection(account_id: str, connection_id: str):
    from app.cloud import emby_library
    return {"status":"success", **call(emby_library.test,account_id,connection_id)}


@router.get("/accounts/{account_id}/ingestion")
def ingestion_records(account_id: str):
    from app.cloud import emby_library
    call(store.account,account_id)
    return {"status":"success","items":call(emby_library.records,account_id)}


@router.post("/accounts/{account_id}/ingestion/{record_id}/retry")
def retry_ingestion(account_id: str, record_id: str):
    from app.cloud import emby_library
    call(emby_library.retry,account_id,record_id)
    return {"status":"success"}


@router.get("/accounts/{account_id}/organizer-sources")
def organizer_sources(account_id: str):
    from app.cloud import intake_automation
    return {"status":"success", **call(intake_automation.sources,account_id)}


class IntakeRulesInput(ScheduleInput):
    source_ids: list[str] = Field(min_length=1,max_length=20)
    expected_revision: int = Field(ge=0)


@router.post("/accounts/{account_id}/organizer-sources")
def organizer_sources_save(account_id: str, body: IntakeRulesInput):
    from app.cloud import intake_automation
    config=body.model_dump(exclude={"source_ids","expected_revision"})
    return {"status":"success", **call(intake_automation.save,account_id,config,body.source_ids,body.expected_revision)}


class DirectShareInput(AccountInput):
    link: str = Field(min_length=1,max_length=4096)
    password: str = Field(default="",max_length=12)
    organize: bool = False
    expected_revision: int = Field(ge=0)
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    confirmed: bool = False


@router.post("/shares/receive",status_code=202)
def receive_share_link(body: DirectShareInput):
    jid=call(workflow.transfer_link,body.account_id,body.link,body.password,body.organize,body.expected_revision,body.request_id,body.confirmed)
    return {"status":"success","job_id":jid}


class DirectResourceInput(AccountInput):
    text: str = Field(min_length=1,max_length=16000)
    expected_revision: int = Field(ge=0)
    request_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    organize: bool = False
    confirmed: bool = False


@router.post('/resources/receive',status_code=202)
def receive_resources(body: DirectResourceInput):
    from app.cloud import receiving
    return {'status':'success','job_id':call(receiving.receive,body.account_id,body.text,body.expected_revision,body.request_id,body.confirmed,body.organize)}


@router.post('/accounts/{account_id}/organizer/run',status_code=202)
def organizer_run(account_id: str, body: dict):
    from app.cloud import intake_automation
    return {'status':'success','job_id':call(intake_automation.run_now,account_id,body.get('expected_revision'),body.get('source_cid'))}


@router.post('/accounts/{account_id}/organizer/stop')
def organizer_stop(account_id: str):
    from app.cloud import intake_automation
    return {'status':'success',**call(intake_automation.stop_all,account_id)}
