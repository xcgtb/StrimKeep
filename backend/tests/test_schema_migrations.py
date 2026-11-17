import sqlite3

import pytest

from strimkeep.schema_migrations import COLUMN_PATCHES, MIGRATIONS, apply


def fresh_db():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.executescript('''
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE notifications(id INTEGER PRIMARY KEY, event_key TEXT UNIQUE, title TEXT, message TEXT,
          job_id INTEGER, at REAL, read_at REAL, telegram_state TEXT DEFAULT 'disabled');
        CREATE TABLE emby_deliveries(job_id INTEGER PRIMARY KEY, state TEXT, payload TEXT,
          attempts INTEGER DEFAULT 0, next_due REAL DEFAULT 0, deadline REAL, message TEXT DEFAULT '');
        CREATE TABLE offline_submissions(id INTEGER PRIMARY KEY, state TEXT, message TEXT);
        CREATE TABLE deep_delete_queue(id INTEGER PRIMARY KEY, batch TEXT, state TEXT);
        CREATE TABLE organized_files(file_id TEXT PRIMARY KEY, relative_path TEXT);
        CREATE TABLE output_registry(account_id TEXT, file_id TEXT, relative_path TEXT);
        CREATE TABLE archive_entries(snapshot_id INTEGER, relative_path TEXT);
        CREATE TABLE playbacks(id TEXT PRIMARY KEY, file_id TEXT, relative_path TEXT);
        CREATE TABLE share_strms(id TEXT PRIMARY KEY, strm_path TEXT);
        CREATE TABLE organize_items(job_id INTEGER, file_id TEXT, state TEXT);
    ''')
    return conn


def test_migrations_apply_once_and_are_idempotent():
    conn = fresh_db()
    applied = apply(conn)
    assert applied == [number for number, _note, _sql in MIGRATIONS]
    # 再跑一次：不重复应用（版本号已记录），列补丁幂等。
    assert apply(conn) == []
    row = conn.execute("SELECT value FROM meta WHERE key='migration_version'").fetchone()
    assert row[0] == str(len(MIGRATIONS))


def test_column_patches_fill_missing_columns_only():
    conn = fresh_db()
    apply(conn)
    for table, columns in COLUMN_PATCHES.items():
        names = {row[1] for row in conn.execute('PRAGMA table_info(%s)' % table)}
        for name, _ddl in columns:
            assert name in names, (table, name)
    # 已存在的列不会被重复添加（否则 ALTER 会抛错），重复执行不报错。
    apply(conn)


def test_indexes_created_for_hot_paths():
    conn = fresh_db()
    apply(conn)
    indexes = {row[1] for row in conn.execute('PRAGMA index_list(playbacks)')}
    assert {'playbacks_relative', 'playbacks_relative_nocase', 'playbacks_file'} <= indexes
    share_indexes = {row[1] for row in conn.execute('PRAGMA index_list(share_strms)')}
    assert 'share_strms_path_nocase' in share_indexes
