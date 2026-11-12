import json
import sqlite3

import pytest

from strimkeep.inventory_stale import candidates, clear


def setup(root):
    (root / 'cloud').mkdir()
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.executescript('''CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
      CREATE TABLE organized_files(file_id TEXT,job_id INTEGER,item_id INTEGER,playback_id TEXT,relative_path TEXT,metadata TEXT);
      CREATE TABLE playbacks(id TEXT,file_id TEXT,revoked INTEGER);
      CREATE TABLE output_registry(file_id TEXT);
      CREATE TABLE owned_outputs(file_id TEXT,relative_path TEXT);''')
    db.execute('INSERT INTO organized_files VALUES (?,?,?,?,?,?)', ('1', 1, 1, 'p1', 'cloud/a.strm', '{}'))
    db.execute('INSERT INTO playbacks VALUES (?,?,?)', ('p1', '1', 1))
    db.commit()
    return db


def test_only_revoked_unowned_missing_record_is_backed_up_and_removed(tmp_path):
    db = setup(tmp_path)
    assert len(candidates(db, tmp_path, 'cloud')) == 1
    result = clear(db, tmp_path, 'cloud', ['1'])
    assert result['removed'] == 1
    backup = db.execute('SELECT value FROM meta WHERE key=?', ('inventory-stale-backup:' + result['backup_id'],)).fetchone()
    assert json.loads(backup[0])[0]['file_id'] == '1'
    assert db.execute('SELECT count(*) FROM organized_files').fetchone()[0] == 0
    assert db.execute('SELECT revoked FROM playbacks').fetchone()[0] == 1


def test_reappeared_file_active_or_owned_record_is_never_removed(tmp_path):
    db = setup(tmp_path)
    path = tmp_path / 'cloud' / 'a.strm'
    path.write_text('exists')
    with pytest.raises(ValueError): clear(db, tmp_path, 'cloud', ['1'])
    path.unlink()
    db.execute('INSERT INTO output_registry VALUES (?)', ('1',))
    db.commit()
    assert not candidates(db, tmp_path, 'cloud')
    with pytest.raises(ValueError): clear(db, tmp_path, 'cloud', ['1'])
    assert db.execute('SELECT count(*) FROM organized_files').fetchone()[0] == 1


def test_transaction_rolls_back_all_if_any_id_invalid(tmp_path):
    db = setup(tmp_path)
    with pytest.raises(ValueError): clear(db, tmp_path, 'cloud', ['1', 'missing'])
    assert db.execute('SELECT count(*) FROM organized_files').fetchone()[0] == 1
    assert db.execute('SELECT count(*) FROM meta').fetchone()[0] == 0


def test_another_active_playback_blocks_cleanup(tmp_path):
    db = setup(tmp_path)
    db.execute('INSERT INTO playbacks VALUES (?,?,?)', ('p2', '1', 0))
    db.commit()
    assert not candidates(db, tmp_path, 'cloud')
