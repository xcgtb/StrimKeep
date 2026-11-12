"""0.6.5 operational fixes: login throttle order, asset caching, log retention,
frontend build chain and the silently ignored ADMIN_PASSWORD."""
import time
from pathlib import Path

from fastapi.testclient import TestClient

from strimkeep.main import create_app
from strimkeep.store import Store
from test_app import FakeCloud


def test_failed_attempts_never_block_the_correct_password(tmp_path, monkeypatch):
    monkeypatch.setenv('ADMIN_USERNAME', 'admin')
    monkeypatch.setenv('ADMIN_PASSWORD', 'test-password-1234')
    app = create_app(tmp_path / 'data', tmp_path / 'strm', FakeCloud)
    with TestClient(app) as client:
        codes = [client.post('/api/v1/auth/login',
                             json={'username': 'admin', 'password': 'wrong-password'}).status_code
                 for _ in range(8)]
        assert codes[:7] == [401] * 7 and codes[7] == 429
        # Every client of a tunnelled deployment shares one peer address, so the
        # owner must still be able to sign in while the budget is exhausted.
        ok = client.post('/api/v1/auth/login',
                         json={'username': 'admin', 'password': 'test-password-1234'})
        assert ok.status_code == 200 and ok.json()['csrf']


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
