"""账本体检与孤儿清单：用文件自带的身份反向核对登记表。只读、只报，不改数据。

STRM 的身份本来就写在文件内容里：

    /play115/<pickcode>/<file_id>/<sha1>/<size>/<name>    网盘库
    /play115/share/<share_id>/<name>                      分享库

所以 playbacks / share_strms / output_registry / organized_files 本质是**索引**。
索引和文件不同步时系统不会报错，只会静默地少做事（0.9.1~0.9.47 那 70 条媒体信息
空窗就是这么来的：本地有值就不再追 Emby）。这里把偏差算成三个数字交给 /metrics：

- stale_active   登记为有效（playbacks.revoked=0 / share_strms.revoked=0 /
                 output_registry.active=1）但本地文件已经不在的行；
- unregistered   库根下存在、却没有任何登记行认领的 STRM；
- orphan_sidecar 只剩侧车（nfo/字幕/media-info.json 等）、没有对应 STRM 或视频的孤儿。

`audit()` 只统计，`orphans()` 额外给出清单供清理脚本使用（真正删除在
`scripts/purge_orphan_sidecars.py`，默认 dry-run）。两者共用同一套判定，
避免"统计说 128、清理删了 130"这种两套规则。
"""
import time
from pathlib import Path

# 体检间隔：6 小时一次，误差无所谓，只要"别静默"。
INTERVAL = 6 * 3600
# 只统计这两个库根；「分享STRM待整理 / 分享STRM未识别」本来就没有登记行。
# 现场库名是「媒体库」「分享影视库」，历史名「影视媒体库」也一并算上，改名不会漏统计。
LIBRARY_DIRS = ('影视媒体库', '媒体库', '分享影视库')
# 目录级侧车：它们属于整部剧，不按集算孤儿。
DIR_SIDECARS = {'tvshow.nfo', 'season.nfo', 'season-all.nfo', 'poster.jpg', 'fanart.jpg',
                'banner.jpg', 'thumb.jpg', 'logo.png', 'clearlogo.png', 'landscape.jpg',
                'backdrop.jpg', 'folder.jpg'}
VIDEO_SUFFIX = {'.strm', '.mkv', '.mp4', '.m4v', '.mov', '.avi', '.ts', '.m2ts',
                '.webm', '.wmv', '.flv', '.rmvb', '.iso'}
SIDECAR_SUFFIX = {'.nfo', '.json', '.xml', '.bif', '.jpg', '.jpeg', '.png', '.webp',
                  '.srt', '.ass', '.ssa', '.vtt', '.sub', '.idx', '.sup'}
# 本项目会随片落盘的文本/图形字幕；图片与 xml/bif 判不出归属，保守不动。
OWNED_SUBTITLE = {'.srt', '.ass', '.ssa', '.vtt', '.sub', '.sup', '.idx'}
# 孤儿清理的保护窗口：太新的文件可能正被在跑的整理任务使用。
ORPHAN_MIN_AGE = 7 * 86400


def _paths(store, sql):
    try:
        return {row[0] for row in store.db.execute(sql) if row[0]}
    except Exception:
        # 表还没建出来（升级瞬间）不应该让体检变成故障。
        return set()


def ledger_paths(store):
    """四张「当前有效」的登记表，按表分开，便于分别统计。"""
    return {
        'playbacks': _paths(store, 'SELECT relative_path FROM playbacks WHERE revoked=0'),
        'share_strms': _paths(store, 'SELECT strm_path FROM share_strms WHERE revoked=0'),
        'organized_files': _paths(store, 'SELECT relative_path FROM organized_files'),
        'output_registry': _paths(store,
                                  'SELECT relative_path FROM output_registry WHERE active=1'),
    }


def _scan(root):
    """一次遍历：返回 (entries, video_stems)。entries 是 [(rel, path, in_library)]。"""
    root = Path(root)
    entries = []
    video_stems = {}
    for path in root.rglob('*'):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        parts = Path(rel).parts
        in_library = bool(parts) and parts[0] in LIBRARY_DIRS
        entries.append((rel, path, in_library))
        if in_library and path.suffix.lower() in VIDEO_SUFFIX:
            video_stems.setdefault(str(Path(rel).parent), set()).add(path.stem)
    return entries, video_stems


def orphan_stem(path, video_stems, rel):
    """是孤儿侧车就返回它的"主名"，否则 None。"""
    if path.suffix.lower() not in SIDECAR_SUFFIX or path.name.lower() in DIR_SIDECARS:
        return None
    stem = path.stem
    if stem.endswith('-media-info'):
        stem = stem[:-len('-media-info')]
    siblings = video_stems.get(str(Path(rel).parent)) or set()
    # 字幕常见 name.zh.srt / name.chs&eng.ass：前缀匹配也算有主。
    if any(stem == other or stem.startswith(other + '.') or stem.startswith(other + '-')
           for other in siblings):
        return None
    return stem


def audit(store, root):
    """返回 {'at','strm','stale_active','unregistered','orphan_sidecar'}；只读。"""
    entries, video_stems = _scan(root)
    on_disk = {rel for rel, _, _ in entries}
    ledger = ledger_paths(store)
    stale = sum(1 for name in ('playbacks', 'share_strms', 'output_registry')
                for rel in ledger[name] if rel not in on_disk)
    claimed = set()
    for rels in ledger.values():
        claimed |= rels
    strm_total = unregistered = orphan = 0
    for rel, path, in_library in entries:
        if path.suffix.lower() == '.strm':
            strm_total += 1
            if in_library and rel not in claimed:
                unregistered += 1
        elif in_library and orphan_stem(path, video_stems, rel) is not None:
            orphan += 1
    return {'at': time.time(), 'strm': strm_total, 'stale_active': stale,
            'unregistered': unregistered, 'orphan_sidecar': orphan}


def orphans(store, root, min_age=ORPHAN_MIN_AGE, now=None):
    """列出孤儿侧车清单，供清理脚本使用；本函数不删任何东西。

    每项 {'relative','stem','suffix','age_days','owned','deletable'}：
    - owned     是不是本项目生成的（nfo/json 看内部字段，字幕按目录归属）；
    - deletable owned 且超过保护窗口。判不出归属的一律 False。
    """
    now = time.time() if now is None else now
    entries, video_stems = _scan(root)
    found = []
    for rel, path, in_library in entries:
        if not in_library:
            continue
        stem = orphan_stem(path, video_stems, rel)
        if stem is None:
            continue
        suffix = path.suffix.lower()
        try:
            age = max(0.0, now - path.stat().st_mtime)
        except OSError:
            continue
        if suffix in ('.nfo', '.json'):
            try:
                text = path.read_text(encoding='utf-8', errors='replace')[:4096]
            except OSError:
                text = ''
            # 本项目写的 nfo 带 <uniqueid type="tmdb">（剧集/电影一致），
            # sidecar json 带 "version" + playback_id；只认这些自家标记，
            # 别人的 nfo 一律判"归属不明"并保留。
            owned = any(marker in text for marker in
                        ('<uniqueid', '<tmdbid>', 'playback_id', '"version"'))
        elif suffix in OWNED_SUBTITLE:
            owned = True
        else:
            owned = False          # 海报/缩略图/xml 等判不出归属，保守不动
        found.append({'relative': rel, 'stem': stem, 'suffix': suffix,
                      'age_days': round(age / 86400, 1), 'owned': owned,
                      'deletable': bool(owned and age >= min_age)})
    return found
