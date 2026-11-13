"""Emby 媒体库归属：片库映射的「目录分类」直接读 Emby 的库。

1.2.12 口径把分类写死成目录关键词；StrimKeep 的库由用户在 Emby 里自己划分，
所以按 Emby 的库列表（CollectionFolder）建立「媒体库 → 作品」映射：
    tv:123   → 国产剧集
    movie:456 → 电影
缓存 30 分钟；Emby 不可用时写入失败缓存（5 分钟），调用方退回目录关键词。
"""
import time

CACHE_KEY = 'emby-library-map'
TTL = 30 * 60
FAIL_TTL = 5 * 60
MAX_PAGES = 20
PAGE_SIZE = 2000


def cached(store):
    value = store.meta_json(CACHE_KEY, None)
    if not isinstance(value, dict):
        return None
    stamp = value.get('ts')
    if type(stamp) not in (int, float) or stamp > time.time() + 300:
        return None
    if time.time() - stamp > (FAIL_TTL if value.get('error') else TTL):
        return None
    return value


def remember(store, value):
    store.set_meta(CACHE_KEY, value)


def remember_failure(store, message):
    store.set_meta(CACHE_KEY, {'error': str(message)[:200], 'ts': int(time.time())})


async def _get(http, api_url, key, path, params=None):
    from .network import emby_request
    response = await emby_request(http, 'GET', api_url + path,
                                  headers={'X-Emby-Token': key}, params=params or {}, timeout=60)
    response.raise_for_status()
    return response.json()


def _names(rows):
    out = []
    for entry in rows or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get('Name') or '')[:40]
        library_id = str(entry.get('ItemId') or entry.get('Id') or '')
        if name and library_id and not any(item['id'] == library_id for item in out):
            out.append({'id': library_id, 'name': name})
    return out


async def _libraries(http, api_url, key):
    """按优先级尝试三种取库列表的方式，返回 (库列表, 来源, 错误信息)。"""
    errors = []
    # 1) 管理接口：Emby 的「媒体库」列表（最准）
    try:
        data = await _get(http, api_url, key, '/Library/VirtualFolders')
        libraries = _names(data if isinstance(data, list) else (data or {}).get('Items'))
        if libraries:
            return libraries, 'VirtualFolders', errors
        errors.append('VirtualFolders: 空')
    except Exception as exc:  # noqa: BLE001
        errors.append('VirtualFolders: %s' % exc)
    # 2) 用户视图（Emby 网页左上角看到的那几个库）
    try:
        users = await _get(http, api_url, key, '/Users')
        user_id = next((str(u.get('Id')) for u in users or []
                        if isinstance(u, dict) and u.get('Id')), '')
        if user_id:
            views = await _get(http, api_url, key, '/Users/%s/Views' % user_id)
            libraries = _names((views or {}).get('Items'))
            if libraries:
                return libraries, 'Views', errors
            errors.append('Views: 空')
    except Exception as exc:  # noqa: BLE001
        errors.append('Views: %s' % exc)
    # 3) 递归找 CollectionFolder
    try:
        data = await _get(http, api_url, key, '/Items',
                          {'Recursive': 'true', 'IncludeItemTypes': 'CollectionFolder', 'Limit': 200})
        libraries = _names((data or {}).get('Items'))
        if libraries:
            return libraries, 'CollectionFolder', errors
        errors.append('CollectionFolder: 空')
    except Exception as exc:  # noqa: BLE001
        errors.append('CollectionFolder: %s' % exc)
    return [], 'none', errors


async def fetch(http, api_url, key):
    """读 Emby 的媒体库列表与库内作品，返回 {'libraries','by_tmdb','counts','source','errors'}。"""
    libraries, source, errors = await _libraries(http, api_url, key)
    by_tmdb, counts = {}, {}
    for library in libraries:
        start = 0
        for _ in range(MAX_PAGES):
            try:
                page = await _get(http, api_url, key, '/Items', {
                    'ParentId': library['id'], 'Recursive': 'true',
                    'IncludeItemTypes': 'Movie,Series', 'Fields': 'ProviderIds,ProductionYear',
                    'Limit': PAGE_SIZE, 'StartIndex': start})
            except Exception as exc:  # noqa: BLE001
                errors.append('%s 读取失败: %s' % (library['name'], exc))
                break
            rows = page.get('Items') if isinstance(page, dict) else None
            if not isinstance(rows, list):
                errors.append('%s: 响应不完整' % library['name'])
                break
            for row in rows:
                if not isinstance(row, dict):
                    continue
                providers = row.get('ProviderIds') or {}
                tmdb = str(providers.get('Tmdb') if isinstance(providers, dict) else '').strip()
                if not tmdb.isdigit():
                    continue
                kind = 'tv' if str(row.get('Type') or '').lower() == 'series' else 'movie'
                by_tmdb.setdefault('%s:%s' % (kind, int(tmdb)), library['name'])
                counts[library['name']] = counts.get(library['name'], 0) + 1
            total = page.get('TotalRecordCount')
            start += len(rows)
            if not rows or len(rows) < PAGE_SIZE or (type(total) is int and start >= total):
                break
    return {'libraries': [{'name': entry['name'], 'count': counts.get(entry['name'], 0)}
                          for entry in libraries],
            'by_tmdb': by_tmdb, 'counts': counts, 'source': source,
            'errors': errors[:5], 'ts': int(time.time())}
