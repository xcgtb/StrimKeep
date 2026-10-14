"""Explicit organizer cleanup and durable delayed STRM publication."""

import time
from app.cloud import store, settings, jobs
from app.cloud.errors import CloudError

OUTPUT_KEYS = ('public_url', 'output_root', 'emby_output_root', 'nfo_enabled', 'artwork_enabled')


def output_snapshot():
    config = settings.get()
    return {k: config[k] for k in OUTPUT_KEYS}


def defer(job_id, account_id, revision, scope, row, media, companions, seconds):
    """Queue + due marker are committed together, once per completed source item."""
    with store.db() as con:
        existing = con.execute('SELECT job_id,due FROM cloud_job_due WHERE origin_job=? AND file_id=?', (job_id, row['id'])).fetchone()
        if existing:
            return {'job_id': existing[0], 'due': existing[1], 'file_id': row['id']}
        due = time.time()+seconds
        payload = {'account_id': account_id, 'account_revision': revision, 'scope_cid': scope,
                   'source': row, 'media': media, 'companions': companions, 'output_snapshot': output_snapshot()}
        jid = jobs.enqueue('publish', payload, con)
        con.execute('INSERT INTO cloud_job_due VALUES(?,?,?,?,?)', (jid, due, job_id, row['id'], time.time()))
    return {'job_id': jid, 'due': due, 'file_id': row['id']}


def publish(job_id, payload):
    from app.cloud import workflow, strm
    provider = workflow.verify_account(payload)
    if payload['output_snapshot'] != output_snapshot():
        raise CloudError('STRM 输出配置已变化，请核对后从 STRM 页面重新同步', 409)
    row = payload['source']
    for source in [row, *payload['companions']]:
        if not workflow._snapshot_equal(source, provider.info(source['id'])):
            raise CloudError('延时期间源文件或关联文件变化，请重新预览同步', 409)
    parent, visited = row['parent_id'], set()
    while parent != payload['scope_cid']:
        if parent == '0' or parent in visited or len(visited) >= 64:
            raise CloudError('延时源文件已离开整理归档范围', 409)
        visited.add(parent)
        folder = provider.info(parent)
        if not folder['is_dir']:
            raise CloudError('归档目录身份异常', 409)
        parent = folder['parent_id']
    jobs.progress(job_id, 1, 0, '延时生成 STRM', row['name'])
    asset = strm.generate(payload['account_id'], row, payload['media'], payload['scope_cid'], payload['companions'], provider)
    jobs.progress(job_id, 1, 1, 'STRM 已生成', row['name'])
    return {'generated': [{'asset_id': asset['id'], 'path': asset['local_path']}],
            'errors': [{'file_id': row['id'], 'reason': v} for v in asset['payload'].get('warnings', [])],
            'refresh_pending': not workflow.refresh_emby()}


def cleanup(provider, job_id, plan, moved_parents):
    from app.cloud import directories
    candidates = {d['id']: d for d in plan.get('cleanup_candidates', [])}
    protected = {'0', plan['source_cid'], plan['target_cid'], plan.get('unknown_cid')}
    protected.update(d['id'] for d in directories.get(plan['account_id'])['directories'].values() if d)
    selected = {}
    # Only ancestors of successfully relocated videos, never unrelated preexisting empty folders.
    for parent in moved_parents:
        trail, seen = [], set()
        while parent in candidates and parent not in seen and parent not in protected:
            seen.add(parent)
            trail.append(candidates[parent])
            parent = candidates[parent]['parent_id']
        if parent == plan['source_cid']:
            for depth, folder in enumerate(reversed(trail)):
                selected[folder['id']] = (depth, folder)
    result = {'removed': [], 'kept': []}
    for _, snapshot in sorted(selected.values(), key=lambda item: item[0], reverse=True):
        if jobs.stopping(job_id):
            result['stopped'] = True
            break
        fid = snapshot['id']
        try:
            def remove():
                # Revalidate the full ancestor chain, not only the selected folder's parent ID.
                parent, seen = fid, set()
                while parent != plan['source_cid']:
                    if parent not in candidates or parent in seen or parent in protected:
                        raise CloudError('空目录已离开原来源范围，保留目录', 409)
                    seen.add(parent)
                    expected = candidates[parent]
                    current = provider.info(parent)
                    if not current['is_dir'] or any(current[k] != expected[k] for k in ('id','parent_id','name')):
                        raise CloudError('来源目录身份或位置变化，保留目录', 409)
                    parent = current['parent_id']
                # Provider performs a second emptiness check immediately before the write.
                if provider.list(fid):
                    return {'kept': True, 'reason': '仍有文件或子目录'}
                return provider.remove_empty_directory(snapshot)
            receipt = jobs.step(job_id, 'empty-directory:'+fid, snapshot, remove)
            result['kept' if receipt.get('kept') else 'removed'].append({'id': fid, 'name': snapshot['name'], **receipt})
        except CloudError as error:
            if error.uncertain:
                error.cleanup_result = result
                raise
            result['kept'].append({'id': fid, 'name': snapshot['name'], 'reason': str(error)})
    return result
