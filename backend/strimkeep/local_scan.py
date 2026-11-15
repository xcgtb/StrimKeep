"""本地 STRM 扫描：同步用的「台账」就是磁盘本身。"""

import hashlib
from pathlib import Path

from .identity import parse_file, parse_url


def scan(root):
    """遍历输出根下的 .strm，返回 {relative_path: identity}。

    只收能被解析成身份的（play115 网盘 / 分享）；解析不出来的当作别人的文件，不碰。
    """
    root = Path(root)
    found = {}
    for path in root.rglob('*.strm'):
        identity = parse_file(path)
        if not identity:
            continue
        try:
            relative = str(path.relative_to(root))
        except ValueError:
            continue
        found[relative] = identity
    return found


def identity_of_file(path):
    """读一份本地 STRM 的身份；顺带给出这份文件自己的摘要用于删除前核对。"""
    path = Path(path)
    try:
        raw = path.read_text(encoding='utf-8')
    except (OSError, UnicodeError):
        return None, None
    identity = parse_url(raw)
    if not identity:
        return None, None
    return identity, hashlib.sha256(raw.encode()).hexdigest()
