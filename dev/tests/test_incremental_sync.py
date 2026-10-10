"""P0 incrementality and memory guard regressions; no live Emby credentials required."""
import datetime
import time

import pytest
from app import engine, emby, ingest, morning, overview, state_store


def row(ident, kind='Episode', minutes_ago=0, *, series='show', path=None):
    created = (datetime.datetime.now(datetime.timezone.utc) -
               datetime.timedelta(minutes=minutes_ago)).strftime('%Y-%m-%dT%H:%M:%S.0000000Z')
    obj = {'Id': str(ident), 'DateCreated': created,
           'Path': path or f'/media/{ident}.strm', 'Name': f'item{ident}',
           'ProductionYear': 2026}
    if kind == 'Episode':
        obj.update(SeriesId=series, SeriesName=series, ParentIndexNumber=1,
                   IndexNumber=int(ident) if str(ident).isdigit() else 1)
    return obj


@pytest.fixture
def sandbox(monkeypatch, tmp_path):
    monkeypatch.setattr(engine, 'STATE_DIR', tmp_path)
    monkeypatch.setattr(engine, 'INGEST_CACHE_FILE', tmp_path / 'ingest_cache.json')
    monkeypatch.setattr(engine, 'EMBY_HOST', 'http://emby.test')
    monkeypatch.setattr(engine, 'refresh_mapping_cache_after_ingest', lambda x: {'status': 'ok'})
    return tmp_path


def mock_endpoint(monkeypatch, records):
    calls = []

    def send(path, params=None, **kw):
        calls.append(dict(params))
        values = sorted(records[params['IncludeItemTypes']],
                        key=lambda r: r['DateCreated'], reverse=True)
        # Deliberately ignore MinDateCreated, as some Emby servers do.
        start, limit = params['StartIndex'], params['Limit']
        return {'Items': values[start:start + limit], 'TotalRecordCount': len(values)}
    monkeypatch.setattr(engine, 'emby_request', send)
    return calls


def test_recent_pagination_stops_at_cutoff_even_when_server_ignores_filter(sandbox, monkeypatch):
    records = {'Episode': [row(i, minutes_ago=i) for i in range(60)], 'Movie': []}
    calls = mock_endpoint(monkeypatch, records)
    since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=10)
    recent = engine._paged_items({'IncludeItemTypes': 'Episode', 'MinDateCreated':
                                   since.strftime('%Y-%m-%dT%H:%M:%S.0000000Z'),
                                   'SortBy': 'DateCreated', 'SortOrder': 'Descending'}, page_size=5)
    assert len(recent) == 11 or len(recent) == 10  # clock fractional boundary
    assert len(calls) <= 3 and len(calls) < len(records['Episode']) // 5


def test_out_of_order_pagination_is_rejected_not_silently_truncated(sandbox, monkeypatch):
    def out_of_order(path, params=None, **kw):
        return {'Items': [row('a', minutes_ago=60), row('b', minutes_ago=0)], 'TotalRecordCount': 2}
    monkeypatch.setattr(engine, 'emby_request', out_of_order)
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=30)
    with pytest.raises(ValueError, match='排序异常'):
        engine._paged_items({'MinDateCreated': cutoff.strftime('%Y-%m-%dT%H:%M:%S.0000000Z'),
                            'SortBy': 'DateCreated', 'SortOrder': 'Descending'})


def test_ingest_cursor_merges_only_changes_and_preserves_24h_stats(sandbox, monkeypatch):
    records = {'Episode': [row(1, minutes_ago=15), row(2, minutes_ago=10)],
               'Movie': [row('movie', 'Movie', minutes_ago=25)]}
    calls = mock_endpoint(monkeypatch, records)
    updates = []
    monkeypatch.setattr(engine, 'refresh_mapping_cache_after_ingest', lambda x: updates.append(x))
    first = engine.refresh_ingest_cache()
    assert first['stats'] == {'movies': 1, 'series': 1, 'episodes': 2}
    idx = state_store.read(ingest._ingest_index_path())
    assert len(idx['episodes']) == 2 and len(idx['movies']) == 1
    # Next poll only requests the overlap, still includes the full 24h view.
    records['Episode'].append(row(3, minutes_ago=0))
    calls.clear()
    second = engine.refresh_ingest_cache()
    assert second['stats'] == {'movies': 1, 'series': 1, 'episodes': 3}
    assert {e['Id'] for e in state_store.read(ingest._ingest_index_path())['episodes']} == {'1', '2', '3'}
    assert [e['series_id'] for e in updates[-1]['episodes_raw']] == ['show']
    assert len(updates[-1]['episodes_raw']) == 1  # not every episode in the last 24 hours
    assert len(calls) == 2 and all(c['MinDateCreated'] for c in calls)
    assert all((datetime.datetime.now(datetime.timezone.utc) - engine.parse_dt(c['MinDateCreated'])).total_seconds() <= 1250 for c in calls)


def test_movie_only_ingest_sends_exact_changed_movie_ids(sandbox, monkeypatch):
    records = {'Episode': [], 'Movie': [row('m1', 'Movie', minutes_ago=12)]}
    mock_endpoint(monkeypatch, records)
    messages = []
    monkeypatch.setattr(engine, 'refresh_mapping_cache_after_ingest', lambda data: messages.append(data))
    assert engine.refresh_ingest_cache()['stats']['movies'] == 1
    assert [m['id'] for m in messages[-1]['movies_raw']] == ['m1']
    assert messages[-1]['episodes_raw'] == []
    # No new movies on the second poll => empty list, not a resend of m1.
    assert engine.refresh_ingest_cache()['ok']
    assert messages[-1]['movies_raw'] == []
    records['Movie'].append(row('m2', 'Movie', minutes_ago=0))
    assert engine.refresh_ingest_cache()['stats']['movies'] == 2
    assert [m['id'] for m in messages[-1]['movies_raw']] == ['m2']


def test_delta_error_preserves_prior_cache_and_does_not_advance_cursor(sandbox, monkeypatch):
    records = {'Episode': [row(1)], 'Movie': []}
    mock_endpoint(monkeypatch, records)
    first = engine.refresh_ingest_cache()
    previous = state_store.read(ingest._ingest_index_path())
    def failed(path, params=None, **kw):
        if params['IncludeItemTypes'] == 'Episode':
            raise TimeoutError('offline')
        return {'Items': [], 'TotalRecordCount': 0}
    monkeypatch.setattr(engine, 'emby_request', failed)
    result = engine.refresh_ingest_cache()
    assert result['stats'] == first['stats']
    assert 'offline' in result['stale_error']
    assert state_store.read(ingest._ingest_index_path()) == previous


def test_delta_expiry_drops_old_items_and_six_hour_reconciliation(sandbox, monkeypatch):
    records = {'Episode': [row(1, minutes_ago=60)], 'Movie': []}
    mock_endpoint(monkeypatch, records)
    engine.refresh_ingest_cache()
    old = state_store.read(ingest._ingest_index_path())
    old['episodes'][0]['DateCreated'] = row(2, minutes_ago=1441)['DateCreated']
    state_store.save(ingest._ingest_index_path(), old)
    assert engine.refresh_ingest_cache()['stats']['episodes'] == 0
    old = state_store.read(ingest._ingest_index_path())
    old['full_ts'] -= ingest._INGEST_RECONCILE + 1
    state_store.save(ingest._ingest_index_path(), old)
    records['Episode'] = [row(5, minutes_ago=2)]
    assert engine.refresh_ingest_cache()['stats']['episodes'] == 1
    assert state_store.read(ingest._ingest_index_path())['full_ts'] > old['full_ts']


def test_full_episode_paging_does_not_retain_cache(sandbox, monkeypatch):
    calls = []
    def fake(path, params=None, **kw):
        calls.append(params['StartIndex'])
        # Exact page size followed by the remainder.
        return {'Items': [{'Id': i} for i in range(params['StartIndex'], min(5300, params['StartIndex'] + params['Limit']))],
                'TotalRecordCount': 5300}
    monkeypatch.setattr(engine, 'emby_request', fake)
    engine._ep_cache['data'] = [{'retained': 'old'}]
    assert len(list(emby._fetch_all_episodes())) == 5300
    assert calls == [0, 5000] and engine._ep_cache['data'] is None


def test_periodic_overview_uses_cached_facts_and_hourly_stats(sandbox, monkeypatch):
    monkeypatch.setattr(engine, 'EMBY_KEY', 'yes')
    monkeypatch.setattr(engine, '_overview_refresh_lock', __import__('threading').Lock())
    monkeypatch.setattr(overview, '_job', {'running': False, 'id': 0, 'started_at': 0,
                         'finished_at': 0, 'phase': '', 'error': '', 'warnings': [], 'forced': False})
    monkeypatch.setattr(overview, '_memory', {'path': None, 'stamp': None, 'data': None, 'health_stamp': None})
    monkeypatch.setattr(overview, '_collect', lambda stats: {'schema': 1, 'ts': time.time(), 'emby_host': engine.EMBY_HOST,
                                                             'dashboard': {'localCount': '1', 'shareCount': '1',
                                                                           'libraryHealth': None}, 'library_stats': stats})
    today = time.time()
    state_store.save(engine.STATE_DIR / 'overview_full_facts.json', {'ts': today, 'host': engine.EMBY_HOST})
    state_store.save(engine.STATE_DIR / 'library_stats_cache.json', {'ts': today, 'rows': [], 'local_total': 1,
                                                                     'share_total': 1})
    monkeypatch.setattr(engine, '_recompute_all_stats', lambda: (_ for _ in ()).throw(AssertionError('full scan forbidden')))
    monkeypatch.setattr(engine, '_build_emby_library_overview', lambda: (_ for _ in ()).throw(AssertionError('Episode scan forbidden')))
    overview._refresh_worker(False)
    assert state_store.read(overview._path())['dashboard']['localCount'] == '1'
    assert overview._job['error'] == ''


def test_failed_facts_refresh_backs_off_without_corrupting_old_marker(sandbox, monkeypatch):
    monkeypatch.setattr(engine, '_build_emby_library_overview', lambda: (_ for _ in ()).throw(TimeoutError('offline')))
    old = time.time() - 86401
    state_store.save(engine.STATE_DIR / 'overview_full_facts.json', {'host': engine.EMBY_HOST, 'ts': old})
    with pytest.raises(TimeoutError):
        overview._refresh_facts()
    marker = state_store.read(engine.STATE_DIR / 'overview_full_facts.json')
    assert marker['ts'] == old
    assert marker['retry_after'] > time.time() + 3500
    assert overview._facts_reconcile_due() is False


def test_mapping_disk_cache_does_not_start_five_minute_full_refresh(sandbox, monkeypatch):
    disk = {'series': [], 'movies': []}
    monkeypatch.setattr(morning, '_load_overview_disk', lambda: {'ts': time.time() - 600, 'data': disk})
    monkeypatch.setattr(engine, '_emby_lib_cache', {'ts': 0, 'data': None})
    monkeypatch.setattr(morning.threading, 'Thread', lambda **kw: (_ for _ in ()).throw(AssertionError('full fetch scheduled')))
    assert morning.emby_library_overview() is disk


def test_known_file_deletion_invalidates_hourly_persisted_stats(sandbox, monkeypatch):
    from app import stats
    monkeypatch.setattr(engine, '_STRM_COUNT_CACHE_FILE', sandbox / 'counts.json')
    monkeypatch.setattr(engine, '_strm_count_cache', {'ts': time.time(), 'local': 10, 'share': 5})
    monkeypatch.setattr(engine, '_lib_stats_cache', {'ts': time.time(), 'data': {'rows': []}})
    state_store.save(sandbox / 'library_stats_cache.json', {'ts': time.time(), 'rows': [], 'local_total': 10})
    state_store.save(engine._STRM_COUNT_CACHE_FILE, {'ts': time.time(), 'local': 10})
    stats.invalidate_stats_cache()
    assert state_store.read(sandbox / 'library_stats_cache.json', None) is None
    assert state_store.read(engine._STRM_COUNT_CACHE_FILE, None) is None
    assert engine._lib_stats_cache['data'] is None
