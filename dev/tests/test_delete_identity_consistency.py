"""Regression for Web deletion identities and durable dual-library projections.

Disposable STRM/cloud files only. All Emby/TMDB and notification calls are fake.
"""
import copy
import datetime
import time

import pytest
from app import engine, morning, emby_events, state_store, library_updates, config
from app.routers import library
from dev.tests.test_emby_callbacks import env, episode, record, api, job

_publish = library_updates.publish


@pytest.fixture(autouse=True)
def align_snapshot_store(env, monkeypatch):
    monkeypatch.setattr(engine, 'LIBRARY_SNAPSHOT_FILE', engine.STATE_DIR / 'health.json')
    monkeypatch.setattr(engine, 'MANUAL_DONE_FILE', engine.STATE_DIR / 'manual_done.json')


def pair(monkeypatch, mixed=False):
    a, b = episode(), episode('e2', 's2', 'share', 2)
    items = {'e1': a, 's1': record(), 'e2': b, 's2': record('s2', 'share')}
    series = {'s1': [a], 's2': [b]}
    if mixed:
        series = {'s1': [a, b], 's2': [a, b]}
    api(monkeypatch, items, series)
    emby_events.process_batch([job(a)])
    emby_events.process_batch([job(b)])
    return a, b, items, series


@pytest.mark.parametrize('failure', ['foreign', 'truncated', 'none', 'missing_owner', 'wrong_path', 'wrong_meta'])
@pytest.mark.parametrize('dry_run', [True, False])
def test_single_delete_refuses_uncertain_identity_before_mutation(env, monkeypatch, failure, dry_run):
    ep = episode()
    meta = record()
    if failure == 'foreign':
        ep['SeriesId'] = 'foreign'
    elif failure == 'missing_owner':
        ep.pop('SeriesId')
    elif failure == 'wrong_path':
        ep['Path'] = '/local/其他作品/evil.strm'
    elif failure == 'wrong_meta':
        meta['Id'] = 'other'
    def query(route, params, **kw):
        if 'Ids' in params:
            return {'Items': [meta], 'TotalRecordCount': 1}
        if failure == 'none':
            return None
        return {'Items': [ep], 'TotalRecordCount': 5001 if failure == 'truncated' else 1}
    monkeypatch.setattr(engine, 'emby_request', query)
    monkeypatch.setattr(library, '_delete_title_files', lambda *a, **k: pytest.fail('must not mutate'))
    result = library._series_delete('s1', {'target': 'local', 'dry_run': dry_run})
    assert result['status'] == 'error' and not result.get('ghost')
    assert (engine.L_ROOT / '剧集/样本/Season 1/e1.strm').is_file()


@pytest.mark.parametrize('mixed', [False, True])
@pytest.mark.parametrize('target,expected', [('local', 'e1.strm'), ('share', 'e2.strm')])
def test_single_and_batch_collect_same_verified_target_files(env, monkeypatch, mixed, target, expected):
    from app import batch_library_delete
    pair(monkeypatch, mixed)
    captured = []
    monkeypatch.setattr(library, '_delete_title_files', lambda files, *a, **k:
                        captured.extend(files) or {'status': 'success', 'count': len(files)})
    result = library._series_delete('s1', {'target': target, 'dry_run': False})
    cached = morning.read_emby_lib_cache()['series'][0]
    files, _ = batch_library_delete._collect(dict(cached, members=cached['series_ids'], target=target))
    assert result['count'] == 1 and {p.name for p in captured} == {expected}
    assert files == sorted(str(p) for p in captured)


def test_single_exact_series_without_tmdb_can_be_cleaned(env, monkeypatch):
    ep, meta = episode(), record()
    meta['ProviderIds'] = {}
    api(monkeypatch, {'e1': ep, 's1': meta}, {'s1': [ep]})
    result = library._series_delete('s1', {'target': 'local', 'dry_run': True})
    assert result['status'] == 'success' and result['count'] == 1


@pytest.mark.parametrize('target', ['local', 'share'])
def test_real_single_delete_chain_preserves_other_library_and_reports_actual_counts(env, monkeypatch, target):
    a, b, _, _ = pair(monkeypatch, mixed=True)
    cloud = engine.DATA_DIR.parent / 'cloud'
    cloud.mkdir()
    monkeypatch.setattr(engine, 'CLOUD_L_ROOT', cloud)
    monkeypatch.setattr(engine, 'LOCK_FILE', engine.DATA_DIR / 'agent.lock')
    monkeypatch.setattr(engine, 'WASH_RESIDUAL_FILE', engine.STATE_DIR / 'residuals.json')
    video = cloud / '剧集/样本/Season 1/e1.mkv'
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_text('temporary video')
    companion = video.with_suffix('.ass'); companion.write_text('temporary subtitle')
    monkeypatch.setattr(engine, 'notify_emby_deleted', lambda *a, **k: None)
    monkeypatch.setattr(engine, 'write_audit_log', lambda *a, **k: None)
    monkeypatch.setattr(library_updates, 'publish', _publish)
    result = library._series_delete('s1', {'target': target, 'dry_run': False, 'series_name': '样本'})
    assert result['status'] == 'success' and result['strm_removed'] == 1 and not result['errors']
    assert result['cloud_removed'] == (1 if target == 'local' else 0)
    assert result['series_removed'] is False
    victim, survivor = (a, b) if target == 'local' else (b, a)
    assert not engine.emby_path_to_container(victim['Path']).exists()
    assert engine.emby_path_to_container(survivor['Path']).is_file()
    assert video.exists() is (target == 'share') and companion.exists() is (target == 'share')
    assert result['entry']['id'] == survivor['SeriesId']
    assert library_updates.changes(0)['version'] == 1


@pytest.mark.parametrize('target', ['local', 'share'])
@pytest.mark.parametrize('remove_metadata', [False, True])
def test_delete_reconciles_both_members_and_publishes_after_commit(env, monkeypatch, target, remove_metadata):
    a, b, items, by_series = pair(monkeypatch)
    victim = a if target == 'local' else b
    survivor = b if target == 'local' else a
    # Exercise a non-primary ID too, and real path disappearance.
    engine.emby_path_to_container(victim['Path']).unlink()
    items.pop(victim['Id'])
    by_series[victim['SeriesId']] = []
    if remove_metadata:
        items.pop(victim['SeriesId'])
    monkeypatch.setattr(library_updates, 'publish', _publish)
    info = morning.patch_emby_lib_cache_after_series_delete(victim['SeriesId'], target)
    assert info['remaining'] == 1 and info['entry']['id'] == survivor['SeriesId']
    assert info['entry']['series_ids'] == [survivor['SeriesId']]
    assert info['entry']['in_' + target] is False
    assert info['entry']['in_' + ('share' if target == 'local' else 'local')] is True
    assert info['entry']['have_eps'] == 1
    data = morning.read_emby_lib_cache()
    snap = state_store.read(engine.LIBRARY_SNAPSHOT_FILE)
    assert snap['episodes'] == data['series'][0]['have_eps'] == 1
    assert snap['facts_ts'] == data['facts_ts'] and data['ts'] <= data['facts_ts']
    assert state_store.read(engine._EMBY_OVERVIEW_CACHE_FILE)['data'] == data
    assert library_updates.changes(0)['version'] == 1
    assert library.api_library_changes(0)['full']  # retired member IDs force reconciliation


def test_missing_root_or_failed_remote_read_cannot_erase_other_library(env, monkeypatch):
    pair(monkeypatch)
    before = morning.read_emby_lib_cache()
    monkeypatch.setattr(engine, 'emby_request', lambda *a, **k: None)
    with pytest.raises(ValueError, match='响应不完整'):
        morning.patch_emby_lib_cache_after_series_delete('s1', 'local')
    assert morning.read_emby_lib_cache() == before


def test_missing_remote_series_with_existing_strm_keeps_old_snapshot(env, monkeypatch):
    a, _, items, by_series = pair(monkeypatch)
    before = morning.read_emby_lib_cache()
    items.pop('s1')
    by_series['s1'] = []
    with pytest.raises(RuntimeError, match='STRM 仍存在'):
        morning.patch_emby_lib_cache_after_series_delete('s1', 'local')
    assert engine.emby_path_to_container(a['Path']).exists()
    assert morning.read_emby_lib_cache() == before


def test_snapshot_write_failure_rolls_back_all_views_and_does_not_publish(env, monkeypatch):
    a, _, items, by_series = pair(monkeypatch)
    engine.emby_path_to_container(a['Path']).unlink()
    items.pop('e1'); by_series['s1'] = []
    previous = morning.read_emby_lib_cache()
    engine._emby_lib_cache.update(data=copy.deepcopy(previous), ts=1)
    monkeypatch.setattr(library_updates, 'publish', _publish)
    put = state_store._put
    def fail(con, name, value):
        if name == engine.LIBRARY_SNAPSHOT_FILE.stem:
            raise OSError('fixture disk full')
        return put(con, name, value)
    monkeypatch.setattr(state_store, '_put', fail)
    with pytest.raises(OSError):
        morning.patch_emby_lib_cache_after_series_delete('s1', 'local')
    assert morning.read_emby_lib_cache() == previous
    assert engine._emby_lib_cache['data'] == previous
    assert library_updates.changes(0)['version'] == 0


def test_concurrent_fact_write_is_preserved_instead_of_overwritten_by_resync(env, monkeypatch):
    a, _, items, by_series = pair(monkeypatch)
    engine.emby_path_to_container(a['Path']).unlink()
    items.pop('e1'); by_series['s1'] = []
    query = engine.emby_request
    latest = morning.read_emby_lib_cache()
    latest['concurrent_revision'] = 'must survive'
    def race(route, params, **kw):
        state_store.save(engine.EMBY_LIB_CACHE_FILE, latest)
        return query(route, params, **kw)
    monkeypatch.setattr(engine, 'emby_request', race)
    with pytest.raises(RuntimeError, match='其他更新'):
        morning.patch_emby_lib_cache_after_series_delete('s1', 'local')
    assert morning.read_emby_lib_cache() == latest


def test_manual_completion_follows_surviving_primary(env, monkeypatch):
    a, _, items, by_series = pair(monkeypatch)
    data = morning.read_emby_lib_cache()
    data['series'][0]['tmdb_info'] = {'tmdb_total': 3, 'tmdb_status': 'Ended',
                                     'seasons': [{'season': 1, 'tmdb': 3}], 'match_status': 'missing'}
    state_store.save(engine.EMBY_LIB_CACHE_FILE, data)
    state_store.save(engine.MANUAL_DONE_FILE, {'s1': {'name': '样本', 'ts': 1}})
    engine.emby_path_to_container(a['Path']).unlink()
    items.pop('e1'); by_series['s1'] = []
    morning.patch_emby_lib_cache_after_series_delete('s1', 'local')
    assert morning.read_manual_done()['s2'] == {'name': '样本', 'ts': 1}
    assert engine.cached_library_view()['series'][0]['_md'] is True


def movie(ident, lib):
    path = (engine.L_ROOT if lib == 'local' else engine.S_ROOT) / '电影/样本' / (ident + '.strm')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('fixture')
    return dict(record(ident, lib), Type='Movie', Path='/' + lib + '/电影/样本/' + path.name,
                DateCreated=datetime.datetime.now(datetime.timezone.utc).isoformat())


@pytest.mark.parametrize('target', ['local', 'share'])
def test_movie_deletion_switches_primary_and_remaining_callbacks_work(env, monkeypatch, target):
    a, b = movie('m1', 'local'), movie('m2', 'share')
    items = {'m1': a, 'm2': b}
    api(monkeypatch, items, {})
    emby_events.process_batch([job(a)]); emby_events.process_batch([job(b)])
    victim, survivor = (a, b) if target == 'local' else (b, a)
    engine.emby_path_to_container(victim['Path']).unlink()
    items.pop(victim['Id'])
    monkeypatch.setattr(library_updates, 'publish', _publish)
    assert morning.patch_emby_lib_cache_after_movie_delete('7', target) == 0
    row = morning.read_emby_lib_cache()['movies'][0]
    assert row['id'] == survivor['Id'] and row['ids'] == [survivor['Id']]
    assert row['path'] == survivor['Path'] and row['paths'] == [survivor['Path']]
    assert not row['in_' + target]
    emby_events.process_batch([job(survivor, 'library.updated')])
    assert morning.read_emby_lib_cache()['movies'][0]['ids'] == [survivor['Id']]


@pytest.mark.parametrize('mode', ['poll', 'webhook'])
@pytest.mark.parametrize('failure', ['no_key', 'partial', 'invalid'])
def test_failed_comparison_preserves_success_clock_and_old_valid_metadata(env, monkeypatch, mode, failure):
    pair(monkeypatch)
    previous = morning.read_emby_lib_cache()
    old = time.time() - 7200
    previous.update(ts=old, facts_ts=old)
    previous['series'][0]['tmdb_info'] = {
        'match_status': 'missing', 'tmdb_status': 'Ended', 'local_total': 2,
        'tmdb_total': 3, 'declared_total': 3, 'diff': -1,
        'poster': '/old.jpg', 'seasons': [{'season': 1, 'tmdb': 3}]}
    state_store.save(engine.EMBY_LIB_CACHE_FILE, previous)
    cfg = config.load_config(); cfg['ingest_mode'] = mode
    monkeypatch.setattr(config, 'load_config', lambda: copy.deepcopy(cfg))
    saves = []
    monkeypatch.setattr(config, 'save_config', lambda value: saves.append(value))
    monkeypatch.setattr(library_updates, 'publish', _publish)
    monkeypatch.setattr(morning, 'emby_library_overview', lambda **k: copy.deepcopy(previous))
    class Client:
        key = '' if failure == 'no_key' else 'key'
        cache = {}
        def save(self): pass
    monkeypatch.setattr(engine, 'Tmdb', Client)
    monkeypatch.setattr(morning, '_tmdb_bulk_get', lambda *a, **k:
                        ({('tv', '7'): None if failure == 'partial' else {'invalid': True}},
                         1 if failure == 'partial' else 0, {}))
    result = morning._refresh_tmdb_scan()
    assert result['status'] == 'success' and result['comparison_complete'] is False
    data = morning.read_emby_lib_cache()
    assert data['ts'] == old and not saves
    assert data['series'][0]['tmdb_info']['poster'] == '/old.jpg'
    assert data['series'][0]['tmdb_info']['match_status'] == 'missing'
    assert data['comparison_attempt_ts'] > old
    assert library_updates.changes(0)['full'] is True


@pytest.mark.parametrize('mode', ['poll', 'webhook'])
def test_successful_comparison_publishes_new_classification_without_forging_callback_facts(env, monkeypatch, mode):
    ep = episode(); api(monkeypatch, {'e1': ep, 's1': record()}, {'s1': [ep]})
    emby_events.process_batch([job(ep)])
    previous = morning.read_emby_lib_cache(); old = time.time() - 7200
    previous.update(ts=old, facts_ts=old)
    state_store.save(engine.EMBY_LIB_CACHE_FILE, previous)
    cfg = config.load_config(); cfg['ingest_mode'] = mode
    monkeypatch.setattr(config, 'load_config', lambda: copy.deepcopy(cfg))
    saves = []; monkeypatch.setattr(config, 'save_config', lambda value: saves.append(value))
    monkeypatch.setattr(library_updates, 'publish', _publish)
    monkeypatch.setattr(morning, 'emby_library_overview', lambda **k: copy.deepcopy(previous))
    class Client:
        key = 'fixture'
        cache = {}
        def save(self): pass
    monkeypatch.setattr(engine, 'Tmdb', Client)
    monkeypatch.setattr(morning, '_tmdb_bulk_get', lambda *a, **k:
                        ({('tv', '7'): {'status': 'Ended', 'seasons': [{'season_number': 1, 'episode_count': 1}]}}, 0, {}))
    assert morning._refresh_tmdb_scan()['comparison_complete']
    data = morning.read_emby_lib_cache()
    assert data['ts'] > old and data['series'][0]['tmdb_info']['match_status'] == 'aligned'
    if mode == 'webhook':
        assert data['facts_ts'] == old
    assert saves and library_updates.changes(0)['full']


def test_never_compared_partial_scan_keeps_zero_success_timestamp(env, monkeypatch):
    cfg = config.load_config(); cfg['ingest_mode'] = 'poll'
    monkeypatch.setattr(config, 'load_config', lambda: cfg)
    monkeypatch.setattr(config, 'save_config', lambda cfg: pytest.fail('not a successful comparison'))
    state_store.save(engine.EMBY_LIB_CACHE_FILE, {'ts': 0, 'facts_ts': 1, 'series': [], 'movies': []})
    monkeypatch.setattr(morning, 'emby_library_overview', lambda **kw: {'series': [], 'movies': []})
    class Client:
        key = ''
        cache = {}
        def save(self): pass
    monkeypatch.setattr(engine, 'Tmdb', Client)
    assert not morning._refresh_tmdb_scan()['comparison_complete']
    assert morning.read_emby_lib_cache()['ts'] == 0


@pytest.mark.parametrize('mode', ['poll', 'webhook'])
@pytest.mark.parametrize('change', ['delete', 'new_title'])
def test_slow_comparison_cannot_restore_a_deleted_title_or_claim_unscanned_new_title(env, monkeypatch, mode, change):
    ep = episode(); api(monkeypatch, {'e1': ep, 's1': record()}, {'s1': [ep]})
    emby_events.process_batch([job(ep)])
    previous = morning.read_emby_lib_cache()
    cfg = config.load_config(); cfg['ingest_mode'] = mode
    monkeypatch.setattr(config, 'load_config', lambda: copy.deepcopy(cfg))
    saves = []; monkeypatch.setattr(config, 'save_config', lambda value: saves.append(value))
    monkeypatch.setattr(morning, 'emby_library_overview', lambda **kw: copy.deepcopy(previous))
    class Client:
        key = 'fixture'
        cache = {}
        def save(self): pass
    monkeypatch.setattr(engine, 'Tmdb', Client)
    def bulk(*a, **k):
        current = copy.deepcopy(previous)
        if change == 'delete':
            current['series'] = []
        else:
            current['series'].append(dict(current['series'][0], id='s3', series_ids=['s3'], tmdb_id='8'))
        current['facts_ts'] = time.time()
        state_store.save(engine.EMBY_LIB_CACHE_FILE, current)
        return {('tv','7'): {'status':'Ended','seasons':[{'season_number':1,'episode_count':1}]}},0,{}
    monkeypatch.setattr(morning, '_tmdb_bulk_get', bulk)
    result = morning._refresh_tmdb_scan()
    assert result['status'] == 'success'
    rows = morning.read_emby_lib_cache()['series']
    if change == 'delete':
        assert rows == []
    else:
        assert {row['tmdb_id'] for row in rows} == {'7', '8'}
        assert not result['comparison_complete'] and not saves
