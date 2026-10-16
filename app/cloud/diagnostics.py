"""Read-only explanations shared by tasks, logs and notifications.

Original result lists remain authoritative. Historical results are normalized on
read, so upgrading can explain old skipped files without rerunning a task.
"""
from app.cloud import events

LABELS = {'error': '执行异常', 'skipped': '识别或执行跳过', 'warning': '提示',
          'filtered': '规则过滤', 'retained': '版本保留', 'unknown': '待识别'}


def operation(result):
    return result.get('organization') if isinstance(result.get('organization'), dict) else result


def guidance(reason, category):
    if category == 'filtered':
        return '文件过滤', '核对命中的规则；如需处理此文件，调整过滤条件后重新预览来源目录。', 'organize', 'naming'
    if category == 'retained':
        return '版本比较', '按当前版本策略保留来源。只有需要改变优先级时才调整策略并重新预览。', 'organize', 'quality'
    if any(word in reason for word in ('凭证', '登录', '授权', 'Cookie', '401', '403')):
        return '账号与授权', '核对登录凭证并检查账号连接，再重新预览；已经提交且结果未知的操作先核实。', 'accounts', 'login'
    if 'TMDB' in reason and any(word in reason for word in ('密钥', '未配置', '连接', '限流', '不可用')):
        return 'TMDB 连接', '核对规则设置中的 TMDB 密钥和服务连接，恢复后重新预览识别。', 'settings', ''
    if any(word in reason for word in ('TMDB', '识别', '作品', '季集', '季号', '集号', '季、集', '片名')):
        return '影视识别', '核对片名、年份和 TMDB，剧集同时核对季集；修正作品后重新预览。', 'organize', 'run'
    if any(word in reason for word in ('相同名称', '命名冲突', '同名')):
        return '识别与命名', '核对重复版本和命名模板，选择保留的版本或修正作品后重新预览。', 'organize', 'naming'
    if any(word in reason for word in ('输出', '挂载', '写入', '不可写', '磁盘')):
        return 'STRM 输出', '核对 NAS 输出位置、挂载和写入权限，再从 STRM 页面重新预览同步。', 'strm', 'strm-output'
    if any(word in reason for word in ('源文件', '变化', '不存在', '移动结果', '指纹')):
        return '来源核验', '查看来源文件的当前名称与位置，确认后重新预览；已核实转存的资源无需重复转存。', 'files', ''
    if any(word in reason for word in ('ffprobe', '媒体信息', '探测')):
        return '媒体信息', '核对媒体探测设置和文件可读性，再重新预览；已完成的归档不需要回滚。', 'organize', 'naming'
    if 'Emby' in reason:
        return 'Emby 联动', '核对入库连接和路径映射，再刷新入库记录；生成 STRM 不等于已确认入库。', 'playback', 'emby'
    return '任务执行', '先核对文件与下面的具体原因，再修正配置并重新预览；结果未知的远端操作先核实，避免重复提交。', 'jobs', ''


def items(result):
    value = operation(result)
    rows, seen = [], set()
    for key, category in (('errors', 'error'), ('skipped', 'skipped'), ('warnings', 'warning'),
                          ('filtered', 'filtered'), ('quality_held', 'retained'), ('unknown', 'unknown')):
        entries = value.get(key, [])
        if not isinstance(entries, list):
            continue
        for entry in entries:
            entry = entry if isinstance(entry, dict) else {'reason': str(entry)}
            source = entry.get('source') if isinstance(entry.get('source'), dict) else {}
            reason = events.clean(entry.get('reason') or entry.get('message') or '原记录未保存具体原因，请核对任务步骤；不要根据数量推测原因。')
            actual = 'filtered' if entry.get('action') == 'filtered' else category
            if actual == 'retained' and entry.get('blocked'):
                actual = 'skipped'
            name = events.clean(entry.get('name') or source.get('name') or '')
            fid = str(entry.get('file_id') or source.get('id') or '')
            identity = (fid, name, reason)
            if identity in seen:
                continue
            seen.add(identity)
            stage, suggestion, page, module = guidance(reason, actual)
            rows.append({'category': actual, 'label': LABELS[actual], 'file_id': fid,
                         'name': name or ('文件 ID '+fid if fid else '任务级记录'),
                         'reason': reason, 'stage': events.clean(entry.get('stage') or stage),
                         'suggestion': suggestion, 'page': page, 'module': module,
                         'path': events.clean(entry.get('path') or entry.get('source_path') or ''),
                         'rule': events.clean(entry.get('rule') or ''),
                         'attention': actual in ('error', 'skipped', 'unknown')})
    if value.get('refresh_pending'):
        rows.append({'category': 'warning', 'label': '联动提示', 'name': 'Emby 刷新', 'file_id': '',
                     'reason': 'Emby 扫描请求未成功，STRM 生成结果仍然保留。', 'stage': 'Emby 联动',
                     'suggestion': guidance('Emby', 'warning')[1], 'page': 'playback', 'module': 'emby',
                     'path': '', 'rule': '', 'attention': True})
    return rows


def report(result, state='', message='', offset=0, limit=30, category='', query=''):
    rows = items(result)
    if not rows and state in ('failed', 'uncertain', 'needs_review'):
        reason = events.clean(message or '原任务没有保存逐文件原因，需核对步骤记录。')
        stage, suggestion, page, module = guidance(reason, 'error')
        rows.append({'category': 'error', 'label': '任务级异常', 'name': '任务级记录', 'file_id': '',
                     'reason': reason, 'stage': stage, 'suggestion': suggestion, 'page': page, 'module': module,
                     'path': '', 'rule': '', 'attention': True})
    counts = {key: sum(r['category'] == key for r in rows) for key in LABELS}
    needle = query.casefold()
    selected = [r for r in rows if (not category or r['category'] == category) and
                (not needle or needle in ' '.join(str(v) for v in r.values()).casefold())]
    return {'items': selected[offset:offset+limit], 'total': len(selected), 'offset': offset,
            'has_more': offset+limit < len(selected), 'counts': counts,
            'attention': sum(bool(r['attention']) for r in rows)}


def requires_review(result):
    value = operation(result)
    return bool(value.get('refresh_pending') or any(r['attention'] for r in items(result)
                if r['category'] in ('error', 'skipped')))


def for_job(job, offset=0, limit=30, category='', query=''):
    """Use a saved failure receipt if an exception left no per-file result."""
    from app.cloud import store
    import json
    result = job['result']
    if not items(result) and job['state'] in ('failed', 'uncertain', 'needs_review'):
        with store.db() as con:
            rows = con.execute("SELECT step_key,payload,result FROM cloud_steps WHERE job_id=? AND state!='verified' ORDER BY updated", (job['id'],)).fetchall()
        failures = []
        for key, payload, receipt in rows:
            payload, receipt = json.loads(payload), json.loads(receipt)
            if not isinstance(receipt, dict) or not receipt.get('reason'):
                continue
            failures.append({'file_id': payload.get('file_id', ''), 'name': payload.get('source_name') or payload.get('name') or '',
                             'reason': receipt['reason'], 'stage': {'rename': '重命名', 'move': '移动归档', 'receive': '分享转存', 'mkdir': '创建目录'}.get(key.split(':')[0], '任务执行')})
        if failures:
            result = {'errors': failures}
    return report(result, job['state'], job['message'], offset, limit, category, query)


def emit_result(job_id, result, state, message):
    rows = for_job({'id': job_id, 'result': result, 'state': state, 'message': message}, limit=100)['items']
    for row in rows:
        events.emit('recognize' if row['category'] == 'skipped' else 'organize',
                    row['label']+'：'+row['name']+' · '+row['reason']+'；处理建议：'+row['suggestion'],
                    job_id, level='WARNING' if row['attention'] else 'INFO',
                    details={'event': 'diagnostic', 'status': 'blocked' if row['attention'] else 'skipped',
                             'source_name': row['name'], 'file_id': row['file_id'], 'reason': row['reason'],
                             'suggestion': row['suggestion'], 'category': row['label'], 'stage': row['stage'],
                             'rule': row['rule']})
