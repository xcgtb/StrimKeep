"""实时日志 SSE 的续传游标、分页读取与 Last-Event-ID。"""
import json

from fastapi.testclient import TestClient

from strimkeep.console_api import _log_cursor, _read_logs
from strimkeep.main import create_app
from strimkeep.store import Store


def test_log_cursor_uses_the_larger_resume_position():
    assert _log_cursor(0, '7') == 7
    assert _log_cursor(9, '7') == 9
    assert _log_cursor(0, None) == 0
    assert _log_cursor(0, 'not-a-number') == 0


def test_read_logs_returns_every_row_across_batches(tmp_path):
    store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
    try:
        for index in range(250):
            store.event('system', 'line %d' % index)
        first = _read_logs(store, 0)
        second = _read_logs(store, first[-1]['id'])
        assert [row['message'] for row in first] == ['line %d' % i for i in range(200)]
        assert [row['message'] for row in second] == ['line %d' % i for i in range(200, 250)]
    finally:
        store.close()


class FakeCloud:
    def __init__(self, store):
        self.store = store

    async def close(self):
        pass

    async def status(self):
        return {'connected': False}


def test_live_logs_resumes_from_last_event_id(tmp_path, monkeypatch):
    monkeypatch.setenv('ADMIN_USERNAME', 'admin')
    monkeypatch.setenv('ADMIN_PASSWORD', 'test-password-1234')
    app = create_app(tmp_path / 'data', tmp_path / 'strm', FakeCloud)
    with TestClient(app) as client:
        login = client.post('/api/v1/auth/login',
                            json={'username': 'admin', 'password': 'test-password-1234'})
        assert login.status_code == 200
        store = app.state.store
        for index in range(3):
            store.event('system', 'live-test-%d' % index)
        rows = store.db.execute(
            "SELECT id,message FROM events WHERE message LIKE 'live-test-%' ORDER BY id").fetchall()
        resume = rows[1]['id']
        with client.stream('GET', '/api/v1/logs/events', params={'after': 0},
                           headers={'Last-Event-ID': str(resume)}) as stream:
            assert stream.status_code == 200
            for line in stream.iter_lines():
                if line.startswith('data: '):
                    items = json.loads(line[6:])['items']
                    assert [row['message'] for row in items] == ['live-test-2'], items
                    break
