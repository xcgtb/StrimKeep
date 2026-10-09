"""Archive integrity tests: all checks must remain strictly read-only."""
import importlib.util
import io
import os
import tarfile
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    'migrated_archive_verify',
    Path(__file__).resolve().parents[2] / 'dev' / 'tools' / 'migrated_archive_verify.py')
verify = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(verify)


def make_archive(tmp_path, members):
    archive = tmp_path / 'migrated.tar.gz'
    with tarfile.open(archive, 'w:gz') as tf:
        for name, value in members:
            if isinstance(value, tarfile.TarInfo):
                tf.addfile(value)
                continue
            content = value
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))
    return archive


def test_success_and_no_modifications(tmp_path):
    state = tmp_path / 'state'
    state.mkdir()
    (state / 'x.json.migrated').write_bytes(b'hello')
    (state / 'nested').mkdir()
    (state / 'nested' / 'plan_x.json.migrated').write_bytes(b'{"ok":1}')
    archive = make_archive(tmp_path, [
        ('data/state/x.json.migrated', b'hello'),
        ('data/state/nested/plan_x.json.migrated', b'{"ok":1}'),
    ])
    files = [state / 'x.json.migrated', state / 'nested' / 'plan_x.json.migrated', archive]
    before = [(p.read_bytes(), p.stat().st_mtime_ns) for p in files]
    report = verify.inspect(state, archive, expected_count=2)
    assert report['ok'] and report['matched'] == 2 and report['gzip_verified']
    assert report['bytes_matched'] == 13
    assert [(p.read_bytes(), p.stat().st_mtime_ns) for p in files] == before


def test_changed_contents_are_not_accepted(tmp_path):
    state = tmp_path / 'state'
    state.mkdir()
    (state / 'x.migrated').write_bytes(b'abc')
    archive = make_archive(tmp_path, [('data/state/x.migrated', b'abd')])
    report = verify.inspect(state, archive)
    assert not report['ok'] and report['matched'] == 0
    assert 'SHA256' in report['different'][0]


def test_missing_archive_member_is_failure(tmp_path):
    state = tmp_path / 'state'
    state.mkdir()
    (state / 'x.migrated').write_bytes(b'abc')
    archive = make_archive(tmp_path, [])
    report = verify.inspect(state, archive)
    assert not report['ok'] and report['missing_in_archive'] == ['x.migrated']


def test_extra_archive_member_is_failure(tmp_path):
    state = tmp_path / 'state'
    state.mkdir()
    archive = make_archive(tmp_path, [('data/state/extra.migrated', b'a')])
    report = verify.inspect(state, archive)
    assert not report['ok'] and report['only_in_archive'] == ['extra.migrated']


def test_duplicate_archive_member_is_failure(tmp_path):
    state = tmp_path / 'state'
    state.mkdir()
    (state / 'x.migrated').write_bytes(b'a')
    archive = make_archive(tmp_path, [
        ('data/state/x.migrated', b'a'), ('data/state/x.migrated', b'a')])
    report = verify.inspect(state, archive)
    assert not report['ok'] and any('重复' in e for e in report['errors'])


def test_symlink_in_archive_is_failure(tmp_path):
    state = tmp_path / 'state'
    state.mkdir()
    info = tarfile.TarInfo('data/state/x.migrated')
    info.type = tarfile.SYMTYPE
    info.linkname = '/etc/passwd'
    archive = make_archive(tmp_path, [(info.name, info)])
    report = verify.inspect(state, archive)
    assert not report['ok'] and report['errors']


def test_path_traversal_is_failure(tmp_path):
    state = tmp_path / 'state'
    state.mkdir()
    archive = make_archive(tmp_path, [('data/state/../x.migrated', b'abc')])
    report = verify.inspect(state, archive)
    assert not report['ok'] and report['errors']


def test_corrupted_gzip_is_failure(tmp_path):
    state = tmp_path / 'state'
    state.mkdir()
    archive = make_archive(tmp_path, [('data/state/x.migrated', b'x')])
    with archive.open('r+b') as f:
        f.seek(-1, os.SEEK_END)
        f.write(b'!')
    report = verify.inspect(state, archive)
    assert not report['ok'] and not report['gzip_verified']


def test_expected_count_and_cli_return(tmp_path, capsys):
    state = tmp_path / 'state'
    state.mkdir()
    (state / 'a.migrated').write_bytes(b'abc')
    archive = make_archive(tmp_path, [('data/state/a.migrated', b'abc')])
    assert verify.main([str(archive), '--state', str(state), '--expect-count', '1']) == 0
    assert '校验通过' in capsys.readouterr().out
    assert verify.main([str(archive), '--state', str(state), '--expect-count', '61']) == 1
    assert '校验失败' in capsys.readouterr().out


def test_source_file_symlink_is_rejected(tmp_path):
    import pytest
    state = tmp_path / 'state'
    state.mkdir()
    outside = tmp_path / 'outside'
    outside.write_bytes(b'a')
    (state / 'alias.migrated').symlink_to(outside)
    archive = make_archive(tmp_path, [])
    with pytest.raises(ValueError):
        verify.inspect(state, archive)
