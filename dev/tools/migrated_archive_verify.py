#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only verification of an archive created from data/state/*.migrated.

Only reads the archive and original files; never extracts, moves, deletes,
changes DB state, or updates any on-disk manifests. No archive member is
trusted as a filesystem destination.
"""
import argparse
import gzip
import hashlib
import json
import os
import tarfile
from pathlib import Path, PurePosixPath


def _digest_stream(stream):
    h = hashlib.sha256()
    while True:
        chunk = stream.read(1024 * 1024)
        if not chunk:
            break
        h.update(chunk)
    return h.hexdigest()


def _archived_name(member_name, prefix):
    parts = member_name.split('/')
    if any(part in ('..', '') for part in parts):
        raise ValueError('含危险路径片段: ' + member_name)
    while parts and parts[0] == '.':
        parts.pop(0)
    if not parts or any(part in ('.', '..') for part in parts):
        raise ValueError('归档路径无效: ' + member_name)
    path = PurePosixPath(*parts)
    root = PurePosixPath(prefix)
    try:
        rel = path.relative_to(root)
    except ValueError as exc:
        raise ValueError('归档成员不属于预期目录: ' + member_name) from exc
    if rel == PurePosixPath('.') or not rel.name.endswith('.migrated'):
        raise ValueError('归档成员不是迁移副本: ' + member_name)
    return rel.as_posix()


def _disk_files(state):
    files = {}
    for directory, dirs, names in os.walk(state, followlinks=False):
        parent = Path(directory)
        dirs[:] = [d for d in dirs if not (parent / d).is_symlink()]
        for name in names:
            if not name.endswith('.migrated'):
                continue
            path = parent / name
            if path.is_symlink():
                raise ValueError('原文件为符号链接，停止校验: ' + str(path))
            if not path.is_file():
                raise ValueError('原文件不是常规文件: ' + str(path))
            files[path.relative_to(state).as_posix()] = path
    return files


def inspect(state, archive, prefix='data/state', expected_count=None):
    state = Path(state)
    archive = Path(archive)
    if not state.is_dir() or state.is_symlink():
        raise ValueError('state 必须是实际目录，不能是符号链接')
    if not archive.is_file() or archive.is_symlink():
        raise ValueError('归档必须是实际文件，不能是符号链接')
    prefix_parts = PurePosixPath(prefix).parts
    if not prefix_parts or any(p in ('.', '..', '/') for p in prefix_parts):
        raise ValueError('不安全的归档目录前缀')
    result = {
        'readonly': True, 'archive': str(archive), 'state': str(state),
        'archive_prefix': prefix, 'archive_members': 0,
        'source_members': 0, 'matched': 0, 'bytes_matched': 0,
        'missing_in_archive': [], 'only_in_archive': [],
        'different': [], 'errors': [], 'gzip_verified': False,
        'archive_sha256': '', 'ok': False,
    }
    disk_files = _disk_files(state)
    result['source_members'] = len(disk_files)
    if expected_count is not None and len(disk_files) != expected_count:
        result['errors'].append('原始迁移文件数量不是预期值 %d: %d' % (expected_count, len(disk_files)))

    # A full gzip stream read confirms the CRC/length footer, including the
    # case of a truncated archive that tarfile might otherwise stop reading.
    try:
        with gzip.open(archive, 'rb') as g:
            while g.read(1024 * 1024):
                pass
        result['gzip_verified'] = True
    except (OSError, EOFError) as exc:
        result['errors'].append('压缩流校验失败: ' + str(exc))
        return result
    with archive.open('rb') as raw:
        result['archive_sha256'] = _digest_stream(raw)

    seen = set()
    try:
        with tarfile.open(archive, mode='r:gz') as tf:
            for member in tf:
                if not member.isfile():
                    result['errors'].append('归档包含非常规文件或链接: ' + member.name)
                    continue
                try:
                    name = _archived_name(member.name, prefix)
                except ValueError as exc:
                    result['errors'].append(str(exc))
                    continue
                if name in seen:
                    result['errors'].append('归档成员重复: ' + name)
                    continue
                seen.add(name)
                result['archive_members'] += 1
                original = disk_files.get(name)
                if original is None:
                    result['only_in_archive'].append(name)
                    continue
                if original.stat().st_size != member.size:
                    result['different'].append(name + ' (字节数不同)')
                    continue
                archived = tf.extractfile(member)
                if archived is None:
                    result['errors'].append('无法读取归档成员: ' + name)
                    continue
                with archived, original.open('rb') as original_stream:
                    archived_digest = _digest_stream(archived)
                    original_digest = _digest_stream(original_stream)
                if archived_digest != original_digest:
                    result['different'].append(name + ' (SHA256 不同)')
                else:
                    result['matched'] += 1
                    result['bytes_matched'] += member.size
    except (tarfile.TarError, OSError, EOFError) as exc:
        result['errors'].append('归档读取失败: ' + str(exc))
    result['missing_in_archive'] = sorted(set(disk_files) - seen)
    result['only_in_archive'].sort()
    if expected_count is not None and result['archive_members'] != expected_count:
        result['errors'].append('归档迁移条目数量不是预期值 %d: %d' % (
            expected_count, result['archive_members']))
    result['ok'] = bool(result['gzip_verified'] and not result['errors']
                        and not result['different'] and not result['missing_in_archive']
                        and not result['only_in_archive']
                        and result['matched'] == result['archive_members'])
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(description='只读校验 *.migrated 归档（不提取、不删除）')
    ap.add_argument('archive', help='迁移副本 .tar.gz 归档的完整路径')
    ap.add_argument('--state', default='data/state', help='原始 state 目录')
    ap.add_argument('--prefix', default='data/state', help='归档内 state 路径前缀')
    ap.add_argument('--expect-count', type=int, default=None)
    ap.add_argument('--json', action='store_true')
    args = ap.parse_args(argv)
    try:
        report = inspect(args.state, args.archive, args.prefix, args.expect_count)
    except (ValueError, OSError) as exc:
        ap.error(str(exc))
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print('迁移副本归档校验（只读）')
        print('归档: %s' % report['archive'])
        print('gzip 完整性: %s' % ('通过' if report['gzip_verified'] else '失败'))
        print('归档条目: %d；当前原文件: %d；逐文件 SHA256 一致: %d' % (
            report['archive_members'], report['source_members'], report['matched']))
        print('一致的源数据: %.2f MiB' % (report['bytes_matched'] / 1048576))
        print('缺少: %d；额外: %d；不同: %d；异常: %d' % (
            len(report['missing_in_archive']), len(report['only_in_archive']),
            len(report['different']), len(report['errors'])))
        for field in ('missing_in_archive', 'only_in_archive', 'different', 'errors'):
            for item in report[field][:10]:
                print('  %s: %s' % (field, item))
        if report['archive_sha256']:
            print('归档 SHA256: %s' % report['archive_sha256'])
        print('结果: %s' % ('校验通过（仍不代表允许删除原文件）' if report['ok'] else '校验失败，禁止清理'))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
