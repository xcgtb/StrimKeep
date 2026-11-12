"""Guarded single-file deletion adapter for inventory plans.

The caller holds organizer.publication_gate for the plan and verifies the
affected work and retained evidence immediately before each call. The adapter
also verifies the exact target and its source identity before deletion.

来源可核实性决定删除范围：分享侧只要本地文件（缺登记也删，删不掉登记就跳过
revoked 更新）；网盘侧必须有登记，否则拒绝——不能凭文件名猜 115 上删哪个文件。
"""
import os
from pathlib import Path, PurePosixPath

from .inventory_fs import is_synthetic
from .inventory_snapshot import _fingerprint
from .inventory_sources import source_fingerprints


def cleanup_empty_work(engine, relative):
    """After a confirmed deletion, remove an empty TMDB work directory."""
    from .inventory_orphans import MEDIA_DIR, clean_one, fingerprint, inventory
    from .organize_options import library_root
    try:
        config = engine.store.get('organize_config', {}) or {}
        roots = [library_root(config), '分享影视库']
        parts = PurePosixPath(relative).parts
        if not parts or parts[0] not in roots:
            return
        candidates = [i for i, name in enumerate(parts[:-1]) if MEDIA_DIR.search(name)]
        if len(candidates) != 1:
            return
        work = PurePosixPath(*parts[:candidates[0] + 1]).as_posix()
        # 目录是事实：作品目录里还有任何 STRM 就留着。以前查台账，未登记的
        # 残留文件会被当成「已经空了」而被清掉侧车。
        base = Path(engine.root).joinpath(*PurePosixPath(work).parts)
        if not base.is_dir():
            return
        for _current, _dirs, files in os.walk(base, followlinks=False):
            if any(name.lower().endswith('.strm') for name in files):
                return
        entries = inventory(engine.root, work, roots)
        result = clean_one(engine.root, roots, work, fingerprint(entries), engine.store.root / 'cleanup' / 'orphans')
        if hasattr(engine.store, 'event'):
            engine.store.event('inventory', '作品残留清理：%s；文件 %d，目录 %d%s' %
                               (work, result['files_removed'], result['dirs_removed'],
                                '；部分未完成' if result['errors'] else ''))
    except Exception:
        # Other episodes, unknown files and unreadable paths must be retained.
        return


async def delete_many(engine, evidences):
    """批量删除（仅网盘侧）：把库存证据换成 DB 行后交给批量回收。"""
    if not engine.publication_gate.locked():
        raise RuntimeError('删除需要持有发布锁')
    from .deep_delete import guarded_managed_delete_many
    rows = []
    for evidence in evidences:
        if is_synthetic(evidence.get('id')):
            raise RuntimeError('网盘源文件未登记，不能删除')
        row = engine.store.db.execute(
            'SELECT file_id,playback_id,relative_path FROM organized_files WHERE file_id=?',
            (evidence['id'],)).fetchone()
        if not row or row['relative_path'] != evidence['path']:
            raise RuntimeError('网盘删除目标已变化')
        rows.append(dict(row))
    return await guarded_managed_delete_many(engine, rows)


def _local_share_strm(root, relative):
    parts = PurePosixPath(relative).parts
    if not parts or any(part in ('', '.', '..') for part in parts):
        return None
    local = Path(root).joinpath(*parts)
    if local.is_symlink() or not local.is_file():
        return None
    return local


async def delete_one(engine, evidence, source):
    if not engine.publication_gate.locked():
        raise RuntimeError('删除需要持有发布锁')
    side, record_id, path = evidence['side'], evidence['id'], evidence['path']
    account = engine.index.account({})
    if source_fingerprints(engine.store.db, [(side, record_id, path)], account) != [source]:
        return False
    actual = _fingerprint(engine.root, path)
    if any(actual.get(key) != evidence.get(key) for key in ('sha256', 'size', 'mtime_ns', 'device', 'inode')):
        return False
    if side == 'cloud':
        if is_synthetic(record_id):
            raise RuntimeError('网盘源文件未登记，不能删除')
        from .deep_delete import guarded_managed_delete
        row = engine.store.db.execute('SELECT file_id,playback_id,relative_path FROM organized_files WHERE file_id=?', (record_id,)).fetchone()
        if not row or row['relative_path'] != path:
            return False
        status, message, _ = await guarded_managed_delete(engine, dict(row))
        if status == 'done':
            cleanup_empty_work(engine, path)
            return True
        if message and message.startswith('网盘已回收'):
            raise RuntimeError(message)
        return False
    if side == 'share':
        from .fsops import purge_strm
        if is_synthetic(record_id):
            # 本地文件就是全部事实：没有登记行，删完也不用改台账。
            local = _local_share_strm(engine.root, path)
            if local is None:
                return False
            result = purge_strm(local, engine.root, store=engine.store)
            if result['file'] != 1 or local.exists() or local.is_symlink():
                return False
            cleanup_empty_work(engine, path)
            return True
        from .share_service import _verified_share_strm
        local = _verified_share_strm(engine.root, path, record_id)
        if local is None:
            return False
        result = purge_strm(local, engine.root, store=engine.store)
        if result['file'] != 1 or local.exists() or local.is_symlink():
            return False
        with engine.store.db:
            count = engine.store.db.execute('UPDATE share_strms SET revoked=1 WHERE id=? AND revoked=0 AND strm_path=?',
                                            (record_id, path)).rowcount
        if count == 1:
            cleanup_empty_work(engine, path)
        return count == 1
    return False
