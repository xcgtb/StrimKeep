"""Reproduce the user's NOT NULL docs.version startup crash in fresh processes."""
import pytest
from dev.checks.verify_docs_schema_compat import check_case


@pytest.mark.parametrize('historical', [True, False], ids=['required-version', 'three-column'])
@pytest.mark.parametrize('mode', ['package', 'cli'])
def test_existing_docs_database_survives_startup_and_state_writes(tmp_path, historical, mode, monkeypatch):
    # Production requirements omit httpx. Block it even on developer machines
    # so the NAS verification cannot accidentally depend on TestClient again.
    hooks = tmp_path / 'dependency_hooks'
    hooks.mkdir()
    (hooks / 'sitecustomize.py').write_text(
        "import sys\n"
        "class BlockHttpx:\n"
        "    def find_spec(self, fullname, path=None, target=None):\n"
        "        if fullname == 'httpx' or fullname.startswith('httpx.'):\n"
        "            raise ModuleNotFoundError('production image has no httpx')\n"
        "sys.meta_path.insert(0, BlockHttpx())\n"
    )
    monkeypatch.setenv('PYTHONPATH', str(hooks))
    check_case(tmp_path, historical, mode)
