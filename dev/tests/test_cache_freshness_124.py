"""Successful metadata comparison stamps its own clock, never the Emby facts clock."""
import time

from app import engine, morning, state_store
from dev.tests.test_library_fact_counts import cached, series, write_cache


def test_full_tmdb_metadata_update_advances_comparison_not_facts(cached):
    old = time.time() - 3600
    facts = time.time() - 120
    write_cache([series(13)], ts=old, facts_ts=facts)
    morning._patch_all_caches(
        lambda data: (data['series'][0]['tmdb_info'].update(match_status='aligned') or True),
        comparison_ts=time.time())
    fresh = state_store.read(engine.EMBY_LIB_CACHE_FILE)
    assert fresh['ts'] > old
    assert fresh['facts_ts'] == facts
    assert fresh['series'][0]['tmdb_info']['match_status'] == 'aligned'


def test_callback_patch_can_advance_facts_without_falsifying_tmdb(cached):
    old = time.time() - 3600
    write_cache([series(13)], ts=old, facts_ts=old + 10)
    def callback(data):
        data['series'][0]['have_eps'] = 14
        data['facts_ts'] = time.time()
        return True
    morning._patch_all_caches(callback)
    got = state_store.read(engine.EMBY_LIB_CACHE_FILE)
    assert got['ts'] == old
    assert got['facts_ts'] > old
    assert got['series'][0]['have_eps'] == 14


def test_partial_metadata_patch_preserves_verified_clocks(cached):
    old = time.time() - 3600
    facts = time.time() - 300
    write_cache([series(13)], ts=old, facts_ts=facts)
    morning._patch_all_caches(
        lambda data: (data['series'][0]['tmdb_info'].update(poster='/ok.jpg') or True),
        comparison_ts=None)
    got = state_store.read(engine.EMBY_LIB_CACHE_FILE)
    assert got['ts'] == old and got['facts_ts'] == facts
    assert got['series'][0]['tmdb_info']['poster'] == '/ok.jpg'
