"""Regression: a stale frontend cannot rotate Emby webhook tokens accidentally."""
import pytest
from fastapi import HTTPException
from app.routers import library
from app import emby_events


def test_webhook_token_explicit_generation_and_reset(monkeypatch):
    current = {'ingest_mode': 'poll', 'ingest_webhook_token': '', 'ingest_enabled': '1'}
    saves = []
    monkeypatch.setattr(library, 'load_config', lambda: dict(current))
    def save(c):
        current.update(c)
        saves.append(dict(c))
    monkeypatch.setattr(library, 'save_config', save)
    monkeypatch.setattr(library, 'get_ingest_cfg', lambda: {'webhook_token': current['ingest_webhook_token']})
    monkeypatch.setattr(library.engine, 'reload_config', lambda: None)
    monkeypatch.setattr(emby_events, 'kick', lambda: None)

    with pytest.raises(HTTPException) as first:
        library.api_set_ingest_settings({'mode': 'webhook'})
    assert first.value.status_code == 400
    assert not saves

    result = library.api_set_ingest_settings({'generate_token': True})
    token = result['settings']['webhook_token']
    assert token and len(token) >= 32

    with pytest.raises(HTTPException) as duplicate:
        library.api_set_ingest_settings({'generate_token': True})
    assert duplicate.value.status_code == 409
    assert current['ingest_webhook_token'] == token

    with pytest.raises(HTTPException) as stale_ui:
        library.api_set_ingest_settings({'reset_token': True})
    assert stale_ui.value.status_code == 400
    assert current['ingest_webhook_token'] == token

    result = library.api_set_ingest_settings({'reset_token': True, 'confirm_reset': 'RESET_WEBHOOK_TOKEN'})
    assert result['settings']['webhook_token'] != token
    assert current['ingest_webhook_token'] != token
    result = library.api_set_ingest_settings({'mode': 'webhook'})
    assert result['settings']['webhook_token'] == current['ingest_webhook_token']
    assert current['ingest_mode'] == 'webhook'
