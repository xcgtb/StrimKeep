from strimkeep.store import Store


def test_close_checkpoints_wal(tmp_path):
    store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
    store.set_many({'wal_probe': {'ok': True}})
    wal = tmp_path / 'data' / 'data' / 'state.db-wal'
    assert wal.exists() and wal.stat().st_size > 0
    store.close()
    assert not wal.exists() or wal.stat().st_size == 0
