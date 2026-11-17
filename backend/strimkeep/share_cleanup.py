"""分享登记的轻量维护：待整理计数、播放 token、地址刷新、空目录清理。

这些函数只依赖 store/文件路径，不依赖分享整理状态机。
"""
import hashlib
import hmac
import os
import re
from pathlib import Path

from .fsutil import atomic_write
from .share_urls import share_play_path_matches, share_play_url

PENDING_NAME = '分享STRM待整理'


def pending_count(store):
    return store.db.execute(
        "SELECT count(*) FROM share_strms WHERE revoked=0 AND strm_path LIKE ?", (PENDING_NAME + '/%',)).fetchone()[0]


def validate_play_token(store, row, token):
    import hmac
    import re
    expected = str(row['token_hash'])
    hashed = hashlib.sha256(token.encode()).hexdigest()
    if hmac.compare_digest(expected, hashed):
        return True
    # Earlier packages stored the 24-byte token directly. Migrate only after
    # an exact authenticated legacy token, never accept a hash as a token.
    if re.fullmatch(r'[0-9a-f]{48}', expected) and hmac.compare_digest(expected, token):
        with store.db:
            store.db.execute('UPDATE share_strms SET token_hash=? WHERE id=? AND token_hash=?',
                             (hashed, row['id'], expected))
        return True
    return False


def refresh_share_address(store, row, path, base, display_name=None):
    from urllib.parse import urlsplit,parse_qs
    from .core import atomic_write
    try:
        content=path.read_text(encoding='utf-8');parsed=urlsplit(content.strip())
        token=parse_qs(parsed.query).get('token',[''])[0]
        if not share_play_path_matches(parsed.path,row['id']):return False
        # Caller has a registered pending output. Keep its valid token and only
        # change the global address inherited by this task.
        wanted=share_play_url(base, row['id'], display_name or row['name'])
        if parsed.query:wanted += '?' + parsed.query
        if content != wanted:atomic_write(path,wanted.encode(),path.parent)
        return True
    except (OSError,UnicodeError,ValueError):return False


def prune_empty_pending(root):
    """删掉「分享STRM待整理」下的空目录（自底向上），返回删除数量。

    整理走的路径不止一条（正常发布、未识别转移、洗版丢弃），只在发布成功那一步
    清父目录会漏掉其余路径，现场就留下一堆空目录。
    """
    boundary = Path(root)
    removed = 0
    for parent, dirs, _ in os.walk(boundary, topdown=False, followlinks=False):
        for name in dirs:
            path = Path(parent) / name
            if path.is_symlink():
                continue
            try:
                path.rmdir()
                removed += 1
            except OSError:
                pass
    return removed


