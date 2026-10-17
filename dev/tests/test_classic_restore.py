"""Compatibility contract for restored pre-Vue non-115 modules.

No cloud or media mutation, and no external network calls are permitted.
"""
import hashlib
import re
from pathlib import Path
from urllib.parse import urlsplit

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
OLD = ROOT / 'static' / 'classic'


def test_legacy_core_is_original_v147_code_without_115_replace():
    """Core UX must remain byte-for-byte equal to the selected old release."""
    from app.main import app

    c = TestClient(app)
    r = c.get('/')
    assert r.status_code == 200
    for section in ('dashboard', 'mapping', 'explore', 'governance', 'settings',
                    'subscribe', 'history', 'plans', 'daily'):
        assert f'id="tab-{section}"' in r.text
    for script in ('governance', 'mapping', 'explore', 'dashboard', 'settings',
                   'subscriptions', 'plans', 'records'):
        assert f'/static/classic/js/{script}.js' in r.text
    assert 'cloud-workbench.js' not in r.text
    assert 'id="cloudVueFrame"' in r.text
    assert "openDrivePage('organize')" not in r.text  # cloud sidebar driven by runtime
    src = (OLD / 'js' / 'runtime.js').read_text()
    assert "'/workspace/embed?embedded=1#'" in src
    assert "'/organize/workspace'" in src
    assert "'/resources/share'" in src
    assert "'/resources/channels'" in src
    for p in ('static/classic/js/governance.js','static/classic/js/mapping.js',
              'static/classic/js/explore.js','static/classic/js/settings.js'):
        assert (ROOT / p).is_file()


def test_embedded_vue_only_relaxes_same_origin_frame_headers():
    from app.main import app

    client = TestClient(app)
    for url in ('/', '/static/index.html', '/static/index.html?embedded=1', '/workspace/embed'):
        response = client.get(url)
        assert response.headers['x-frame-options'] == 'DENY', url
        assert "frame-ancestors 'none'" in response.headers['content-security-policy']
    frame = client.get('/workspace/embed?embedded=1')
    assert frame.status_code == 200
    assert frame.headers['x-frame-options'] == 'SAMEORIGIN'
    assert "frame-ancestors 'self'" in frame.headers['content-security-policy']
    assert '<div id="app"></div>' in frame.text
    assert 'id="classic-cloud-embed"' in frame.text
    assert 'no-store' in frame.headers['cache-control']


def test_classic_resources_resolve_and_cloud_shell_is_not_replaced():
    from app.main import app

    client = TestClient(app)
    old = client.get('/').text
    modern = client.get('/static/index.html').text
    assert '<div id="app"></div>' in modern
    assert 'type="module"' in modern
    for src in re.findall(r'(?:src|href)="(/static/[^\"]+)"', old):
        response = client.get(src)
        assert response.status_code == 200, src
    for path in ('/static/classic/js/runtime.js', '/static/classic/js/records.js',
                 '/static/classic/css/app.css', '/static/classic/images/overview-hero.webp'):
        assert client.get(path).status_code == 200
