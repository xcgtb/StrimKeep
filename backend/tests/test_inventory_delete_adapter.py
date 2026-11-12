import asyncio
import json
import sqlite3
import sys
from types import ModuleType, SimpleNamespace

import pytest

from strimkeep.inventory_delete_adapter import delete_one
from strimkeep.inventory_snapshot import _fingerprint
from strimkeep.inventory_sources import source_fingerprints


def setup_share(root):
    path = root / 'share' / 'a.strm'
    path.parent.mkdir()
    path.write_text('http://example/play115/share/s1/a', encoding='utf-8')
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.executescript('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);'
                     'CREATE TABLE share_strms(id TEXT,share_code TEXT,receive_code TEXT,file_id TEXT,pickcode TEXT,strm_path TEXT,token_hash TEXT,revoked INTEGER);')
    db.execute('INSERT INTO share_strms VALUES (?,?,?,?,?,?,?,?)', ('s1', 'code', '', 'f1', '', 'share/a.strm', 'hash', 0))
    db.commit()
    engine = SimpleNamespace(root=root, store=SimpleNamespace(db=db), publication_gate=asyncio.Lock(), index=SimpleNamespace(account=lambda _: 'account'))
    item = {'side': 'share', 'id': 's1', 'path': 'share/a.strm', **_fingerprint(root, 'share/a.strm')}
    source = source_fingerprints(db, [('share', 's1', 'share/a.strm')], 'account')[0]
    return engine, item, source, path


def test_share_deletion_requires_lock_and_ownership(tmp_path, monkeypatch):
    engine, item, source, path = setup_share(tmp_path)
    share = ModuleType('strimkeep.share_service')
    share._verified_share_strm = lambda root, relative, sid: root / relative if sid == 's1' else None
    monkeypatch.setitem(sys.modules, 'strimkeep.share_service', share)
    with pytest.raises(RuntimeError):
        asyncio.run(delete_one(engine, item, source))
    async def run():
        async with engine.publication_gate:
            return await delete_one(engine, item, source)
    assert asyncio.run(run()) is True
    assert not path.exists()
    assert engine.store.db.execute('SELECT revoked FROM share_strms').fetchone()[0] == 1
    assert 'share/a.strm' in json.loads(engine.store.db.execute("SELECT value FROM meta WHERE key='internal_deletes'").fetchone()[0])


def test_changed_share_identity_or_file_is_preserved(tmp_path):
    engine, item, source, path = setup_share(tmp_path)
    async def run():
        async with engine.publication_gate:
            return await delete_one(engine, item, source)
    engine.store.db.execute("UPDATE share_strms SET file_id='other'")
    engine.store.db.commit()
    assert not asyncio.run(run()) and path.exists()
    engine.store.db.execute("UPDATE share_strms SET file_id='f1'")
    engine.store.db.commit()
    path.write_text('changed', encoding='utf-8')
    assert not asyncio.run(run()) and path.exists()
