"""Read-only reconciliation of explicit library roots with registered STRMs.

An audit is informative, not an authorization or an atomic filesystem snapshot.
"""
import os
from pathlib import Path, PurePosixPath


def audit_roots(root, roots, rows, limit=200):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError('STRM 根目录不可用')
    if set(roots) != {'cloud', 'share'} or roots['cloud'] == roots['share']:
        raise ValueError('两库根目录必须分别明确指定')
    for name in roots.values():
        if not isinstance(name, str) or name in ('', '.', '..') or '/' in name or '\\' in name:
            raise ValueError('库根目录名称无效')
    expected = {side: set() for side in roots}
    duplicates, outside = [], []
    seen = set()
    for side, record_id, path in rows:
        if side not in roots or not isinstance(path, str):
            outside.append({'side': side, 'id': record_id, 'path': path})
            continue
        p = PurePosixPath(path)
        if p.is_absolute() or len(p.parts) < 2 or p.parts[0] != roots[side] or '..' in p.parts or '\\' in path:
            outside.append({'side': side, 'id': record_id, 'path': path})
            continue
        key = (side, path)
        if key in seen:
            duplicates.append({'side': side, 'id': record_id, 'path': path})
        seen.add(key)
        expected[side].add(path)
    actual = {side: set() for side in roots}
    root_stat = root.stat()
    for side, name in roots.items():
        base = root / name
        if base.is_symlink() or not base.is_dir():
            raise ValueError(f'{side} 库根目录不可用')
        before = base.stat()
        # DirEntry caches the lstat that os.walk's string paths would repeat.
        # Still inspect every directory and reject links before following them.
        pending = [base]
        while pending:
            current = pending.pop()
            with os.scandir(current) as entries:
                for entry in entries:
                    if entry.is_symlink():
                        if entry.is_dir(follow_symlinks=True):
                            raise ValueError(f'{side} 库包含目录链接，无法完整核实')
                        if Path(entry.name).suffix.casefold() == '.strm':
                            raise ValueError(f'{side} 库包含无法核实的 STRM')
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(Path(entry.path))
                    elif Path(entry.name).suffix.casefold() == '.strm':
                        if not entry.is_file(follow_symlinks=False):
                            raise ValueError(f'{side} 库包含无法核实的 STRM')
                        actual[side].add(Path(entry.path).relative_to(root).as_posix())
        after = base.stat()
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise ValueError(f'{side} 库根目录读取期间发生变化')
    end = root.stat()
    if (root_stat.st_dev, root_stat.st_ino) != (end.st_dev, end.st_ino):
        raise ValueError('STRM 根目录读取期间发生变化')
    results = {}
    for side in roots:
        missing = sorted(expected[side] - actual[side])
        unregistered = sorted(actual[side] - expected[side])
        results[side] = {'root': roots[side], 'registered': len(expected[side]), 'on_disk': len(actual[side]),
                         'missing_count': len(missing), 'missing': missing[:limit],
                         'unregistered_count': len(unregistered), 'unregistered': unregistered[:limit]}
    return {'libraries': results, 'outside_count': len(outside), 'outside': outside[:limit],
            'duplicate_count': len(duplicates), 'duplicates': duplicates[:limit],
            'clean': not (outside or duplicates or any(x['missing_count'] or x['unregistered_count'] for x in results.values())),
            'executable': False}
