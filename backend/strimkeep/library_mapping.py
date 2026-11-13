"""片库映射的服务端分页/首屏/详情，判定口径与 1.2.12 一致。

对应1.2.12 口径：
  library_first_screen.py   首屏最多 48 张卡，按状态优先级排序，不下发路径
  library_pagination.py     过滤/排序/计数全部在服务端先算，再切 offset/limit
  library_response.py       Cache-Control: private, no-store（映射页不允许被缓存）
  routers/library.py        /preview、/page、/item 三个入口

差异说明：1.2.12 口径的状态来自它自己的入库目录（cached_library_view），
这里用 TMDB 对照结果的状态缓存（key: inventory-status:tv:<tmdb>），
由现有的「缺集对照」接口在返回时写入；没有缓存的条目按 pending 处理，
页面只对当前页的卡片请求对照，不整体扫库。
"""
import json
import hashlib
import time
import re
from pathlib import PurePosixPath

CATEGORY_PRIORITY = {'missing': 0, 'extra': 1, 'pending': 2, 'ongoing': 3,
                     'unmatched': 2, 'complete': 4, 'movie': 4}
# 1.2.12 的状态只有 缺集/超集/在更/完整/未匹配/待对照；旧的 unknown 归到 pending。
CATEGORY_ALIASES = {'unknown': 'pending'}
ONGOING_STATES = ('Returning Series', 'In Production', 'Planned', 'Pilot')
STATUS_FILTERS = ('all', 'complete', 'ongoing', 'missing', 'extra', 'pending', 'unknown', 'unmatched')
SCOPES = ('all', 'cloud', 'share', 'both')
MEDIA = ('all', 'tv', 'movie')
ITEM_KINDS = ('tv', 'movie')
PREVIEW_LIMIT = 48
PAGE_LIMIT = 80
NO_STORE = {'Cache-Control': 'private, no-store, max-age=0',
            'Pragma': 'no-cache', 'Vary': 'Authorization'}
STATUS_TTL = 6 * 3600
# 与 1.2.12 目录分类一致；同一作品在两库分类不同时允许同时命中。
PATH_CATEGORIES = {
    '儿童节目': ('儿童',), '演唱会': ('演唱会',), '综艺剧': ('综艺',),
    '动漫剧': ('动漫',), '动画电影': ('动画电影',), '纪录片': ('纪录',),
    '国产剧': ('国产剧集', '国产剧'), '欧美剧': ('欧美剧集', '欧美剧', '其他剧集'),
    '日韩剧': ('日韩剧集', '日韩剧'), '华语电影': ('国产电影', '华语电影'),
    '外语电影': ('欧美电影', '日韩电影', '外语电影', '其他电影'),
}


def path_categories(item, library_map=None):
    """目录分类：优先 Emby 媒体库名（按 TMDB 号匹配），否则退回目录关键词。

    library_map 形如 {'tv:123': '国产剧集'}，由 inventory_api 从 Emby 读取后传入；
    为 None/空时退回1.2.12 口径的目录关键词规则，保证 Emby 不可用时筛选仍可用。
    """
    if library_map:
        names = []
        for tmdb_id in (item.get('tmdb_ids') or [item.get('tmdb_id')]):
            name = library_map.get('%s:%s' % (item['kind'], tmdb_id))
            if name and name not in names:
                names.append(name)
        if names:
            return names
    found = set()
    for side in ('cloud', 'share'):
        for file in item.get(side, []):
            parts = str(file.get('path') or '').replace('\\', '/').split('/')
            for part in parts:
                if re.search(r'(?i)\{tmdb-\d+\}', part):
                    break
                matched = next((name for name, words in PATH_CATEGORIES.items()
                                if any(word in part for word in words)), None)
                if matched:
                    found.add(matched)
                    break
    return [name for name in PATH_CATEGORIES if name in found] or ['未分类']


def no_store(response):
    """映射页响应不允许被浏览器/反代缓存（1.2.12 口径 `_mapping_no_store`）。"""
    if response is not None:
        response.headers.update(NO_STORE)


def status_key(tmdb_id):
    return 'inventory-status:tv:%s' % tmdb_id


def work_revision(item):
    """Bind a cached verdict to the actual files used for its calculation."""
    rows = []
    for side in ('cloud', 'share'):
        for row in item.get(side, []):
            rows.append([side, str(row.get('id')), str(row.get('path')), row.get('season'), row.get('episodes')])
    raw = json.dumps(sorted(rows, key=lambda row: (row[0], row[1], row[2])), ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def remember_status(store, tmdb_id, data, item=None):
    """把一次「缺集对照」结果写进状态缓存，供分页/首屏直接使用。"""
    if not isinstance(data, dict):
        return
    missing = data.get('combined_missing')
    extra = data.get('combined_extra')
    if type(missing) is not int or type(extra) is not int:
        return
    ongoing = data.get('series_status') in ONGOING_STATES
    seasons = [s for s in (data.get('seasons') or []) if isinstance(s, dict)]
    # 1.2.12 口径：能算出结果就给 缺集/超集/在更/完整；
    # 只有 TMDB 本身查不到（没有季、取季失败）才算「待对照」。
    # 未播集没有播出日期、库存扫描有小问题，都不再把整部剧打成"待核实"：
    # 前者是连载剧的正常现象，后者用 partial 标记单独提示。
    if (not data.get('tmdb_complete') or not seasons or data.get('failed_seasons')
            or not any(s.get('expected') for s in seasons)):
        category, reason = 'pending', 'tmdb_incomplete'
    elif missing > 0:
        category, reason = ('ongoing' if ongoing else 'missing'), ''
    elif ongoing:
        category, reason = 'complete', ''
    elif extra > 0:
        category, reason = 'extra', ''
    else:
        category, reason = 'complete', ''
    store.set_meta(status_key(tmdb_id),
                   {'category': category, 'diff': missing - extra, 'missing': missing,
                    'extra': extra, 'partial': not data.get('inventory_complete'),
                    'unknown_air_date': sum(len(s.get('unknown_air_date') or []) for s in seasons),
                    'reason': reason, 'ts': int(time.time()),
                    'revision': work_revision(item) if item is not None else None})


def read_status(store, tmdb_id, item=None):
    value = store.meta_json(status_key(tmdb_id), None)
    if not isinstance(value, dict):
        return None
    stamp = value.get('ts')
    if (type(stamp) not in (int, float) or stamp > time.time() + 300 or time.time() - stamp > STATUS_TTL):
        return None
    if item is not None and value.get('revision') != work_revision(item):
        return None
    if value.get('category') == 'unknown':
        # 旧缓存里的 unknown 是"待核实"时代写下的，直接作废让页面重新对照。
        return None
    category = CATEGORY_ALIASES.get(value.get('category'), value.get('category'))
    # 旧版把已播集齐全的连载剧也缓存为 ongoing；无需整库重查即可恢复原版显示。
    if category == 'ongoing' and value.get('missing') == 0:
        category = 'complete'
    if category not in CATEGORY_PRIORITY:
        return None
    return {**value, 'category': category}


def _status_of(item, store):
    if item['kind'] == 'movie':
        return {'category': 'movie', 'diff': 0, 'missing': 0, 'extra': 0}
    value = read_status(store, item['tmdb_id'], item)
    if item.get('manual_complete') and value and value.get('category') in ('missing', 'ongoing'):
        return {**value, 'category': 'complete', 'manual': True}
    return value or {'category': 'pending', 'diff': 0, 'missing': 0, 'extra': 0}


def card(item, store, library_map=None):
    """一张映射卡：只有作品级信息，不含任何 STRM 路径或文件名。"""
    status = _status_of(item, store)
    seasons = sorted({row['season'] for row in item['cloud'] + item['share']
                      if type(row.get('season')) is int and row['season'] > 0})
    return {'kind': item['kind'], 'tmdb_id': item['tmdb_id'],
            'tmdb_ids': item.get('tmdb_ids') or [item['tmdb_id']],
            'title': item['title'], 'year': item.get('year'),
            'id': '%s-%s' % (item['kind'], item['tmdb_id']),
            'in_cloud': bool(item['cloud']), 'in_share': bool(item['share']),
            'cloud_files': len(item['cloud']), 'share_files': len(item['share']),
            'seasons': seasons, 'categories': path_categories(item, library_map),
            'identity_conflict': bool(item.get('identity_conflicts')),
            'manual_complete': bool(item.get('manual_complete')),
            'status': status['category'],
            'missing': int(status.get('missing') or 0),
            'extra': int(status.get('extra') or 0),
            'diff': int(status.get('diff') or 0)}


def _sort_key(row):
    """状态优先级 → 差异大的在前 → 标题字典序（1.2.12 口径 `_series_cmp`）。"""
    return (CATEGORY_PRIORITY.get(row['status'], 9), -abs(row['diff']),
            str(row['title'] or '').casefold(), int(row['tmdb_id'] or 0))


def unidentified_cards(snapshot):
    """Display registered STRMs without TMDB identity without authorizing deletion."""
    groups = {}
    for issue in snapshot.get('issues', []):
        if issue.get('reason') != 'identity_unresolved' or issue.get('side') not in ('cloud', 'share'):
            continue
        path = PurePosixPath(str(issue.get('path') or ''))
        if path.is_absolute() or len(path.parts) < 3 or '..' in path.parts:
            continue
        parent = path.parent
        if re.fullmatch(r'(?i)season[ ._-]*\d+', parent.name):
            parent = parent.parent
        # The top-level library name differs, so omit it when grouping sides.
        identity = PurePosixPath(*parent.parts[1:]).as_posix()
        if not identity or identity == '.':
            continue
        season_hint = any(re.fullmatch(r'(?i)season[ ._-]*\d+', part) for part in path.parts[:-1])
        episode_hint = bool(re.search(r'(?i)(?<![a-z0-9])s\d{1,2}[\s._-]*e\d{1,3}', path.stem))
        kind = 'tv' if season_hint or episode_hint else 'movie'
        key = (kind, identity.casefold())
        group = groups.setdefault(key, {'kind': kind, 'title': parent.name, 'cloud': [], 'share': []})
        group[issue['side']].append({'id': str(issue.get('id') or ''), 'path': path.as_posix(), 'name': path.name})
    cards = []
    for (kind, identity), value in groups.items():
        cloud, share = value['cloud'], value['share']
        synthetic = hashlib.sha256((kind + ':' + identity).encode('utf-8')).hexdigest()[:20]
        cats = path_categories({'kind':kind, 'cloud':cloud, 'share':share})
        cards.append({'kind':kind, 'tmdb_id':0, 'id':'unmatched-'+synthetic,
                      'title':value['title'], 'year':None, 'in_cloud':bool(cloud), 'in_share':bool(share),
                      'cloud_files':len(cloud), 'share_files':len(share), 'cloud':cloud, 'share':share,
                      'seasons':[], 'categories':cats, 'identity_conflict':False, 'manual_complete':False,
                      'status':'unmatched', 'missing':0, 'extra':0, 'diff':0, 'unidentified':True})
    return cards


def catalogue(snapshot, store, library_map=None):
    rows = [card(item, store, library_map) for item in snapshot['items']]
    rows.extend(unidentified_cards(snapshot))
    return sorted(rows, key=_sort_key)


def category_counts(rows):
    """目录分类的真实分布（只列有作品的分类，页面据此生成筛选项）。

    分类来自目录名关键词（如 分享影视库/剧集/国产剧集/…），与 Emby 媒体库划分无关。
    """
    counts = {}
    for row in rows:
        for name in row['categories']:
            counts[name] = counts.get(name, 0) + 1
    # 分类名可能是 Emby 的库名（剧集/动漫/影片…），不能再按写死的目录清单过滤。
    return [{'name': name, 'count': count}
            for name, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


def stats(rows):
    counts = {name: 0 for name in CATEGORY_PRIORITY}
    for row in rows:
        counts[row['status']] = counts.get(row['status'], 0) + 1
    return {'total': len(rows), 'by_status': counts, 'all_categories': category_counts(rows),
            'both': sum(1 for row in rows if row['in_cloud'] and row['in_share']),
            'cloud_only': sum(1 for row in rows if row['in_cloud'] and not row['in_share']),
            'share_only': sum(1 for row in rows if row['in_share'] and not row['in_cloud'])}


def _filtered(rows, media, status, scope, keyword, category='all'):
    out = []
    for row in rows:
        if media != 'all' and row['kind'] != media:
            continue
        if scope == 'cloud' and not row['in_cloud']:
            continue
        if scope == 'share' and not row['in_share']:
            continue
        if scope == 'both' and not (row['in_cloud'] and row['in_share']):
            continue
        if status != 'all' and row['status'] != status:
            continue
        if category != 'all' and category not in row['categories']:
            continue
        if keyword and keyword not in str(row['title'] or '').casefold() \
                and keyword not in str(row['tmdb_id']):
            continue
        out.append(row)
    return out


def preview(snapshot, store, library_map=None):
    """首屏：每类最多 48 张卡，第一屏就够渲染，不等全库（1.2.12 口径 `preview`）。"""
    rows = catalogue(snapshot, store, library_map)
    return {'rows': rows[:PREVIEW_LIMIT],
            'tv': [row for row in rows if row['kind'] == 'tv'][:PREVIEW_LIMIT],
            'movie': [row for row in rows if row['kind'] == 'movie'][:PREVIEW_LIMIT],
            'stats': stats(rows), 'limit': PREVIEW_LIMIT}


def page(snapshot, store, *, media='tv', status='all', scope='all', keyword='',
         category='all', offset=0, limit=40, library_map=None):
    """服务端分页：过滤与计数先算完再切片，计数是全局的（1.2.12 口径 `page`）。"""
    status = CATEGORY_ALIASES.get(status, status)
    if media not in MEDIA or status not in STATUS_FILTERS or scope not in SCOPES:
        raise ValueError('无效片库分页参数')
    if not isinstance(keyword, str) or len(keyword) > 120:
        raise ValueError('无效片库分页参数')
    if type(offset) is not int or not 0 <= offset <= 500000:
        raise ValueError('无效片库分页参数')
    if type(limit) is not int or not 1 <= limit <= PAGE_LIMIT:
        raise ValueError('无效片库分页参数')
    rows = catalogue(snapshot, store, library_map)
    if category != 'all' and category not in {name for row in rows for name in row['categories']}:
        raise ValueError('无效片库分页参数')
    selected = _filtered(rows, media, status, scope, keyword.strip().casefold(), category)
    window = selected[offset:offset + limit]
    return {'media': media, 'offset': offset, 'limit': limit,
            'total': len(selected), 'has_more': offset + len(window) < len(selected),
            'next_offset': offset + len(window), 'rows': window,
            'all_categories': category_counts(rows),
            'stats': stats(selected), 'all_stats': stats(rows)}


def detail(snapshot, store, kind, tmdb_id, library_map=None):
    """详情：保留完整文件清单（只给单部作品，量可控）。"""
    if kind not in ITEM_KINDS or type(tmdb_id) is not int or tmdb_id <= 0:
        raise ValueError('无效片库条目')
    item = next((row for row in snapshot['items']
                 if row['kind'] == kind and row['tmdb_id'] == tmdb_id), None)
    if item is None:
        return None
    return {**card(item, store, library_map), 'cloud': item['cloud'], 'share': item['share'],
            'identity_conflicts': item.get('identity_conflicts', [])}
