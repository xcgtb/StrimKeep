"""片库映射（服务端分页/首屏/详情）：对齐参考实现 1.2.12 的契约。"""
import pytest

from strimkeep.library_mapping import (card, category_counts, detail, no_store, page,
                                       preview, read_status, remember_status)


class Store:
    """只实现映射页用到的那两条 meta 读写。"""

    def __init__(self):
        self.data = {}

    def set_meta(self, key, value):
        self.data[key] = value

    def meta_json(self, key, default=None):
        return self.data.get(key, default)


def row(side, season, episode, quality='1080p'):
    return {'id': '%s-%s-%s-%s' % (side, season, episode, quality),
            'name': 'S%02dE%02d.%s.strm' % (season, episode, quality),
            'path': '/media/%s/S%02d/E%02d.strm' % (side, season, episode),
            'season': season, 'episodes': [episode], 'directory_mtime': 0}


def item(kind, tmdb_id, title, cloud=(), share=(), manual=False):
    return {'kind': kind, 'tmdb_id': tmdb_id, 'title': title,
            'cloud': list(cloud), 'share': list(share), 'manual_complete': manual}


def snapshot(*items):
    return {'complete': True, 'issues': [], 'items': list(items)}


def missing_reply(**overrides):
    value = {'tmdb_complete': True, 'inventory_complete': True, 'series_status': 'Ended',
             'combined_missing': 0, 'combined_extra': 0, 'failed_seasons': [],
             'seasons': [{'expected': [1], 'unknown_air_date': []}]}
    value.update(overrides)
    return value


def test_card_carries_no_paths_and_reports_counts():
    store = Store()
    result = card(item('tv', 1, '某剧', [row('cloud', 1, 1)], [row('share', 1, 1)]), store)
    assert result['status'] == 'pending'
    assert (result['cloud_files'], result['share_files']) == (1, 1)
    assert result['seasons'] == [1]
    assert 'path' not in result and 'name' not in result
    assert not any(isinstance(value, str) and ('/media/' in value or value.endswith('.strm'))
                   for value in result.values())


def test_status_cache_drives_filter_and_priority():
    store = Store()
    works = {tmdb: item('tv', tmdb, '剧 %d' % tmdb, [row('cloud', 1, 1)]) for tmdb in range(1, 7)}
    remember_status(store, 1, missing_reply(combined_missing=3), works[1])
    remember_status(store, 2, missing_reply(series_status='Returning Series', combined_missing=2), works[2])
    remember_status(store, 3, missing_reply(combined_extra=4), works[3])
    remember_status(store, 4, missing_reply(), works[4])
    remember_status(store, 5, missing_reply(inventory_complete=False), works[5])
    remember_status(store, 6, missing_reply(tmdb_complete=False), works[6])
    assert read_status(store, 1)['category'] == 'missing'
    assert read_status(store, 2)['category'] == 'ongoing'
    assert read_status(store, 3)['category'] == 'extra'
    assert read_status(store, 4)['category'] == 'complete'
    # 库存扫描有小问题不再把整部剧打成"待核实"：状态照常给，另标 partial。
    assert read_status(store, 5)['category'] == 'complete'
    assert read_status(store, 5)['partial'] is True
    assert read_status(store, 6)['category'] == 'pending'
    assert read_status(store, 7) is None

    data = snapshot(*works.values())
    rows = page(data, store, media='tv', limit=40)['rows']
    # 优先级：missing → extra → pending → ongoing → complete
    assert [row['tmdb_id'] for row in rows] == [1, 3, 6, 2, 4, 5]
    assert page(data, store, media='tv', status='missing')['total'] == 1
    # 旧链接里的 unknown 归到 pending
    assert page(data, store, media='tv', status='unknown')['total'] == 1
    assert page(data, store, media='tv', status='pending')['total'] == 1


def test_cache_tracks_inventory_changes_and_incomplete_official_data():
    store = Store()
    work = item('tv', 1, '剧', [row('cloud', 1, 1)])
    remember_status(store, 1, missing_reply(), work)
    assert card(work, store)['status'] == 'complete'
    changed = item('tv', 1, '剧', [row('cloud', 1, 1), row('cloud', 1, 2)])
    assert card(changed, store)['status'] == 'pending'
    remember_status(store, 1, missing_reply(seasons=[{'expected': [], 'unknown_air_date': [1]}]), changed)
    # 该季还没有已播集（没有 expected）→ 待对照，而不是"待核实"
    assert card(changed, store)['status'] == 'pending'
    store.data['inventory-status:tv:1']['ts'] -= 7 * 3600
    assert card(changed, store)['status'] == 'pending'


def test_page_totals_filters_and_window():
    store = Store()
    items = [item('tv', tmdb, '剧 %d' % tmdb,
                  [row('cloud', 1, 1)] if tmdb % 2 else [],
                  [row('share', 1, 1)] if tmdb % 3 else []) for tmdb in range(1, 11)]
    items.append(item('movie', 99, '电影', [row('cloud', 0, 0)], [row('share', 0, 0)]))
    data = snapshot(*items)

    first = page(data, store, media='tv', limit=4)
    assert (first['total'], len(first['rows']), first['has_more'], first['next_offset']) == (10, 4, True, 4)
    last = page(data, store, media='tv', offset=8, limit=4)
    assert (len(last['rows']), last['has_more'], last['next_offset']) == (2, False, 10)
    # 过滤在切片之前：计数是全局的
    both = page(data, store, media='tv', scope='both', limit=80)
    assert both['total'] == len(both['rows']) == sum(1 for row in both['rows'] if row['in_cloud'] and row['in_share'])
    only_share = page(data, store, media='tv', scope='share', limit=80)
    assert all(row['in_share'] for row in only_share['rows'])
    assert only_share['total'] == sum(1 for row in page(data, store, media='tv', limit=80)['rows']
                                      if row['in_share'])
    assert page(data, store, media='tv', keyword='剧 7')['total'] == 1
    assert page(data, store, media='movie')['total'] == 1
    assert page(data, store, media='tv')['all_stats']['total'] == 11


def test_page_rejects_invalid_parameters():
    for kwargs in ({'media': 'series'}, {'status': 'aligned'}, {'scope': 'allx'},
                   {'offset': -1}, {'limit': 0}, {'limit': 81}, {'keyword': 'x' * 121}):
        with pytest.raises(ValueError):
            page(snapshot(), Store(), **kwargs)


def test_preview_is_bounded_and_split_by_media():
    store = Store()
    items = [item('tv', tmdb, '剧 %d' % tmdb, [row('cloud', 1, 1)]) for tmdb in range(1, 61)]
    items += [item('movie', 1000 + tmdb, '电影 %d' % tmdb, [row('cloud', 0, 0)]) for tmdb in range(1, 11)]
    data = preview(snapshot(*items), store)
    assert len(data['tv']) == 48 and len(data['movie']) == 10
    assert data['limit'] == 48
    assert data['stats']['total'] == 70


def test_detail_returns_files_only_for_one_work():
    store = Store()
    data = snapshot(item('tv', 5, '某剧', [row('cloud', 1, 1)], [row('share', 1, 1)]))
    result = detail(data, store, 'tv', 5)
    assert result['title'] == '某剧'
    assert [file['path'] for file in result['cloud']] == ['/media/cloud/S01/E01.strm']
    assert detail(data, store, 'tv', 6) is None
    with pytest.raises(ValueError):
        detail(data, store, 'series', 5)


def test_no_store_headers_block_proxy_caching():
    class Response:
        headers = {}

    response = Response()
    no_store(response)
    assert response.headers['Cache-Control'] == 'private, no-store, max-age=0'
    assert response.headers['Pragma'] == 'no-cache'
    assert 'Authorization' in response.headers['Vary']


def test_directory_categories_filter_all_rows_before_pagination():
    store = Store()
    cloud = row('cloud',1,1); cloud['path'] = '媒体库/剧集/国产剧集/作品 {tmdb-1}/Season 1/S01E01.strm'
    share = row('share',1,1); share['path'] = '分享影视库/剧集/日韩剧集/作品 {tmdb-1}/Season 1/S01E01.strm'
    series = item('tv',1,'作品',[cloud],[share])
    other = row('share',1,1); other['path'] = '分享影视库/剧集/日韩剧集/其他 {tmdb-2}/Season 1/S01E01.strm'
    data = snapshot(series,item('tv',2,'其他',[],[other]))
    assert page(data,store,media='tv',category='国产剧')['total'] == 1
    result = page(data,store,media='tv',category='日韩剧',offset=1,limit=1)
    assert result['total'] == 2 and len(result['rows']) == 1
    first = card(series,store)
    assert set(first['categories']) == {'国产剧','日韩剧'}
    assert 'path' not in first and 'name' not in first
    with pytest.raises(ValueError): page(data,store,category='unknown-category')


def test_categories_prefer_emby_library_names():
    store = Store()
    work = item('tv', 7, '剧', [row('cloud', 1, 1)])
    # 没有 Emby 信息时退回目录关键词（这里路径里没有关键词 → 未分类）
    assert card(work, store)['categories'] == ['未分类']
    assert card(work, store, {'tv:7': '我的剧库'})['categories'] == ['我的剧库']


def test_page_filters_by_emby_category_and_rejects_unknown():
    store = Store()
    data = snapshot(item('tv', 7, '剧', [row('cloud', 1, 1)]),
                    item('tv', 8, '另一部', [row('cloud', 1, 1)]))
    names = {'tv:7': '我的剧库'}
    assert page(data, store, media='tv', category='我的剧库', library_map=names)['total'] == 1
    assert page(data, store, media='tv', category='未分类', library_map=names)['total'] == 1
    with pytest.raises(ValueError):
        page(data, store, media='tv', category='不存在的库', library_map=names)


def test_category_counts_uses_real_names():
    rows = [{'categories': ['剧集']}, {'categories': ['剧集']}, {'categories': ['动漫']}]
    assert category_counts(rows) == [{'name': '剧集', 'count': 2}, {'name': '动漫', 'count': 1}]
