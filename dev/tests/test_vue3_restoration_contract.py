"""Guard the native Vue 3 restoration against reintroducing legacy static UI.

These are source-level contract checks, not a substitute for Vue compiler and
browser tests. They protect the desired interface/endpoint linkage in CI.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / 'frontend' / 'src'


def read(relative):
    return (ROOT / relative).read_text(encoding='utf-8')


def test_explore_uses_vue_paging_and_persists_return_position():
    source = read('views/ExploreView.vue')
    assert 'IntersectionObserver' in source
    assert 'exploreSnapshot' in source and 'window.scrollTo' in source
    assert 'ref="loadMarker"' in source
    assert 'updateBadges' in source
    assert 'static/classic/' not in source


def test_mapping_selects_poster_and_requires_verified_delete_result():
    page = read('views/LibraryView.vue')
    receipt = read('components/TaskReceipt.vue')
    assert '@open="selectionMode ? toggle(card.id) : open(card)"' in page
    assert '@done="afterSingleDelete"' in page
    assert 'lastDeletion' in page and 'afterBatchDelete' in page
    assert 'deletionOutcome(state.value, result.value, props.kind === \'delete\')' in receipt
    assert 'strm_removed' in receipt and 'cloud_removed' in receipt
    assert 'static/classic/' not in page


def test_delete_completions_preserve_backend_security_preview():
    for name in ('SingleDelete.vue', 'MappingDelete.vue'):
        content = read('components/' + name)
        assert '<TaskReceipt' in content and '@failed=' in content
        assert '<ConfirmAction' in content
        assert 'source_preview_id' in content or 'preview_id' in content


def test_governance_has_human_evidence_and_active_plan_guard():
    governance = read('views/GovernanceView.vue')
    evidence = read('components/GovernanceEvidence.vue')
    assert 'scan.usable' in governance and 'ruleAligned' in governance
    assert '<GovernanceEvidence' in governance
    for required in ('share_paths', 'local_paths', 'match_basis', 'cloud_sources'):
        assert required in evidence


def test_governance_strategy_remains_native_vue_controls():
    source = read('views/GovernanceSettingsView.vue')
    for required in ('form.decision', 'form.match_strategy', 'form.special_action',
                     'form.season_replace_ratio', 'form.tie_keep_local',
                     'form.multi_season_protect', 'QualityRules', 'whitelist-editor'):
        assert required in source
    assert 'static/classic/' not in source


def test_three_column_mobile_poster_wall():
    css = read('styles/main.css')
    assert '.poster-grid.discovery-grid' in css
    assert '.poster-grid.library-poster-grid' in css
    assert 'grid-template-columns: repeat(3, minmax(0, 1fr))' in css
