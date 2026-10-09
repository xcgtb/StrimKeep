"""Offline, no-app-import tests for read-only disk usage analysis."""
import importlib.util
import json
import os
from pathlib import Path


SPEC = importlib.util.spec_from_file_location(
    'storage_audit', Path(__file__).parents[2] / 'dev' / 'tools' / 'storage_audit.py'
)
AUDIT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUDIT)


def test_storage_categories_and_total(tmp_path):
    state = tmp_path / 'state'
    backup = state / 'residue_backup' / 'old'
    backup.mkdir(parents=True)
    sizes = {
        state / 'strimkeep.db': 11,
        state / 'strimkeep.db-wal': 7,
        state / 'tmdb_cache.json.migrated': 13,
        state / 'tmdb_cache.json': 17,
        state / 'plan_abc.json': 19,
        backup / 'subtitle.srt': 23,
    }
    for path, size in sizes.items():
        path.write_bytes(b'x' * size)
    report = AUDIT.scan_data_dir(tmp_path)
    assert report['readonly'] is True
    assert report['total_bytes'] == sum(sizes.values())
    assert report['total_files'] == len(sizes)
    assert report['categories']['sqlite']['bytes'] == 18
    assert report['categories']['migration_copies']['bytes'] == 13
    assert report['categories']['rebuildable_caches']['bytes'] == 17
    assert report['categories']['plans']['bytes'] == 19
    assert report['categories']['residue_backup']['bytes'] == 23
    assert report['top_directories']['state']['bytes'] == sum(sizes.values())
    assert not report['errors']


def test_symlinks_not_followed(tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'private').write_bytes(b'x' * 99)
    data = tmp_path / 'data'
    data.mkdir()
    (data / 'safe.txt').write_bytes(b'ok')
    os.symlink(outside, data / 'linked-dir')
    os.symlink(outside / 'private', data / 'linked-file')
    report = AUDIT.scan_data_dir(data)
    assert report['total_bytes'] == 2
    assert report['total_files'] == 1
    assert report['symlinks_skipped'] == 2


def test_missing_path_fails_without_creation(tmp_path):
    target = tmp_path / 'missing'
    try:
        AUDIT.scan_data_dir(target)
    except ValueError:
        pass
    else:
        raise AssertionError('missing data directory should fail')
    assert not target.exists()


def test_json_output_and_read_only(tmp_path, capsys):
    (tmp_path / 'test.log').write_bytes(b'abc')
    before = list(tmp_path.iterdir())
    rc = AUDIT.main([str(tmp_path), '--json'])
    report = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert report['total_bytes'] == 3
    assert report['categories']['logs']['bytes'] == 3
    assert before == list(tmp_path.iterdir())
