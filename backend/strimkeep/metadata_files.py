"""给 STRM 产出配一份本地 NFO 与海报，让 Emby 不用再联网刮削。

只写本地附属文件，不碰 115 上的任何东西：
- 电影：`片名.nfo` + 同目录 `poster.jpg` / `fanart.jpg`
- 剧集：每集 `SxxExx.nfo`，作品目录放 `tvshow.nfo` 与海报
- 图片来自 TMDB（就是页面卡片用的那两张），下载失败就跳过，不影响主流程
"""
import os
import re
from pathlib import Path
from xml.sax.saxutils import escape

IMAGE_BASE = 'https://image.tmdb.org/t/p/'
POSTER_SIZE, BACKDROP_SIZE = 'w500', 'w780'
IMAGE_PATH = re.compile(r'^/[A-Za-z0-9_-]+\.(?:jpg|png)$')
MAX_IMAGE = 8 * 1024 * 1024


def _tag(name, value):
    text = '' if value is None else str(value).strip()
    return f'  <{name}>{escape(text)}</{name}>' if text else ''


def _unique(details, kind='tmdb'):
    ident = details.get('id')
    return f'  <uniqueid type="{kind}" default="true">{ident}</uniqueid>' if ident else ''


def _genres(details):
    return [_tag('genre', item.get('name')) for item in details.get('genres') or [] if item.get('name')]


def build_nfo(plan):
    """电影写 movie，剧集写 episodedetails；拿不到 TMDB 详情时返回空串。"""
    hint = plan.get('hint') or {}
    details = plan.get('details') or {}
    if not details or not details.get('id'):
        return ''
    episodes = plan.get('episodes') or []
    overview = details.get('overview') or ''
    if hint.get('media_type') == 'tv':
        episode = episodes[0] if episodes else {}
        lines = ['<?xml version="1.0" encoding="utf-8" standalone="yes"?>', '<episodedetails>',
                 _tag('title', episode.get('name')), _tag('showtitle', details.get('name') or details.get('title')),
                 _tag('season', hint.get('season')), _tag('episode', hint.get('episode')),
                 _tag('plot', episode.get('overview') or overview),
                 _tag('aired', episode.get('air_date')),
                 _unique(details), '</episodedetails>']
    else:
        lines = ['<?xml version="1.0" encoding="utf-8" standalone="yes"?>', '<movie>',
                 _tag('title', details.get('title') or details.get('name')),
                 _tag('originaltitle', details.get('original_title') or details.get('original_name')),
                 _tag('year', (details.get('release_date') or '')[:4]),
                 _tag('plot', overview), _unique(details), *_genres(details),
                 _tag('premiered', details.get('release_date')), _tag('rating', details.get('vote_average')),
                 '</movie>']
    return '\n'.join(line for line in lines if line) + '\n'


def build_tvshow_nfo(plan):
    details = plan.get('details') or {}
    if not details.get('id'):
        return ''
    lines = ['<?xml version="1.0" encoding="utf-8" standalone="yes"?>', '<tvshow>',
             _tag('title', details.get('name') or details.get('title')),
             _tag('originaltitle', details.get('original_name') or details.get('original_title')),
             _tag('year', (details.get('first_air_date') or '')[:4]),
             _tag('plot', details.get('overview')), _unique(details), *_genres(details),
             _tag('premiered', details.get('first_air_date')), _tag('rating', details.get('vote_average')),
             '</tvshow>']
    return '\n'.join(line for line in lines if line) + '\n'


def _write(path, data, root):
    """只允许写在 STRM 根目录里：统一走 fsutil 的原子替换。"""
    from .fsutil import UnsafePath, atomic_write
    try:
        atomic_write(path, data, root)
    except UnsafePath as exc:
        raise ValueError('附属文件路径不安全') from None


def _show_dir(strm_path):
    parent = Path(strm_path).parent
    return parent.parent if parent.name.lower().startswith('season ') else parent


async def _image(engine, tmdb_path, target, root, size, missing_only=False):
    if not isinstance(tmdb_path, str) or not IMAGE_PATH.fullmatch(tmdb_path):
        return False
    if Path(target).exists():
        return False
    url = IMAGE_BASE + size + tmdb_path
    try:
        response = await engine.http.get(url)
        if response.status_code != 200 or not response.content:
            return False
        if len(response.content) > MAX_IMAGE:
            return False
        if missing_only:
            from .strm_options import write_new
            return write_new(target, response.content, root)
        _write(target, response.content, root)
        return True
    except Exception:
        return False


async def write_sidecars(engine, strm_path, plan, settings, missing_only=False):
    """按开关写 NFO 与海报；任何一步失败都只跳过，不影响 STRM 本身。"""
    if not isinstance(settings, dict):
        return []
    want_nfo, want_images = settings.get('generate_nfo'), settings.get('generate_images')
    if not want_nfo and not want_images:
        return []
    root = engine.root
    strm_path = Path(strm_path)
    details = plan.get('details') or {}
    hint = plan.get('hint') or {}
    written = []
    if want_nfo:
        body = build_nfo(plan)
        if body:
            target = strm_path.with_suffix('.nfo')
            if not missing_only or not target.exists():
                if missing_only:
                    from .strm_options import write_new
                    created = write_new(target, body.encode('utf-8'), root)
                else:
                    _write(target, body.encode('utf-8'), root)
                    created = True
                if created: written.append(str(target.relative_to(root)))
        if hint.get('media_type') == 'tv':
            show = build_tvshow_nfo(plan)
            if show:
                target = _show_dir(strm_path) / 'tvshow.nfo'
                content = show.encode('utf-8')
                # 剧集根目录的 tvshow.nfo 是全集共用的：内容没变就不重写，
                # 否则每入库一集都会刷新它的 mtime、触发 Emby 重扫，通知里也会多算一个 nfo。
                if target.exists() and target.read_bytes() == content:
                    pass
                elif not missing_only or not target.exists():
                    if missing_only:
                        from .strm_options import write_new
                        created = write_new(target, content, root)
                    else:
                        _write(target, content, root)
                        created = True
                    if created: written.append(str(target.relative_to(root)))
    if want_images and details:
        folder = _show_dir(strm_path) if hint.get('media_type') == 'tv' else strm_path.parent
        if await _image(engine, details.get('poster_path'), folder / 'poster.jpg', root, POSTER_SIZE, missing_only):
            written.append(str((folder / 'poster.jpg').relative_to(root)))
        if await _image(engine, details.get('backdrop_path'), folder / 'fanart.jpg', root, BACKDROP_SIZE, missing_only):
            written.append(str((folder / 'fanart.jpg').relative_to(root)))
    return written
