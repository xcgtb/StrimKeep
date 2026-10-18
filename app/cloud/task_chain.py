"""Bounded task families from durable ownership links, including older receipts."""
from app.cloud import store
from app.cloud.errors import CloudError

LABELS = {'intake':'资源接收', 'channel':'频道接收', 'transfer':'分享转存',
          'organize':'识别与归档', 'workflow':'整理流程', 'sync':'STRM 同步',
          'publish':'延时发布', 'share_strm':'分享 STRM', 'categories':'分类目录', 'maintenance':'目录维护'}


def _parent(con, jid):
    for sql in (
        'SELECT parent_id FROM cloud_job_links WHERE job_id=?',
        'SELECT origin_job FROM cloud_job_due WHERE job_id=?',
        "SELECT job_id FROM cloud_steps WHERE json_extract(result,'$.job_id')=? AND job_id!=? LIMIT 1",
    ):
        row = con.execute(sql, (jid, jid) if '!=' in sql else (jid,)).fetchone()
        if row and con.execute('SELECT 1 FROM cloud_jobs WHERE id=?', (row[0],)).fetchone():
            return row[0]
    return None


def family(con, job_id):
    if not con.execute('SELECT 1 FROM cloud_jobs WHERE id=?', (job_id,)).fetchone():
        raise CloudError('任务不存在', 404)
    root, seen = job_id, set()
    while root not in seen and len(seen) < 64:
        seen.add(root)
        parent = _parent(con, root)
        if not parent:
            break
        root = parent
    else:
        raise CloudError('任务关联异常，请核对原任务', 409)
    queue, ids, parents = [root], [], {}
    while queue and len(ids) < 500:
        current = queue.pop(0)
        if current in ids:
            continue
        ids.append(current)
        rows = con.execute(
            'SELECT job_id FROM cloud_job_links WHERE parent_id=? UNION '
            'SELECT job_id FROM cloud_job_due WHERE origin_job=? UNION '
            "SELECT json_extract(result,'$.job_id') FROM cloud_steps WHERE job_id=? "
            "AND json_type(result,'$.job_id')='text' LIMIT 501", (current, current, current)).fetchall()
        for (child,) in rows:
            if child not in parents and child != root and con.execute('SELECT 1 FROM cloud_jobs WHERE id=?', (child,)).fetchone():
                parents[child] = current
                queue.append(child)
    return root, ids, parents, bool(queue)


def read(job_id):
    with store.db() as con:
        root, ids, parents, truncated = family(con, job_id)
        marks = ','.join('?' for _ in ids)
        rows = con.execute(f'SELECT id,kind,state,message,created,updated FROM cloud_jobs WHERE id IN ({marks}) ORDER BY created,id', ids).fetchall()
        ingestion = con.execute(
            f"SELECT state,count(*) FROM cloud_ingestion WHERE json_extract(payload,'$.origin_job') IN ({marks}) GROUP BY state", ids).fetchall()
        notifications = con.execute(
            f"SELECT state,count(*) FROM cloud_steps WHERE job_id IN ({marks}) AND "
            "(step_key LIKE 'notification%' OR step_key LIKE 'poster:%') GROUP BY state", ids).fetchall()
        recoveries = dict(con.execute(f'SELECT origin_id,job_id FROM cloud_recovery_requests WHERE origin_id IN ({marks})',ids).fetchall())
    states = {r[0]:r[2] for r in rows}
    jobs = [dict(zip(('id','kind','state','message','created','updated'), r),
                 label=LABELS.get(r[1],r[1]), parent_id=parents.get(r[0]), resolved_by=recoveries.get(r[0]) if states.get(recoveries.get(r[0]))!='cancelled' else None) for r in rows]
    review = any(not j['resolved_by'] and j['state'] in ('failed','uncertain','needs_review','stopped') for j in jobs)
    pending = any(j['state'] in ('queued','running') for j in jobs)
    counts = dict(ingestion)
    waiting = sum(counts.get(k,0) for k in ('pending','timeout','paused','stale'))
    external = any('115云下载' in j['message'] for j in jobs) and not parents
    review = review or any(dict(notifications).get(k,0) for k in ('unknown','dispatched'))
    state = 'needs_review' if review or any(counts.get(k,0) for k in ('timeout','stale')) else 'running' if pending else 'waiting' if waiting or external else 'success'
    if truncated:
        state = 'partial'
    return {'root_id':root, 'selected_id':job_id, 'state':state, 'jobs':jobs,
            'ingestion':counts, 'notifications':dict(notifications), 'truncated':truncated,
            'message':'任务较多，仅显示已加载范围，请分批查看' if truncated else '部分环节需要核实' if state=='needs_review' else '正在处理后续任务' if pending else
                      '等待下载或入库回执' if state=='waiting' else '已记录环节全部结束；入库结果以回执为准'}
