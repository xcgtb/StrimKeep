"""The sidebar status endpoint is cheap, authenticated and omits task payloads."""
from unittest.mock import patch
from fastapi.testclient import TestClient


def test_runtime_status_requires_auth_and_never_probes_media_or_remote_services():
    from app.main import app
    from app.routers.deps import auth
    from app.routers import system
    from app.tasks import Task
    client = TestClient(app)
    assert client.get('/api/runtime/status').status_code == 401
    task = Task('inter_check', source='bot', cancellable=True)
    task.result = {'private': 'huge result'}
    task.logs = ['private paths']
    app.dependency_overrides[auth] = lambda: True
    try:
        with patch.object(system.tasks.manager, 'running', return_value=task), \
             patch.object(system.bot, 'status', return_value={'state':'ok'}), \
             patch.object(system.storage, 'storage_health', return_value={'issues': []}), \
             patch.object(system.engine, 'emby_request', side_effect=AssertionError('remote probe')), \
             patch.object(system.engine, '_get_lib', side_effect=AssertionError('full scan')):
            data = client.get('/api/runtime/status').json()
            assert data['bot']['state'] == 'ok' and data['task']['kind'] == 'inter_check'
            assert data['task']['source'] == 'bot' and data['ts']
            assert 'result' not in data['task'] and 'logs' not in data['task']
            assert 'private' not in str(data)
    finally:
        app.dependency_overrides.pop(auth, None)
