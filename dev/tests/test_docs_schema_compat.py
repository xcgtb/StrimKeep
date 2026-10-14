"""Reproduce the user's NOT NULL docs.version startup crash in fresh processes."""
import pytest
from dev.checks.verify_docs_schema_compat import check_case


@pytest.mark.parametrize('historical', [True, False], ids=['required-version', 'three-column'])
@pytest.mark.parametrize('mode', ['package', 'cli'])
def test_existing_docs_database_survives_startup_and_state_writes(tmp_path, historical, mode, monkeypatch):
    # HTTPX is now a runtime dependency of cloud playback. The historical SQLite
    # verifier must still use real HTTP and never depend on test-only clients.
    hooks = tmp_path / 'dependency_hooks'
    hooks.mkdir()
    (hooks / 'sitecustomize.py').write_text(
        "import sys\n"
        "class BlockTestDependencies:\n"
        "    def find_spec(self, fullname, path=None, target=None):\n"
        "        if fullname in ('fastapi.testclient','starlette.testclient') or fullname.startswith('pytest'):\n"
        "            raise ModuleNotFoundError('production verifier cannot use test dependencies')\n"
        "sys.meta_path.insert(0, BlockTestDependencies())\n"
    )
    monkeypatch.setenv('PYTHONPATH', str(hooks))
    check_case(tmp_path, historical, mode)
