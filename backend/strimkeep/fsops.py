"""本地 STRM 清理的公共规则。

任何入口删掉一个 STRM，都要顺带处理同一目录的附属文件与空目录，否则：
- 目录里只剩 nfo/海报/mediainfo.json，Emby 仍会认为该条目存在；
- 空目录一直堆着，媒体库越来越脏。
"""
from pathlib import Path

from .media import KNOWN_VIDEOS, SUBTITLES, SUBTITLE_TAG

VIDEO_EXT = KNOWN_VIDEOS | {'.strm'}
ARTWORK_EXT = {'.jpg', '.jpeg', '.png', '.webp'}
ARTWORK_NAMES = {'poster', 'fanart', 'thumb', 'backdrop', 'landscape', 'banner',
                 'clearlogo', 'logo', 'disc', 'discart', 'clearart'}
SIDECAR_EXT = ARTWORK_EXT | SUBTITLES | {'.nfo', '.json', '.xml', '.bif'}
LIBRARY_NAMES = ('影视媒体库', '分享影视库')


def _sidecar_stems(path):
    """Filename stems that can own the sidecar under supported naming rules."""
    stem = path.stem
    extension = path.suffix.lower()
    yield stem
    if extension in SUBTITLES:
        while '.' in stem:
            base, _, tag = stem.rpartition('.')
            if not SUBTITLE_TAG.fullmatch(tag):
                break
            yield base
            stem = base
    elif extension in ARTWORK_EXT:
        base, _, role = stem.rpartition('-')
        if role.lower() in ARTWORK_NAMES:
            yield base
    elif extension == '.json' and stem.endswith('-media-info'):
        yield stem.removesuffix('-media-info')


def purge_strm(path, root, *, resume_missing=False):
    """删除一个 STRM，并清理它自己的附属文件；目录无视频时再清整目录侧车并删空目录。

    范围控制（单集/单季/整剧不误伤）：
    - 同名文件、已知字幕标记、图片标记和媒体信息可归属到单个视频；有歧义就保留。
    - 只有当目录里再无任何视频时，才清目录级侧车（tvshow.nfo / poster.jpg 等）并向上删空目录。
    返回 {'file': 0/1, 'sidecars': n, 'dirs': n}。任何一步失败都不抛出。
    """
    path, root = Path(path), Path(root)
    out = {'file': 0, 'sidecars': 0, 'dirs': 0}
    if resume_missing:
        if path.exists() or path.is_symlink():
            return out
    else:
        try:
            path.unlink()
            out['file'] = 1
        except OSError:
            return out

    parent = path.parent
    try:
        entries = list(parent.iterdir())
        videos = {entry.stem.casefold() for entry in entries if entry.suffix.lower() in VIDEO_EXT}
        # Subdirectories and links can contain another season or version.
        directory_has_media = bool(videos) or any(entry.is_symlink() or entry.is_dir() for entry in entries)
        for entry in entries:
            if entry.suffix.lower() not in SIDECAR_EXT or entry.is_symlink() or not entry.is_file():
                continue
            if directory_has_media:
                owners = tuple(_sidecar_stems(entry))
                if path.stem not in owners or any(owner.casefold() in videos for owner in owners):
                    continue
            try:
                entry.unlink()
                out['sidecars'] += 1
            except OSError:
                pass
    except OSError:
        return out

    roots = tuple(root / n for n in LIBRARY_NAMES)
    p = parent
    # 一级目录（库根本身）永远不动：以前靠 LIBRARY_NAMES 白名单，库根改名就失效。
    while p != root and p.parent != root and p not in roots and root in p.parents:
        try:
            if p.is_dir() and not any(p.iterdir()):
                p.rmdir()
                out['dirs'] += 1
            else:
                break
        except OSError:
            break
        p = p.parent
    return out
