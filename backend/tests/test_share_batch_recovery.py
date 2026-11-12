import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from strimkeep import recover_share_batch as recovery


def setup(tmp_path):
    data = tmp_path / 'data'
    root = tmp_path / 'strm'
    (data / 'config').mkdir(parents=True)
    (data / 'data').mkdir()
    (root / '分享影视库' / '作品' / 'Season 1').mkdir(parents=True)
    key = Fernet.generate_key()
    (data / 'config/master.key').write_bytes(key)
    db = sqlite3.connect(data / 'data/state.db')
    db.executescript('''
      CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
      CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
      CREATE TABLE deep_delete_queue(id INTEGER PRIMARY KEY,batch TEXT,kind TEXT,share_id TEXT,relative_path TEXT,state TEXT,dry_run INTEGER);
      CREATE TABLE share_strms(id TEXT PRIMARY KEY,share_code TEXT,receive_code TEXT,file_id TEXT,strm_path TEXT,token_hash TEXT,revoked INTEGER);
      CREATE TABLE share_revocations(share_code TEXT,file_id TEXT);
      CREATE TABLE owned_outputs(relative_path TEXT);
      CREATE TABLE organize_items(id INTEGER PRIMARY KEY,file_id TEXT,original TEXT,plan TEXT);
    ''')
    db.execute('INSERT INTO settings VALUES (?,?)', ('playback_base', Fernet(key).encrypt(json.dumps('http://example.test:9000').encode()).decode()))
    for num in (1, 2):
        sid = str(num) * 32
        fid = 'file' + str(num)
        rel = f'分享影视库/作品/Season 1/S01E0{num}.strm'
        db.execute('INSERT INTO deep_delete_queue VALUES(?,?,?,?,?,?,?)', (num,'batch-1','share',sid,rel,'done',0))
        db.execute('INSERT INTO share_strms VALUES(?,?,?,?,?,?,?)', (sid,'share-code','',fid,rel,hashlib.sha256(b'').hexdigest(),1))
        db.execute('INSERT INTO organize_items VALUES(?,?,?,?)', (num,fid,json.dumps({'id':fid,'name':f'episode{num}.mkv'}),json.dumps({'playback_id':'share/'+sid})))
    db.commit(); db.close()
    return data, root


def test_dry_run_read_only_apply_and_idempotence(tmp_path):
    data, root = setup(tmp_path)
    assert [r['state'] for r in recovery.inspect_batch(data, root, 'batch-1', 2)] == ['ready','ready']
    db = sqlite3.connect(data / 'data/state.db')
    assert db.execute("SELECT count(*) FROM sqlite_master WHERE name='share_recovery_intents'").fetchone()[0] == 0
    db.close()
    assert [r['state'] for r in recovery.apply_batch(data, root, 'batch-1', 2)] == ['done','done']
    assert [r['state'] for r in recovery.apply_batch(data, root, 'batch-1', 2)] == ['done','done']
    db = sqlite3.connect(data / 'data/state.db')
    assert db.execute('SELECT count(*) FROM share_strms WHERE revoked=0').fetchone()[0] == 2
    pending = json.loads(db.execute("SELECT value FROM meta WHERE key='share_url_refresh_pending'").fetchone()[0])
    assert len(pending) == 2
    db.close()


def test_existing_target_blocks_all_without_overwrite(tmp_path):
    data, root = setup(tmp_path)
    existing = root / '分享影视库/作品/Season 1/S01E01.strm'
    existing.write_text('user content')
    with pytest.raises(recovery.RecoveryError, match='预检'):
        recovery.apply_batch(data, root, 'batch-1', 2)
    assert existing.read_text() == 'user content'
    assert not (existing.parent / 'S01E02.strm').exists()


def test_interruption_after_publish_replays_intent(tmp_path, monkeypatch):
    data, root = setup(tmp_path)
    link = recovery.os.link
    def interrupted(*args, **kwargs):
        link(*args, **kwargs)
        raise OSError('simulated interruption after publish')
    monkeypatch.setattr(recovery.os, 'link', interrupted)
    with pytest.raises(OSError): recovery.apply_batch(data, root, 'batch-1', 2)
    monkeypatch.setattr(recovery.os, 'link', link)
    assert recovery.inspect_batch(data, root, 'batch-1', 2)[0]['state'] == 'resume'
    assert all(x['state'] == 'done' for x in recovery.apply_batch(data, root, 'batch-1', 2))


def test_changed_record_and_symlink_are_blocked(tmp_path):
    data, root = setup(tmp_path)
    db = sqlite3.connect(data / 'data/state.db')
    db.execute("UPDATE share_strms SET token_hash='changed' WHERE id=?", ('1'*32,)); db.commit(); db.close()
    assert recovery.inspect_batch(data, root, 'batch-1', 2)[0]['state'] == 'blocked'
    folder = root / '分享影视库/作品/Season 1'
    folder.rmdir(); folder.symlink_to(tmp_path, target_is_directory=True)
    assert recovery.inspect_batch(data, root, 'batch-1', 2)[1]['state'] == 'blocked'


def test_batch_count_and_active_owner_must_match(tmp_path):
    data, root = setup(tmp_path)
    with pytest.raises(recovery.RecoveryError, match='数量'):
        recovery.inspect_batch(data, root, 'batch-1', 27)
    db = sqlite3.connect(data / 'data/state.db')
    db.execute('INSERT INTO owned_outputs VALUES(?)', ('分享影视库/作品/Season 1/S01E01.strm',));db.commit();db.close()
    assert recovery.inspect_batch(data, root, 'batch-1', 2)[0]['state'] == 'blocked'
