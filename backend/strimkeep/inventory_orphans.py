"""Find and safely remove metadata-only TMDB media directories."""
import hashlib
import json
import os
import re
import stat
import tarfile
import time
import secrets
from pathlib import Path, PurePosixPath

from .fsops import SIDECAR_EXT
from .media import KNOWN_VIDEOS

MEDIA_DIR = re.compile(r'(?i)\{tmdb-[1-9]\d*\}')
MAX_ITEMS = 1000


class ProtectedContent(ValueError):
    def __init__(self, name, ordinary):
        super().__init__('存在媒体、未知文件、软链接或特殊文件：' + name)
        self.ordinary = ordinary


def _sig(st):
    return [st.st_dev, st.st_ino, st.st_mode, st.st_size, st.st_mtime_ns, st.st_ctime_ns]


def _directory(root, relative):
    root = Path(root)
    rel = PurePosixPath(relative)
    if rel.is_absolute() or rel.as_posix() != relative or len(rel.parts) < 2 or any(x in ('', '.', '..') for x in rel.parts) or '\\' in relative:
        raise ValueError('路径无效')
    if not MEDIA_DIR.search(rel.name):
        raise ValueError('仅允许带 TMDB 标识的作品目录')
    current = root
    for part in ('', *rel.parts):
        if part: current = current / part
        st = current.lstat()
        if not stat.S_ISDIR(st.st_mode):
            raise ValueError('目录不存在或包含软链接')
    return root.joinpath(*rel.parts)


def inventory(root, relative, allowed_roots):
    rel = PurePosixPath(relative)
    if not rel.parts or rel.parts[0] not in allowed_roots:
        raise ValueError('目录不属于已配置的媒体库')
    base = _directory(root, relative)
    entries = {}
    def visit(path, depth):
        if depth > 64 or len(entries) > 20000:
            raise ValueError('目录过深或文件过多')
        before = path.lstat()
        if not stat.S_ISDIR(before.st_mode):
            raise ValueError('目录不是普通目录')
        key = path.relative_to(base).as_posix()
        entries[key] = {'kind': 'dir', 'signature': _sig(before)}
        with os.scandir(path) as scan:
            children = sorted(scan, key=lambda entry: entry.name)
        for child in children:
            p = path / child.name
            st = child.stat(follow_symlinks=False)
            if stat.S_ISDIR(st.st_mode):
                visit(p, depth + 1)
            elif stat.S_ISREG(st.st_mode) and p.suffix.lower() in SIDECAR_EXT:
                entries[p.relative_to(base).as_posix()] = {'kind': 'file', 'signature': _sig(st)}
            else:
                raise ProtectedContent(p.name, stat.S_ISREG(st.st_mode) and p.suffix.lower() in (KNOWN_VIDEOS | {'.strm'}))
        if _sig(path.lstat()) != _sig(before):
            raise ValueError('扫描时目录发生变化')
    visit(base, 0)
    return entries


def fingerprint(entries):
    raw = json.dumps(entries, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
    return hashlib.sha256(raw).hexdigest()


SKIP_REASONS = {
    'symlink': '目录是软链接',
    'has_media_or_unknown': '目录里有视频、未知文件或特殊文件',
    'changed': '扫描时目录发生变化',
    'too_large': '目录过深或文件过多',
    'not_plain_directory': '不是普通目录',
    'no_tmdb': '目录名没有 TMDB 标识',
    'unreadable': '读取失败（权限或磁盘错误）',
    'invalid': '路径或配置不合法',
}


def _skip_reason(message):
    """把 inventory() 的异常文本归成可统计的原因码。"""
    for needle, code in (('存在媒体、未知文件', 'has_media_or_unknown'),
                         ('扫描时目录发生变化', 'changed'),
                         ('目录过深或文件过多', 'too_large'),
                         ('目录不是普通目录', 'not_plain_directory'),
                         ('目录不存在或包含软链接', 'not_plain_directory'),
                         ('仅允许带 TMDB 标识', 'no_tmdb')):
        if needle in message:
            return code
    return 'invalid'


def scan(root, allowed_roots):
    root = Path(root)
    if root.is_symlink() or not root.is_dir(): raise ValueError('STRM 根目录不可用')
    if len(set(allowed_roots)) != len(allowed_roots): raise ValueError('库根重复')
    hits, skipped_items = [], []

    def note(relative, side, reason, detail='', attention=True):
        skipped_items.append({'path': relative, 'side': side, 'reason': reason,
                              'detail': str(detail)[:200], 'attention':attention})

    for name in allowed_roots:
        if not name or name in ('.', '..') or '/' in name or '\\' in name:
            raise ValueError('库根配置无效')
        library = root / name
        if library.is_symlink() or not library.is_dir(): raise ValueError('库根不可用：' + name)
        def onerror(error): raise error
        for folder, dirs, files in os.walk(library, topdown=True, followlinks=False, onerror=onerror):
            directory = Path(folder)
            linked = [name for name in dirs if (directory / name).is_symlink()]
            for name in linked:
                note((directory / name).relative_to(root).as_posix(),
                     'share' if name == '分享影视库' else 'cloud', 'symlink')
            dirs[:] = [name for name in dirs if name not in linked]
            if directory == library or not MEDIA_DIR.search(directory.name): continue
            relative = directory.relative_to(root).as_posix()
            side = 'share' if name == '分享影视库' else 'cloud'
            try:
                entries = inventory(root, relative, allowed_roots)
            except OSError as exc:
                note(relative, side, 'unreadable', exc)
                continue
            except ValueError as exc:
                note(relative, side, _skip_reason(str(exc)), exc,
                     not (isinstance(exc, ProtectedContent) and exc.ordinary))
                continue
            hits.append({'path': relative, 'side': 'share' if name == '分享影视库' else 'cloud',
                         'files': sum(x['kind'] == 'file' for x in entries.values()),
                         'dirs': sum(x['kind'] == 'dir' for x in entries.values()),
                         'fingerprint': fingerprint(entries)})
            if len(hits) > MAX_ITEMS: raise ValueError('候选超过 1000 项，请缩小扫描范围')
            dirs[:] = []
    reasons = {}
    for item in skipped_items:
        reasons[item['reason']] = reasons.get(item['reason'], 0) + 1
    attention_items = [item for item in skipped_items if item['attention']]
    return {'items': hits, 'skipped': len(skipped_items),
            'skipped_items': skipped_items[:500], 'reasons': reasons,
            'skipped_truncated': max(0, len(skipped_items) - 500),
            'attention_count': len(attention_items), 'attention_items': attention_items[:200]}


def _open_dir(root, relative):
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(root, flags)
    try:
        for part in PurePosixPath(relative).parts:
            if part in ('', '.'): continue
            nxt = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = nxt
        return fd
    except BaseException:
        os.close(fd)
        raise


def clean_one(root, allowed_roots, relative, expected, backup_root, backup=False):
    """Back up exact candidate then remove unchanged entries without following links."""
    root = Path(root)
    entries = inventory(root, relative, allowed_roots)
    if fingerprint(entries) != expected: raise ValueError('目录内容已变化，请重新扫描')
    if not backup:
        # 不备份直接清理：仍然复核一次指纹，避免拿过期快照删除。
        if fingerprint(inventory(root, relative, allowed_roots)) != expected:
            raise ValueError('目录内容已变化，请重新扫描')
        return _remove_entries(root, allowed_roots, relative, entries, '')
    backup_root = Path(backup_root)
    backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    archive = backup_root / (time.strftime('%Y%m%d-%H%M%S') + '-' + secrets.token_hex(8) + '.tar')
    try:
        with tarfile.open(archive, 'x') as tar:
            for rel, record in sorted(entries.items()):
                if record['kind'] != 'file': continue
                f = PurePosixPath(relative) / rel
                dfd = _open_dir(root, f.parent.as_posix())
                try:
                    fd = os.open(f.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dfd)
                    with os.fdopen(fd, 'rb') as file:
                        if _sig(os.fstat(file.fileno())) != record['signature']: raise ValueError('文件发生变化')
                        info = tarfile.TarInfo(f.as_posix()); info.size = record['signature'][3]
                        info.mode = record['signature'][2] & 0o777; info.mtime = record['signature'][4] / 1e9
                        tar.addfile(info, file)
                        if _sig(os.fstat(file.fileno())) != record['signature']: raise ValueError('备份时文件发生变化')
                finally: os.close(dfd)
        os.chmod(archive, 0o600)
        with open(archive, 'rb') as f: os.fsync(f.fileno())
        if fingerprint(inventory(root, relative, allowed_roots)) != expected:
            raise ValueError('备份后目录发生变化，请重新扫描')
    except BaseException:
        if archive.exists(): archive.unlink()
        raise
    return _remove_entries(root, allowed_roots, relative, entries, str(archive))


def _remove_entries(root, allowed_roots, relative, entries, archive):
    """按快照逐项复核后删除文件与空目录；archive 为空表示本次未备份。"""
    removed_files, removed_dirs, errors = 0, 0, []
    for rel, record in sorted(entries.items()):
        if record['kind'] != 'file': continue
        f = PurePosixPath(relative) / rel
        try:
            # Recheck for newly arrived media or unknown content before every
            # unlink; another process may write outside our publication lock.
            inventory(root, relative, allowed_roots)
            dfd = _open_dir(root, f.parent.as_posix())
            try:
                if _sig(os.stat(f.name, dir_fd=dfd, follow_symlinks=False)) != record['signature']:
                    raise ValueError('文件已变化')
                os.unlink(f.name, dir_fd=dfd); removed_files += 1
            finally: os.close(dfd)
        except (OSError, ValueError) as exc: errors.append(f'{f}: {exc}')
    directories = sorted((rel for rel, record in entries.items() if record['kind'] == 'dir'),
                         key=lambda x: len(PurePosixPath(x).parts), reverse=True)
    for rel in directories:
        d = PurePosixPath(relative) if rel == '.' else PurePosixPath(relative) / rel
        try:
            dfd = _open_dir(root, d.parent.as_posix())
            try:
                st = os.stat(d.name, dir_fd=dfd, follow_symlinks=False)
                if _sig(st)[:3] != entries[rel]['signature'][:3]: raise ValueError('目录身份已变化')
                os.rmdir(d.name, dir_fd=dfd); removed_dirs += 1
            finally: os.close(dfd)
        except (OSError, ValueError) as exc: errors.append(f'{d}: {exc}')
    return {'path': relative, 'files_removed': removed_files, 'dirs_removed': removed_dirs,
            'backup': archive, 'errors': errors}


def sidecars_for_deleted(root, allowed_roots, source):
    """Return exact-name sidecars of a recently deleted registered STRM."""
    source_path = PurePosixPath(source)
    if (source_path.is_absolute() or source_path.as_posix() != source or len(source_path.parts) < 3
            or source_path.parts[0] not in allowed_roots or source_path.suffix.lower() != '.strm'
            or any(x in ('', '.', '..') for x in source_path.parts) or '\\' in source):
        raise ValueError('原 STRM 路径无效')
    if not any(MEDIA_DIR.search(p) for p in source_path.parts[1:-1]):
        raise ValueError('原 STRM 不属于带 TMDB 标识的作品')
    dfd = _open_dir(root, source_path.parent.as_posix())
    try:
        try:
            os.stat(source_path.name, dir_fd=dfd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise ValueError('原 STRM 已存在，不能按残留处理')
        parent_stat = _sig(os.fstat(dfd))
        names = os.listdir(dfd)
        videos = [n for n in names if Path(n).suffix.lower() in {'.strm', '.mkv', '.mp4', '.m4v', '.mov', '.avi', '.ts', '.m2ts', '.webm', '.wmv', '.flv', '.rmvb', '.iso'}]
        items = []
        for name in names:
            if not (name.startswith(source_path.stem + '.') or name.startswith(source_path.stem + '-')) or Path(name).suffix.lower() not in SIDECAR_EXT:
                continue
            # A surviving video's full stem may own this file instead.
            if any(name.startswith(Path(v).stem + '.') or name.startswith(Path(v).stem + '-') for v in videos):
                continue
            st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
            if not stat.S_ISREG(st.st_mode): continue
            path = (source_path.parent / name).as_posix()
            evidence = {'source':source, 'path':path, 'parent':parent_stat, 'file':_sig(st)}
            items.append({'path':path, 'source':source, 'kind':'file',
                          'side':'share' if source_path.parts[0] == '分享影视库' else 'cloud',
                          'files':1, 'dirs':0, 'fingerprint':fingerprint(evidence)})
        if _sig(os.fstat(dfd)) != parent_stat: raise ValueError('扫描时目录发生变化')
        return items
    finally:
        os.close(dfd)


def clean_sidecar(root, allowed_roots, source, path, expected, backup_root, backup=False):
    """清理已删除 STRM 的同名残留侧车；``backup`` 为假时只复核后直接删除。"""
    matches = [x for x in sidecars_for_deleted(root, allowed_roots, source)
               if x['path'] == path and x['fingerprint'] == expected]
    if len(matches) != 1:
        raise ValueError('文件或同目录视频已变化，请重新扫描')
    rel = PurePosixPath(path)
    dfd = _open_dir(root, rel.parent.as_posix())
    archive = ''
    try:
        if backup:
            backup_dir = Path(backup_root)
            backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            archive = backup_dir / (time.strftime('%Y%m%d-%H%M%S') + '-'
                                    + secrets.token_hex(8) + '.tar')
        fd = os.open(rel.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dfd)
        with os.fdopen(fd, 'rb') as file:
            before = _sig(os.fstat(file.fileno()))
            if backup:
                with tarfile.open(archive, 'x') as tar:
                    info = tarfile.TarInfo(path)
                    info.size = before[3]
                    info.mode = before[2] & 0o777
                    info.mtime = before[4] / 1e9
                    tar.addfile(info, file)
                if _sig(os.fstat(file.fileno())) != before:
                    raise ValueError('备份时文件发生变化')
        if backup:
            os.chmod(archive, 0o600)
            with open(archive, 'rb') as handle:
                os.fsync(handle.fileno())
        if not any(x['path'] == path and x['fingerprint'] == expected
                   for x in sidecars_for_deleted(root, allowed_roots, source)):
            raise ValueError('目录变化，请重新扫描')
        if _sig(os.stat(rel.name, dir_fd=dfd, follow_symlinks=False)) != before:
            raise ValueError('文件已变化')
        os.unlink(rel.name, dir_fd=dfd)
        return {'path': path, 'files_removed': 1, 'dirs_removed': 0,
                'backup': str(archive), 'errors': []}
    except BaseException:
        if archive and Path(archive).exists():
            Path(archive).unlink()
        raise
    finally:
        os.close(dfd)
