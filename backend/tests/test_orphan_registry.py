"""台账孤儿清理：只删文件确实不存在的行，宽限期内的新行不碰。"""

import json
import sqlite3
import time


class FakeStore:
    def __init__(self, root):
        self.root = root
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            'CREATE TABLE output_registry (account_id TEXT, relative_path TEXT, updated REAL);'
            'CREATE TABLE share_strms (id TEXT PRIMARY KEY, strm_path TEXT, created REAL);')
        self.events = []

    def event(self, kind, message):
        self.events.append((kind, message))


def _add_file(root, relative):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('http://x/play115/a/b/c/1/d.mkv', encoding='utf-8')
    return path


def test_prune_removes_only_missing_files(tmp_path):
    from strimkeep.orphan_registry import prune, scan

    _add_file(tmp_path, '媒体库/电影/在的/a.strm')
    store = FakeStore(tmp_path)
    old = time.time() - 7200
    store.db.executemany('INSERT INTO output_registry VALUES (?,?,?)',
                         [('acc', '媒体库/电影/在的/a.strm', old),
                          ('acc', '媒体库/电影/没的/b.strm', old)])
    store.db.executemany('INSERT INTO share_strms VALUES (?,?,?)',
                         [('s1', '分享/在的/c.strm', old), ('s2', '分享/没的/d.strm', old)])
    _add_file(tmp_path, '分享/在的/c.strm')
    store.db.commit()

    orphans, share_orphans, total, total_share = scan(store, str(tmp_path))
    assert (len(orphans), len(share_orphans), total, total_share) == (1, 1, 2, 2)

    preview = prune(store, str(tmp_path), dry_run=True)
    assert preview['registry'] == 1 and preview['share'] == 1 and preview['backup'] == ''
    assert store.db.execute('SELECT count(*) FROM output_registry').fetchone()[0] == 2

    result = prune(store, str(tmp_path))
    assert result['registry'] == 1 and result['share'] == 1
    rows = [row['relative_path'] for row in store.db.execute('SELECT relative_path FROM output_registry')]
    assert rows == ['媒体库/电影/在的/a.strm']
    shares = [row['id'] for row in store.db.execute('SELECT id FROM share_strms')]
    assert shares == ['s1']

    dump = json.loads(open(result['backup'], encoding='utf-8').read())
    assert [row['relative_path'] for row in dump['output_registry']] == ['媒体库/电影/没的/b.strm']
    assert [row['id'] for row in dump['share_strms']] == ['s2']


def test_prune_respects_grace_period(tmp_path):
    """刚写进去、文件还没落盘的行不能被当成孤儿删掉。"""
    from strimkeep.orphan_registry import prune

    store = FakeStore(tmp_path)
    store.db.execute('INSERT INTO output_registry VALUES (?,?,?)',
                     ('acc', '媒体库/电影/刚写的/e.strm', time.time()))
    store.db.commit()

    assert prune(store, str(tmp_path), dry_run=True)['registry'] == 0
    assert prune(store, str(tmp_path))['registry'] == 0
    assert store.db.execute('SELECT count(*) FROM output_registry').fetchone()[0] == 1


def test_managed_repair_requires_missing_source_and_matching_account(tmp_path):
    import asyncio
    from types import SimpleNamespace
    from strimkeep.orphan_registry import repair_missing_organized
    root = tmp_path / 'strm'; (root / '媒体库').mkdir(parents=True)
    store = FakeStore(tmp_path)
    db = store.db
    db.executescript('DROP TABLE output_registry; CREATE TABLE output_registry(file_id TEXT, relative_path TEXT);'
                     'CREATE TABLE owned_outputs(file_id TEXT, relative_path TEXT);'
                     'CREATE TABLE organized_files(file_id TEXT, playback_id TEXT, relative_path TEXT, job_id INTEGER);'
                     'CREATE TABLE organize_jobs(id INTEGER, config TEXT, updated REAL, state TEXT);'
                     'CREATE TABLE playbacks(id TEXT,file_id TEXT);')
    cfg = json.dumps({'account_id':'a'})
    db.execute('INSERT INTO organize_jobs VALUES(1,?,?,?)', (cfg, time.time()-7200, 'completed'))
    for fid in ('gone','live','error'):
        db.execute('INSERT INTO playbacks VALUES(?,?)', ('p'+fid, fid))
        db.execute('INSERT INTO organized_files VALUES(?,?,?,1)', (fid,'p'+fid,'媒体库/'+fid+'.strm'))
    db.commit()
    store.get = lambda key, default=None: {'library_dir_name':'媒体库'} if key == 'organize_config' else default
    class Cloud:
        async def live_file_exists(self, fid):
            if fid == 'error': raise RuntimeError('offline')
            return fid == 'live'
    engine = SimpleNamespace(index=SimpleNamespace(account=lambda _: 'a'), publication_gate=asyncio.Lock())
    app = SimpleNamespace(state=SimpleNamespace(strms=root, organizer=engine, cloud=Cloud()))
    assert asyncio.run(repair_missing_organized(app, store)) == 1
    assert {r[0] for r in db.execute('SELECT file_id FROM organized_files')} == {'live','error'}
    engine.index.account = lambda _: 'other'
    assert asyncio.run(repair_missing_organized(app, store)) == 0
    db.close()
