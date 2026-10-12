"""On-demand TgtoDrive adapter. No discovery polling, media mutation or retries.

Only the documented business routes below can be called. Resource identities are
kept server-side; a browser can submit a selected handle, never an arbitrary URL.
Remote write acknowledgement is distinct from confirmed Emby library presence.
"""
import hashlib
import json
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from . import config, state_store

ROUTES = {
    'spec': ('GET', '/api/openapi/spec'),
    'providers': ('GET', '/api/media/providers'),
    'search': ('POST', '/api/media/resources/search'),
    'transfer': ('POST', '/api/media/resources/transfer'),
    'history': ('GET', '/api/organize-history'),
    'detail': ('GET', '/api/organize-history/detail'),
}
MAX_RESPONSE = 2 * 1024 * 1024
MAX_HANDLES = 500
HANDLE_TTL = 600
_NETWORK = threading.BoundedSemaphore(2)
_WRITE = threading.Lock()
_LOCK = threading.RLock()
_handles = {}
_cap_cache = {}


class UpstreamError(ValueError):
    def __init__(self, message, code=400):
        super().__init__(message)
        self.code = code


def url_value(value):
    if not isinstance(value, str) or len(value) > 1024:
        raise UpstreamError('请填写 TgtoDrive 的 HTTP/HTTPS 服务地址')
    value = value.strip().rstrip('/')
    try:
        parts = urllib.parse.urlsplit(value)
        if (parts.scheme not in ('http', 'https') or not parts.hostname or
                parts.username is not None or parts.password is not None or
                parts.query or parts.fragment or
                any(c.isspace() or ord(c) < 32 or c in '\\<>"\x7f' for c in value) or
                (parts.port is not None and not 1 <= parts.port <= 65535) or
                any(p in ('.', '..') for p in urllib.parse.unquote(parts.path).split('/'))):
            raise ValueError()
    except ValueError:
        raise UpstreamError('地址不能包含令牌、用户名、查询参数或无效路径') from None
    return value


def integer(value, name, lo, hi):
    if isinstance(value, bool) or not re.fullmatch(r'\d+', str(value)):
        raise UpstreamError(name + '必须为整数')
    number = int(value)
    if not lo <= number <= hi:
        raise UpstreamError('%s范围为 %s～%s' % (name, lo, hi))
    return number


def settings(body=None, saved=None):
    saved = config.load_config() if saved is None else saved
    body = body or {}
    url = body.get('url', saved.get('tgto_url', ''))
    url = url_value(url) if url else ''
    token = body.get('token') or ''
    if not isinstance(token, str):
        raise UpstreamError('令牌格式无效')
    if body.get('clear_token') is True:
        token = ''
    elif not token or config.is_masked_value(token):
        if url != saved.get('tgto_url', '').rstrip('/') and saved.get('tgto_token'):
            raise UpstreamError('更换上游地址后请重新填写令牌，不能沿用旧地址的令牌')
        token = saved.get('tgto_token', '')
    token = token.strip()
    if len(token) > 2048 or any(c.isspace() or ord(c) < 32 for c in token):
        raise UpstreamError('请填写完整 Bearer 令牌，不包含 Bearer 前缀')
    enabled = body.get('enabled', saved.get('tgto_enabled') == '1')
    if not isinstance(enabled, bool):
        raise UpstreamError('启用状态必须为布尔值')
    points = integer(body.get('max_points', saved.get('tgto_max_points', '0')), '积分上限', 0, 10000)
    if enabled and (not url or not token):
        raise UpstreamError('启用上游接入前请填写地址和令牌')
    return {'url': url, 'token': token, 'enabled': enabled, 'max_points': points}


def public_settings(cfg):
    return {k: v for k, v in cfg.items() if k != 'token'} | {'has_token': bool(cfg['token'])}


def save_settings(body):
    saved = config.load_config()
    cfg = settings(body, saved)
    saved.update(tgto_url=cfg['url'], tgto_token=cfg['token'],
                 tgto_enabled='1' if cfg['enabled'] else '0', tgto_max_points=str(cfg['max_points']))
    config.save_config(saved)
    return public_settings(cfg)


def fingerprint(cfg):
    return hashlib.sha256((cfg['url'] + '\0' + cfg['token']).encode()).hexdigest()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request(action, payload=None, query=None, cfg=None):
    cfg = cfg or settings()
    if not cfg['url'] or not cfg['token']:
        raise UpstreamError('请先在服务连接中配置 TgtoDrive 地址和令牌')
    method, path = ROUTES[action]
    if not _NETWORK.acquire(blocking=False):
        raise UpstreamError('上游请求正在处理，请稍后再试', 429)
    try:
        url = cfg['url'] + path
        if query:
            url += '?' + urllib.parse.urlencode(query)
        data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            'Authorization': 'Bearer ' + cfg['token'], 'Accept': 'application/json',
            'Content-Type': 'application/json', 'User-Agent': 'StrimKeep-TgtoDrive'})
        # NAS service traffic never uses an external/system HTTP proxy. Do not
        # forward administrator credentials through redirect or login pages.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        try:
            with opener.open(req, timeout=20) as response:
                content_type = response.headers.get('Content-Type', '').lower()
                raw = response.read(MAX_RESPONSE + 1)
                if len(raw) > MAX_RESPONSE:
                    raise UpstreamError('上游响应过大，请缩小查询范围', 502)
                if 'application/json' not in content_type:
                    raise UpstreamError('上游返回了非 JSON 内容，请检查地址、反向代理和令牌', 502)
                try:
                    result = json.loads(raw)
                except (ValueError, UnicodeError):
                    raise UpstreamError('上游 JSON 响应无效', 502) from None
        except urllib.error.HTTPError as error:
            code = error.code
            error.close()
            message = {401: '上游令牌无效或已撤销', 403: '上游拒绝此操作，请检查授权与业务条件',
                       404: '上游当前版本没有此接口', 429: '上游限流，请稍后再试'}.get(code)
            raise UpstreamError(message or '上游 HTTP %s，请查看上游日志' % code, 502) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise UpstreamError('上游连接中断或超时，请检查服务连接；写入结果需在上游核实', 502) from None
        if not isinstance(result, (dict, list)):
            raise UpstreamError('上游响应结构不受支持', 502)
        if isinstance(result, dict) and (result.get('success') is False or result.get('ok') is False or
                str(result.get('status', '')).lower() in ('error', 'failed', 'failure')):
            raise UpstreamError('上游报告操作失败，请查看上游日志中的具体原因', 502)
        return result
    finally:
        _NETWORK.release()


def capabilities(cfg=None, force=False):
    cfg = cfg or settings()
    key = fingerprint(cfg)
    with _LOCK:
        cached = _cap_cache.get(key)
        if cached and not force and time.monotonic() - cached['time'] < 300:
            return dict(cached['data'])
    spec = request('spec', cfg=cfg)
    if not isinstance(spec, dict) or not isinstance(spec.get('paths'), dict):
        raise UpstreamError('上游没有返回有效的 OpenAPI 规范', 502)
    found = {name: isinstance(spec['paths'].get(path), dict) and method.lower() in spec['paths'][path]
             for name, (method, path) in ROUTES.items()}
    info = spec.get('info') if isinstance(spec.get('info'), dict) else {}
    found['version'] = str(info.get('version') or '')[:40]
    with _LOCK:
        _cap_cache.clear()
        _cap_cache[key] = {'time': time.monotonic(), 'data': found}
    return found


def enabled_settings(action):
    cfg = settings()
    if not cfg['enabled']:
        raise UpstreamError('上游接入未启用，请在规则设置 → 服务连接中启用', 409)
    if not capabilities(cfg).get(action):
        raise UpstreamError('上游当前版本不支持此功能', 409)
    return cfg


def text(value, limit=300):
    return str(value)[:limit] if isinstance(value, (str, int, float)) and not isinstance(value, bool) else ''


def resource_text(row, *keys, limit=300):
    """Only public display fields; tolerate scalar and string-list variants."""
    for key in keys:
        value = row.get(key)
        if isinstance(value, list):
            value = ' · '.join(filter(None, (text(x, 80) for x in value[:16])))
        result = text(value, limit)
        if result:
            return result
    return ''


def resource_fields(row):
    """Read bounded known metadata envelopes, never forward the raw response."""
    fields = [row]
    for key in ('metadata', 'resource_info', 'attributes', 'extra', 're0'):
        value = row.get(key)
        if isinstance(value, dict):
            fields.append(value)
    return fields


def resource_display(row, *keys, limit=300):
    for fields in resource_fields(row):
        value = resource_text(fields, *keys, limit=limit)
        if value:
            return value
    return ''


def resource_flag(row, *keys):
    for fields in resource_fields(row):
        for key in keys:
            value = fields.get(key)
            if value is True or value == 1 or (isinstance(value, str) and value.lower() in ('true', '1', 'yes')):
                return True
            if value is False or value == 0 or (isinstance(value, str) and value.lower() in ('false', '0', 'no')):
                return False
    return None


def resource_metadata(row):
    unlocked = resource_flag(row, 'is_unlocked', 'unlocked', 'has_unlocked', 'already_unlocked')
    if unlocked is None:
        status = resource_display(row, 'unlock_status', 'access_status', limit=40).lower()
        if status in ('unlocked', '已解锁'):
            unlocked = True
        elif status in ('locked', '未解锁'):
            unlocked = False
    if unlocked is None:
        locked = resource_flag(row, 'is_locked', 'locked')
        if locked is not None:
            unlocked = not locked
    verification = resource_display(row, 'verification', 'verification_status', 'validation_status', 'check_status', limit=80)
    if not verification:
        valid = resource_flag(row, 'is_valid', 'link_valid', 'validated')
        verification = '校验有效' if valid is True else ('校验无效' if valid is False else '')
    return {
        'publisher': resource_display(row, 'publisher', 'publisher_name', 'sharer', 'share_user', 'uploader', 'author', limit=100),
        'points': resource_display(row, 'points', 'unlock_points', 'points_cost', 'point_cost', 'required_points', 'cost_points', 'unlock_cost', limit=40),
        'unlocked': unlocked,
        'unlock_count': resource_display(row, 'unlock_count', 'unlocked_count', 'unlock_people', 'unlocked_users_count', 'unlock_user_count', limit=20),
        'reviewed_count': resource_display(row, 'reviewed_count', 'review_count', 'reviewer_count', limit=20),
        'subtitles': resource_display(row, 'subtitles', 'subtitle', 'subtitle_tags', limit=200),
        'verification': verification,
        'note': resource_display(row, 'note', 'notes', 'remark', 'remarks', 'description', limit=600),
        'specs': resource_display(row, 'specs', 'quality_tags', 'format_tags', limit=300),
        'audio': resource_display(row, 'audio', 'audio_codec', 'audio_format', limit=100),
    }


def collection(value, keys):
    """Known response envelopes only. Unknown is an error, never fake empty."""
    for _ in range(8):
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            for key in keys:
                if isinstance(value.get(key), list):
                    return value[key]
            value = value.get('data')
        else:
            break
    raise UpstreamError('上游返回结构暂不兼容，请提供脱敏响应样例以扩展适配', 502)


def providers():
    """Read only explicit transfer-target groups; never guess resource sources as targets."""
    cfg = enabled_settings('providers')
    result = request('providers', cfg=cfg)
    value = result
    rows = None
    for _ in range(8):
        if not isinstance(value, dict):
            break
        for key in ('transfer_targets', 'transfer_providers', 'targets', 'netdisk_providers'):
            group = value.get(key)
            if isinstance(group, list):
                rows = group
                break
            if isinstance(group, dict):
                rows = [dict(v, id=k) if isinstance(v, dict) else {'id': k, 'name': v}
                        for k, v in list(group.items())[:40]]
                break
        if rows is not None:
            break
        value = value.get('data')
    if rows is None:
        raise UpstreamError('暂未识别上游转存目标列表，请在高级设置填写上游目标标识', 502)
    targets, seen = [], set()
    for row in rows[:40]:
        if isinstance(row, str):
            row = {'id': row, 'name': row}
        if not isinstance(row, dict) or row.get('enabled') is False:
            continue
        key = text(row.get('id') or row.get('provider') or row.get('key'), 50)
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,50}', key) or key in seen:
            continue
        seen.add(key)
        targets.append({'id': key, 'name': text(row.get('name') or row.get('label') or key, 80)})
    return {'items': targets}


def search(body):
    cfg = enabled_settings('search')
    title = body.get('title')
    if not isinstance(title, str) or not 1 <= len(title.strip()) <= 200:
        raise UpstreamError('请输入作品名称（最多 200 字）')
    media = body.get('media_type')
    if media not in ('movie', 'tv'):
        raise UpstreamError('请选择电影或剧集')
    payload = {'title': title.strip(), 'media_type': media}
    for key, lo, hi in [('tmdb_id', 1, 999999999), ('year', 1800, 2200),
                        ('season', 0, 999), ('episode', 1, 99999)]:
        if body.get(key) not in (None, ''):
            payload[key] = integer(body[key], key, lo, hi)
    if media == 'movie' and ('season' in payload or 'episode' in payload):
        raise UpstreamError('电影不支持季集筛选')
    if 'episode' in payload and 'season' not in payload:
        raise UpstreamError('指定集数时请同时指定季数')
    rows = collection(request('search', payload, cfg=cfg), ('resources', 'results', 'items', 'list'))
    candidates = []
    now = time.monotonic()
    with _LOCK:
        for handle in list(_handles):
            if now - _handles[handle]['time'] >= HANDLE_TTL:
                del _handles[handle]
        for row in rows[:50]:
            if not isinstance(row, dict):
                continue
            # No invented IDs: bare `id` is not a subscription candidate ID.
            identity = {key: row[key] for key in ('source', 'slug', 'share_url', 'candidate_item_id')
                        if isinstance(row.get(key), (str, int)) and row[key] != ''}
            valid = (bool(identity.get('candidate_item_id')) or
                     bool(identity.get('source') and (identity.get('slug') or identity.get('share_url'))))
            if len(json.dumps(identity)) > 4096:
                valid = False
            handle = secrets.token_urlsafe(24) if valid else ''
            if valid:
                while len(_handles) >= MAX_HANDLES:
                    del _handles[next(iter(_handles))]
                _handles[handle] = {'identity': identity, 'time': now, 'connection': fingerprint(cfg),
                                    'title': payload['title']}
            candidates.append({'handle': handle, 'title': text(row.get('title') or row.get('name') or title),
                               'source': text(row.get('source'), 80),
                               'quality': text(row.get('quality') or row.get('resolution'), 100),
                               'size': resource_display(row, 'size', 'size_text', 'file_size_text', limit=80),
                               'provider': resource_display(row, 'provider', 'drive_type', 'netdisk', 'cloud_type', limit=80),
                               'resolution': resource_display(row, 'resolution', limit=40),
                               'release_type': resource_display(row, 'release_type', 'video_type', limit=60),
                               'video_effect': resource_display(row, 'video_effect', 'dynamic_range', 'hdr', limit=80),
                               'codec': resource_display(row, 'codec', 'video_codec', limit=60),
                               'release_group': resource_display(row, 'release_group', 'group', limit=80),
                               'file_count': resource_display(row, 'file_count', limit=20),
                               **resource_metadata(row), 'transferable': valid})
    return {'items': candidates, 'truncated': len(rows) > 50,
            'message': '资源仅为候选，请核对季集和版本后转存；入库状态由 Emby 确认'}


def _job_path():
    return config.DATA_DIR / 'state' / 'tgto_jobs.json'


def _jobs():
    jobs = state_store.read(_job_path(), {})
    if not isinstance(jobs, dict):
        raise UpstreamError('上游任务记录不可用', 503)
    return jobs


def public_job(job):
    return {k: job.get(k) for k in ('id', 'title', 'provider', 'state', 'created', 'message')}


def get_jobs():
    with _LOCK:
        jobs = _jobs()
        recovered = False
        if not _WRITE.locked():
            for job in jobs.values():
                if job['state'] == 'submitting':
                    job.update(state='unknown', message='提交进程已结束，结果未确认；请核实上游历史，不会自动重试')
                    recovered = True
        if recovered:
            state_store.save(_job_path(), jobs)
        return [public_job(j) for j in sorted(jobs.values(), key=lambda x: x['created'], reverse=True)][:30]


def transfer(body):
    if body.get('confirmed') is not True:
        raise UpstreamError('请先确认候选资源、转存目标和积分上限')
    operation = body.get('operation_id')
    if not isinstance(operation, str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,80}', operation):
        raise UpstreamError('请求编号无效')
    handle = body.get('handle')
    if not isinstance(handle, str) or not 1 <= len(handle) <= 100:
        raise UpstreamError('资源选择编号无效')
    cfg = enabled_settings('transfer')
    provider = body.get('provider')
    if not isinstance(provider, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,50}', provider):
        raise UpstreamError('请填写上游使用的目标网盘标识，例如 115')
    with _LOCK:
        jobs = _jobs()
        if operation in jobs:
            if jobs[operation]['provider'] != provider:
                raise UpstreamError('此请求编号已用于其他目标，请查看任务记录', 409)
            return public_job(jobs[operation])
        candidate = _handles.get(handle)
        if not candidate or time.monotonic() - candidate['time'] >= HANDLE_TTL:
            raise UpstreamError('资源选择已过期，请重新搜索', 409)
        if candidate['connection'] != fingerprint(cfg):
            raise UpstreamError('上游连接已变更，请重新搜索', 409)
        signature = hashlib.sha256(json.dumps([candidate['identity'], provider, fingerprint(cfg)],
                                              sort_keys=True).encode()).hexdigest()
        for job in jobs.values():
            if job.get('signature') == signature:
                return public_job(job)
        if len(jobs) >= 100:
            raise UpstreamError('上游任务记录已达上限，请先核实并清除已完成记录', 409)
        if not _WRITE.acquire(blocking=False):
            raise UpstreamError('已有转存正在提交，请稍后再试', 409)
        job = {'id': operation, 'title': candidate['title'], 'provider': provider,
               'signature': signature, 'state': 'submitting', 'created': time.time(),
               'message': '正在提交上游转存请求，请勿重复操作'}
        try:
            jobs[operation] = job
            state_store.save(_job_path(), jobs)
            payload = dict(candidate['identity'], provider=provider, enforce_points_limit=True,
                           max_points=cfg['max_points'])
            worker = threading.Thread(target=_submit, args=(operation, payload, cfg),
                                      name='strimkeep-tgto-transfer', daemon=True)
            worker.start()
        except Exception:
            _WRITE.release()
            raise
        return public_job(job)


def _submit(operation, payload, cfg):
    try:
        try:
            result = request('transfer', payload, cfg=cfg)
            acknowledged = isinstance(result, dict) and (
                result.get('success') is True or result.get('ok') is True or
                str(result.get('status', '')).lower() in ('success', 'ok', 'accepted', 'queued') or
                bool(result.get('task_id') or result.get('job_id') or result.get('job')))
            state = 'submitted' if acknowledged else 'unknown'
            message = ('上游已接收转存请求；请查看整理记录，完成 STRM 生成后等待 Emby 入库' if acknowledged else
                       '上游返回结果未能确认，请核实上游转存历史；不会自动重试')
        except Exception:
            # A timeout/HTTP error cannot prove that a remote write did not run.
            state, message = 'unknown', '转存结果未确认，请核实上游转存历史；不会自动重试'
        with _LOCK:
            jobs = _jobs()
            jobs[operation].update(state=state, message=message)
            state_store.save(_job_path(), jobs)
    finally:
        _WRITE.release()


def acknowledge(operation, confirmed):
    if confirmed is not True:
        raise UpstreamError('请先在上游核实此任务，清除记录会解除重复提交保护')
    with _LOCK:
        jobs = _jobs()
        if operation in jobs:
            if jobs[operation]['state'] == 'submitting' and _WRITE.locked():
                raise UpstreamError('请求仍在提交，请等待完成', 409)
            del jobs[operation]
            state_store.save(_job_path(), jobs)


def history(keyword, page=1):
    cfg = enabled_settings('history')
    if not isinstance(keyword, str) or not 1 <= len(keyword.strip()) <= 200:
        raise UpstreamError('请输入整理记录的搜索关键词')
    rows = collection(request('history', query={'keyword': keyword.strip(),
                      'page': integer(page, '页码', 1, 10000), 'limit': 20}, cfg=cfg),
                      ('records', 'history', 'items', 'list', 'results'))
    return [{'id': text(row.get('record_id') or row.get('id'), 40),
             'title': text(row.get('title') or row.get('name') or row.get('file_name')),
             'source': text(row.get('source'), 80), 'status': text(row.get('status'), 80)}
            for row in rows[:20] if isinstance(row, dict)]


def detail(record_id):
    cfg = enabled_settings('detail')
    record_id = integer(record_id, '整理记录编号', 1, 999999999999999)
    result = request('detail', query={'record_id': record_id}, cfg=cfg)
    # Explicit whitelist only: never return cloud cookies, bearer tokens, share
    # credentials, playback URLs or arbitrary upstream config to the browser.
    data = result.get('data', result) if isinstance(result, dict) else {}
    if isinstance(data, dict) and isinstance(data.get('record'), dict):
        data = data['record']
    if not isinstance(data, dict):
        raise UpstreamError('整理详情结构暂不兼容', 502)
    keys = ('record_id', 'id', 'title', 'name', 'year', 'tmdb_id', 'season', 'episode',
            'source', 'status', 'original_name', 'file_name', 'original_path',
            'target_path', 'strm_path', 'created_at')
    fields = {key: text(data[key], 1024) for key in keys if key in data and
              isinstance(data[key], (str, int, float))}
    if not fields:
        raise UpstreamError('整理详情字段暂不兼容，请提供脱敏响应样例', 502)
    return fields
