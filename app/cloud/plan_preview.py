"""Read-only organization previews on the existing durable job queue."""
import json
import time

from app.cloud import jobs, store, workflow
from app.cloud.errors import CloudError
from app.cloud.provider115 import numeric


def start(account_id, source_cid, target_cid, overrides=None, unknown_cid=None):
    source_cid, target_cid = numeric(source_cid), numeric(target_cid)
    unknown_cid = numeric(unknown_cid) if unknown_cid is not None else None
    if source_cid == target_cid:
        raise CloudError('来源和归档不能是同一目录', 409)
    with store.db() as con:
        account = con.execute('SELECT revision FROM cloud_accounts WHERE id=?', (account_id,)).fetchone()
        if not account:
            raise CloudError('账号不存在', 404)
        payload = {'account_id': account_id, 'account_revision': account[0],
                   'source_cid': source_cid, 'target_cid': target_cid,
                   'unknown_cid': unknown_cid, 'overrides': overrides or {}}
        active = con.execute("SELECT id,payload FROM cloud_jobs WHERE kind='organize_preview' "
                             "AND state IN ('queued','running') AND json_extract(payload,'$.account_id')=? "
                             "AND json_extract(payload,'$.source_cid')=?", (account_id, source_cid)).fetchone()
        if active:
            if json.loads(active[1]) != payload:
                raise CloudError('来源目录已有不同选项的整理预览，请等待或停止原扫描', 409)
            return active[0]
        return jobs.enqueue('organize_preview', payload, con)


def run(job_id, payload):
    workflow.verify_account(payload)
    planned = workflow.plan(payload['account_id'], payload['source_cid'], payload['target_cid'],
                            payload.get('overrides'), payload.get('unknown_cid'), job_id=job_id,
                            expected_revision=payload['account_revision'])
    jobs.progress(job_id, len(planned['items']), len(planned['items']), '预览完成，等待确认整理')
    return {'plan_id': planned['id'], 'preview_only': True,
            'video_count': len(planned['items']), 'skipped': planned['skipped'],
            'filtered': planned['filtered'], 'quality_held': planned['quality_held']}


def read(plan_id):
    with store.db() as con:
        row = con.execute('SELECT account_id,revision,payload,expires,state FROM cloud_plans WHERE id=?',
                          (plan_id,)).fetchone()
        if not row:
            raise CloudError('整理预览不存在', 404)
        account = con.execute('SELECT revision FROM cloud_accounts WHERE id=?', (row[0],)).fetchone()
    if not account or account[0] != row[1]:
        raise CloudError('账号授权已变化，请重新扫描预览', 409)
    if row[3] <= time.time() or row[4] != 'pending':
        raise CloudError('计划已过期或已提交，请重新预览', 409)
    payload = json.loads(row[2])
    return {**payload, 'id': plan_id, 'expires_in': max(0, int(row[3] - time.time()))}
