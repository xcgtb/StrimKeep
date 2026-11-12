"""Conservative, reversible cleanup of missing, already-revoked ledger rows."""
import json
import os
import secrets
from pathlib import Path, PurePosixPath


def _absent(root, relative, library):
    parts = PurePosixPath(relative or '').parts
    if len(parts) < 2 or parts[0] != library or '..' in parts or '\\' in relative:
        return False
    base = Path(root)
    if base.is_symlink() or not base.is_dir():
        raise ValueError('STRM 根目录不可用')
    library_path = base / library
    if library_path.is_symlink() or not library_path.is_dir():
        raise ValueError('网盘库根目录不可用')
    current = base
    for part in parts:
        current = current / part
        try:
            os.lstat(current)
        except FileNotFoundError:
            return True
        if current.is_symlink():
            return False
    return False


def candidates(db, root, library):
    if not isinstance(library, str) or library in ('', '.', '..') or '/' in library or '\\' in library:
        raise ValueError('网盘库根目录名称无效')
    rows = db.execute('''SELECT o.* FROM organized_files o JOIN playbacks p ON p.id=o.playback_id AND p.file_id=o.file_id
        WHERE p.revoked=1 AND NOT EXISTS(SELECT 1 FROM output_registry r WHERE r.file_id=o.file_id)
        AND NOT EXISTS(SELECT 1 FROM playbacks active WHERE active.file_id=o.file_id AND active.revoked=0)
        AND NOT EXISTS(SELECT 1 FROM owned_outputs w WHERE w.file_id=o.file_id OR w.relative_path=o.relative_path)
        ORDER BY o.file_id''').fetchall()
    return [dict(row) for row in rows if _absent(root, row['relative_path'], library)]


def clear(db, root, library, ids):
    if not ids or len(ids) > 100 or len(set(ids)) != len(ids) or any(not isinstance(x, str) or not x for x in ids):
        raise ValueError('请选择 1 至 100 条不重复的记录')
    if db.in_transaction:
        raise ValueError('数据库正在处理其他事务')
    db.execute('BEGIN IMMEDIATE')
    try:
        root_identity = (Path(root).stat().st_dev, Path(root).stat().st_ino)
        lib_identity = ((Path(root) / library).stat().st_dev, (Path(root) / library).stat().st_ino)
        found = {str(row['file_id']): row for row in candidates(db, root, library)}
        if any(fid not in found for fid in ids):
            raise ValueError('记录状态已变化，请重新核对')
        selected = [found[fid] for fid in ids]
        backup_id = secrets.token_hex(16)
        db.execute('INSERT INTO meta(key,value) VALUES(?,?)',
                   ('inventory-stale-backup:' + backup_id, json.dumps(selected, ensure_ascii=False)))
        for row in selected:
            if not _absent(root, row['relative_path'], library):
                raise ValueError('文件已出现，请重新核对')
            result = db.execute('DELETE FROM organized_files WHERE file_id=? AND playback_id=? AND relative_path=?',
                                (row['file_id'], row['playback_id'], row['relative_path']))
            if result.rowcount != 1:
                raise ValueError('记录在清理时发生变化')
        if (root_identity != (Path(root).stat().st_dev, Path(root).stat().st_ino)
                or lib_identity != ((Path(root) / library).stat().st_dev, (Path(root) / library).stat().st_ino)):
            raise ValueError('库根目录发生变化')
        db.commit()
        return {'removed': len(selected), 'backup_id': backup_id}
    except BaseException:
        db.rollback()
        raise
