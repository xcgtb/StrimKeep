"""Offline StrimKeep Vue3 / one-dragon configuration regressions.

No provider writes, Emby requests, media deletions or test secrets.
"""
from pathlib import Path

import pytest

from app.cloud import emby_library, pipeline, settings, directories
from app import engine


def local_configuration(monkeypatch, *, emby_rows=(), enabled=True, generate=True):
    monkeypatch.setattr(
        directories, 'get',
        lambda _id: {'directories': {
            'source': {'id': '10', 'name': '待整理'},
            'target': {'id': '20', 'name': '归档'},
            'transfer': {'id': '30', 'name': '转存'},
        }}
    )
    monkeypatch.setattr(settings, 'get', lambda: {
        'generate_strm_enabled': generate,
        'emby_refresh_enabled': enabled,
        'public_url': 'https://example.invalid',
        'output_root': '/strm/local',
        'naming_style': 'standard',
    })
    monkeypatch.setattr(emby_library, 'get', lambda _id: {'items': list(emby_rows)})
    monkeypatch.setattr(pipeline, 'output_ready', lambda kind='personal': '/strm/local')
    monkeypatch.setattr(engine, 'RUNTIME_CFG', {**engine.RUNTIME_CFG, 'tmdb_key': 'configured-dummy'})


def test_a_strm_output_alone_is_not_an_emby_ingestion_ready_signal(monkeypatch):
    local_configuration(monkeypatch)
    value = pipeline.status('fixture-id')
    checks = {c['key']: c for c in value['checks']}
    assert checks['output']['ready']
    assert not checks['emby']['ready']
    assert not value['ready']
    assert 'STRM' in checks['emby']['detail']


def test_readiness_requires_valid_personal_emby_mapping(monkeypatch):
    local_configuration(monkeypatch, emby_rows=[{
        'enabled': True, 'url': 'https://emby.example.invalid',
        'key_saved': True, 'share_root': '/share', 'personal_root': ''
    }])
    assert not pipeline.status('fixture-id')['ready']
    local_configuration(monkeypatch, emby_rows=[{
        'enabled': True, 'url': 'https://emby.example.invalid',
        'key_saved': True, 'share_root': '/share', 'personal_root': '/local',
    }])
    assert pipeline.status('fixture-id')['ready']
    assert '仍需' in next(c['detail'] for c in pipeline.status('fixture-id')['checks'] if c['key'] == 'emby')


def test_disabling_ingestion_breaks_end_to_end_but_not_organize_only(monkeypatch):
    local_configuration(monkeypatch, enabled=False, generate=True)
    assert not pipeline.status('fixture-id')['ready']
    local_configuration(monkeypatch, enabled=False, generate=False)
    assert pipeline.status('fixture-id')['ready']


def test_frontend_regressions_only_use_eligible_scopes():
    base = Path(__file__).resolve().parents[2] / 'frontend' / 'src'
    view = (base / 'views' / 'LibraryView.vue').read_text()
    assert 'if (selectionMode.value)' in view
    assert 'toggle(card.id);' in view
    assert 'filters.scope === \'all\'' in view
    assert 'allSelected.value = false;' in view
    receipt = (base / 'components' / 'TaskReceipt.vue').read_text()
    assert 'role="progressbar"' in receipt
    assert '查看处理明细与技术回执' in receipt
