import json
import time

import pytest

from strimkeep import internal_delete
from strimkeep.fsops import purge_strm
from strimkeep.library_roots import apply_rename, rollback
from strimkeep.store import Store


def make_store(tmp_path):
    return Store(tmp_path / 'data', 'admin', 'test-password-1234')


def test_registration_failure_never_deletes_strm(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    root = tmp_path / 'strm'
    target = root / '媒体库/电影/test.strm'
    target.parent.mkdir(parents=True)
    target.write_text('contents')
    monkeypatch.setattr(internal_delete, 'remember', lambda *args: False)
    with pytest.raises(RuntimeError):
        purge_strm(target, root, store)
    assert target.read_text() == 'contents'
    store.close()


def test_bulk_markers_are_not_silently_evicted(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    names = [f'媒体库/剧集/Season 1/{i}.strm' for i in range(3101)]
    assert internal_delete.remember(store, names)
    assert set(names) <= internal_delete.recent(store)
    monkeypatch.setattr(internal_delete, 'KEEP', len(names))
    with pytest.raises(RuntimeError):
        internal_delete.remember(store, ['另一个.strm'])
    assert set(names) <= internal_delete.recent(store)
    assert '另一个.strm' not in internal_delete.recent(store)
    store.close()


def test_corrupt_markers_never_fail_open(tmp_path):
    store = make_store(tmp_path)
    with store.db:
        store.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (internal_delete.KEY, 'broken-json'))
    with pytest.raises(ValueError):
        internal_delete.recent(store)
    with pytest.raises(ValueError):
        internal_delete.remember(store, ['电影/a.strm'])
    assert store.meta(internal_delete.KEY) == 'broken-json'
    store.close()


def test_clock_rollback_never_discards_protection(tmp_path):
    store = make_store(tmp_path)
    store.set_meta(internal_delete.KEY, {'剧/a.strm': time.time() + 600})
    assert '剧/a.strm' in internal_delete.recent(store)
    internal_delete.remember(store, ['剧/b.strm'])
    assert internal_delete.recent(store) == {'剧/a.strm', '剧/b.strm'}
    store.close()


def test_rename_fails_closed_before_any_file_moves(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    root = tmp_path / 'strm'
    old = root / '旧库/剧/Season 1'
    old.mkdir(parents=True)
    (old / 'a.strm').write_text('a')
    (old / 'b.strm').write_text('b')
    monkeypatch.setattr(internal_delete, 'remember', lambda *args: False)
    result = apply_rename(store, root, '旧库', '新库', dry_run=False)
    assert result['status'] == 'blocked'
    assert (old / 'a.strm').is_file() and (old / 'b.strm').is_file()
    assert not (root / '新库').exists()
    store.close()


def test_rollback_protects_new_paths_before_moving_back(tmp_path):
    store = make_store(tmp_path)
    root = tmp_path / 'strm'
    old = root / '旧库/剧/Season 1'
    old.mkdir(parents=True)
    (old / 'a.strm').write_text('a')
    assert apply_rename(store, root, '旧库', '新库', dry_run=False)['status'] == 'applied'
    assert rollback(store, root)['status'] == 'rolled_back'
    assert '新库/剧/Season 1/a.strm' in internal_delete.recent(store)
    store.close()


def test_queued_deletion_rechecks_marker_and_identity(tmp_path):
    import asyncio
    from types import SimpleNamespace
    from strimkeep.deep_delete import run_row

    store = make_store(tmp_path)
    store.set_many({'sync_config': {'deep_delete_dry_run': False}})
    path = '媒体库/剧/Season 1/a.strm'
    with store.db:
        store.db.execute("INSERT INTO organized_files(file_id,job_id,item_id,playback_id,relative_path,metadata) VALUES('1',1,1,'pid',?,'{}')", (path,))
    engine = SimpleNamespace(store=store, root=tmp_path / 'strm')
    queued = {'kind':'managed','file_id':'1','playback_id':'pid','relative_path':path,'dry_run':0}
    internal_delete.remember(store, [path])
    state, message, _ = asyncio.run(run_row(engine, queued))
    assert state == 'skipped' and '内部删除' in message
    with store.db:
        store.db.execute("UPDATE organized_files SET relative_path='媒体库/other.strm' WHERE file_id='1'")
    state, message, _ = asyncio.run(run_row(engine, queued))
    assert state == 'skipped' and '路径' in message
    with store.db:
        store.db.execute('UPDATE organized_files SET relative_path=? WHERE file_id=?', (path, '1'))
        store.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (internal_delete.KEY, 'broken-json'))
    with pytest.raises(ValueError):
        asyncio.run(run_row(engine, queued))
    store.close()


def test_queued_share_deletion_rechecks_current_path(tmp_path):
    import asyncio
    from types import SimpleNamespace
    from strimkeep.deep_delete import run_row
    store = make_store(tmp_path)
    store.set_many({'sync_config': {'deep_delete_dry_run': False}})
    with store.db:
        store.db.execute("INSERT INTO share_strms(id,share_code,receive_code,file_id,pickcode,name,strm_path,token_hash,revoked,created) VALUES('s','code','','1','','x','新路径/a.strm','',0,0)")
    engine = SimpleNamespace(store=store, root=tmp_path / 'strm')
    queued = {'kind':'share','share_id':'s','relative_path':'旧路径/a.strm','dry_run':0}
    state, message, _ = asyncio.run(run_row(engine, queued))
    assert state == 'skipped' and '路径' in message
    store.close()


def test_queued_real_delete_is_skipped_when_mode_switches_to_preview(tmp_path):
    import asyncio
    from types import SimpleNamespace
    from strimkeep.deep_delete import run_row
    store = make_store(tmp_path)
    root = tmp_path / 'strm'
    path = '媒体库/电影/a.strm'
    local = root / path
    local.parent.mkdir(parents=True)
    local.write_text('keep')
    with store.db:
        store.db.execute("INSERT INTO organized_files(file_id,job_id,item_id,playback_id,relative_path,metadata) VALUES('1',1,1,'pid',?,'{}')", (path,))
    queued = {'kind':'managed','file_id':'1','playback_id':'pid','relative_path':path,'dry_run':0}
    store.set_many({'sync_config': {'deep_delete_dry_run': True}})
    state, message, _ = asyncio.run(run_row(SimpleNamespace(store=store, root=root), queued))
    assert state == 'skipped' and '预演' in message and local.read_text() == 'keep'
    store.close()
