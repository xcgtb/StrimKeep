"""Consistent backup and restore for StrimKeep.

Backups are a ZIP with a VACUUM INTO snapshot of state.db, the Fernet master
key, and a manifest of sizes/SHA-256. Restore verifies every byte before
touching the target directory, then runs SQLite integrity_check and checks the
core tables exist. This is the same code path used by the HTTP backup endpoint.
"""
import hashlib
import json
import secrets
import sqlite3
import time
import zipfile
from io import BytesIO
from pathlib import Path

from . import __version__

REQUIRED_TABLES = ('meta', 'settings', 'organize_jobs', 'organize_items')


def create_backup(store):
    """Return a consistent ZIP payload. Call from a worker thread."""
    tmp_dir = store.root / 'config' / '.backup-tmp'
    tmp_dir.mkdir(parents=True, exist_ok=True)
    db_path = tmp_dir / ('state-' + secrets.token_hex(6) + '.db')
    try:
        store.db.execute('VACUUM INTO ?', (str(db_path),))
        files = [('state.db', db_path)]
        key_path = store.root / 'config/master.key'
        if key_path.exists():
            files.append(('master.key', key_path))
        buffer = BytesIO()
        manifest = {'created_at': time.time(), 'version': __version__, 'files': []}
        with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for name, path in files:
                payload = path.read_bytes()
                archive.writestr(name, payload)
                manifest['files'].append({
                    'name': name, 'size': len(payload),
                    'sha256': hashlib.sha256(payload).hexdigest()})
            archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))
    finally:
        db_path.unlink(missing_ok=True)
    return buffer.getvalue()


def verify_manifest(archive):
    """Verify every manifest entry; returns the parsed manifest."""
    try:
        manifest = json.loads(archive.read('manifest.json'))
    except (KeyError, TypeError, ValueError):
        raise ValueError('备份缺少有效的 manifest.json。') from None
    names = set(archive.namelist())
    for entry in manifest.get('files') or []:
        name = entry.get('name')
        if name not in names:
            raise ValueError('备份缺少文件：%s' % name)
        payload = archive.read(name)
        if len(payload) != entry.get('size'):
            raise ValueError('备份文件大小不匹配：%s' % name)
        if hashlib.sha256(payload).hexdigest() != entry.get('sha256'):
            raise ValueError('备份文件校验失败：%s' % name)
    return manifest


def restore_backup(zip_path, target_root, force=False):
    """Restore a backup into target_root/{data,config}.

    Refuses to overwrite an existing state.db unless force=True. Verifies all
    hashes first, then checks SQLite integrity and required tables.
    """
    zip_path = Path(zip_path)
    target_root = Path(target_root)
    data_dir = target_root / 'data'
    config_dir = target_root / 'config'
    state_path = data_dir / 'state.db'
    if state_path.exists() and not force:
        raise FileExistsError('目标已有 state.db；确认覆盖请使用 force=True。')
    with zipfile.ZipFile(zip_path) as archive:
        manifest = verify_manifest(archive)
        data_dir.mkdir(parents=True, exist_ok=True)
        config_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / 'state.db').write_bytes(archive.read('state.db'))
        if 'master.key' in archive.namelist():
            (config_dir / 'master.key').write_bytes(archive.read('master.key'))
    connection = sqlite3.connect(str(state_path))
    try:
        integrity = connection.execute('PRAGMA integrity_check').fetchone()[0]
        if integrity != 'ok':
            raise RuntimeError('恢复后的数据库完整性检查失败：%s' % integrity)
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        missing = [name for name in REQUIRED_TABLES if name not in tables]
        if missing:
            raise RuntimeError('恢复后的数据库缺少表：%s' % ', '.join(missing))
        row = connection.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        schema_version = json.loads(row[0]) if row else None
    finally:
        connection.close()
    return {'manifest': manifest, 'integrity': integrity,
            'schema_version': schema_version, 'target': str(target_root)}


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description='Restore a StrimKeep backup ZIP.')
    parser.add_argument('zip_path')
    parser.add_argument('target_root')
    parser.add_argument('--force', action='store_true')
    args = parser.parse_args(argv)
    result = restore_backup(args.zip_path, args.target_root, force=args.force)
    print('restored:', result['target'])
    print('schema_version:', result['schema_version'])
    print('integrity:', result['integrity'])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
