"""本地 STRM 清理的公共规则。

任何入口删掉一个 STRM，都要顺带处理同一目录的附属文件与空目录，否则：
- 目录里只剩 nfo/海报/mediainfo.json，Emby 仍会认为该条目存在；
- 空目录一直堆着，媒体库越来越脏。
"""
from pathlib import Path

VIDEO_EXT = {'.mkv', '.mp4', '.m4v', '.mov', '.avi', '.ts', '.m2ts', '.webm',
             '.wmv', '.flv', '.rmvb', '.iso', '.strm'}
SIDECAR_EXT = {'.nfo', '.json', '.xml', '.bif', '.jpg', '.jpeg', '.png', '.webp',
               '.srt', '.ass', '.ssa', '.vtt', '.sub', '.idx', '.sup'}
LIBRARY_NAMES = ('影视媒体库', '分享影视库')


def purge_strm(path, root, store=None):
    """删除一个 STRM，并清理它自己的附属文件；目录无视频时再清整目录侧车并删空目录。

    范围控制（单集/单季/整剧不误伤）：
    - 先删本条目同名的 <stem>.*（nfo/srt/ass…）与 <stem>-*.json（-media-info.json）；
    - 只有当目录里再无任何视频时，才清目录级侧车（tvshow.nfo / poster.jpg 等）并向上删空目录。
    返回 {'file': 0/1, 'sidecars': n, 'dirs': n}。任何一步失败都不抛出。
    """
    path, root = Path(path), Path(root)
    out = {'file': 0, 'sidecars': 0, 'dirs': 0}
    # No local STRM mutation without a durable protection record.
    if store is None:
        raise RuntimeError('缺少内部删除登记，已保留本地文件。')
    from .internal_delete import remember
    if not remember(store, [Path(path).relative_to(Path(root)).as_posix()]):
        raise RuntimeError('内部删除登记失败，已保留本地文件。')
    try:
        path.unlink()
        out['file'] = 1
    except FileNotFoundError:
        return out
    except OSError:
        return out

    parent = path.parent
    stem = path.stem

    def _is_sidecar(name):
        return Path(name).suffix.lower() in SIDECAR_EXT

    # 1) 本条目专属侧车：同名 <stem>.*（nfo / srt / ass…）与 <stem>-*.json（-media-info.json）
    try:
        for e in list(parent.iterdir()):
            if not e.is_file() or e.name == path.name:
                continue
            if (e.name.startswith(stem + '.') or e.name.startswith(stem + '-')) and _is_sidecar(e.name):
                try:
                    e.unlink()
                    out['sidecars'] += 1
                except OSError:
                    pass
    except OSError:
        return out

    # 2) 目录已无任何视频：整目录侧车（tvshow.nfo / poster / fanart…）一起清
    try:
        entries = list(parent.iterdir())
        # Never infer a directory is media-free from its immediate files if it
        # has subdirectories or links: another season or version may be below.
        still_video = any(e.is_symlink() or e.is_dir() or (e.is_file() and e.suffix.lower() in VIDEO_EXT) for e in entries)
        if not still_video:
            for e in entries:
                if e.is_file() and _is_sidecar(e.name):
                    try:
                        e.unlink()
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
