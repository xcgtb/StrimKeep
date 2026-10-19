"""Durable, account-scoped catalogue snapshots. A failed scan never replaces success."""
import json
import time

from app.cloud import jobs, store, workflow, settings, recognition
from app.cloud.errors import CloudError
from app.cloud.provider115 import numeric
from app.domain import inventory as rules


def start(account_id, root_cid, kind_hint='auto', enrich=True):
    root_cid = numeric(root_cid)
    if kind_hint not in ('auto', 'movie', 'tv') or type(enrich) is not bool:
        raise CloudError('库存类型或海报开关无效')
    with store.db() as con:
        account = con.execute('SELECT revision FROM cloud_accounts WHERE id=?', (account_id,)).fetchone()
        if not account:
            raise CloudError('账号不存在', 404)
        payload = {'account_id': account_id, 'account_revision': account[0], 'source_cid': root_cid,
                   'kind_hint': kind_hint, 'enrich': enrich}
        active = con.execute("SELECT id,payload FROM cloud_jobs WHERE kind='inventory' AND state IN ('queued','running') AND json_extract(payload,'$.account_id')=? AND json_extract(payload,'$.source_cid')=?", (account_id, root_cid)).fetchone()
        if active:
            if json.loads(active[1]) != payload:
                raise CloudError('此目录已有不同选项的库存扫描，请先等待或停止原任务', 409)
            return active[0]
        return jobs.enqueue('inventory', payload, con)


def _current(payload):
    account = store.account(payload['account_id'])
    if not account or account['revision'] != payload['account_revision']:
        raise CloudError('扫描期间账号授权变化，请重新扫描', 409)


def run(job_id, payload):
    provider = workflow.verify_account(payload)
    root_id = workflow._directory(provider, payload['source_cid'])
    root = provider.info(root_id) if root_id != '0' else {'id': '0', 'parent_id': '0', 'name': '全部文件', 'is_dir': True}
    folders = []
    jobs.progress(job_id, None, 0, '读取盘内库存', root['name'])
    files = workflow.walk(provider, root_id, job_id=job_id, directory_rows=folders)
    jobs.checkpoint(job_id)
    config = settings.get()
    try:
        catalogue = rules.collect(root, folders, files, set(config['video_extensions']) | recognition.VIDEO,
                                  payload['kind_hint'], config['movie_folder'], config['tv_folder'])
    except ValueError as exc:
        raise CloudError(str(exc), 409) from None
    items = catalogue.pop('items')
    metadata, failed, pending = {}, 0, 0
    for index, item in enumerate(items):
        jobs.checkpoint(job_id)
        _current(payload)
        jobs.progress(job_id, len(items), index, '整理库存记录', item['title'])
        identity = (item['kind'], item['tmdb_id'])
        if payload['enrich'] and item['kind'] in ('movie', 'tv') and item['tmdb_id']:
            if identity not in metadata and len(metadata) < 200:
                try:
                    detail = recognition.tmdb().get('/' + item['kind'] + '/' + item['tmdb_id'])
                    if str(detail.get('id')) != item['tmdb_id']:
                        raise ValueError('identity mismatch')
                    metadata[identity] = {'title': detail.get('title') or detail.get('name') or item['title'],
                                          'poster': detail.get('poster_path') or '', 'metadata_verified': True}
                except Exception:
                    metadata[identity] = None
            value = metadata.get(identity)
            if value:
                item.update(value)
            elif identity in metadata:
                failed += 1
            else:
                pending += 1
        jobs.progress(job_id, len(items), index + 1, '整理库存记录', item['title'])
    jobs.checkpoint(job_id)
    _current(payload)
    def save_snapshot():
        with store.db() as con:
            account = con.execute('SELECT revision FROM cloud_accounts WHERE id=?', (payload['account_id'],)).fetchone()
            if not account or account[0] != payload['account_revision']:
                raise CloudError('账号已变化，未更新库存', 409)
            if con.execute('SELECT 1 FROM cloud_job_controls WHERE job_id=? AND stop_requested=1', (job_id,)).fetchone():
                raise jobs.JobStopped()
            for item in items:
                con.execute('INSERT INTO cloud_inventory_items VALUES(?,?,?,?,?)',
                            (job_id, payload['account_id'], item['cid'], store.encode(item), time.time()))
        return {'media_count': len(items)}
    jobs.step(job_id, 'inventory-snapshot', {'root_cid': root_id}, save_snapshot)
    return {'inventory': {'root_cid': root_id, 'root_name': root['name'], 'media_count': len(items),
                          **catalogue, 'metadata_failed': failed, 'metadata_pending': pending,
                          'note': '仅完整成功的扫描会显示为最新库存；目录标记和文件名不代表已确认 Emby 入库'}}


def listing(account_id, root_cid='0', kind='', keyword='', offset=0, limit=48):
    root_cid = numeric(root_cid)
    account = store.account(account_id)
    if not account:
        raise CloudError('账号不存在', 404)
    with store.db() as con:
        latest = con.execute("SELECT id,updated,result FROM cloud_jobs WHERE kind='inventory' AND state='success' AND json_extract(payload,'$.account_id')=? AND json_extract(payload,'$.account_revision')=? AND json_extract(payload,'$.source_cid')=? ORDER BY created DESC,id DESC LIMIT 1", (account_id, account['revision'], root_cid)).fetchone()
        active = con.execute("SELECT id FROM cloud_jobs WHERE kind='inventory' AND state IN ('queued','running') AND json_extract(payload,'$.account_id')=? AND json_extract(payload,'$.account_revision')=? AND json_extract(payload,'$.source_cid')=? ORDER BY created DESC LIMIT 1", (account_id, account['revision'], root_cid)).fetchone()
        if not latest:
            return {'items': [], 'total': 0, 'has_snapshot': False, 'active_job': active[0] if active else '', 'counts': {}}
        rows = [json.loads(row[0]) for row in con.execute('SELECT payload FROM cloud_inventory_items WHERE job_id=? AND account_id=? ORDER BY cid', (latest[0], account_id))]
    counts = {key: sum(item['kind'] == key for item in rows) for key in ('movie', 'tv', '')}
    counts['all'] = len(rows)
    selected = [item for item in rows if (not kind or item['kind'] == ('' if kind == 'unknown' else kind)) and
                (not keyword or keyword.casefold() in (item['title'] + ' ' + item['path']).casefold())]
    selected.sort(key=lambda item: (item['title'], item['cid']))
    summary = json.loads(latest[2]).get('inventory', {})
    return {'items': selected[offset:offset + limit], 'total': len(selected), 'counts': counts,
            'has_snapshot': True, 'scan_job': latest[0], 'updated': latest[1], 'summary': summary,
            'active_job': active[0] if active else '', 'has_more': offset + limit < len(selected)}
