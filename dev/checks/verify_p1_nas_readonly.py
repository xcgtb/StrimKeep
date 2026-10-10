#!/usr/bin/env python3
"""NAS P1 *read-only* field checks using a short-lived Docker process.

Run only with separate tmpfs /data, read-only /media/local and /media/share.
Accept Emby connection details on stdin, never in CLI args or logs.
No network writes or governance actions are allowed in this script.
"""
import copy
import json
import resource
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app import engine, morning, state_store, library_updates


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def main():
    settings = json.load(sys.stdin)
    engine.EMBY_HOST = settings['host']
    engine.EMBY_KEY = settings['key']
    engine.EMBY_PATHS = engine.emby_path_map(settings['local'], settings['share'])
    check(engine.EMBY_KEY and engine.EMBY_HOST.startswith(('http://', 'https://')),
          'Emby connection details missing')
    original_request = engine.emby_request
    def readonly_request(path, params=None, method='GET', timeout=None, body=None, retries=None):
        check(method == 'GET' and body is None, 'BLOCKED non-read Emby request')
        return original_request(path, params, method=method, timeout=timeout, body=body, retries=retries)
    engine.emby_request = readonly_request

    print('=== Live Emby series read-only verification ===', flush=True)
    records = engine.emby_request('/Items', {
        'Recursive': 'true', 'IncludeItemTypes': 'Series', 'SearchTerm': '美人余',
        'Fields': 'ProviderIds,Path,ProductionYear', 'Limit': 100
    }, timeout=20, retries=0).get('Items') or []
    candidates = [item for item in records
                  if str((item.get('ProviderIds') or {}).get('Tmdb') or '') == '294446']
    check(len(candidates) == 1,
          f'FIELD FAIL: expected exactly one verified TMDB 294446 series; got {len(candidates)}')
    series_id = str(candidates[0]['Id'])
    # Verify the precise Emby metadata route used by P1 before exercising
    # creation, so a server-specific API incompatibility is diagnostic.
    detail = engine.emby_request('/Items', {
        'Ids': series_id, 'Recursive': 'true',
        'IncludeItemTypes': 'Series',
        'Fields': 'ProviderIds,Path,ProductionYear,CommunityRating,ImageTags,Genres',
        'StartIndex': 0, 'Limit': 2,
    }, timeout=15, retries=0)
    matches = detail.get('Items') if isinstance(detail, dict) else None
    check(isinstance(matches, list) and len(matches) == 1 and
          str(matches[0].get('Id')) == series_id and matches[0].get('Type') == 'Series' and
          (detail.get('TotalRecordCount') is None or int(detail['TotalRecordCount']) == 1),
          f'FIELD FAIL: Emby Ids lookup did not return one exact Series ({series_id}); not safe to materialize')
    check(bool(matches[0].get('Path')),
          'FIELD FAIL: exact Series metadata lacks Path; cannot verify library location')
    print('READONLY Emby exact Series metadata lookup: PASS (GET /Items?Ids=...)', flush=True)
    live = morning._live_series_episodes(series_id)
    actual = {(int(e['ParentIndexNumber']), int(e['IndexNumber'])) for e in live
              if e.get('ParentIndexNumber') is not None and e.get('IndexNumber') is not None
              and int(e['ParentIndexNumber']) >= 0 and int(e['IndexNumber']) > 0
              and engine.EMBY_PATHS.lib_of(e.get('Path') or '') in ('local', 'share')}
    check((1, 5) in actual, 'Emby S01E05 missing, or live mount unreadable')
    print('READONLY Emby series ID discovered; live valid episodes:', len(actual), flush=True)

    # All mutations below target isolated /data tmpfs only.
    state_store.save(engine.EMBY_LIB_CACHE_FILE, {
        'ts': time.time() - 120, 'series': [], 'movies': [],
        'stats': {'total_series': 0}
    })
    payload = {'ok': True, 'episodes_raw': [{'series_id': series_id}]}
    created = engine.refresh_mapping_cache_after_ingest(payload)
    cache = state_store.read(engine.EMBY_LIB_CACHE_FILE)
    check(created.get('created') == 1 and len(cache['series']) == 1,
          f'P1 new series creation failed: {created}')
    row = cache['series'][0]
    check(row['have_eps'] == len(actual), 'new series episode count mismatch')
    check(not row.get('complete') and (row.get('tmdb_info') or {}).get('match_status') == 'pending',
          'new series incorrectly marked complete')
    check(library_updates.changes(0)['ids'] == [series_id], 'missing P1 version event')
    print('PASS: new series pending, accurate episode facts, bounded revision event', flush=True)

    # Simulate a one-episode-behind *isolated cache*. Real Emby and media stay unchanged.
    old = copy.deepcopy(row)
    old['have_eps'] = max(0, len(actual) - 1)
    old['local_eps'] = max(0, int(old.get('local_eps') or 0) - 1)
    old['share_eps'] = max(0, int(old.get('share_eps') or 0) - 1)
    cache['series'] = [old]
    state_store.save(engine.EMBY_LIB_CACHE_FILE, cache)
    existing = engine.refresh_mapping_cache_after_ingest(payload)
    cache = state_store.read(engine.EMBY_LIB_CACHE_FILE)
    check(existing.get('updated') == 1, f'existing series did not refresh: {existing}')
    check(cache['series'][0]['have_eps'] == len(actual), 'existing series count not updated')
    print('PASS: existing series catches up without a full-library Emby scan', flush=True)

    # Emby timeouts/partial results must never clobber the cached fact row.
    before = copy.deepcopy(state_store.read(engine.EMBY_LIB_CACHE_FILE))
    original_live = morning._live_series_episodes
    try:
        morning._live_series_episodes = lambda sid: (_ for _ in ()).throw(TimeoutError('simulated timeout'))
        failed = engine.refresh_mapping_cache_after_ingest(payload)
    finally:
        morning._live_series_episodes = original_live
    after = state_store.read(engine.EMBY_LIB_CACHE_FILE)
    check(failed.get('updated') == 0 and before == after, 'timeout overwrote a good cache')
    print('PASS: offline/timeout preserves known-good facts', flush=True)

    # Real movie sample: no full library fetch. Discover recent items first,
    # falling back to a small newest-items window; only one exact Movie ID may
    # materialize a card in our *isolated* SQLite.
    print('=== Live Emby movie read-only verification ===', flush=True)
    candidates = engine.emby_request('/Items', {
        'Recursive': 'true', 'IncludeItemTypes': 'Movie',
        'SortBy': 'DateCreated', 'SortOrder': 'Descending',
        'Fields': 'ProviderIds,Path,ProductionYear', 'StartIndex': 0, 'Limit': 40,
    }, timeout=20, retries=0).get('Items') or []
    selected = None
    for item in candidates:
        if (item.get('Id') and item.get('Path') and item.get('Name') and
                engine.EMBY_PATHS.lib_of(item.get('Path')) in ('local', 'share')):
            try:
                selected = morning._movie_metadata_by_id(str(item['Id']))
                break
            except (OSError, RuntimeError, TimeoutError, ValueError):
                continue
    check(selected is not None,
          'FIELD FAIL: no valid Movie sample in the last 40 Emby items; movie real-NAS field test incomplete')
    movie_id = str(selected['Id'])
    movie_data = engine.refresh_mapping_cache_after_ingest({
        'ok': True, 'movies_raw': [{'id': movie_id}]})
    cached_movie = state_store.read(engine.EMBY_LIB_CACHE_FILE)['movies']
    check(movie_data.get('movies_updated') == 1 and len(cached_movie) == 1 and
          str(cached_movie[0].get('id')) == movie_id,
          f'P1 real movie creation failed: {movie_data}')
    check(bool(cached_movie[0].get('in_local') or cached_movie[0].get('in_share')),
          'Movie source path is not classified')
    movie_revision = library_updates.changes(0)
    check(movie_id in movie_revision.get('movie_ids', []), 'P1 Movie revision was not recorded')
    print('PASS: read-only exact Movie new-card creation, verified location and movie revision', flush=True)
    # Simulate old local/share indicators in SQLite, not an Emby mutation.
    movie_old = copy.deepcopy(cached_movie[0])
    movie_old['in_local'] = False
    movie_old['in_share'] = False
    cache_now = state_store.read(engine.EMBY_LIB_CACHE_FILE)
    cache_now['movies'] = [movie_old]
    state_store.save(engine.EMBY_LIB_CACHE_FILE, cache_now)
    refreshed = engine.refresh_mapping_cache_after_ingest({
        'ok': True, 'movies_raw': [{'id': movie_id}]})
    check(refreshed.get('movies_updated') == 1 and
          any((m.get('in_local') or m.get('in_share')) for m in
              state_store.read(engine.EMBY_LIB_CACHE_FILE)['movies']),
          'Movie previously cached location not refreshed')
    print('PASS: existing movie recovers correct verified library flags', flush=True)
    movie_before = state_store.read(engine.EMBY_LIB_CACHE_FILE)
    original_meta = morning._movie_metadata_by_id
    try:
        morning._movie_metadata_by_id = lambda movie_id: (_ for _ in ()).throw(TimeoutError('simulated Movie timeout'))
        failed_movie = engine.refresh_mapping_cache_after_ingest({'ok': True, 'movies_raw': [{'id': movie_id}]})
    finally:
        morning._movie_metadata_by_id = original_meta
    check(failed_movie.get('movies_updated') == 0 and
          state_store.read(engine.EMBY_LIB_CACHE_FILE) == movie_before,
          'Movie timeout overwrote known-good data')
    print('PASS: movie timeout preserves known-good facts', flush=True)

    # A fast 2-step real Emby ingest test. Disable mapping writes for this phase;
    # the mapping scenario above already validated them in isolated tmpfs.
    engine.refresh_mapping_cache_after_ingest = lambda data: None
    first = engine.refresh_ingest_cache(hours=24)
    second = engine.refresh_ingest_cache(hours=24)
    check(first.get('ok') and second.get('ok'), 'P0/P1 ingest poll failed')
    # If records aged out or changed during testing, totals may legitimately differ.
    print('PASS: two recent-window ingests; 1st stats:', first.get('stats'),
          '2nd stats:', second.get('stats'), flush=True)
    print('Peak Python RSS: %.1f MiB' %
          (resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024), flush=True)
    print('NAS FIELD TEST PASS; no POST/DELETE sent to Emby; isolated SQLite and media read-only', flush=True)


if __name__ == '__main__':
    main()
