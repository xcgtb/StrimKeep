# -*- coding: utf-8 -*-
"""STRM 全量同步：Cron 到期后，对每个 STRM 角色目录各投递一次 sync 任务。

与上游行为对齐：
- 定时是**全量**扫描，不是增量；
- 多个目录串行（沿用任务总线的互斥）；
- 每个目录一条独立回执（字段来自 `workflow.sync` 的 result）。

安全边界：本模块**不删除任何东西**。清理仍由 `strm.reconcile` 在 sync 内部执行，
并保留「连续两次扫描均缺失才删」的保护。
"""
import time

from app.cloud import directories, events, schedules, settings, store

DEFAULT_CRON = ''
DEFAULT_TZ = 'Asia/Shanghai'


def targets(account_id):
    """该账号配置为 STRM 角色的目录 ID 列表（去重、保持配置顺序）。"""
    try:
        dirs = (directories.get(account_id) or {}).get('directories') or {}
    except Exception:
        return []
    raw = dirs.get('strm')
    if raw is None:
        return []
    items = raw if isinstance(raw, list) else [raw]
    out, seen = [], set()
    for item in items:
        if isinstance(item, dict):
            item = item.get('id')
        text = str(item).strip()
        if text and text.isdigit() and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def next_run(cron, timezone, now=None):
    """下次执行时间戳；Cron 为空或非法返回 0（不排期）。"""
    if not isinstance(cron, str) or not cron.strip():
        return 0
    try:
        return schedules.cron_next(cron, timezone or DEFAULT_TZ,
                                   time.time() if now is None else now)
    except Exception:
        return 0


def due(cron, timezone, last_run, now=None):
    """是否到点。从未跑过时视为到点（首次开启立即执行一次）。"""
    now = time.time() if now is None else now
    try:
        last = float(last_run or 0)
    except (TypeError, ValueError):
        return False
    # Validate even on the first run; an invalid schedule must never enqueue work.
    scheduled = next_run(cron, timezone, now if last <= 0 else last)
    if scheduled <= 0:
        return False
    if last <= 0:
        return True
    return scheduled <= now


def start(account_id=None):
    """对每个 STRM 目录投递一次 sync，返回 (queued, skipped)。

    串行由任务总线保证：先投递的先跑，后面的排队。
    """
    accounts = [account_id] if account_id else [a['id'] for a in store.accounts()]
    queued, skipped = [], []
    for aid in accounts:
        account = store.account(aid)
        if not account:
            continue
        dirs = targets(aid)
        if not dirs:
            skipped.append({'account_id': aid, 'reason': '没有配置 STRM 角色目录'})
            continue
        for cid in dirs:
            job_id, created = store.enqueue_sync_once(aid, account['revision'], cid, settings.get()['strm_reconcile_enabled'])
            if not created:
                skipped.append({'account_id': aid, 'source_cid': cid,
                                'active_job_id': job_id, 'reason': '此目录已有排队或执行中的同步任务'})
                continue
            queued.append({'account_id': aid, 'source_cid': cid, 'job_id': job_id})
    if queued:
        events.emit('strm', 'STRM 全量同步已排队：%d 个目录' % len(queued))
    return queued, skipped


def status(config, now=None):
    """Read-only schedule overview. Keep historical runs across disable/re-enable."""
    now = time.time() if now is None else now
    last = config.get('strm_scan_last_run', 0)
    anchor = config.get('strm_scan_anchor', 0)
    enabled = next_run(config.get('strm_scan_cron'), config.get('strm_scan_timezone'), now) > 0
    immediate = config.get('strm_scan_pending', False) or (last <= 0 and anchor <= 0 and config.get('strm_scan_start_mode', 'immediate') == 'immediate')
    baseline = max(last, anchor) if config.get('strm_scan_start_mode') == 'scheduled' else last
    next_at = 0 if not enabled else now if immediate else next_run(config.get('strm_scan_cron'), config.get('strm_scan_timezone'), baseline or now)
    return {'enabled': enabled, 'last_run': config.get('strm_scan_last_enqueued', 0), 'last_check': last, 'next_run': next_at,
            'due': enabled and next_at > 0 and next_at <= now,
            'start_mode': config.get('strm_scan_start_mode','immediate'),
            'cleanup_enabled': config.get('strm_reconcile_enabled', True)}


def tick(config, now=None):
    """调度器每拍调用；命中则投递并记录本次时间。返回本次投递列表。"""
    now = time.time() if now is None else now
    if not status(config, now)['due']:
        return []
    queued, _skipped = start()
    if queued or any(item.get('active_job_id') for item in _skipped):
        settings.record_scan(config, now, enqueued=bool(queued))
    return queued
