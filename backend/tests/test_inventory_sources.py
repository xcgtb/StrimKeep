import json
import sqlite3

import pytest

from strimkeep.inventory_sources import source_fingerprints


def database():
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.executescript('''CREATE TABLE organized_files(file_id TEXT,playback_id TEXT,relative_path TEXT);
      CREATE TABLE output_registry(account_id TEXT,file_id TEXT,source TEXT,trail TEXT,playback_id TEXT,relative_path TEXT,files TEXT,active INTEGER);
      CREATE TABLE owned_outputs(relative_path TEXT,account_id TEXT,file_id TEXT,digest TEXT);
      CREATE TABLE share_strms(id TEXT,share_code TEXT,receive_code TEXT,file_id TEXT,pickcode TEXT,strm_path TEXT,token_hash TEXT,revoked INTEGER);''')
    db.execute('INSERT INTO organized_files VALUES (?,?,?)', ('1', 'p1', 'cloud/a.strm'))
    db.execute('INSERT INTO output_registry VALUES (?,?,?,?,?,?,?,?)', ('account', '1', '{"id":"1"}', '[]', 'p1', 'cloud/a.strm', json.dumps({'cloud/a.strm': 'digest'}), 1))
    db.execute('INSERT INTO owned_outputs VALUES (?,?,?,?)', ('cloud/a.strm', 'account', '1', 'digest'))
    db.execute('INSERT INTO share_strms VALUES (?,?,?,?,?,?,?,?)', ('s1', 'secret-code', 'pass', '9', 'pick', 'share/a.strm', 'hash', 0))
    return db


def test_fingerprints_change_with_source_without_exposing_secrets():
    db = database()
    rows = [('cloud', '1', 'cloud/a.strm'), ('share', 's1', 'share/a.strm')]
    first = source_fingerprints(db, rows, 'account')
    assert 'secret-code' not in json.dumps(first) and 'pass' not in json.dumps(first)
    db.execute("UPDATE share_strms SET file_id='10' WHERE id='s1'")
    second = source_fingerprints(db, rows, 'account')
    assert first[0] == second[0] and first[1]['source_hash'] != second[1]['source_hash']


def test_missing_ownership_or_changed_path_fails_closed():
    db = database()
    with pytest.raises(ValueError):
        source_fingerprints(db, [('cloud', '1', 'cloud/a.strm')], 'wrong-account')
    db.execute("UPDATE owned_outputs SET digest='different'")
    with pytest.raises(ValueError):
        source_fingerprints(db, [('cloud', '1', 'cloud/a.strm')], 'account')
    with pytest.raises(ValueError):
        source_fingerprints(db, [('share', 's1', 'share/other.strm')], 'account')
