"""内部删除登记已移除：删除即删除，不留任何会挡住下次清理/入库的状态。"""
from strimkeep import internal_delete
from strimkeep.fsops import purge_strm
from strimkeep.store import Store


def make_store(tmp_path):
    return Store(tmp_path / 'data', 'admin', 'test-password-1234')


def test_remember_and_recent_are_stateless(tmp_path):
    store = make_store(tmp_path)
    assert internal_delete.remember(store, ['媒体库/电影/a.strm'])
    assert internal_delete.recent(store) == set()
    assert internal_delete.recent(store, kind='managed') == set()
    assert internal_delete.recent(store, kind='share') == set()
    assert store.db.execute("SELECT value FROM meta WHERE key=?", (internal_delete.KEY,)).fetchone() is None
    store.close()


def test_forget_is_noop(tmp_path):
    store = make_store(tmp_path)
    assert internal_delete.forget(store, ['媒体库/电影/a.strm']) == 0
    store.close()


def test_purge_strm_deletes_file_without_marker(tmp_path):
    store = make_store(tmp_path)
    root = tmp_path / 'strm'
    target = root / '媒体库/电影/a.strm'
    target.parent.mkdir(parents=True)
    target.write_text('contents')
    purge_strm(target, root, store=store)
    assert not target.exists()
    assert internal_delete.recent(store) == set()
    assert store.db.execute("SELECT value FROM meta WHERE key=?", (internal_delete.KEY,)).fetchone() is None
    store.close()
