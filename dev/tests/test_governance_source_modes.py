"""Explicit STRM governance origin isolation. No NAS, provider or Emby mutations."""
from pathlib import Path
from collections import defaultdict
from types import SimpleNamespace

import pytest
from app import cloud_bridge, engine, governance
from app.cloud import settings
from app.cloud.errors import CloudError


@pytest.fixture
def roots(tmp_path, monkeypatch):
    legacy_local, legacy_share = tmp_path/'upstream-local', tmp_path/'upstream-share'
    own_local, own_share = tmp_path/'generated-local', tmp_path/'generated-share'
    for path in (legacy_local, legacy_share, own_local, own_share):
        path.mkdir()
    monkeypatch.setattr(engine, 'L_ROOT', legacy_local)
    monkeypatch.setattr(engine, 'S_ROOT', legacy_share)
    data = dict(settings.DEFAULTS, output_root=str(own_local), share_output_root=str(own_share))
    monkeypatch.setattr(settings, 'get', lambda: data)
    return data, (legacy_local, legacy_share), (own_local, own_share)


def test_legacy_switches_keep_prior_meaning(roots):
    cfg, upstream, generated = roots
    assert settings.effective_governance_mode(cfg) == 'upstream'
    assert cloud_bridge.governance_roots() == upstream
    cfg.update(governance_generated_enabled=True)
    assert settings.effective_governance_mode(cfg) == 'combined'
    assert cloud_bridge.governance_roots() == (upstream[0], generated[0], upstream[1], generated[1])
    cfg.update(governance_generated_only=True)
    assert settings.effective_governance_mode(cfg) == 'generated'
    assert cloud_bridge.governance_roots() == generated


@pytest.mark.parametrize('mode,expected', [
    ('upstream','upstream'), ('generated','generated'), ('combined','combined'),
])
def test_explicit_modes_use_only_their_selected_roots(roots, monkeypatch, mode, expected):
    cfg, upstream, generated = roots
    cfg.update(governance_source_mode=mode, governance_generated_enabled=mode != 'upstream',
               governance_generated_only=mode == 'generated')
    seen = []
    def fake_index(root):
        seen.append(root)
        return SimpleNamespace(root=root, strm_count=0, mov=defaultdict(list),
            tv=defaultdict(lambda: defaultdict(list)), meta={}, key_tmdb_movie={},
            key_tmdb_tv={}, path_tmdb={}, by_base=defaultdict(set), tmdb_refs=defaultdict(set))
    monkeypatch.setattr(engine, '_get_lib', fake_index)
    assert cloud_bridge.governance_library(upstream[0]).root == (upstream[0] if mode != 'generated' else generated[0])
    assert cloud_bridge.governance_library(upstream[1]).root == (upstream[1] if mode != 'generated' else generated[1])
    if mode == 'upstream':
        assert seen == list(upstream)
        with pytest.raises(ValueError, match='不属于'):
            cloud_bridge.governance_root(generated[0]/'a.strm', True)
    elif mode == 'generated':
        assert seen == list(generated)
        with pytest.raises(ValueError, match='不属于'):
            cloud_bridge.governance_root(upstream[0]/'a.strm', True)
    else:
        assert seen == [upstream[0], generated[0], upstream[1], generated[1]]


def test_setting_mode_switch_mirrors_legacy_flags_and_preserves_other_values(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'path', lambda: tmp_path/'settings.json')
    saved = settings.save({'movie_folder': '电影归档'})
    assert saved['governance_source_mode'] == ''
    own = settings.save({'governance_source_mode': 'generated'})
    assert own['governance_generated_enabled'] is True
    assert own['governance_generated_only'] is True
    assert own['movie_folder'] == '电影归档'
    upstream = settings.save({'governance_source_mode': 'upstream'})
    assert upstream['governance_generated_enabled'] is False
    assert upstream['governance_generated_only'] is False
    hybrid = settings.save({'governance_source_mode': 'combined'})
    assert hybrid['governance_generated_enabled'] is True
    assert hybrid['governance_generated_only'] is False
    assert settings.get()['governance_source_mode'] == 'combined'
    # A legacy API client changing old flags must still be honored.
    legacy = settings.save({'governance_generated_enabled': False})
    assert legacy['governance_source_mode'] == ''
    assert settings.effective_governance_mode(legacy) == 'upstream'


def test_rejects_invalid_or_inconsistent_scope_without_persisting(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'path', lambda: tmp_path/'settings.json')
    with pytest.raises(CloudError, match='治理来源'):
        settings.save({'governance_source_mode': 'arbitrary'})
    with pytest.raises(CloudError, match='不一致'):
        settings.validate(dict(settings.DEFAULTS, governance_source_mode='generated'))
    assert not (tmp_path/'settings.json').exists()


def test_generated_missing_mount_fails_closed_but_upstream_unaffected(roots):
    cfg, upstream, generated = roots
    generated[0].rmdir()
    cfg.update(governance_source_mode='generated', governance_generated_enabled=True,
               governance_generated_only=True)
    with pytest.raises(ValueError, match='不能当作空库'):
        cloud_bridge.governance_roots()
    cfg.update(governance_source_mode='upstream', governance_generated_enabled=False,
               governance_generated_only=False)
    assert cloud_bridge.governance_roots() == upstream


def test_plan_signature_changes_when_switching_methods(roots):
    cfg, *_ = roots
    sig_upstream = governance._current_rule_snapshot()['sig']
    cfg.update(governance_source_mode='generated', governance_generated_enabled=True,
               governance_generated_only=True)
    sig_generated = governance._current_rule_snapshot()['sig']
    cfg.update(governance_source_mode='combined', governance_generated_only=False)
    sig_combined = governance._current_rule_snapshot()['sig']
    assert len({sig_upstream, sig_generated, sig_combined}) == 3


def test_governance_summary_explains_each_mechanism(roots, monkeypatch):
    from app.routers import governance as routes
    cfg, _, generated = roots
    cfg.update(governance_source_mode='generated', governance_generated_enabled=True,
               governance_generated_only=True)
    monkeypatch.setattr(engine, 'load_latest_scan', lambda: None)
    monkeypatch.setattr(engine, 'daily_consistency_snapshot', lambda **kw: {'rule_sig':'none'})
    monkeypatch.setattr(routes, 'load_scan_status', lambda: {'status': 'success'})
    response = routes.api_governance_summary()
    assert response['status'] == 'success', response
    assert response['source_mode'] == 'generated'
    assert [x['method'] for x in response['sources']] == ['path', 'path', 'asset_id', 'asset_id']
    assert [x['enabled'] for x in response['sources']] == [False, False, True, True]
    assert all(x['present'] for x in response['sources'])


def test_authenticated_http_mode_switch_and_summary(roots, tmp_path, monkeypatch):
    """Exercise the actual Vue workbench endpoints, not a mocked settings save."""
    from fastapi.testclient import TestClient
    from app.main import app
    from app.routers import cloud as cloud_router, governance as governance_router
    from app.routers.deps import auth
    from app.cloud import emby_proxy

    cfg, _, generated = roots
    monkeypatch.setattr(settings, 'path', lambda: tmp_path / 'cloud_settings.json')
    monkeypatch.setattr(engine, 'load_latest_scan', lambda: None)
    monkeypatch.setattr(engine, 'daily_consistency_snapshot', lambda **kw: {'rule_sig':'none'})
    monkeypatch.setattr(governance_router, 'load_scan_status', lambda: {'status':'success'})
    monkeypatch.setattr(emby_proxy, 'status', lambda: {'enabled':False})
    app.dependency_overrides[auth] = lambda: True
    app.dependency_overrides[cloud_router.require_password] = lambda: True
    try:
        client = TestClient(app)
        response = client.post('/api/cloud/settings', json={
            'output_root': str(generated[0]), 'share_output_root': str(generated[1]),
            'governance_source_mode': 'generated',
        })
        assert response.status_code == 200, response.text
        assert response.json()['settings']['governance_generated_only'] is True
        read = client.get('/api/governance/summary')
        assert read.status_code == 200, read.text
        assert read.json()['source_mode'] == 'generated'
        assert [r['enabled'] for r in read.json()['sources']] == [False, False, True, True]
        response = client.post('/api/cloud/settings', json={'governance_source_mode':'upstream'})
        assert response.status_code == 200, response.text
        assert client.get('/api/governance/summary').json()['source_mode'] == 'upstream'
        response = client.post('/api/cloud/settings', json={'governance_source_mode':'unsafe'})
        assert response.status_code == 409
        assert client.get('/api/governance/summary').json()['source_mode'] == 'upstream'
    finally:
        app.dependency_overrides.pop(auth, None)
        app.dependency_overrides.pop(cloud_router.require_password, None)


def test_running_governance_blocks_http_scope_changes(roots, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.routers import cloud as cloud_router
    from app.routers.deps import auth
    from app import tasks
    from app.cloud import emby_proxy
    monkeypatch.setattr(settings, 'path', lambda: tmp_path / 'cloud_settings.json')
    monkeypatch.setattr(emby_proxy, 'status', lambda: {'enabled':False})
    active = tasks.Task('inter_clean')
    monkeypatch.setattr(tasks.manager, 'current', active)
    app.dependency_overrides[auth] = lambda: True
    app.dependency_overrides[cloud_router.require_password] = lambda: True
    try:
        client = TestClient(app)
        denied = client.post('/api/cloud/settings', json={'governance_source_mode': 'generated'})
        assert denied.status_code == 409
        assert '正在执行治理' in denied.text
        assert settings.get()['governance_source_mode'] == ''
    finally:
        app.dependency_overrides.pop(auth, None)
        app.dependency_overrides.pop(cloud_router.require_password, None)


def test_inactive_origin_cannot_reach_safe_delete_even_with_valid_path(roots, monkeypatch):
    cfg, upstream, generated = roots
    deleted = []
    monkeypatch.setattr(engine, 'safe_delete_files', lambda *a, **kw: deleted.append(a))
    cfg.update(governance_source_mode='generated', governance_generated_enabled=True,
               governance_generated_only=True)
    with pytest.raises(ValueError, match='不属于'):
        cloud_bridge.governance_delete([upstream[0]/'film.strm'], True)
    cfg.update(governance_source_mode='upstream', governance_generated_enabled=False,
               governance_generated_only=False)
    with pytest.raises(ValueError, match='不属于'):
        cloud_bridge.governance_delete([generated[0]/'film.strm'], True)
    assert deleted == []
