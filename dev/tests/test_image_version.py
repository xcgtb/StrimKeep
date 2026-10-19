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


def test_main_head_release_yields_fixed_and_latest_metadata(tmp_path):
    root = repo(tmp_path)
    git(root, 'tag', 'v1.2.8')
    metadata = versions.publish_metadata(root)
    assert metadata == {'value': '1.2.8', 'tag': 'v1.2.8', 'release': 'true'}


def test_new_untagged_commit_only_updates_latest(tmp_path):
    root = repo(tmp_path)
    git(root, 'tag', 'v1.2.7')
    (root/'next.txt').write_text('next')
    git(root, 'add', 'next.txt')
    git(root, 'commit', '-qm', 'next')
    assert versions.publish_metadata(root) == {
        'value': '1.2.7', 'tag': '', 'release': 'false'
    }


def test_ambiguous_release_tags_on_one_commit_are_rejected(tmp_path):
    root = repo(tmp_path)
    git(root, 'tag', 'v1.2.7')
    git(root, 'tag', 'v1.2.8')
    with pytest.raises(ValueError, match='Multiple release tags'):
        versions.publish_metadata(root)


def test_unrelated_nonrelease_tags_do_not_trigger_version_publish(tmp_path):
    root = repo(tmp_path)
    git(root, 'tag', 'v1.2.7')
    git(root, 'tag', 'latest-test')
    assert versions.head_release_tag(root) == 'v1.2.7'


@pytest.mark.parametrize('annotated', [False, True])
def test_historical_large_version_does_not_override_newer_release_ancestry(tmp_path, annotated):
    root = repo(tmp_path)
    git(root, 'tag', 'v1.12.11')
    (root/'release.txt').write_text('current release')
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'current release')
    if annotated:
        git(root, 'tag', '-a', 'v1.5.18', '-m', 'current release')
    else:
        git(root, 'tag', 'v1.5.18')
    (root/'next.txt').write_text('unreleased fix')
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'fix')
    assert versions.resolve(root, 'branch', 'main') == '1.5.18'
    assert versions.publish_metadata(root) == {'value': '1.5.18', 'tag': '', 'release': 'false'}
    assert git(root, 'tag', '--list', 'v1.12.11') == 'v1.12.11'


def test_merged_feature_branch_release_does_not_override_main_lineage(tmp_path):
    root = repo(tmp_path)
    git(root, 'tag', '-a', 'v1.5.18', '-m', 'main release')
    git(root, 'checkout', '-qb', 'feature')
    (root/'feature.txt').write_text('feature')
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'feature')
    git(root, 'tag', 'v9.0.0')
    git(root, 'checkout', '-q', '-')
    git(root, 'merge', '--no-ff', '-qm', 'merge feature', 'feature')
    assert versions.resolve(root, 'branch', 'main') == '1.5.18'
