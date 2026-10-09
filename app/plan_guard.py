# -*- coding: utf-8 -*-
"""Bounded STRM fingerprints and validation; no media mutations or full scans."""
import hashlib
import os
import stat
from pathlib import Path

VERSION = 1
MAX_STRM_BYTES = 65536


def directory_strms(raw):
    """Read STRM membership in one known directory, never recurse."""
    try:
        path = Path(raw)
        if path.is_symlink() or not path.is_dir():
            return None
        return sorted(p.name for p in path.iterdir() if p.suffix.lower() == '.strm')
    except OSError:
        return None


def fingerprint(raw, roots):
    """Return a regular STRM's identity, or None when it cannot be verified."""
    try:
        path = Path(raw)
        if not path.is_absolute() or '..' in path.parts or path.suffix.lower() != '.strm':
            return None
        root = next((Path(r) for r in roots if path.is_relative_to(Path(r))), None)
        if root is None:
            return None
        # Reject links in every component, including the file and configured root.
        cur = path
        while True:
            if cur.is_symlink():
                return None
            if cur == root:
                break
            cur = cur.parent
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
        before = path.stat()
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_STRM_BYTES:
            return None
        with path.open('rb') as stream:
            opened = os.fstat(stream.fileno())
            data = stream.read(MAX_STRM_BYTES + 1)
            after = os.fstat(stream.fileno())
        def identity(st):
            return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
        if len(data) > MAX_STRM_BYTES or identity(before) != identity(opened) or identity(opened) != identity(after):
            return None
        if identity(path.stat()) != identity(after):
            return None
        return {'root': str(root), 'identity': list(identity(after)),
                'sha256': hashlib.sha256(data).hexdigest()}
    except (OSError, ValueError, TypeError, RuntimeError):
        return None


def validate(action, snapshots, roots):
    """Return a skip reason; an empty string means all related files match."""
    guard = action.get('file_guard') or {}
    if guard.get('version') != VERSION:
        return '计划缺少文件校验快照，请重新扫描'
    targets = guard.get('targets')
    retained = guard.get('retained')
    if not isinstance(targets, list) or not isinstance(retained, list) or not targets:
        return '计划文件校验快照不完整，请重新扫描'
    if set(targets) != set(action.get('files') or []):
        return '待删文件与确认快照不一致，请重新扫描'
    for directory, old_names in (guard.get('directories') or {}).items():
        if old_names is None or directory_strms(directory) != old_names:
            return '缺集判定目录已变化或不可读，跳过并请重新扫描'
    for label, paths in (('保留依据', retained), ('待删文件', targets)):
        for path in paths:
            old = snapshots.get(path)
            if not isinstance(old, dict) or fingerprint(path, roots) != old:
                return f'{label}已变化、不可读或不存在，跳过并请重新扫描: {Path(path).name}'
    return ''
