"""Background settings persist without changing connection or governance configuration."""
import os
import tempfile
import pytest
os.environ.setdefault('WEB_PASSWORD', 'test-pass-123')
os.environ.setdefault('AGENT_DATA', tempfile.mkdtemp(prefix='strimkeep-appearance-tests-'))
from fastapi.testclient import TestClient
from app import config
from app.main import app
from app.routers import settings


@pytest.fixture
def backgrounds(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(config, 'CONFIG_FILE', tmp_path / 'config.json')
    monkeypatch.setattr(config, '_CFG_CACHE', {'sig': None, 'data': None})
    monkeypatch.setattr(settings.engine, 'reload_config', lambda: pytest.fail('appearance must not reload services'))
    monkeypatch.setattr(settings.bot, 'restart', lambda: pytest.fail('appearance must not restart Telegram'))
    yield


def test_defaults_partial_updates_restart_and_reset_preserve_other_settings(backgrounds):
    assert settings.api_get_appearance()['appearance'] == dict.fromkeys(settings.BACKGROUND_KEYS, '')
    config.save_config(dict(config.DEFAULTS, tmdb_key='private-key', strategy_cover='custom-rules'))
    settings.api_set_appearance({'background_light_url': 'https://images.example/sky.webp?token=abc&width=1920'})
    settings.api_set_appearance({'background_dark_url': 'https://images.example/night.jpg', 'tmdb_key':'unexpected'})
    config._CFG_CACHE.update(sig=None, data=None)
    saved = config.load_config()
    assert saved['background_light_url'].endswith('token=abc&width=1920')
    assert saved['background_dark_url'].endswith('night.jpg')
    assert saved['tmdb_key'] == 'private-key' and saved['strategy_cover'] == 'custom-rules'
    settings.api_set_appearance(dict.fromkeys(settings.BACKGROUND_KEYS, ''))
    assert settings.api_get_appearance()['appearance'] == dict.fromkeys(settings.BACKGROUND_KEYS, '')
    assert config.load_config()['tmdb_key'] == 'private-key'


@pytest.mark.parametrize('url', ['javascript:alert(1)', 'data:image/png;base64,xxx', '//images.example/a.jpg',
    'https://', 'https://user:password@images.example/a.jpg', 'https://@images.example/a.jpg',
    'https://images.example:invalid/a.jpg', 'https://images.example:65536/a.jpg',
    'https://images.example/a\nb.jpg', 'https://images.example/a\\b.jpg',
    'https://images.example/a".jpg', 'https://images.example/'+('x'*2048), None, {}])
def test_reject_invalid_address_without_partial_save(backgrounds, url):
    settings.api_set_appearance({'background_dark_url':'https://images.example/old.jpg'})
    with pytest.raises(settings.HTTPException) as error:
        settings.api_set_appearance({'background_light_url':'https://images.example/new.jpg', 'background_dark_url':url})
    assert error.value.status_code == 400
    assert settings.api_get_appearance()['appearance'] == {
        'background_light_url':'', 'background_dark_url':'https://images.example/old.jpg'}


def test_real_http_endpoint_requires_auth_and_returns_only_backgrounds(backgrounds):
    client = TestClient(app)
    assert client.get('/api/appearance').status_code == 401
    assert client.post('/api/appearance', json={}).status_code == 401
    app.dependency_overrides[settings.auth] = lambda: None
    try:
        assert client.post('/api/appearance', json={'background_light_url':'https://images.example/a.jpg'}).status_code == 200
        result = client.get('/api/appearance').json()
        assert set(result['appearance']) == set(settings.BACKGROUND_KEYS)
        assert client.post('/api/appearance', json={'background_dark_url':'file:///etc/passwd'}).status_code == 400
    finally:
        app.dependency_overrides.pop(settings.auth, None)
