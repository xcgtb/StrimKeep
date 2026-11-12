"""本地文件写入的唯一实现：原子替换、仅创建、以及不支持硬链接时的兜底。

以前多处各写一套：有的用固定临时名（并发互踩、断电留半截），有的直接
`write_bytes`（写一半崩溃就永久留一张坏图），有的只用 `os.link` 做「仅创建」
（SMB/NFS 返回 EPERM 就整批失败）。这里收成两个函数，调用方只管选语义。
"""
import os
import secrets
from pathlib import Path

CHUNK = 1024 * 1024


class UnsafePath(ValueError):
    """目标路径越界或经过符号链接：拒绝写入。"""


def _checked(path, root):
    path, root = Path(path), Path(root).resolve()
    if not path.resolve().is_relative_to(root) or path.is_symlink():
        raise UnsafePath('本地目标路径越界或为符号链接，已停止写入。')
    return path


def atomic_write(path, data, root):
    """随机临时名 + fsync + 原子替换 + 目录 fsync。

    覆盖写用它：要么是旧内容，要么是新内容，不会出现半截文件。
    """
    path = _checked(path, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.' + path.name + '.' + secrets.token_hex(6) + '.tmp')
    try:
        with temporary.open('xb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            descriptor = os.open(path.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        except OSError:
            return path
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def write_once(path, data, root):
    """仅在目标不存在时创建；已存在返回 False。

    先用硬链接实现「原子仅创建」，遇到不支持的挂载（CIFS/NFS 常见 EPERM、
    EXDEV、ENOTSUP）退回 O_EXCL 直写，而不是让整批附属文件失败。
    """
    path = _checked(path, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.' + path.name + '.' + secrets.token_hex(6) + '.tmp')
    try:
        temporary.write_bytes(data)
        try:
            os.link(temporary, path)
            return True
        except FileExistsError:
            return False
        except OSError:
            pass
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            return False
        try:
            with os.fdopen(descriptor, 'wb') as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            path.unlink(missing_ok=True)
            raise
        return True
    finally:
        temporary.unlink(missing_ok=True)
