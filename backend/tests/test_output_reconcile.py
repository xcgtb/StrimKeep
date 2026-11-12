"""三表对账：每一类不一致都要能被发现，且只做能证明的修复。"""
import json
import sqlite3
import time

SCHEMA = """
CREATE TABLE output_registry(
  account_id TEXT NOT NULL, file_id TEXT NOT NULL, source TEXT NOT NULL, trail TEXT NOT NULL,
  playback_id TEXT NOT NULL, token_cipher TEXT NOT NULL, relative_path TEXT NOT NULL,
  files TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, updated REAL NOT NULL,
  PRIMARY KEY(account_id, file_id));
CREATE TABLE owned_outputs(
  relative_path TEXT PRIMARY KEY, account_id TEXT NOT NULL, file_id TEXT NOT NULL,
  digest TEXT NOT NULL);
CREATE TABLE share_strms(
  id TEXT PRIMARY KEY, share_code TEXT NOT NULL, receive_code TEXT NOT NULL DEFAULT '',
  file_id TEXT NOT NULL, pickcode TEXT NOT NULL DEFAULT '', name TEXT NOT NULL,
  strm_path TEXT NOT NULL, token_hash TEXT NOT NULL, revoked INTEGER NOT NULL DEFAULT 0,
  created REAL NOT NULL);
"""


class Store:
    def __init__(self, root):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.root = str(root)
        self.events = []

    def get(self, key, default=''):
        return default

    def event(self, kind, message):
        self.events.append((kind, message))

    def registry(self, fid, files, updated=None, account='a'):
        self.db.execute('INSERT INTO output_registry VALUES(?,?,?,?,?,?,?,?,1,?)',
                        (account, fid, '{}', '[]', 'p' + fid, 'x', next(iter(files)),
                         json.dumps(files, ensure_ascii=False),
                         time.time() - 7200 if updated is None else updated))

    def owned(self, relative, fid, digest='d', account='a'):
        self.db.execute('INSERT OR REPLACE INTO owned_outputs VALUES(?,?,?,?)',
                        (relative, account, fid, digest))

    def share(self, sid, path, revoked=0, created=None):
        self.db.execute('INSERT INTO share_strms VALUES(?,?,?,?,?,?,?,?,?,?)',
                        (sid, 'code', '', 'f1', '', 'name', path, 'h', revoked,
                         time.time() - 7200 if created is None else created))


def test_scan_detects_every_class(tmp_path):
    from strimkeep.output_reconcile import scan

    store = Store(tmp_path)
    (tmp_path / 'fine.strm').write_text('x', encoding='utf-8')
    (tmp_path / 'kept.strm').write_text('x', encoding='utf-8')

    store.registry('f1', {'fine.strm': 'd1', 'gone.strm': 'd2'})   # gone → stale
    store.owned('fine.strm', 'f1')                                  # 一致
    store.owned('ghost.strm', 'f1')                                 # 文件不存在 → ghost
    store.owned('kept.strm', 'f9')                                  # 台账无 f9 → orphan
    store.share('s1', 'share-missing.strm')                         # 文件不存在 → 撤销
    store.share('s2', 'fine.strm')                                  # 与台账同路径 → 冲突
    store.db.commit()

    report = scan(store, str(tmp_path))
    counts = report['counts']
    assert counts['registry'] == 1 and counts['owned'] == 3 and counts['share'] == 2
    assert counts['ghost_ownership'] == 1
    assert counts['orphan_ownership'] == 1
    assert counts['stale_in_registry'] == 1
    assert counts['share_missing'] == 1
    assert counts['path_conflict'] == 1
    assert counts['missing_owned'] == 0
    assert report['healthy'] is False
    assert {item['kind'] for item in report['issues']} == {
        'ghost_ownership', 'orphan_ownership', 'stale_in_registry',
        'share_missing', 'path_conflict'}


def test_fix_repairs_only_what_is_provable(tmp_path):
    from strimkeep.output_reconcile import fix, scan

    store = Store(tmp_path)
    (tmp_path / 'fine.strm').write_text('x', encoding='utf-8')
    (tmp_path / 'heal.strm').write_text('x', encoding='utf-8')
    (tmp_path / 'kept.strm').write_text('x', encoding='utf-8')   # 存在但台账里没有

    store.registry('f1', {'fine.strm': 'd1', 'gone.strm': 'd2', 'heal.strm': 'd3'})
    store.owned('fine.strm', 'f1')
    store.owned('ghost.strm', 'f1')
    store.owned('kept.strm', 'f9')
    store.share('s1', 'share-missing.strm')
    store.share('s2', 'fine.strm')
    store.db.commit()

    preview = fix(store, str(tmp_path), dry_run=True)
    assert preview['ghost_ownership'] == 1 and preview['stale_in_registry'] == 1
    assert store.db.execute('SELECT count(*) FROM owned_outputs').fetchone()[0] == 3

    result = fix(store, str(tmp_path))
    assert result['ghost_ownership'] == 1 and result['orphan_ownership'] == 1
    assert result['missing_owned'] == 1 and result['stale_in_registry'] == 1
    assert result['share_missing'] == 1 and result['path_conflict'] == 1
    assert result['backup'] and 'reconcile-' in result['backup']

    paths = {row[0] for row in store.db.execute('SELECT relative_path FROM owned_outputs')}
    assert paths == {'fine.strm', 'heal.strm'}          # ghost/orphan 删除，heal 补建
    files = json.loads(store.db.execute(
        'SELECT files FROM output_registry WHERE file_id=?', ('f1',)).fetchone()[0])
    assert 'gone.strm' not in files and 'fine.strm' in files and 'heal.strm' in files
    assert store.db.execute('SELECT revoked FROM share_strms WHERE id=?',
                            ('s1',)).fetchone()[0] == 1
    assert store.db.execute('SELECT revoked FROM share_strms WHERE id=?',
                            ('s2',)).fetchone()[0] == 0   # 冲突不自动处理，交人工

    # 冲突类修不了，复查仍应报出来（不静默吞掉）
    assert scan(store, str(tmp_path))['counts']['path_conflict'] == 1
    backup = json.load(open(result['backup'], encoding='utf-8'))
    assert len(backup['owned_outputs_deleted']) == 2
    assert len(backup['path_conflict_unhandled']) == 1


def test_grace_protects_fresh_rows(tmp_path):
    from strimkeep.output_reconcile import scan

    store = Store(tmp_path)
    store.registry('f1', {'just-written.strm': 'd'}, updated=time.time())
    store.share('s1', 'fresh-share.strm', created=time.time())
    store.db.commit()
    counts = scan(store, str(tmp_path))['counts']
    assert counts['stale_in_registry'] == 0 and counts['share_missing'] == 0
