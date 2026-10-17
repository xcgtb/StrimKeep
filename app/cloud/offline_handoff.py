"""Read-only 115 download reconciliation -> one durable organizer job.

Only an explicitly opted-in intake is watched. Submission, completion and
organization are different receipts; a remote write is never replayed to poll.
"""

import re
import time
from concurrent.futures import ThreadPoolExecutor

from app.cloud import directories, jobs, store, workflow
from app.cloud.errors import CloudError

_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix='cloud-offline-handoff')
_future = None
INTERVAL = 180
MAX_PAGES = 20
MAX_ROWS = 40


def register(job_id, index, payload, item, response):
    """Register only a verified accepted offline task with a usable hash."""
    if not payload.get('organize_target') or item['kind'] not in ('magnet', 'ed2k'):
        return None
    value = str(response.get('info_hash') or '').strip().lower()
    if not re.fullmatch('[a-f0-9]{40}', value):
        # Never infer completion from a filename or submit the magnet again.
        return {'state': 'needs_review', 'message': '115未返回可核对的云下载任务哈希，请在离线任务完成后手动整理'}
    target = str(payload['targets'][item['kind']])
    if target == str(payload['organize_target']):
        return {'state': 'needs_review', 'message': '接收目录与归档目录相同，需先调整目录后手动整理'}
    with store.db() as con:
        con.execute(
            "INSERT OR IGNORE INTO cloud_offline_handoffs "
            "(origin_job,item_index,account_id,account_revision,directory_revision,info_hash,source_cid,target_cid,unknown_cid,state,child_job,next_check,last_error,updated) "
            "VALUES(?,?,?,?,?,?,?,?,?,'waiting','',?,'',?)",
            (job_id, index, payload['account_id'], payload['account_revision'],
             payload['directory_revision'], value, target, str(payload['organize_target']),
             '', time.time(), time.time()),
        )
    return {'state': 'waiting', 'message': '已登记115云下载完成后自动整理（按任务哈希核验）'}


def _tasks(provider):
    """Fail closed if the provider truncates/paginates inconsistently."""
    result, seen = [], set()
    for page in range(1, MAX_PAGES + 1):
        response = provider.offline_tasks(page)
        rows = response.get('items')
        if not isinstance(rows, list):
            raise CloudError('115云下载任务列表不完整，暂不接续整理', 502)
        for row in rows:
            if not isinstance(row, dict):
                raise CloudError('115云下载任务状态无效，暂不接续整理', 502)
            key = str(row.get('info_hash') or '').lower()
            if not re.fullmatch('[a-f0-9]{40}', key) or key in seen:
                # Incomplete unrelated historical tasks still need to block a source
                # via intake_automation.ready(), but cannot prove this handoff.
                if key in seen:
                    raise CloudError('115云下载任务哈希重复，暂停自动接续', 502)
            else:
                seen.add(key)
            result.append(row)
        count = response.get('page_count')
        if isinstance(count, str) and count.isdecimal():
            count = int(count)
        if count is not None:
            if type(count) is not int or not 0 <= count <= MAX_PAGES:
                raise CloudError('115云下载分页异常，暂停自动接续', 502)
            if page >= count:
                return result
        elif len(rows) < 30:
            return result
    raise CloudError('115云下载超过核验页数上限，暂停自动接续', 409)


def _match(rows, hash_value, source):
    candidates = [r for r in rows if str(r.get('info_hash') or '').lower() == hash_value]
    if len(candidates) != 1:
        return False
    row = candidates[0]
    if str(row.get('wp_path_id') or '') != source or str(row.get('status')) != '2':
        return False
    # Do not scan a directory containing an in-progress or unknown-state task.
    return all(str(r.get('status')) == '2' for r in rows if str(r.get('wp_path_id') or '') == source)


def poll_one():
    now = time.time()
    with store.db() as con:
        pending = con.execute(
            "SELECT origin_job,item_index,account_id,account_revision,directory_revision,info_hash,source_cid,target_cid "
            "FROM cloud_offline_handoffs WHERE state='waiting' AND next_check<=? ORDER BY updated LIMIT ?",
            (now, MAX_ROWS),
        ).fetchall()
    if not pending:
        return 0
    cached, enqueued = {}, 0
    for origin, index, aid, revision, directory_revision, hash_value, source, target in pending:
        try:
            account = store.account(aid)
            current = directories.get(aid)
            if (not account or account['revision'] != revision or
                current['revision'] != directory_revision or
                not current['directories'].get('target') or
                str(current['directories']['target']['id']) != target):
                with store.db() as con:
                    con.execute("UPDATE cloud_offline_handoffs SET state='blocked',last_error='账号或归档目录配置已变化，请手动核对',updated=? WHERE origin_job=? AND item_index=? AND state='waiting'",(time.time(), origin, index))
                continue
            if aid not in cached:
                provider = workflow.verify_account({'account_id': aid, 'account_revision': revision})
                cached[aid] = (provider, _tasks(provider))
            provider, rows = cached[aid]
            if not _match(rows, hash_value, source):
                continue
            from app.cloud import intake_automation, pipeline
            intake_automation._outside(provider, source, target)
            # Preparation is read-only; failure must never authorize a remote write.
            pipeline.prepare()
            payload = {'account_id': aid, 'account_revision': revision,
                       'expected_directory_revision': directory_revision,
                       'source_cid': source, 'target_cid': target,
                       'unknown_cid': current['directories']['unknown']['id'] if current['directories'].get('unknown') else None,
                       'overrides': {}, 'intake_rule': True, 'offline_handoff': True}
            with store.db() as con:
                row = con.execute('SELECT state FROM cloud_offline_handoffs WHERE origin_job=? AND item_index=?',(origin,index)).fetchone()
                if not row or row[0] != 'waiting':
                    continue
                active = con.execute("SELECT id FROM cloud_jobs WHERE state IN ('queued','running') AND kind='workflow' AND json_extract(payload,'$.account_id')=? AND json_extract(payload,'$.source_cid')=? LIMIT 1",(aid,source)).fetchone()
                jid = active[0] if active else jobs.enqueue('workflow', payload, con)
                con.execute("UPDATE cloud_offline_handoffs SET state='queued',child_job=?,last_error='',updated=? WHERE origin_job=? AND item_index=? AND state='waiting'",(jid,time.time(),origin,index))
                enqueued += 1
        except (CloudError, OSError, ValueError):
            # Do not expose provider exceptions (URLs, cookies, authorization data).
            # Preserve a user-visible reason and retry later without remote writes.
            with store.db() as con:
                con.execute("UPDATE cloud_offline_handoffs SET last_error='115云下载状态暂不可核验；未移动任何文件，稍后重试',updated=? WHERE origin_job=? AND item_index=? AND state='waiting'",(time.time(),origin,index))
        finally:
            with store.db() as con:
                con.execute("UPDATE cloud_offline_handoffs SET next_check=?,updated=? WHERE origin_job=? AND item_index=? AND state='waiting'",(time.time()+INTERVAL,time.time(),origin,index))
    return enqueued


def schedule():
    global _future
    if _future is not None and not _future.done():
        return
    with store.db() as con:
        waiting = con.execute("SELECT 1 FROM cloud_offline_handoffs WHERE state='waiting' AND next_check<=? LIMIT 1",(time.time(),)).fetchone()
    if waiting:
        _future = _POOL.submit(poll_one)


def records(account_id):
    with store.db() as con:
        rows = con.execute(
            'SELECT h.origin_job,h.item_index,h.info_hash,h.source_cid,h.state,h.child_job,h.updated,j.state,j.message,h.last_error '
            'FROM cloud_offline_handoffs h LEFT JOIN cloud_jobs j ON j.id=h.child_job '
            'WHERE h.account_id=? ORDER BY h.updated DESC LIMIT 100', (account_id,)
        ).fetchall()
    try:
        saved = directories.get(account_id)['directories']
        names = {str(v['id']):v['name'] for v in saved.values() if isinstance(v,dict)}
    except (CloudError, KeyError):
        names = {}
    return [{'origin_job':r[0],'item_index':r[1],'task_hash':r[2][:8]+'…',
             'source_cid':r[3], 'source_name':names.get(r[3], '历史接收目录'),
             'state':r[4], 'child_job':r[5], 'updated':r[6],
             'child_state':r[7] or '', 'child_message':r[8] if r[7] else '',
             'detail':r[9] or ''} for r in rows]


def pause(account_id):
    """Explicit organizer Stop must also pause accepted-but-unfinished downloads."""
    with store.db() as con:
        return con.execute(
            "UPDATE cloud_offline_handoffs SET state='paused',last_error='用户已暂停自动整理',updated=? "
            "WHERE account_id=? AND state='waiting'",(time.time(),account_id)
        ).rowcount
