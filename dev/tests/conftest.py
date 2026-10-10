"""Initialize a disposable test environment before collecting application imports.

Some application paths and authentication values are read at import time. Setting
them in individual test modules makes collection order decide whether a test uses
temporary directories or the real container paths. Never inherit NAS settings.
"""
import os
import tempfile
from pathlib import Path


_SANDBOX = tempfile.TemporaryDirectory(prefix='strimkeep-pytest-')
_ROOT = Path(_SANDBOX.name)
for _key, _leaf in [('AGENT_DATA', 'data'), ('L_ROOT', 'local'),
                    ('S_ROOT', 'share'), ('CLOUD_L_ROOT', 'cloud')]:
    os.environ[_key] = str(_ROOT / _leaf)
os.environ.update(WEB_USER='admin', WEB_PASSWORD='test-pass-123', ALLOW_NO_AUTH='',
                  TMDB_KEY='', TG_BOT_TOKEN='', TG_ALLOWED_USERS='',
                  INGEST_QUIET_MINUTES='0')


def pytest_sessionfinish(session, exitstatus):
    _SANDBOX.cleanup()
