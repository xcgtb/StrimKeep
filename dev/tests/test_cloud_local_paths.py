from pathlib import Path
import pytest
from app.cloud import local_paths
from app.cloud.errors import CloudError


def mounted(monkeypatch, tmp_path):
    root = tmp_path / 'mounted'
    root.mkdir()
    (root / 'movies').mkdir()
    (root / 'films.strm').write_text('not browsed')
    monkeypatch.setattr(local_paths, '_mount_points', lambda: [Path('/'), Path('/data'), root])
    return root


def test_lists_only_media_mounts_and_directories(monkeypatch, tmp_path):
    root = mounted(monkeypatch, tmp_path)
    assert [p['path'] for p in local_paths.browse()['roots']] == [str(root)]
    result = local_paths.browse(str(root))
    assert [p['name'] for p in result['items']] == ['movies']
    assert result['parent'] == ''
    assert local_paths.browse(str(root / 'movies'))['parent'] == str(root)


@pytest.mark.parametrize('value', ['/', '/data', '/etc', '../mounted', '/tmp/../etc'])
def test_rejects_unmounted_and_parent_traversal(monkeypatch, tmp_path, value):
    mounted(monkeypatch, tmp_path)
    with pytest.raises(CloudError):
        local_paths.browse(value)


def test_symlink_cannot_escape_mount(monkeypatch, tmp_path):
    root = mounted(monkeypatch, tmp_path)
    outside = tmp_path / 'secret'
    outside.mkdir()
    (root / 'escape').symlink_to(outside, target_is_directory=True)
    assert [p['name'] for p in local_paths.browse(str(root))['items']] == ['movies']
    with pytest.raises(CloudError):
        local_paths.browse(str(root / 'escape'))


def test_missing_directory_does_not_create_output(monkeypatch, tmp_path):
    root = mounted(monkeypatch, tmp_path)
    path = root / 'missing'
    with pytest.raises(CloudError):
        local_paths.browse(str(path))
    assert not path.exists()
