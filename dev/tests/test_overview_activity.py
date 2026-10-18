"""Dashboard activity uses only persisted facts and acknowledged messages."""
from app import overview_activity as activity
from app import engine, state_store


def project(**kwargs):
    args = dict(health={}, cache={}, ingest={}, subscriptions=[], report={}, state={},
                notification={}, morning_cfg={}, now=100000)
    args.update(kwargs)
    return activity.build(**args)


def test_recent_preserves_real_season_ranges_records_and_unknown_season():
    ingest = {'ts': 100000, 'tree': {'tv_detail': {'分享': {'动漫': {
        '跨季剧': {'last_ts': 99999, 'count': 4, 'seasons': {
            '0': {'episodes': [1]}, '1': {'episodes': [2, 3, 3]}}},
        '未编号': {'last_ts': 99998, 'count': 2, 'seasons': {}}}}},
        'mov_detail': {'本地': {'电影': {'旧片': 1, '新片': 99997}}}}}
    rows = project(ingest=ingest)['recent']
    assert [r['title'] for r in rows] == ['跨季剧', '未编号', '新片']
    assert rows[0]['episode_count'] == 3 and rows[0]['count'] == 4
    assert rows[0]['episodes'] == 'S00E01 · S01E02–E03'
    assert rows[1]['episodes'] == '' and rows[1]['episode_count'] is None
    assert rows[0]['identity_known'] is False


def test_duplicate_titles_with_distinct_identities_do_not_open_wrong_work():
    ingest = {'tree': {'mov_detail': {'本地': {'电影': {'同名电影': 99999}}}}}
    cache = {'movies': [{'name': '同名电影', 'tmdb_id': '1', 'year': 2000},
                        {'name': '同名电影', 'tmdb_id': '2', 'year': 2020}]}
    assert not project(cache=cache, ingest=ingest)['recent'][0]['identity_known']
    cache['movies'][1]['tmdb_id'] = '1'
    result = project(cache=cache, ingest=ingest)['recent'][0]
    assert result['identity_known'] and result['tmdb_id'] == '1'


def test_following_reuses_acknowledged_report_and_no_inferred_library_progress():
    result = project(cache={'series': [{'name': '剧', 'tmdb_id': '10',
                             'tmdb_info': {'poster': '/abc.jpg'}, 'have_eps': 100}]},
        report={'ts': 99000, 'updates': [{'name': '剧', 'tmdb_id': '10', 'new_eps': ['S01E02']} ]},
        subscriptions=[{'id': 'sub1', 'name': '剧', 'tmdb_id': '10', 'enabled': True},
                       {'id': 'off', 'name': '停用', 'enabled': False}],
        state={'sub1': {'latest_ep': 'S01E02'}}, notification={'pending': 0})
    assert result['following'][0]['detail'] == '新增 S01E02'
    assert result['following'][0]['poster'] == '/api/tmdb/poster/abc.jpg'
    assert result['watching'][0]['detail'] == '最近通知 S01E02'
    assert result['subscriptions'] == {'total': 2, 'enabled': 1, 'pending': 0}
    assert project()['subscriptions']['pending'] is None
    assert project()['following'] == []


def test_projection_bounds_and_missing_unknowns():
    result = project(health={'stale': True, 'ts': 50, 'top_missing': [
        {'name': str(i), 'diff': i, 'have': 0} for i in range(50)]},
        subscriptions=[{'name': str(i)} for i in range(40)])
    assert len(result['missing']) == 3 and len(result['watching']) == 2
    assert result['missing'][0]['diff'] == 0 and result['missing'][0]['have'] == 0
    assert result['missing'][0]['total'] is None and result['missing_stale']


def test_repeat_reads_reuse_projection_and_expire_without_remote_scan(tmp_path, monkeypatch):
    from app import config, morning
    monkeypatch.setattr(engine, 'STATE_DIR', tmp_path)
    for name in ['EMBY_LIB_CACHE_FILE', 'INGEST_CACHE_FILE', 'SUB_STATE_FILE']:
        monkeypatch.setattr(engine, name, tmp_path / (name + '.json'))
    monkeypatch.setattr(config, 'CONFIG_FILE', tmp_path / 'config.json')
    monkeypatch.setattr(activity, '_cache', {'key': None, 'data': None})
    clock = [100000]
    monkeypatch.setattr(activity.time, 'time', lambda: clock[0])
    calls = []
    monkeypatch.setattr(morning, 'read_emby_lib_cache', lambda **kw: calls.append('cache') or {})
    monkeypatch.setattr(engine, 'read_ingest_cache', lambda: {'tree': {'mov_detail': {'本地': {'电影': {'片': 99999}}}}})
    monkeypatch.setattr(config, 'get_subscriptions', lambda: [])
    monkeypatch.setattr(config, 'get_morning_report', lambda: {})
    monkeypatch.setattr(engine, '_load_subscription_report', lambda: {})
    monkeypatch.setattr(engine, '_load_sub_state', lambda: {})
    monkeypatch.setattr(engine, 'load_subscription_notification_status', lambda: {})
    monkeypatch.setattr(engine, 'emby_request', lambda *a, **kw: (_ for _ in ()).throw(AssertionError('network forbidden')))
    first = activity.snapshot(); first['recent'].clear()
    assert activity.snapshot()['recent'][0]['title'] == '片'
    assert calls == ['cache']
    clock[0] += 86400
    assert activity.snapshot()['recent'] == [] and calls == ['cache']
    state_store.save(engine.INGEST_CACHE_FILE, {'ts': clock[0]})
    activity.snapshot()
    assert calls == ['cache', 'cache']


def test_missing_poster_uses_cached_metadata_without_remote_lookup():
    result = project(health={'top_missing': [
        {'name': '黑道教父', 'year': 2025, 'diff': 10, 'have': 30, 'tot': 40},
        {'name': '无法匹配', 'diff': 2, 'have': 2, 'tot': 4},
    ]}, cache={'series': [
        {'name': '黑道教父', 'year': 2025, 'tmdb_id': '1234',
         'tmdb_info': {'poster': '/season.jpg'}},
    ]})
    missing = result['missing']
    assert missing[0]['poster'] == '/api/tmdb/poster/season.jpg'
    assert missing[0]['diff'] == 10 and missing[0]['have'] == 30
    assert missing[1]['poster'] == ''


def test_missing_poster_disambiguates_same_title_by_year():
    result = project(health={'top_missing': [
        {'name': '同名作品', 'year': 2024, 'diff': 3, 'have': 7, 'tot': 10},
        {'name': '同名作品', 'year': 2020, 'diff': 5, 'have': 5, 'tot': 10},
        {'name': '同名作品', 'diff': 2, 'have': 8, 'tot': 10},
    ]}, cache={'series': [
        {'name': '同名作品', 'year': 2020, 'tmdb_id': '22', 'has_image': True, 'id': 'emby22'},
        {'name': '同名作品', 'year': 2024, 'tmdb_id': '44',
         'tmdb_info': {'poster': '/2024.jpg'}},
    ]})
    missing = result['missing']
    assert missing[0]['tmdb_id'] == '44'
    assert missing[0]['poster'] == '/api/tmdb/poster/2024.jpg'
    assert missing[1]['tmdb_id'] == '22'
    assert missing[1]['poster'] == '/api/emby/poster/emby22'
    assert missing[2]['poster'] == ''  # unknown year, ambiguous identity
