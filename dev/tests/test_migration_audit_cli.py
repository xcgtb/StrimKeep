"""Inspect historical migration files without touching user state."""
import importlib.util
import json
import sqlite3
from pathlib import Path


_SPEC = importlib.util.spec_from_file_location(
    'migration_audit', Path(__file__).resolve().parents[2] / 'dev' / 'tools' / 'migration_audit.py')
migration_audit = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(migration_audit)


def _state(tmp_path):
    p = tmp_path / 'state'
    p.mkdir()
    return p


def test_counts_and_sqlite_coverage(tmp_path):
    state = _state(tmp_path)
    con = sqlite3.connect(state / 'strimkeep.db')
    con.execute('CREATE TABLE plans(id TEXT PRIMARY KEY)')
    con.execute('INSERT INTO plans VALUES(?)', ('abc',))
    con.commit()
    con.close()
    (state / 'plan_abc.json.migrated').write_text('{}')
    (state / 'plan_xyz.json.migrated').write_text('{}')
    (state / 'tmdb_cache.json.migrated').write_text('{}')
    (state / 'tmdb_cache.json').write_text('{}')
    (state / 'old_cache.json.migrated').write_text('{}')
    before = {p.name: (p.stat().st_size, p.stat().st_mtime_ns) for p in state.iterdir()}
    report = migration_audit.inspect(state)
    after = {p.name: (p.stat().st_size, p.stat().st_mtime_ns) for p in state.iterdir()}
    assert before == after
    assert report['readonly'] and report['total'] == 4
    assert report['plans'] == {'total': 2, 'in_db': 1, 'not_in_db': 1, 'db_unknown': 0}
    assert report['others'] == {'total': 2, 'has_current_file': 1, 'missing_current_file': 1}


def test_missing_database_is_unknown_not_safe_to_delete(tmp_path):
    state = _state(tmp_path)
    (state / 'plan_abc.json.migrated').write_text('{}')
    report = migration_audit.inspect(state)
    assert report['plans']['db_unknown'] == 1
    assert report['plans']['not_in_db'] == 0
    assert report['database_error']


def test_symlinks_are_not_followed(tmp_path):
    state = _state(tmp_path)
    outside = tmp_path / 'elsewhere'
    outside.mkdir()
    (outside / 'plan_abc.json.migrated').write_text('{}')
    (state / 'link').symlink_to(outside, target_is_directory=True)
    (state / 'fake.json.migrated').symlink_to(outside / 'plan_abc.json.migrated')
    assert migration_audit.inspect(state)['total'] == 0


def test_json_cli(tmp_path, capsys):
    state = _state(tmp_path)
    assert migration_audit.main([str(state), '--json']) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['total'] == 0 and report['readonly'] is True


def test_reject_symlink_state_root(tmp_path):
    state = _state(tmp_path)
    alias = tmp_path / 'alias'
    alias.symlink_to(state, target_is_directory=True)
    import pytest
    with pytest.raises(ValueError):
        migration_audit.inspect(alias)
