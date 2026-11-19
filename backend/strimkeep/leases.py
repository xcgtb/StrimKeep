"""Job lease reaper: mark stale running jobs as needs_review.

Borrowed from Celery's visibility timeout and Gunicorn's worker timeout: a job
that has been running far beyond a sane lease is not silently replayed (remote
writes may have happened); it is moved to needs_review for explicit recovery.
"""
import time

STALE_SECONDS = 2 * 60 * 60


def reap_stale_jobs(store, now=None, timeout=STALE_SECONDS):
    now = time.time() if now is None else now
    cutoff = now - timeout
    rows = store.db.execute(
        "SELECT id FROM organize_jobs WHERE state='running' AND updated<?",
        (cutoff,)).fetchall()
    changed = 0
    for row in rows:
        try:
            updated = store.jobs.update(
                row['id'], state='needs_review',
                stage='任务租约超时，等待核查',
                message='任务运行超过 %d 分钟，已暂停自动重放；请核查后继续。' % (timeout // 60))
            if updated is not None:
                changed += 1
        except Exception:
            continue
    if changed:
        store.event('system', '租约超时回收：%d 个任务已转入待核查。' % changed)
    return changed
