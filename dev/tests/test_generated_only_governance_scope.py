"""New-only STRM governance is explicitly opted into and remains fail-closed."""
from pathlib import Path
from types import SimpleNamespace

import pytest
from app import cloud_bridge, engine, governance
from app.cloud import settings
from app.cloud.errors import CloudError


@pytest.fixture
def generated_only(tmp_path, monkeypatch):
    personal = tmp_path / 'generated' / 'local'
    shared = tmp_path / 'generated' / 'share'
    personal.mkdir(parents=True)
    shared.mkdir(parents=True)
    legacy_local = tmp_path / 'unmounted' / 'local'
    legacy_share = tmp_path / 'unmounted' / 'share'
    monkeypatch.setattr(engine, 'L_ROOT', legacy_local)
    monkeypatch.setattr(engine, 'S_ROOT', legacy_share)
    opts = dict(settings.DEFAULTS, output_root=str(personal), share_output_root=str(shared),
                governance_generated_enabled=True, governance_generated_only=True)
    monkeypatch.setattr(settings, 'get', lambda: opts)
    return opts, personal, shared, legacy_local, legacy_share


def test_only_generated_roots_are_enumerated_without_legacy_mounts(generated_only):
    _cfg, personal, shared, legacy_local, legacy_share = generated_only
    assert cloud_bridge.governance_roots() == (personal, shared)
    assert cloud_bridge.governance_roots(True) == (personal,)
    assert cloud_bridge.governance_roots(False) == (shared,)
    assert not legacy_local.exists() and not legacy_share.exists()
    assert cloud_bridge.governance_root(personal/'a.strm', True) == personal
    with pytest.raises(ValueError, match='不属于'):
        cloud_bridge.governance_root(legacy_local/'a.strm', True)


def test_plan_index_does_not_ever_read_legacy_root(generated_only, monkeypatch):
    _cfg, personal, shared, legacy_local, legacy_share = generated_only
    calls = []
    def index(root):
        calls.append(root)
        assert root not in (legacy_local, legacy_share), 'legacy must not be read'
        return SimpleNamespace(root=root, strm_count=0)
    monkeypatch.setattr(engine, '_get_lib', index)
    assert cloud_bridge.governance_library(legacy_local).root == personal
    assert cloud_bridge.governance_library(legacy_share).root == shared
    assert calls == [personal, shared]


def test_rule_scope_and_labels_handle_generated_root(generated_only):
    _cfg, personal, shared, _, _ = generated_only
    assert governance._split_root(personal/'电影'/'影片.strm')[0] == '本地'
    assert governance._split_root(shared/'剧集'/'剧名.strm')[0] == '分享'


def test_missing_generated_mount_never_returns_empty_library(generated_only):
    _cfg, personal, shared, _, _ = generated_only
    shared.rmdir()
    with pytest.raises(ValueError, match='不能当作空库'):
        cloud_bridge.governance_roots()


def test_cannot_enable_new_only_without_generated_binding(generated_only):
    cfg, *_ = generated_only
    cfg['governance_generated_enabled'] = False
    with pytest.raises(ValueError, match='必须先启用'):
        cloud_bridge.governance_roots()
    with pytest.raises(CloudError, match='开启自生成库接入'):
        settings.validate(dict(settings.DEFAULTS, governance_generated_only=True))


def test_legacy_access_remains_default_opt_out(generated_only):
    cfg, personal, shared, legacy_local, legacy_share = generated_only
    cfg['governance_generated_only'] = False
    assert cloud_bridge.governance_roots(True) == (legacy_local, personal)
    assert cloud_bridge.governance_roots(False) == (legacy_share, shared)


def test_generated_only_dashboard_counts_exact_new_roots(generated_only, tmp_path, monkeypatch):
    """Governance and overview must agree without a visible /media legacy mount."""
    from app import stats, state_store
    cfg, personal, shared, legacy_local, legacy_share = generated_only
    state = tmp_path / 'state'
    state.mkdir()
    monkeypatch.setattr(engine, 'STATE_DIR', state)
    monkeypatch.setattr(engine, '_STRM_COUNT_CACHE_FILE', state / 'strm_count_cache.json')
    (personal / '国产电影').mkdir()
    (personal / '国产电影' / 'one.strm').write_text('safe-reference')
    (shared / '欧美剧').mkdir()
    (shared / '欧美剧' / 'two.strm').write_text('safe-reference')
    result = stats._recompute_all_stats()
    assert (result['local_total'], result['share_total']) == (1, 1)
    assert stats._scope_matches_disk()
    assert state_store.read(state / 'strm_directory_counts.json')['roots'] == [str(personal), str(shared)]
    assert not legacy_local.exists() and not legacy_share.exists()
    assert stats._load_strm_count_disk()[:2] == (1, 1)


def test_stats_scope_switch_cannot_reuse_legacy_counts(generated_only, tmp_path, monkeypatch):
    from app import stats, state_store
    cfg, personal, shared, legacy_local, legacy_share = generated_only
    state = tmp_path / 'state'
    state.mkdir()
    monkeypatch.setattr(engine, 'STATE_DIR', state)
    monkeypatch.setattr(engine, '_STRM_COUNT_CACHE_FILE', state / 'strm_count_cache.json')
    (personal / 'one.strm').write_text('safe-reference')
    result = stats._recompute_all_stats()
    assert result['local_total'] == 1
    cfg['governance_generated_only'] = False
    assert not stats._scope_matches_disk()
    assert stats._load_strm_count_disk() is None
    # Old /media roots are not present. The new scan must fail, *not* silently
    # reuse the totals from before the scope switch or publish a zero baseline.
    with pytest.raises(stats.LibraryScanError):
        stats.action_library_stats(None)
    assert state_store.read(state / 'library_stats_cache.json')['local_total'] == 1


def test_hybrid_dashboard_counts_legacy_and_generated_without_overlap(generated_only, tmp_path, monkeypatch):
    from app import stats, state_store
    cfg, personal, shared, legacy_local, legacy_share = generated_only
    for root in (legacy_local, legacy_share):
        root.mkdir(parents=True)
    (legacy_local/'before.strm').write_text('legacy')
    (personal/'after.strm').write_text('generated')
    (shared/'share.strm').write_text('shared')
    cfg['governance_generated_only'] = False
    state = tmp_path/'state'; state.mkdir()
    monkeypatch.setattr(engine, 'STATE_DIR', state)
    monkeypatch.setattr(engine, '_STRM_COUNT_CACHE_FILE', state/'strm_count_cache.json')
    result = stats._recompute_all_stats()
    assert (result['local_total'],result['share_total'])==(2,1)
    assert state_store.read(state/'strm_directory_counts.json')['roots'] == [
        str(legacy_local),str(personal),str(legacy_share),str(shared)]
    (personal/'second.strm').write_text('generated2')
    assert stats.refresh_event_directories([str(personal)]) is True
    assert state_store.read(state/'library_stats_cache.json')['local_total']==3


def test_overview_invalidates_memory_on_root_scope_switch(generated_only, tmp_path, monkeypatch):
    from app import stats, overview, state_store
    cfg, personal, shared, _, _ = generated_only
    state = tmp_path/'state'; state.mkdir()
    monkeypatch.setattr(engine, 'STATE_DIR', state)
    monkeypatch.setattr(engine, '_STRM_COUNT_CACHE_FILE', state/'strm_count_cache.json')
    (personal/'only.strm').write_text('safe-reference')
    stats._recompute_all_stats()
    doc={'ts':9999999999.0, 'emby_host':engine.EMBY_HOST,
         'dashboard':{'localCount':'1'},'library_stats':{'local_total':1}}
    state_store.save(state/'media_overview.json',doc)
    overview._memory.update(path=None, stamp=None, data=None, health_stamp=None)
    assert overview._read()['dashboard']['localCount']=='1'
    cfg['governance_generated_only']=False
    assert overview._read() is None
    assert state_store.read(state/'media_overview.json')['dashboard']['localCount']=='1'
    # Reverting to the previous valid roots must rehydrate, not stay pending.
    cfg['governance_generated_only']=True
    assert overview._read()['dashboard']['localCount']=='1'


def test_disappeared_and_recreated_mount_cannot_reuse_total(generated_only, tmp_path, monkeypatch):
    from app import stats, state_store
    cfg, personal, shared, _, _ = generated_only
    state = tmp_path/'state'; state.mkdir()
    monkeypatch.setattr(engine,'STATE_DIR',state)
    monkeypatch.setattr(engine,'_STRM_COUNT_CACHE_FILE',state/'strm_count_cache.json')
    (personal/'original.strm').write_text('safe-reference')
    stats._recompute_all_stats()
    assert stats._load_strm_count_disk()[:2] == (1,0)
    # Build the replacement *while* the old root is still present. This proves
    # distinct inode identity on filesystems that immediately recycle deleted
    # inode numbers for a newly created directory at the same path.
    replacement = tmp_path / 'replacement-mount'
    replacement.mkdir()
    assert (replacement.stat().st_dev, replacement.stat().st_ino) != (
        personal.stat().st_dev, personal.stat().st_ino)
    (personal/'original.strm').unlink()
    personal.rmdir()
    # A missing volume preserves the last known good value for display only.
    assert stats._load_strm_count_disk()[:2] == (1,0)
    assert stats._mounts_match_disk() is False
    replacement.rename(personal)
    assert stats._mounts_match_disk() is False
    assert stats.action_library_stats(None)['local_total'] == 1
    with pytest.raises(stats.LibraryScanError, match='挂载身份'):
        stats._recompute_all_stats()
    assert state_store.read(state/'library_stats_cache.json')['local_total'] == 1
