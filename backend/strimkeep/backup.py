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


def write_backup(store, destination):
    """Write a consistent ZIP without loading the SQLite snapshot into memory."""
    tmp_dir = store.root / 'config' / '.backup-tmp'
    tmp_dir.mkdir(parents=True, exist_ok=True)
    db_path = tmp_dir / ('state-' + secrets.token_hex(6) + '.db')
    try:
        store.db.execute('VACUUM INTO ?', (str(db_path),))
        files = [('state.db', db_path)]
        key_path = store.root / 'config/master.key'
        if key_path.exists():
            files.append(('master.key', key_path))
        manifest = {'created_at': time.time(), 'version': __version__, 'files': []}
        with zipfile.ZipFile(destination, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for name, path in files:
                digest = hashlib.sha256()
                size = 0
                with path.open('rb') as source, archive.open(name, 'w', force_zip64=True) as output:
                    for chunk in iter(lambda: source.read(1024 * 1024), b''):
                        output.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
                manifest['files'].append({
                    'name': name, 'size': size, 'sha256': digest.hexdigest()})
            archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))
    finally:
        db_path.unlink(missing_ok=True)


def create_backup(store):
    """In-memory backup for callers that need a bytes payload."""
    buffer = BytesIO()
    write_backup(store, buffer)
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
        if archive.getinfo(name).file_size != entry.get('size'):
            raise ValueError('备份文件大小不匹配：%s' % name)
        with archive.open(name) as source:
            digest = hashlib.file_digest(source, 'sha256').hexdigest()
        if digest != entry.get('sha256'):
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
        import shutil
        with archive.open('state.db') as source, state_path.open('wb') as output:
            shutil.copyfileobj(source, output)
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


def verify_backup(zip_path, temporary_directory):
    from tempfile import TemporaryDirectory
    from cryptography.fernet import Fernet
    with TemporaryDirectory(prefix='strimkeep-verify-', dir=temporary_directory) as target:
        result = restore_backup(zip_path, target)
        verified = {entry['name'] for entry in result['manifest']['files']}
        if not {'state.db', 'master.key'} <= verified:
            raise ValueError('备份清单未覆盖数据库或加密密钥。')
        Fernet((Path(target) / 'config/master.key').read_bytes())


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
