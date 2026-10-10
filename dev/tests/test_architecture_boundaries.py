"""Regression contracts for asset delivery and package/CLI engine entry points."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / 'dev/tests/fixtures/engine_exports.json'


def isolated(tmp_path, code, extra_path=None):
    env = dict(os.environ)
    env.update({name: str(tmp_path / name) for name in ('AGENT_DATA', 'L_ROOT', 'S_ROOT', 'CLOUD_L_ROOT')})
    env.update(WEB_PASSWORD='fixture-password', WEB_USER='admin', TMDB_KEY='', TG_BOT_TOKEN='',
               ENABLE_CD2_WATCHDOG='0')
    prelude = 'import sys; sys.path.insert(0, %r)\n' % str(extra_path or ROOT)
    result = subprocess.run([sys.executable, '-c', prelude + code], cwd=ROOT, env=env,
                            capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stderr[-3000:]
    return result.stdout


@pytest.mark.parametrize('mode', ['package', 'standalone'])
def test_old_export_contract_resolves_original_objects_without_background_work(tmp_path, mode):
    module = 'app.engine' if mode == 'package' else 'engine'
    prefix = 'app.' if mode == 'package' else ''
    code = '''import importlib, json, threading
from pathlib import Path
engine = importlib.import_module(%r)
contract = json.loads(Path(%r).read_text())
for name, (owner, original) in contract.items():
    m = importlib.import_module(%r + owner)
    assert getattr(engine, name) is (m if original is None else getattr(m, original)), name
assert len(threading.enumerate()) == 1
assert engine.ACTIONS['inter_check'] is engine.action_inter_check
assert engine.ACTIONS['inter_clean'] is engine.action_inter_clean
assert engine.MUTATING == {'inter_clean', 'clean_orphans'}
assert not Path(engine.DATA_DIR).exists(), 'import must not create runtime state'
print('207 original exports intact')
''' % (module, str(CONTRACT), prefix)
    assert '207 original exports intact' in isolated(tmp_path, code, ROOT if mode == 'package' else ROOT / 'app')


def test_compat_import_failure_propagates_without_fallback_or_partial_publication(monkeypatch):
    from app import compat
    seen = []
    error = ImportError('dependency failed inside the owning module')

    def broken(name, package=None):
        seen.append((name, package))
        raise error

    monkeypatch.setattr(compat, 'import_module', broken)
    namespace = {'existing': object()}
    before = dict(namespace)
    with pytest.raises(ImportError) as caught:
        compat.bind_engine_exports(namespace, 'app')
    assert caught.value is error
    assert seen == [('.core', 'app')]
    assert namespace == before


def test_engine_state_and_cache_remain_live_shared_owners(tmp_path):
    code = '''from app import engine, storage, lib
from pathlib import Path
original = engine.STATE_DIR
engine.STATE_DIR = original / 'rebound'
assert storage._state_dir() == engine.STATE_DIR
assert engine._lib_cache is lib._lib_cache
marker = object()
engine._lib_cache['boundary-fixture'] = marker
assert lib._lib_cache['boundary-fixture'] is marker
assert not Path(engine.DATA_DIR).exists()
'''
    isolated(tmp_path, code)


def test_real_static_mount_serves_all_frontend_assets_and_retains_uncached_html(tmp_path):
    code = '''import hashlib, re
from urllib.parse import urlsplit, parse_qs
from fastapi.testclient import TestClient
from app.main import app
# No lifespan: no scheduler, Bot, CD2 or media/API calls.
client = TestClient(app)
page = client.get('/')
assert page.status_code == 200
assert 'no-store' in page.headers['cache-control']
urls = re.findall(r'(?:src|href)="(/static/[^"]+)"', page.text)
assert urls and len(set(urls)) == len(urls), 'missing or duplicate static assets'
paths = {urlsplit(url).path for url in urls}
required = {'/static/css/app.css', '/static/css/overview.css', '/static/css/glass.css',
            '/static/js/bootstrap.js', '/static/js/theme-meta.js',
            '/static/icons/favicon.ico', '/static/icons/favicon.svg',
            '/static/icons/apple-touch-icon.png'}
assert required <= paths, ('missing required assets', required - paths)
for url in urls:
    response = client.get(url)
    assert response.status_code == 200, url
    kind = {'css': 'css', 'js': 'javascript', 'svg': 'image/svg+xml',
            'png': 'image/png', 'ico': 'image/'}.get(urlsplit(url).path.rsplit('.', 1)[-1])
    assert kind in response.headers['content-type'], url
    assert parse_qs(urlsplit(url).query)['v'][0] == hashlib.sha256(response.content).hexdigest()[:12], url
assert client.get('/static/js/missing-file.js').status_code == 404
icon = client.get('/favicon.ico')
assert icon.status_code == 200 and icon.headers['content-type'] == 'image/vnd.microsoft.icon'
assert icon.content == client.get('/static/icons/favicon.ico').content
assert icon.content.startswith(bytes([0, 0, 1, 0]))
assert '/favicon.ico' not in app.openapi()['paths']
'''
    isolated(tmp_path, code)
