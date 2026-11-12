import pytest

from strimkeep.inventory_audit import audit_roots


def create(root, relative):
    file = root / relative
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text('test', encoding='utf-8')


def test_reconcile_both_libraries_and_bound_examples(tmp_path):
    (tmp_path / 'cloud').mkdir()
    (tmp_path / 'share').mkdir()
    create(tmp_path, 'cloud/a.strm')
    create(tmp_path, 'cloud/unregistered.strm')
    create(tmp_path, 'share/b.strm')
    records = [('cloud', '1', 'cloud/a.strm'), ('cloud', '2', 'cloud/missing.strm'),
               ('share', '3', 'share/b.strm'), ('share', '4', 'share/b.strm'),
               ('cloud', '5', '../outside.strm')]
    result = audit_roots(tmp_path, {'cloud': 'cloud', 'share': 'share'}, records, limit=1)
    assert result['libraries']['cloud']['unregistered'] == ['cloud/unregistered.strm']
    assert result['libraries']['cloud']['missing'] == ['cloud/missing.strm']
    assert result['duplicate_count'] == result['outside_count'] == 1
    assert not result['clean'] and not result['executable']


def test_missing_or_ambiguous_roots_cannot_look_empty(tmp_path):
    (tmp_path / 'cloud').mkdir()
    with pytest.raises(ValueError):
        audit_roots(tmp_path, {'cloud': 'cloud', 'share': 'share'}, [])
    with pytest.raises(ValueError):
        audit_roots(tmp_path, {'cloud': 'cloud', 'share': 'cloud'}, [])
    with pytest.raises(ValueError):
        audit_roots(tmp_path, {'cloud': '../outside', 'share': 'share'}, [])


def test_clean_reconciliation_is_still_not_executable(tmp_path):
    create(tmp_path, 'cloud/a.strm')
    (tmp_path / 'share').mkdir()
    result = audit_roots(tmp_path, {'cloud': 'cloud', 'share': 'share'}, [('cloud', '1', 'cloud/a.strm')])
    assert result['clean'] and result['executable'] is False


def test_links_to_directories_and_strms_are_rejected(tmp_path):
    import os
    if os.name == 'nt':
        pytest.skip('Windows symlink creation may require administrator privileges')
    (tmp_path / 'cloud').mkdir(); (tmp_path / 'share').mkdir(); (tmp_path / 'outside').mkdir()
    (tmp_path / 'cloud' / 'linked').symlink_to(tmp_path / 'outside', target_is_directory=True)
    with pytest.raises(ValueError, match='目录链接'):
        audit_roots(tmp_path, {'cloud': 'cloud', 'share': 'share'}, [])
    (tmp_path / 'cloud' / 'linked').unlink()
    (tmp_path / 'cloud' / 'bad.strm').symlink_to(tmp_path / 'outside' / 'missing')
    with pytest.raises(ValueError, match='STRM'):
        audit_roots(tmp_path, {'cloud': 'cloud', 'share': 'share'}, [])
