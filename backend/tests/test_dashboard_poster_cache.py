"""总览页海报缓存：进程内命中之外，重启后也不该再为同一批封面等 TMDB。"""
import httpx
from fastapi.testclient import TestClient

from strimkeep.main import create_app
from test_app import FakeCloud


def test_poster_path_is_remembered_across_restart(tmp_path, monkeypatch):
    monkeypatch.setenv('ADMIN_USERNAME', 'admin')
    monkeypatch.setenv('ADMIN_PASSWORD', 'test-password-1234')
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, json={'poster_path': '/abc123.jpg'})

    app = create_app(tmp_path / 'data', tmp_path / 'strm', FakeCloud)
    with TestClient(app) as client:
        login = client.post('/api/v1/auth/login', json={'username': 'admin', 'password': 'test-password-1234'})
        client.headers['X-CSRF-Token'] = login.json()['csrf']
        app.state.store.set_many({'tmdb_key': 'test-key'})
        previous = app.state.http
        app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        first = client.get('/api/v1/dashboard/tmdb-poster/tv/95480', follow_redirects=False)
        assert first.status_code in (302, 307), first.text
        assert first.headers['location'].endswith('/abc123.jpg')
        assert len(calls) == 1

        second = client.get('/api/v1/dashboard/tmdb-poster/tv/95480', follow_redirects=False)
        assert second.status_code in (302, 307)
        assert len(calls) == 1, '进程内缓存应命中'

        # 模拟重启：进程内缓存没了，但 meta 里记着海报路径
        app.state.dashboard_poster_cache = {}
        third = client.get('/api/v1/dashboard/tmdb-poster/tv/95480', follow_redirects=False)
        assert third.status_code in (302, 307)
        assert len(calls) == 1, '重启后仍应走 meta 缓存，不再请求 TMDB'

        import asyncio
        asyncio.run(app.state.http.aclose())
        app.state.http = previous
