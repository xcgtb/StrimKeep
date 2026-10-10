"""Published versions come from Git, independently of source fallback values."""
import importlib.util
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('image_version', ROOT/'dev/tools/image_version.py')
versions = importlib.util.module_from_spec(spec)
spec.loader.exec_module(versions)


def git(root, *args):
    return subprocess.check_output(['git', *args], cwd=root, text=True).strip()


def repo(tmp_path):
    git(tmp_path, 'init', '-q')
    git(tmp_path, 'config', 'user.email', 'fixture@example.test')
    git(tmp_path, 'config', 'user.name', 'Version fixture')
    (tmp_path/'app').mkdir()
    (tmp_path/'app/version.py').write_text("__version__ = '1.0.2'\n")
    git(tmp_path, 'add', '.')
    git(tmp_path, 'commit', '-qm', 'fixture')
    return tmp_path


def test_release_tag_overrides_old_source_version(tmp_path):
    root = repo(tmp_path)
    assert versions.resolve(root, 'tag', 'v1.0.3') == '1.0.3'
    assert "'1.0.2'" in (root/'app/version.py').read_text()
    assert git(root, 'status', '--porcelain') == ''


@pytest.mark.parametrize('tag', ['vnext', 'v1.2', 'v01.2.3', 'v1.2.3;echo unsafe'])
def test_invalid_release_tag_rejected(tmp_path, tag):
    with pytest.raises(ValueError, match='v1.2.3'):
        versions.resolve(tmp_path, 'tag', tag)


def test_main_build_without_release_tags_fails_closed(tmp_path):
    root = repo(tmp_path)
    with pytest.raises(ValueError, match='No reachable release tag'):
        versions.resolve(root, 'branch', 'main')


def test_main_build_uses_latest_merged_release(tmp_path):
    root = repo(tmp_path)
    for tag in ('v1.0.9', 'v1.0.10', 'vnext'):
        git(root, 'tag', tag)
    git(root, 'checkout', '-qb', 'other')
    (root/'other.txt').write_text('unmerged')
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'other')
    git(root, 'tag', 'v9.0.0')
    git(root, 'checkout', '-q', '-')
    assert versions.resolve(root, 'branch', 'main') == '1.0.10'
