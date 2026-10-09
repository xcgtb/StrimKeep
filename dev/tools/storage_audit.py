#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only disk usage audit for StrimKeep's /data volume.

No import of application modules and no filesystem mutations. Symbolic links are
counted but never followed, so the inspection cannot traverse mounted media roots.
"""
import argparse
import json
import os
from collections import defaultdict
from pathlib import Path


def classify(rel: Path) -> str:
    """Put each file into exactly one category; precedence is intentional."""
    name = rel.name.lower()
    parts = {p.lower() for p in rel.parts}
    if 'residue_backup' in parts:
        return 'residue_backup'
    if name.endswith('.migrated'):
        return 'migration_copies'
    if name.endswith(('.db', '.db-wal', '.db-shm', '.db-journal')):
        return 'sqlite'
    if name.startswith('plan_') and name.endswith('.json'):
        return 'plans'
    if name.endswith('.json') and (
        'cache' in name or name in {
            'library_snapshot.json', 'emby_library_with_tmdb.json',
            'tmdb_tv_meta.json', 'ingest_cache.json'
        }
    ):
        return 'rebuildable_caches'
    if name.endswith(('.jsonl', '.log')):
        return 'logs'
    return 'other'


def scan_data_dir(root: Path) -> dict:
    """Read filesystem metadata only; followlinks=False and no file contents read."""
    root = Path(root)
    if not root.is_dir():
        raise ValueError(f'数据目录不存在或不是目录: {root}')
    categories = defaultdict(lambda: {'files': 0, 'bytes': 0})
    directories = defaultdict(lambda: {'files': 0, 'bytes': 0})
    total_size = total_files = symlinks = 0
    errors = []

    def onerror(err):
        errors.append(f'{err.__class__.__name__}: {err.filename}')

    for directory, dirs, files in os.walk(root, topdown=True, followlinks=False, onerror=onerror):
        directory = Path(directory)
        # Don't follow symlinked directories, even if a different os.walk runtime
        # implementation should attempt to include them in dirs.
        real_dirs = []
        for name in dirs:
            sub = directory / name
            if sub.is_symlink():
                symlinks += 1
            else:
                real_dirs.append(name)
        dirs[:] = real_dirs
        for name in files:
            path = directory / name
            try:
                if path.is_symlink():
                    symlinks += 1
                    continue
                stat = path.stat(follow_symlinks=False)
                if not path.is_file():
                    continue
            except OSError as err:
                errors.append(f'{err.__class__.__name__}: {path}')
                continue
            rel = path.relative_to(root)
            size = stat.st_size
            category = categories[classify(rel)]
            category['files'] += 1
            category['bytes'] += size
            first = rel.parts[0] if len(rel.parts) > 1 else '(根目录文件)'
            group = directories[first]
            group['files'] += 1
            group['bytes'] += size
            total_files += 1
            total_size += size
    return {
        'root': str(root),
        'total_bytes': total_size,
        'total_files': total_files,
        'symlinks_skipped': symlinks,
        'categories': dict(sorted(categories.items())),
        'top_directories': dict(sorted(directories.items(), key=lambda t: -t[1]['bytes'])),
        'errors': errors,
        'readonly': True,
    }


def fmt_bytes(n):
    return f'{n / 1048576:,.2f} MiB'


def main(argv=None):
    parser = argparse.ArgumentParser(description='仅检查 StrimKeep /data 的文件占用，不修改任何文件')
    parser.add_argument('data_path', nargs='?', default='/data', help='宿主机 data 路径，容器内默认 /data')
    parser.add_argument('--json', action='store_true', help='输出 JSON 结果')
    args = parser.parse_args(argv)
    try:
        report = scan_data_dir(Path(args.data_path))
    except ValueError as error:
        parser.error(str(error))
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0
    print('StrimKeep 数据空间审计（只读；文件逻辑大小，并非 du 磁盘分配量）')
    print('位置:', report['root'])
    print(f"总计: {fmt_bytes(report['total_bytes'])} / {report['total_files']} 个文件")
    print('按用途分类:')
    labels = {
        'sqlite': 'SQLite 主库、WAL 和 SHM',
        'migration_copies': '迁移副本 .migrated（仅候选，不自动删除）',
        'rebuildable_caches': '可重建缓存（需验证业务依赖）',
        'plans': '计划文件',
        'logs': '日志',
        'residue_backup': '残留备份（可能需要恢复）',
        'other': '其他数据',
    }
    for category, value in sorted(report['categories'].items(), key=lambda t: -t[1]['bytes']):
        print(f"  {labels.get(category, category)}: {fmt_bytes(value['bytes'])} / {value['files']} 个")
    print('一级目录:')
    for directory, value in report['top_directories'].items():
        print(f"  {directory}: {fmt_bytes(value['bytes'])} / {value['files']} 个")
    print(f"跳过符号链接: {report['symlinks_skipped']} 个；读取错误: {len(report['errors'])} 个")
    if report['errors']:
        print('注意: 目录无法完整读取，结果可能偏小')
    print('提示：本工具不执行删除、VACUUM、CHECKPOINT，也不读取任何配置内容。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
