"""Four real filesystem roots, actual SQLite and isolated generated asset identities."""
import copy
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from app import engine, stats, library_sources, state_store, library_identity, cloud_bridge, lib
from app.cloud import settings, strm
from dev.tests.test_cloud_workbench import cloud
from dev.tests.test_cloud_governance import bound
from dev.tests.test_my_drive import client


@pytest.fixture
def dual(cloud, monkeypatch, tmp_path):
    old_local, old_share, generated_share = [tmp_path / key for key in ('upstream-local', 'upstream-share', 'generated-share')]
    for root in (old_local, old_share, generated_share):
        root.mkdir()
    settings.save({'share_output_root': str(generated_share)})
    monkeypatch.setattr(engine, 'L_ROOT', old_local)
    monkeypatch.setattr(engine, 'S_ROOT', old_share)
    monkeypatch.setattr(engine, '_STRM_COUNT_CACHE_FILE', engine.STATE_DIR/'strm_count_cache.json')
    monkeypatch.setattr(engine, '_lib_stats_cache', {'ts':0, 'data':None})
    monkeypatch.setattr(engine, '_strm_count_cache', {'ts':0, 'local':0, 'share':0})
    cfg = {**engine.RUNTIME_CFG, 'emby_generated_local_path':'/emby/generated/local', 'emby_generated_share_path':'/emby/generated/share'}
    monkeypatch.setattr(engine, 'RUNTIME_CFG', cfg)
    monkeypatch.setattr(engine, 'EMBY_PATHS', engine.emby_path_map('/emby/upstream/local', '/emby/upstream/share'))
    return SimpleNamespace(cloud=cloud, roots=[old_local,old_share,cloud.root,generated_share])


def test_four_sources_count_and_preserve_per_origin_facts(dual):
    for index, root in enumerate(dual.roots):
        (root / ('movie-'+str(index)+'.strm')).write_text('http://fixture/play')
    result = stats._recompute_all_stats()
    assert result['local_total'] == result['share_total'] == 2
    assert {row['id']:row['count'] for row in result['sources']} == {
        'upstream_local':1,'upstream_share':1,'generated_local':1,'generated_share':1}
    assert [r['count'] for r in library_sources.status()['items']] == [1,1,1,1]
    assert stats.action_library_stats(SimpleNamespace()) == result


def test_same_actual_file_is_counted_once_not_same_link_text(dual):
    first = dual.roots[2] / 'first.strm'
    first.write_text('http://fixture/play')
    os.link(first, dual.roots[0] / 'alias.strm')
    (dual.roots[0] / 'different-file.strm').write_text('http://fixture/play')
    result = stats._recompute_all_stats()
    assert result['local_total'] == 2
    rows = {r['id']:r for r in result['sources']}
    assert rows['generated_local']['count'] == 1
    assert rows['upstream_local']['count'] == 1 and rows['upstream_local']['duplicates'] == 1


def test_missing_new_output_is_unverified_not_a_verified_zero(dual):
    dual.roots[3].rmdir()
    result = stats._recompute_all_stats()
    row = next(r for r in result['sources'] if r['id']=='generated_share')
    assert row['count'] is None and row['error'] and not row['verified_ts']


def test_lost_previously_verified_generated_mount_keeps_all_totals(dual):
    (dual.roots[2] / 'generated.strm').write_text('fixture')
    before = stats._recompute_all_stats()
    saved = state_store.read(engine.STATE_DIR/'library_stats_cache.json')
    dual.roots[2].rename(dual.roots[2].with_name('offline-output'))
    with pytest.raises(stats.LibraryScanError):
        stats._recompute_all_stats()
    assert state_store.read(engine.STATE_DIR/'library_stats_cache.json') == saved
    assert engine._strm_count_cache['local'] == before['local_total'] == 1


def test_generated_callback_updates_same_source_and_total(dual):
    stats._recompute_all_stats()
    (dual.roots[2] / 'new.strm').write_text('fixture')
    assert stats.refresh_event_directories([str(dual.roots[2])])
    data = state_store.read(engine.STATE_DIR/'library_stats_cache.json')
    assert data['local_total'] == 1
    assert {r['id']:r['count'] for r in data['sources']}['generated_local'] == 1
    assert stats.refresh_event_directories([str(dual.roots[2])])
    assert state_store.read(engine.STATE_DIR/'library_stats_cache.json')['local_total'] == 1


def test_four_emby_mappings_are_strict_and_keep_old_ownership(dual):
    for index, root in enumerate(dual.roots):
        path = root/'Movie.strm'
        path.write_text('fixture')
        origin = 'upstream' if index < 2 else 'generated'
        kind = 'local' if index in (0,2) else 'share'
        emby_path = '/emby/'+origin+'/'+kind+'/Movie.strm'
        assert engine.EMBY_PATHS.lib_of(emby_path, fallback=False) == kind
        assert engine.EMBY_PATHS.to_container(emby_path) == path
        assert library_identity.mapped_path(engine, emby_path, file=True) == (path,kind)
        assert engine.container_to_emby_path(path, kind) == emby_path
    assert engine.EMBY_PATHS.to_container('/emby/generated/local/../share/Movie.strm') is None


def test_ambiguous_emby_source_never_falls_back_to_delete_path(dual):
    entries = library_sources.extra_mappings(engine.RUNTIME_CFG)
    entries[0]['emby_root'] = '/emby/upstream/local'
    mapping = engine.EmbyPathMap('/emby/upstream/local','/emby/upstream/share', entries)
    assert mapping.lib_of('/emby/upstream/local/Movie.strm') == ''
    assert mapping.to_container('/emby/upstream/local/Movie.strm') is None
    with pytest.raises(ValueError, match='同一 Emby'):
        library_sources.validate_config({**engine.RUNTIME_CFG,'emby_generated_local_path':engine.EMBY_PATHS.local,'emby_local_path':engine.EMBY_PATHS.local})


def test_mapping_cards_and_detail_report_both_origins_without_raw_paths(dual):
    from app.library_first_screen import _movie_card
    row={'id':'movie','paths':['/emby/upstream/local/Movie.strm','/emby/generated/share/Movie.strm']}
    card = _movie_card(row)
    assert card['source_labels'] == ['上游个人库','自建分享库']
    assert 'paths' not in card


def test_source_status_endpoint_is_cached_read_only_and_authenticated(dual, client, monkeypatch):
    stats._recompute_all_stats()
    monkeypatch.setattr(stats, '_statistics_files', Mock(side_effect=AssertionError('no API scan')))
    result=client.get('/api/library/sources')
    assert result.status_code==200 and len(result.json()['items'])==4
    client.cookies.clear()
    assert client.get('/api/library/sources').status_code==401


def test_generated_governance_remains_explicit_and_unregistered_source_is_blocked(dual):
    path=dual.roots[2]/'foreign.strm';path.write_text('http://unregistered/play')
    assert dual.roots[2] not in cloud_bridge.governance_roots(True)
    with pytest.raises(ValueError):
        cloud_bridge.governance_delete([path], True)
    settings.save({'governance_generated_enabled':True})
    result=cloud_bridge.preview(path, dual.roots[2], True)
    assert result['blocked']
    with pytest.raises(ValueError, match='未登记'):
        cloud_bridge.governance_delete([path], True)
    assert path.exists()


def test_mixed_origin_delete_dispatches_exact_root_and_keeps_generated_asset_identity(bound, monkeypatch, tmp_path):
    from app.routers import library
    original = bound.path
    old = tmp_path/'upstream';old.mkdir()
    share = tmp_path/'upstream-share';share.mkdir()
    monkeypatch.setattr(engine,'L_ROOT',old)
    monkeypatch.setattr(engine,'S_ROOT',share)
    settings.save({'governance_generated_enabled':True})
    legacy=old/'movie.strm';legacy.write_text('http://upstream/play')
    called=[]
    def deleting(files, root, cloud, dry_run=False, **kwargs):
        called.append((root,cloud,list(files)))
        return {'strm_removed':0,'cloud_removed':0,'errors':[]}
    monkeypatch.setattr(engine,'safe_delete_files',deleting)
    preview=cloud_bridge.capture([legacy, original], old, True)
    assert preview['source_counts']=={'上游个人库':1,'自建个人库':1}
    assert preview['cloud_sources'][0]['file_id']=='4'
    cloud_bridge.confirm(preview['source_preview_id'],[legacy,original],old,True)
    cloud_bridge.governance_delete([legacy, original],True)
    assert {r[0] for r in called}=={old,bound.cloud.root}
    assert original.exists() and legacy.exists()


def test_governance_deduplicates_cross_origin_hard_links(dual):
    settings.save({'governance_generated_enabled':True})
    relative=Path('电影')/'Movie (2024) {tmdb-44}'/'Movie.2024.1080p.strm'
    one, alias=dual.roots[2]/relative,dual.roots[0]/relative
    one.parent.mkdir(parents=True);alias.parent.mkdir(parents=True)
    one.write_text('http://fixture/play');os.link(one,alias)
    merged=cloud_bridge.governance_library(dual.roots[0])
    files=[p for values in merged.mov.values() for p in values]
    assert files == [one] and merged.strm_count == 1


def test_cross_kind_overlapping_root_never_publishes_statistics(dual,monkeypatch):
    previous=stats._recompute_all_stats()
    monkeypatch.setattr(library_sources, '_settings', lambda:{**settings.get(),'share_output_root':str(dual.roots[2])})
    with pytest.raises(stats.LibraryScanError, match='个人库与分享库'):
        stats._recompute_all_stats()
    assert state_store.read(engine.STATE_DIR/'library_stats_cache.json')['sources']==previous['sources']


def test_changed_emby_source_mapping_invalidates_card_cache_key(dual):
    from app import library_response
    before=library_response._key()
    engine.reload_config()
    assert library_response._key()!=before


def test_real_generated_delete_uses_registered_115_ids_outside_legacy_root(bound, monkeypatch, tmp_path):
    legacy=tmp_path/'upstream';legacy.mkdir()
    monkeypatch.setattr(engine,'L_ROOT',legacy)
    settings.save({'governance_generated_enabled':True})
    preview=cloud_bridge.capture([bound.path],legacy,True)
    cloud_bridge.confirm(preview['source_preview_id'],[bound.path],legacy,True)
    result=cloud_bridge.governance_delete([bound.path],True)
    assert result['strm_removed']==1 and result['cloud_removed']==1 and result['cloud_sidecars_removed']==1, result
    assert set(bound.deleted)=={'4','5'} and not bound.path.exists()
    engine.cloud_videos.assert_not_called()


def test_cross_origin_delete_progress_never_resets_between_roots(dual,monkeypatch):
    one,two=dual.roots[0]/'old.strm',dual.roots[2]/'new.strm'
    one.write_text('old');two.write_text('new')
    settings.save({'governance_generated_enabled':True})
    monkeypatch.setattr(cloud_bridge,'owned',lambda path:True)
    events=[]
    def deleting(files, root, cloud, dry_run=False, progress=None):
        progress({'phase':'deleting','total':1,'processed':0,'strm_removed':0})
        progress({'phase':'finalizing','total':1,'processed':1,'strm_removed':1})
        return {'strm_removed':1,'cloud_removed':0,'errors':[]}
    monkeypatch.setattr(engine,'safe_delete_files',deleting)
    result=cloud_bridge.governance_delete([one,two],True,progress=events.append)
    assert [row['processed'] for row in events]==[0,1,1,2]
    assert [row['strm_removed'] for row in events]==[0,1,1,2]
    assert all(row['total']==2 for row in events) and result['strm_removed']==2
