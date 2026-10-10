# -*- coding: utf-8 -*-
"""从 engine.py 拆出。跨层符号统一经 _eng() 惰性访问（monkeypatch 穿透 + 双导入兼容）。"""
import os, re, sys, json, threading, shutil, fcntl, time, argparse, datetime, hashlib, logging, traceback
import urllib.parse, urllib.request, urllib.error
from pathlib import Path
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import contextlib, dataclasses, html

log = logging.getLogger('strimkeep')

try:
    from .core import (esc, parse_season_dir, get_ep, title_key,
                       governance_title_key, analyze_season_episodes, parse_emby_library,
                       quality_label, RE_SXXEXX)
except ImportError:
    from core import (esc, parse_season_dir, get_ep, title_key,
                      governance_title_key, analyze_season_episodes, parse_emby_library,
                      quality_label, RE_SXXEXX)


try:
    from . import config as _cfg
    from . import logger
except ImportError:
    import config as _cfg
    import logger


def _eng():
    try:
        from . import engine as _e
        return _e
    except ImportError:
        try:
            import engine as _e
            return _e
        except ImportError:
            return None


def emby_request(path, params=None, method='GET', timeout=None, body=None, retries=None):
    if not _eng().EMBY_KEY:
        raise RuntimeError('未配置 EMBY_KEY')
    timeout = timeout or _eng().EMBY_TIMEOUT
    url = _eng().EMBY_HOST + path + ('?' + urllib.parse.urlencode(params) if params else '')
    headers = {'X-Emby-Token': _eng().EMBY_KEY}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    else:
        data = b'' if method == 'POST' else None
    attempts = 1 + ((_eng().EMBY_RETRIES if retries is None else retries) if method == 'GET' else 0)
    for i in range(attempts):
        req = urllib.request.Request(url, method=method, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
            break
        except urllib.error.HTTPError as e:
            if e.code < 500 or i == attempts - 1:
                raise
            err = e
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            if i == attempts - 1:
                raise
            err = e
        log.warning('Emby 请求失败(%s)，第 %d/%d 次重试: %s', path, i + 1, attempts - 1, err)
        time.sleep(1 + i)
    return json.loads(raw.decode('utf-8')) if (method == 'GET' and raw) else None

def container_to_emby_path(p, target):
    """容器内路径 → Emby 侧路径（notify_emby_deleted 用）；映射不上返回 None"""
    root = _eng().L_ROOT if target == 'local' else _eng().S_ROOT
    eroot = _eng().EMBY_PATHS.local if target == 'local' else _eng().EMBY_PATHS.share
    if not eroot:
        return None
    try:
        rel = Path(p).relative_to(root)
    except ValueError:
        return None
    return eroot.rstrip('/') + ('/' + rel.as_posix() if rel.parts else '')

def notify_emby_deleted(emby_paths, background=True):
    """告诉 Emby「这些路径已删除」（POST /Library/Media/Updated, UpdateType=Deleted）。
    比 /Library/Refresh 整库扫描快得多，Emby 会直接把对应条目（含没有分集的空剧）移除。
    失败时退回整库刷新。默认放后台线程，不阻塞删除接口。"""
    paths = sorted({p for p in (emby_paths or []) if p})

    def _run():
        if paths:
            for i in range(0, len(paths), 200):
                body = {'Updates': [{'Path': p, 'UpdateType': 'Deleted'} for p in paths[i:i + 200]]}
                ok = False
                for ep in ('/Library/Media/Updated', '/emby/Library/Media/Updated'):
                    try:
                        _eng().emby_request(ep, method='POST', timeout=15, body=body)
                        ok = True
                        break
                    except Exception as e:
                        log.warning('Emby 删除通知失败 %s: %s', ep, e)
                if not ok:
                    notify_emby_refresh()
                    return
        else:
            notify_emby_refresh()

    if background:
        threading.Thread(target=_run, daemon=True, name='emby-notify-deleted').start()
    else:
        _run()

def notify_emby_refresh():
    for ep in ('/Library/Refresh', '/emby/Library/Refresh'):
        try:
            _eng().emby_request(ep, method='POST', timeout=10)
            return True
        except Exception as e:
            log.warning('Emby 刷新失败 %s: %s', ep, e)
    return False

def parse_dt(s):
    if not s: return None
    try:
        return datetime.datetime.fromisoformat(s.split('.')[0].rstrip('Z')).replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        return None

def _fetch_all_episodes(force=False):
    """单向流式读取完整分集，避免 15 万条原始 Emby 字典常驻内存。

    只有片库全量校准使用此入口；校准周期由调度器控制，而不是
    在每个请求后再保留一份大列表做十分钟缓存。
    """
    start, page_size = 0, 5000
    with _eng()._ep_lock:
        # Old cache may originate from an older release; release its references.
        _eng()._ep_cache.update(ts=0, data=None)
        while True:
            response = _eng().emby_request('/Items', {
                'Recursive': 'true', 'IncludeItemTypes': 'Episode',
                'Fields': 'SeriesId,ParentIndexNumber,IndexNumber,Path',
                'StartIndex': start, 'Limit': page_size,
            }) or {}
            page = response.get('Items') or []
            total = int(response.get('TotalRecordCount') or 0)
            for row in page:
                yield row
            start += len(page)
            log.info('拉取分集 %d/%d', start, total)
            if not page or len(page) < page_size or (total and start >= total):
                break

def _dir_has_media(d):
    """目录里是否还有媒体文件（一次 scandir，遇到就返回）。无法确认（权限/IO 错误）时保守返回 True。"""
    try:
        with os.scandir(d) as it:
            for e in it:
                if os.path.splitext(e.name)[1].lower() in _eng()._MEDIA_EXT:
                    return True
        return False
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        return True

def _alive_dir_map(paths):
    """对一批 Emby 文件路径，按父目录去重后检查是否还有媒体文件：{父目录: bool}。
    只按「目录」判断，与集号无关（只有 S08/S09、没有 S01 的剧也没问题）；
    十万分集通常只有几千个季目录。库根不可用（NAS 掉挂载）或死目录占比异常时整体不信任，
    返回空 dict（= 全部视为存在），避免把整库误判成幽灵。"""
    dirs = {}
    for p in paths:
        if not p:
            continue
        p = p.replace('\\', '/')
        d = p.rsplit('/', 1)[0] if '/' in p else ''
        if not d or d in dirs:
            continue
        conv = _eng().EMBY_PATHS.to_container(d)
        if conv is None:
            dirs[d] = True
            continue
        root = _eng().L_ROOT if str(conv).startswith(str(_eng().L_ROOT) + os.sep) else _eng().S_ROOT
        dirs[d] = True if not root.exists() else _dir_has_media(conv)
    dead = sum(1 for v in dirs.values() if not v)
    if len(dirs) > 20 and dead / len(dirs) > 0.5:
        log.warning('片库映射：%d/%d 个目录检测为空，疑似挂载异常，本次不做幽灵过滤', dead, len(dirs))
        return {}
    return dirs

def _paged_items(params, page_size=1000, max_items=200000):
    """分页获取 /Items；近期入库按日期倒序提前停止（兼容忽略 MinDateCreated 的 Emby）。

    不能因页数/数量上限静默截断结果；否则 24 小时统计会错误变少。
    检测到违反降序排序契约时抛错，由入库层保留上次成功数据。
    """
    items, start = [], 0
    recent = (params.get('MinDateCreated') and params.get('SortBy') == 'DateCreated'
              and params.get('SortOrder') == 'Descending')
    cutoff = parse_dt(params['MinDateCreated']) if recent else None
    previous = None
    while True:
        data = _eng().emby_request('/Items', dict(params, StartIndex=start, Limit=page_size)) or {}
        page = data.get('Items') or []
        total = int(data.get('TotalRecordCount') or 0)
        if not page:
            break
        if cutoff:
            reached_old = False
            for row in page:
                dt = parse_dt(row.get('DateCreated'))
                if dt is None:
                    # Do not stop prematurely when a server omits DateCreated.
                    continue
                if previous is not None and dt > previous:
                    raise ValueError('Emby 入库时间排序异常，保留已有缓存')
                previous = dt
                if dt < cutoff:
                    reached_old = True
                elif reached_old:
                    raise ValueError('Emby 入库时间排序异常，保留已有缓存')
            items.extend(row for row in page if not (parse_dt(row.get('DateCreated')) or cutoff) < cutoff)
            if reached_old:
                break
        else:
            items.extend(page)
        start += len(page)
        if total and start >= total:
            break
        if len(page) < page_size:
            break
        if start >= max_items:
            raise ValueError('Emby 返回的入库条目超出安全分页上限；保留已有缓存')
    return items

def _recent(item_type, fields, limit, cutoff):
    """给旧 action_played 用，保留兼容"""
    params = {
        'Recursive': 'true', 'IncludeItemTypes': item_type,
        'Fields': fields, 'SortBy': 'DateCreated', 'SortOrder': 'Descending', 'Limit': limit,
    }
    data = _eng().emby_request('/Items', params) or {}
    for it in data.get('Items', []):
        dt = parse_dt(it.get('DateCreated'))
        if dt is None: continue
        if dt < cutoff: break
        yield it

def _src(path):
    lib = _eng().emby_lib_of(path)
    return '本地影视库' if lib == 'local' else ('分享影视库' if lib == 'share' else '其它库')

def emby_path_to_container(emby_path):
    """将 Emby 返回的 Path 转成容器内路径"""
    # 严格按配置的根目录前缀映射（不做末级目录名兜底），宁可对不上也不能删错库
    return _eng().EMBY_PATHS.to_container(emby_path)
