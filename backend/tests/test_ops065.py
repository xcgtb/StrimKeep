"""0.6.5 operational fixes: login throttle order, asset caching, log retention,
frontend build chain and the silently ignored ADMIN_PASSWORD."""
import time
from pathlib import Path

from fastapi.testclient import TestClient

from strimkeep.main import create_app
from strimkeep.store import Store
from test_app import FakeCloud


def test_failed_attempts_are_throttled_before_authentication(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace

    import httpx

    from strimkeep import http_guard

    now = [1000.0]
    monkeypatch.setattr(http_guard, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setenv('ADMIN_USERNAME', 'admin')
    monkeypatch.setenv('ADMIN_PASSWORD', 'test-password-1234')
    app = create_app(tmp_path / 'data', tmp_path / 'strm', FakeCloud)
    store = app.state.store
    authenticate = store.authenticate
    calls = []

    def counted(username, password):
        calls.append(username)
        return authenticate(username, password)

    monkeypatch.setattr(store, 'authenticate', counted)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url='http://testserver') as client:
            for index in range(8):
                response = await client.post('/api/v1/auth/login',
                                             json={'username': 'admin', 'password': 'wrong-password'})
                assert response.status_code == (401 if index < 7 else 429)
            response = await client.post('/api/v1/auth/login',
                                         json={'username': 'admin', 'password': 'test-password-1234'})
            assert response.status_code == 429 and response.headers['retry-after'] == '2'
            assert len(calls) == 8

            # A shared tunnel address can retry after a short, non-renewing cooldown.
            now[0] += 2
            response = await client.post('/api/v1/auth/login',
                                         json={'username': 'admin', 'password': 'test-password-1234'})
            assert response.status_code == 200 and response.json()['csrf']
            assert len(calls) == 9

    try:
        asyncio.run(scenario())
    finally:
        store.close()


def test_mutation_body_limit_counts_streamed_bytes(tmp_path, monkeypatch):
    import asyncio

    import httpx

    from strimkeep.http_guard import MAX_BODY_BYTES

    monkeypatch.setenv('ADMIN_USERNAME', 'admin')
    monkeypatch.setenv('ADMIN_PASSWORD', 'test-password-1234')
    app = create_app(tmp_path / 'data', tmp_path / 'strm', FakeCloud)
    payload = b'{"username":"admin","password":"test-password-1234"}'
    padding = b' ' * (MAX_BODY_BYTES - len(payload))

    async def body(oversize):
        yield payload
        yield padding
        if oversize:
            yield b' '
            raise AssertionError('The oversized request must stop consuming the body')

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url='http://testserver') as client:
            for path in ('/api/v1/auth/login', '/api/v1/resources/webhook/test'):
                response = await client.post(path, content=body(True), headers={'content-type': 'application/json'})
                assert response.status_code == 413
                assert response.json()['error']['code'] == 'BODY_TOO_LARGE'
                assert 'x-request-id' in response.headers
            response = await client.post('/api/v1/auth/login', content=body(False),
                                         headers={'content-type': 'application/json'})
            assert response.status_code == 200
            response = await client.post('/api/v1/auth/login', content=b' ' * (MAX_BODY_BYTES + 1))
            assert response.status_code == 413

    try:
        asyncio.run(scenario())
    finally:
        app.state.store.close()


def test_password_workers_stay_bounded_after_caller_cancellation():
    import asyncio
    import threading

    import pytest
    from fastapi import HTTPException

    from strimkeep.http_guard import LoginGuard

    entered = threading.Event()
    release = threading.Event()

    def authenticate(*_):
        entered.set()
        return release.wait(3)

    async def scenario():
        guard = LoginGuard()
        guard.MAX_ACTIVE = 1
        request = asyncio.create_task(guard.verify(authenticate, 'first', 'admin', 'ignored'))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
            with pytest.raises(HTTPException) as error:
                await guard.verify(authenticate, 'second', 'admin', 'ignored')
            assert error.value.status_code == 429
        finally:
            release.set()
        async with asyncio.timeout(2):
            while guard.active:
                await asyncio.sleep(.01)
        assert await guard.verify(authenticate, 'second', 'admin', 'ignored')

    asyncio.run(scenario())


def test_hashed_assets_are_cacheable_while_html_stays_uncached(tmp_path, monkeypatch):
    static = tmp_path / 'static'
    (static / 'assets').mkdir(parents=True)
    (static / 'assets' / 'index-abc123.js').write_text('console.log(1)', encoding='utf-8')
    (static / 'index.html').write_text('<div id="app"></div>', encoding='utf-8')
    monkeypatch.setenv('STRIMKEEP_STATIC_DIR', str(static))
    monkeypatch.setenv('ADMIN_USERNAME', 'admin')
    monkeypatch.setenv('ADMIN_PASSWORD', 'test-password-1234')
    app = create_app(tmp_path / 'data', tmp_path / 'strm', FakeCloud)
    with TestClient(app) as client:
        asset = client.get('/assets/index-abc123.js')
        assert asset.status_code == 200
        assert 'immutable' in asset.headers['cache-control']
        page = client.get('/')
        assert page.status_code == 200
        assert page.headers['cache-control'] == 'no-store'
        api = client.get('/health/live')
        assert api.headers['cache-control'] == 'no-store'


def test_prune_keeps_recent_logs_and_bounds_total_rows(tmp_path):
    store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
    now = time.time()
    with store.db:
        store.db.executemany('INSERT INTO events(at,kind,message) VALUES(?,?,?)',
                             [(now - 400 * 86400, 'system', 'old'), (now, 'system', 'fresh')])
        store.db.executemany(
            'INSERT INTO notifications(at,level,job_id,event_key,title,message,read_at) VALUES(?,?,?,?,?,?,?)',
            [(now - 400 * 86400, 'info', 1, 'read-old', 't', 'm', now),
             (now - 400 * 86400, 'info', 2, 'unread-old', 't', 'm', None),
             (now, 'info', 3, 'fresh', 't', 'm', now)])
        for index in range(12):
            store.db.execute('INSERT INTO events(at,kind,message) VALUES(?,?,?)',
                             (now - index, 'progress', f'burst {index}'))
        store.db.execute('INSERT INTO sessions(hash,csrf,expires) VALUES(?,?,?)',
                         ('dead', 'csrf', now - 10))
    result = store.prune(event_days=90, event_max=10, notification_days=180)
    assert 'old' not in [row[0] for row in store.db.execute('SELECT message FROM events')]
    assert store.db.execute('SELECT count(*) FROM events').fetchone()[0] == 10
    keys = {row[0] for row in store.db.execute('SELECT event_key FROM notifications')}
    assert keys == {'unread-old', 'fresh'}
    assert not store.db.execute("SELECT 1 FROM sessions WHERE hash='dead'").fetchone()
    assert result['events'] > 0 and result['notifications'] == 1 and result['sessions'] == 1
    store.close()


def test_frontend_is_built_inside_the_image():
    """A revert to `COPY frontend/dist` silently ships stale JavaScript."""
    root = Path(__file__).resolve().parents[2]
    dockerfile = (root / 'Dockerfile').read_text(encoding='utf-8')
    assert 'AS frontend' in dockerfile and 'npm run build' in dockerfile
    assert 'COPY --from=frontend /build/dist ./static' in dockerfile
    assert 'COPY frontend/dist' not in dockerfile
    ignored = (root / '.dockerignore').read_text(encoding='utf-8').splitlines()
    assert 'frontend/dist' in [line.strip() for line in ignored]


def test_changed_admin_password_is_reported_once(tmp_path, monkeypatch):
    root = tmp_path / 'data'
    Store(root, 'admin', 'original-password-123').close()
    monkeypatch.setenv('ADMIN_USERNAME', 'admin')
    monkeypatch.setenv('ADMIN_PASSWORD', 'another-password-12345')
    first = create_app(root, tmp_path / 'strm', FakeCloud)
    with TestClient(first):
        messages = [row[0] for row in first.state.store.db.execute('SELECT message FROM events')]
        assert any('ADMIN_USERNAME' in message for message in messages)
    second = create_app(root, tmp_path / 'strm', FakeCloud)
    with TestClient(second):
        count = second.state.store.db.execute(
            "SELECT count(*) FROM events WHERE message LIKE '%ADMIN_USERNAME%'").fetchone()[0]
        assert count == 1
