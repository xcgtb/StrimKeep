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

from app.cloud import directories, events, jobs, schedules, settings, store

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
        text = str(item).strip()
        if text and text.isdigit() and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def next_run(cron, timezone, now=None):
    """下次执行时间戳；Cron 为空或非法返回 0（不排期）。"""
    cron = (cron or '').strip()
    if not cron:
        return 0
    try:
        return schedules.cron_next(cron, timezone or DEFAULT_TZ,
                                   time.time() if now is None else now)
    except Exception:
        return 0


def due(cron, timezone, last_run, now=None):
    """是否到点。从未跑过时视为到点（首次开启立即执行一次）。"""
    now = time.time() if now is None else now
    if not (cron or '').strip():
        return False
    last = float(last_run or 0)
    if last <= 0:
        return True
    return next_run(cron, timezone, last) <= now


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
            job_id = jobs.enqueue('sync', {
                'account_id': aid,
                'account_revision': account['revision'],
                'source_cid': cid,
            })
            queued.append({'account_id': aid, 'source_cid': cid, 'job_id': job_id})
    if queued:
        events.emit('strm', 'STRM 全量同步已排队：%d 个目录' % len(queued))
    return queued, skipped


def tick(config, now=None):
    """调度器每拍调用；命中则投递并记录本次时间。返回本次投递列表。"""
    now = time.time() if now is None else now
    if not due(config.get('strm_scan_cron'), config.get('strm_scan_timezone'),
               config.get('strm_scan_last_run'), now):
        return []
    queued, _skipped = start()
    if queued:
        current = dict(settings.get())
        current['strm_scan_last_run'] = now
        settings.save(current)
    return queued
