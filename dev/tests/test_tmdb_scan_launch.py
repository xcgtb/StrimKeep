"""New scans must replace old errors before the background thread runs."""
import threading
import io
from types import SimpleNamespace
import pytest
from app import engine, morning, config, emby


@pytest.fixture
def routes(monkeypatch):
    monkeypatch.setenv('WEB_PASSWORD', 'temporary-test-password')
    from app.routers import library, settings
    return library, settings


@pytest.fixture
def scan(monkeypatch):
    progress = {'running': False, 'total': 0, 'done': 0, 'stage': '失败',
                'started_at': 1, 'finished_at': 2, 'stats': {}, 'error': '未配置 EMBY_KEY'}
    lock = threading.Lock()
    monkeypatch.setattr(engine, '_tmdb_scan_progress', progress)
    monkeypatch.setattr(engine, '_tmdb_scan_lock', lock)
    threads = []

    class DeferredThread:
        def __init__(self, target, **kwargs):
            self.target = target
            threads.append(self)

        def start(self):
            pass

    monkeypatch.setattr(morning.threading, 'Thread', DeferredThread)
    return progress, lock, threads


def test_route_reserves_scan_and_clears_old_error_before_thread_starts(scan, routes, monkeypatch):
    library, _ = routes
    progress, lock, threads = scan
    monkeypatch.setattr(morning, '_refresh_tmdb_scan', lambda: progress.update(running=False, finished_at=3))
    result = library.api_emby_library(force=1)
    assert result['status'] == 'started'
    assert lock.locked() and progress['running']
    assert progress['error'] == '' and progress['finished_at'] == 0
    assert library.api_emby_library(force=1)['status'] == 'running'
    assert len(threads) == 1
    threads[0].target()
    assert not lock.locked() and not progress['running']


def test_thread_start_failure_clears_running_and_releases_lock(scan, routes, monkeypatch):
    library, _ = routes
    progress, lock, threads = scan

    def fail_start(self):
        raise RuntimeError('thread unavailable')

    monkeypatch.setattr(morning.threading.Thread, 'start', fail_start)
    result = library.api_emby_library(force=1)
    assert result['status'] == 'error' and 'thread unavailable' in result['message']
    assert not lock.locked() and not progress['running']
    assert progress['error'] == 'thread unavailable'


def test_synchronous_scheduled_scan_also_resets_old_error(scan, monkeypatch):
    progress, lock, threads = scan

    def worker():
        assert lock.locked() and progress['running'] and progress['error'] == ''
        assert progress['finished_at'] == 0
        progress.update(running=False, finished_at=3)
        return {'status': 'success'}

    monkeypatch.setattr(morning, '_refresh_tmdb_scan', worker)
    assert morning.refresh_tmdb_scan()['status'] == 'success'
    assert not lock.locked() and not threads


def test_saved_key_is_used_by_scan_without_connection_test(scan, routes, tmp_path, monkeypatch):
    library, settings = routes
    progress, lock, threads = scan
    monkeypatch.setattr(config, 'CONFIG_FILE', tmp_path / 'config.json')
    monkeypatch.setattr(config, '_CFG_CACHE', {'sig': None, 'data': None})
    for env_name in config.ENV_OVERRIDE_KEYS.values():
        monkeypatch.delenv(env_name, raising=False)
    for name in ('RUNTIME_CFG', 'EMBY_HOST', 'EMBY_KEY', 'EMBY_PATHS'):
        monkeypatch.setattr(engine, name, getattr(engine, name))
    monkeypatch.setattr(engine, 'EMBY_KEY', '')
    monkeypatch.setattr(settings.bot, 'restart', lambda: None)
    monkeypatch.setattr(settings, 'api_test_emby', lambda *a: pytest.fail('connection test must not be required'))
    tokens = []

    def network(request, timeout):
        assert request.full_url == 'http://emby.test/Items'
        tokens.append(request.get_header('X-emby-token'))
        return io.BytesIO(b'{"Items": []}')

    monkeypatch.setattr(emby.urllib.request, 'urlopen', network)

    def overview(**kwargs):
        emby.emby_request('/Items')
        return {'series': [], 'movies': []}

    monkeypatch.setattr(morning, 'emby_library_overview', overview)
    monkeypatch.setattr(engine, 'Tmdb', lambda: SimpleNamespace(key='', cache={}, save=lambda: None))
    monkeypatch.setattr(morning, 'save_emby_lib_cache', lambda data: None)
    monkeypatch.setattr(morning, 'save_library_snapshot', lambda data: None)
    monkeypatch.setattr(morning, 'build_library_health_snapshot', lambda: {})
    assert settings.api_set_config({'emby_host': 'http://emby.test', 'emby_key': 'saved-test-key'})['status'] == 'success'
    assert library.api_emby_library(force=1)['status'] == 'started'
    assert progress['error'] == ''
    assert threads[0].target()['status'] == 'success'
    assert tokens == ['saved-test-key']
    assert not progress['running'] and not progress['error'] and not lock.locked()
