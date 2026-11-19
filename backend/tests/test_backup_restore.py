import json
import sqlite3
import zipfile
from io import BytesIO

import pytest

from strimkeep.backup import create_backup, restore_backup
from strimkeep.store import Store

PASSWORD = 'testpassword123'


def make_store(tmp_path, name='source'):
    return Store(tmp_path / name, 'admin', PASSWORD)


def test_backup_restore_roundtrip(tmp_path):
    store = make_store(tmp_path)
    store.set_meta('restore_probe', {'ok': True})
    store.set_many({'restore_setting': 'value'})
    payload = create_backup(store)
    zip_path = tmp_path / 'backup.zip'
    zip_path.write_bytes(payload)
    target = tmp_path / 'target'
    result = restore_backup(zip_path, target)
    assert result['integrity'] == 'ok'
    restored = Store(target, 'admin', PASSWORD)
    assert restored.meta_json('restore_probe') == {'ok': True}
    assert restored.get('restore_setting') == 'value'
    restored.close()
    store.close()


def test_restore_refuses_overwrite_without_force(tmp_path):
    store = make_store(tmp_path)
    zip_path = tmp_path / 'backup.zip'
    zip_path.write_bytes(create_backup(store))
    target = tmp_path / 'target'
    restore_backup(zip_path, target)
    with pytest.raises(FileExistsError):
        restore_backup(zip_path, target)
    restore_backup(zip_path, target, force=True)
    store.close()


def test_restore_rejects_tampered_manifest(tmp_path):
    store = make_store(tmp_path)
    payload = create_backup(store)
    bad_path = tmp_path / 'bad.zip'
    with zipfile.ZipFile(BytesIO(payload)) as source, zipfile.ZipFile(bad_path, 'w') as target:
        for name in source.namelist():
            data = source.read(name)
            if name == 'manifest.json':
                manifest = json.loads(data)
                manifest['files'][0]['sha256'] = '0' * 64
                data = json.dumps(manifest).encode()
            target.writestr(name, data)
    with pytest.raises(ValueError, match='校验失败'):
        restore_backup(bad_path, tmp_path / 'target')
    store.close()


def test_set_meta_retries_database_locked(tmp_path):
    store = make_store(tmp_path)
    real = store.db

    class Flaky:
        def __init__(self):
            self.calls = 0

        def __enter__(self):
            return real.__enter__()

        def __exit__(self, *args):
            return real.__exit__(*args)

        def execute(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise sqlite3.OperationalError('database is locked')
            return real.execute(*args, **kwargs)

    flaky = Flaky()
    store.db = flaky
    store.set_meta('retry_probe', 1)
    assert flaky.calls >= 2
    store.db = None
    assert store.meta_json('retry_probe') == 1
    store.close()


def test_store_creates_pre_upgrade_backup(tmp_path):
    from strimkeep import __version__

    root = tmp_path / 'data'
    store = Store(root, 'admin', PASSWORD)
    store.set_meta('last_started_version', '0.0.1')
    store.close()
    reopened = Store(root, 'admin', PASSWORD)
    backups = list((root / 'config' / 'backups').glob('pre-upgrade-*.zip'))
    assert backups
    assert reopened.meta_json('last_started_version') == __version__
    reopened.close()


def test_prune_backups_keeps_newest(tmp_path):
    import os

    directory = tmp_path / 'backups'
    directory.mkdir()
    for index in range(7):
        path = directory / ('pre-upgrade-x-%d.zip' % index)
        path.write_bytes(b'x')
        os.utime(path, (1000 + index, 1000 + index))
    Store._prune_backups(directory, keep=5)
    remaining = sorted(directory.glob('pre-upgrade-*.zip'))
    assert len(remaining) == 5
    assert not (directory / 'pre-upgrade-x-0.zip').exists()

