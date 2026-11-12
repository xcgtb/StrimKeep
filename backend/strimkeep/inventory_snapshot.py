"""Capture evidence for an inventory deletion plan.

The snapshot binds a plan to: the库根 identity, every file that may enter the
plan (size/mtime/sha256/inode), its source identity, and the files that exist
but cannot be planned (未登记 / 目录认不出). It is not a lock: everything is
revalidated immediately before each mutation.

台账不参与「库里有什么」的判断——``rows`` 来自目录扫描；台账只在
``source_reader`` 里核对来源身份。
"""
import hashlib
import os
import stat
from pathlib import Path, PurePosixPath

MAX_STRM_BYTES = 64 * 1024


def _identity(path):
    info = path.stat()
    return [info.st_dev, info.st_ino]


def _fingerprint(root, relative):
    parts = PurePosixPath(relative).parts
    path = root.joinpath(*parts)
    # Reject links at every level, not only the final component.
    current = root
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise ValueError('STRM 路径存在链接')
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(path, flags)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_STRM_BYTES:
            raise ValueError('STRM 类型或大小异常')
        digest = hashlib.sha256()
        total = 0
        while True:
            block = os.read(fd, 16384)
            if not block:
                break
            total += len(block)
            if total > MAX_STRM_BYTES:
                raise ValueError('STRM 读取期间超出大小限制')
            digest.update(block)
        after = os.fstat(fd)
        named = path.stat()
        def signature(st):
            return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
        # Windows may report creation time from path.stat and change time from
        # fstat; identity, size and mtime remain comparable across both calls.
        same_named = signature(after)[:4] == signature(named)[:4] if os.name == 'nt' else signature(after) == signature(named)
        if signature(before) != signature(after) or not same_named:
            raise ValueError('STRM 读取期间发生变化')
        return {'sha256': digest.hexdigest(), 'size': after.st_size, 'mtime_ns': after.st_mtime_ns,
                'device': after.st_dev, 'inode': after.st_ino}
    finally:
        os.close(fd)


def capture_snapshot(root, roots, rows, policy, *, source_reader, scope_reader=None, unsupported=()):
    root = Path(root)
    rows = list(rows)
    sources = source_reader()
    expected_sources = {(side, str(record_id), relative) for side, record_id, relative in rows}
    if (not isinstance(sources, list) or len(sources) != len(rows)
            or any(not isinstance(item, dict) or set(item) != {'side', 'id', 'path', 'source_hash'}
                   or not isinstance(item['source_hash'], str) or len(item['source_hash']) != 64
                   or any(c not in '0123456789abcdef' for c in item['source_hash']) for item in sources)
            or {(item['side'], item['id'], item['path']) for item in sources} != expected_sources):
        raise ValueError('来源身份证据不完整')
    skipped = sorted({(str(item.get('side') or ''), str(item.get('path') or ''),
                       str(item.get('reason') or '')) for item in unsupported or ()
                      if isinstance(item, dict) and item.get('side') in ('cloud', 'share')
                      and item.get('path')})
    identity = {'root': _identity(root), **{side: _identity(root / name) for side, name in roots.items()}}
    files = []
    for side, record_id, relative in sorted(rows):
        files.append({'side': side, 'id': str(record_id), 'path': relative,
                      **_fingerprint(root, relative)})
    if scope_reader is None:
        valid_scope = True
    else:
        second = scope_reader()
        valid_scope = second['rows'] == sorted(rows) and second['sources'] == sources
    if (not valid_scope or sources != source_reader()
            or identity != {'root': _identity(root), **{side: _identity(root / name) for side, name in roots.items()}}):
        raise ValueError('目录在建立快照时发生变化')
    return {'version': 2, 'roots': dict(roots), 'identity': identity, 'files': files, 'sources': sources,
            # 目录里存在、但来源核不上（或作品目录认不出）的文件：进不了计划，
            # 但必须记下来——它们若在有计划期间增删，快照就该失效。
            'unsupported': [{'side': side, 'path': path, 'reason': reason}
                            for side, path, reason in skipped],
            'policy': policy}
