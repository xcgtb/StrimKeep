# -*- coding: utf-8 -*-
"""双库治理：扫描 / 清理 / 计划 / 巡检 / 残留 / 手动完结

变化点：/api/plans 列表改走 SQLite 索引（先 sync 磁盘文件再查库），
计划列表与详情通过 SQLite 读取；旧 plan_*.json 仅用于首次迁移。
"""
from app import state_store as _state
from app import logger
from app.routers.library import _slim_record
import json, time, threading
from fastapi import APIRouter, Depends, HTTPException

try:
    from app.routers.deps import auth, engine, scheduler, Args, spawn
    from app.plan_guard import VERSION as FILE_GUARD_VERSION
    from app.governance import load_scan_status
    from app.wash import browse_media_dirs
    from app.cloud_residue import preview_cloud_residue, clean_cloud_residue, scan_directory_cleanup, clean_directory_cleanup
except ImportError:
    from routers.deps import auth, engine, scheduler, Args, spawn
    from plan_guard import VERSION as FILE_GUARD_VERSION
    from governance import load_scan_status
    from wash import browse_media_dirs
    from cloud_residue import preview_cloud_residue, clean_cloud_residue, scan_directory_cleanup, clean_directory_cleanup

router = APIRouter()


@router.get('/api/plan/{plan_id}', dependencies=[Depends(auth)])
def api_get_plan(plan_id: str):
    # 查看单个 Plan 详情（白皮书 §17）
    data = engine.load_plan(plan_id)
    if data is None:
        raise HTTPException(404, 'Plan 不存在或格式过旧')
    return {'status': 'success', 'plan': data}


@router.get('/api/plans', dependencies=[Depends(auth)])
def api_list_plans(limit: int = 20):
    # 列出最近 Plan（白皮书 §17）：SQLite 索引查询，先同步磁盘上手工写入/变更的文件
    if limit < 1: limit = 1
    if limit > 100: limit = 100
    try:
        plans = engine.db_list_plans(limit)
    except Exception:
        plans = []
    return {'status': 'success', 'plans': plans,
            'archives': [_slim_record(r) for r in logger.read_archives(limit)]}


@router.post('/api/check', dependencies=[Depends(auth)])
def api_check():
    t = spawn('inter_check', engine.ACTIONS['inter_check'], Args(), cancellable=True)
    return {'task_id': t.id, 'status': 'success'}


@router.post('/api/clean', dependencies=[Depends(auth)])
def api_clean(body: dict = None):
    body = body or {}
    plan_id = str(body.get('plan_id', '')).strip()
    dry = bool(body.get('dry_run', False))
    if not plan_id:
        raise HTTPException(400, '必须提供 plan_id（请先执行诊断）')
    t = spawn('inter_clean', engine.ACTIONS['inter_clean'], Args(plan=plan_id, dry_run=dry))
    return {'task_id': t.id, 'dry_run': dry, 'status': 'success'}


_manual_done_lock = threading.Lock()


def _manual_done_path():
    return engine.MANUAL_DONE_FILE


def _read_manual_done() -> dict:
    return engine.read_manual_done()


def _write_manual_done(data):
    _state.save(engine.MANUAL_DONE_FILE, data)


@router.get('/api/manual_done', dependencies=[Depends(auth)])
def api_get_manual_done():
    return {'status': 'success', 'items': _read_manual_done()}


@router.post('/api/manual_done', dependencies=[Depends(auth)])
def api_set_manual_done(body: dict = None):
    """标记 / 取消标记某部剧为「已完结」（body: id, name, done）。仅记录，不动任何文件。"""
    body = body or {}
    sid = str(body.get('id') or '').strip()
    if not sid:
        return {'status': 'error', 'message': '缺少剧集 id'}
    with _manual_done_lock:
        data = _read_manual_done()
        if body.get('done', True):
            data[sid] = {'name': str(body.get('name') or '')[:200], 'ts': int(time.time())}
        else:
            data.pop(sid, None)
        try:
            _write_manual_done(data)
        except OSError as e:
            return {'status': 'error', 'message': '保存失败：%s' % e}
    return {'status': 'success', 'items': data}


_orphan_lock = threading.Lock()


@router.get('/api/orphans', dependencies=[Depends(auth)])
def api_orphans(max_depth: int = 3):
    # 扫描未知/孤儿文件 + 无 strm 的孤儿目录（白皮书 §16，只报告不删）
    if not _orphan_lock.acquire(blocking=False):
        return {'status': 'busy', 'message': '已有孤儿扫描任务在跑，请稍候'}
    try:
        return engine.action_scan_orphans(Args(max_depth=max_depth))
    except Exception as e:
        return {'status': 'error', 'message': str(e)}
    finally:
        _orphan_lock.release()


@router.post('/api/orphans/clean', dependencies=[Depends(auth)])
def api_clean_orphan_dirs(body: dict = None):
    # 删除孤儿目录（前端传入 paths 列表；dry_run 默认 True 只预览）
    body = body or {}
    paths = body.get('paths') or []
    dry_run = bool(body.get('dry_run', True))
    return engine.clean_orphan_dirs(paths, dry_run=dry_run)


@router.get('/api/wash/empty-dirs/browse', dependencies=[Depends(auth)])
def api_wash_empty_browse(path: str = '', search: str = '', offset: int = 0, limit: int = 200):
    return browse_media_dirs(path, search=search, offset=offset, limit=limit)


@router.post('/api/wash/empty-dirs/scan', dependencies=[Depends(auth)])
def api_wash_empty_scan(body: dict = None):
    """目录级残留扫描（照搬上游）：叶子目录内完全没有 .strm 的媒体目录。"""
    body = body or {}
    path = str(body.get('path') or '')
    limit = int(body.get('limit') or 100)
    scope = str(body.get('scope') or 'directory')
    if scope not in ('all', 'directory'):
        raise HTTPException(400, '扫描范围无效')
    if scope == 'all' and path:
        raise HTTPException(400, '整个媒体库扫描不能同时指定子目录')
    if scope == 'directory' and not path:
        raise HTTPException(400, '缺少扫描目录')
    if not _orphan_lock.acquire(blocking=False):
        raise HTTPException(409, '有扫描/清理正在进行，请稍后再试')
    try:
        return scan_directory_cleanup(path, scope=scope, limit=limit)
    finally:
        _orphan_lock.release()


@router.post('/api/wash/empty-dirs/clean', dependencies=[Depends(auth)])
def api_wash_empty_clean(body: dict = None):
    """直接清理目录残留，无备份；返回删除数量及失败原因。"""
    body = body or {}
    paths = body.get('paths') or []
    items = body.get('items')
    if not paths and not items:
        raise HTTPException(400, '缺少清理路径')
    if not _orphan_lock.acquire(blocking=False):
        raise HTTPException(409, '有扫描/清理正在进行，请稍后再试')
    try:
        if items is not None:
            return clean_directory_cleanup(items)
        return engine.clean_empty_dirs(paths)
    finally:
        _orphan_lock.release()


@router.post('/api/wash/cloud-residue/preview', dependencies=[Depends(auth)])
def api_cloud_residue_preview(body: dict = None):
    body = body or {}
    if not _orphan_lock.acquire(blocking=False):
        raise HTTPException(409, '有扫描/清理正在进行，请稍后再试')
    try:
        return preview_cloud_residue(str(body.get('local_path') or ''))
    finally:
        _orphan_lock.release()


@router.post('/api/wash/cloud-residue/clean', dependencies=[Depends(auth)])
def api_cloud_residue_clean(body: dict = None):
    body = body or {}
    if not _orphan_lock.acquire(blocking=False):
        raise HTTPException(409, '有扫描/清理正在进行，请稍后再试')
    try:
        return clean_cloud_residue(body.get('token'), confirmed=body.get('confirmed') is True)
    finally:
        _orphan_lock.release()


@router.get('/api/governance/latest', dependencies=[Depends(auth)])
def api_gov_latest():
    d = engine.load_latest_scan()
    if not d or not isinstance(d.get('result'), dict):
        return {'status': 'success', 'found': False}
    ts = float(d.get('ts') or 0)
    age = time.time() - ts
    ttl = engine.PLAN_TTL
    pid = d.get('plan_id')
    usable, reason = True, ''
    scan_status = load_scan_status()
    if age > ttl:
        usable, reason = False, 'expired'
    elif pid:
        p = engine.load_plan(pid)
        st = (p or {}).get('state') if p else 'missing'
        if st == 'executing':
            usable, reason = False, 'running'
        elif st != 'pending':
            usable, reason = False, ('used' if st in ('done', 'failed') else 'expired')
    if usable:
        current_sig = engine._current_rule_snapshot()['sig']
        scan_sig = str(d.get('rule_sig') or '')
        plan_sig = str((p or {}).get('rule_sig') or '') if pid else ''
        if any(sig and sig != current_sig for sig in (scan_sig, plan_sig)):
            usable, reason = False, 'rule_changed'
        elif pid and (p or {}).get('guard_version') != FILE_GUARD_VERSION:
            usable, reason = False, 'snapshot_missing'
    if scan_status.get('status') == 'failed':
        usable, reason = False, 'scan_failed'
    return {'status': 'success', 'found': True, 'usable': usable, 'reason': reason,
            'plan_id': pid, 'ts': ts, 'age_sec': int(age),
            'remaining_sec': max(0, int(ttl - age)), 'ttl_sec': int(ttl),
            'result': d['result'] if usable else None, 'scan_attempt': scan_status}


@router.get('/api/governance/summary', dependencies=[Depends(auth)])
def api_governance_summary():
    """双库治理单一事实入口：扫描事实 + 片库快照 + 入库事实 + 当前规则指纹。
    页面不再分别拼接多个接口后自行判断口径。"""
    try:
        latest = engine.load_latest_scan() or {}
        result = latest.get('result') if isinstance(latest.get('result'), dict) else {}
        consistency = engine.daily_consistency_snapshot(force_refresh=False)
        ts = float(latest.get('ts') or 0)
        age = max(0, time.time() - ts) if ts else None
        ttl = engine.PLAN_TTL
        pid = latest.get('plan_id') or result.get('plan_id')
        plan = engine.load_plan(pid) if pid else None
        state = (plan or {}).get('state') if plan else ('missing' if pid else 'none')
        current_rule_sig = str((consistency or {}).get('rule_sig') or '')
        scan_rule_sig = str(latest.get('rule_sig') or result.get('rule_sig') or '')
        rule_aligned = (not scan_rule_sig or not current_rule_sig or scan_rule_sig == current_rule_sig)
        if plan and plan.get('rule_sig') and current_rule_sig:
            rule_aligned = rule_aligned and plan.get('rule_sig') == current_rule_sig
        file_guard_ready = not pid or bool(plan and plan.get('guard_version') == FILE_GUARD_VERSION)
        scan_status = load_scan_status()
        usable = bool(latest) and bool(ts) and age <= ttl and (state in ('pending', 'none')) and rule_aligned and file_guard_ready and scan_status.get('status') != 'failed'
        return {'status':'success', 'schema_version':2,
                'scan': {'found': bool(latest), 'ts': ts, 'age_sec': int(age or 0),
                         'plan_id': pid or '', 'plan_state': state, 'usable': usable,
                         'ttl_sec': int(ttl), 'remaining_sec': max(0, int(ttl-(age or 0))) if ts else 0,
                         'rule_sig': scan_rule_sig, 'current_rule_sig': current_rule_sig,
                         'rule_aligned': rule_aligned,
                         'file_guard_ready': file_guard_ready,
                         'scan_attempt': scan_status,
                         'result': result},
                'consistency': consistency}
    except Exception as e:
        return {'status':'error','message':str(e)}


@router.get('/api/governance/auto', dependencies=[Depends(auth)])
def api_get_gov_auto():
    return {'status': 'success', 'settings': scheduler.gov_auto_view()}


@router.post('/api/governance/auto', dependencies=[Depends(auth)])
def api_set_gov_auto(body: dict = None):
    return {'status': 'success', 'settings': scheduler.gov_auto_update(body or {})}
