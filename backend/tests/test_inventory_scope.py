import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest
from strimkeep.inventory_scope import verify_work
from strimkeep.inventory_snapshot import _fingerprint, _identity
from strimkeep.inventory_api import inventory_config_value
from strimkeep.quality import public_policy


def setup_work(tmp_path, monkeypatch):
    from strimkeep import inventory_scope as scope
    root = tmp_path
    (root / 'cloud').mkdir(); (root / '分享影视库').mkdir()
    rows = []
    for side, prefix, fid in [('cloud','cloud','a'),('share','分享影视库','b')]:
        rel = prefix + '/剧集/Show {tmdb-12}/Season 1/Show.S01E01.strm'
        p = root / rel; p.parent.mkdir(parents=True); p.write_text('http://example/'+fid)
        rows.append((side,fid,rel))
    files = [{'side':s,'id':i,'path':p,**_fingerprint(root,p)} for s,i,p in rows]
    sources = [{'side':s,'id':i,'path':p,'source_hash':'a'*64} for s,i,p in sorted(rows)]
    settings = {'organize_config': {'library_dir_name':'cloud'}}
    store = SimpleNamespace(db=None, get=settings.get)
    engine = SimpleNamespace(root=root, store=store, index=SimpleNamespace(account=lambda _:'account'))
    roots = {'cloud':'cloud','share':'分享影视库'}
    expected = {'roots':roots,'identity':{'root':_identity(root), **{s:_identity(root/name) for s,name in roots.items()}},
                'files':files,'sources':sources,'policy':{'upgrade':public_policy(), 'inventory':inventory_config_value({}), 'guard_revision':4}}
    monkeypatch.setattr(scope, 'registered_rows', lambda store: rows)
    monkeypatch.setattr(scope, 'source_fingerprints', lambda db, rows, account: deepcopy(sources))
    return engine, expected, files


def test_work_guard_rejects_changed_retained_file(tmp_path, monkeypatch):
    engine, expected, files = setup_work(tmp_path, monkeypatch)
    assert verify_work(engine, expected, files[1])
    (tmp_path / files[0]['path']).write_text('replaced')
    with pytest.raises(ValueError, match='保留依据'):
        verify_work(engine, expected, files[1])


def test_work_guard_rejects_added_episode_but_ignores_other_work(tmp_path, monkeypatch):
    engine, expected, files = setup_work(tmp_path, monkeypatch)
    other = tmp_path / 'cloud/剧集/Other {tmdb-44}/Season 1/Other.S01E01.strm'
    other.parent.mkdir(parents=True); other.write_text('other')
    assert verify_work(engine, expected, files[1])
    added = (tmp_path / files[0]['path']).with_name('Show.S01E02.strm')
    added.write_text('added')
    with pytest.raises(ValueError, match='成员已变化'):
        verify_work(engine, expected, files[1])
