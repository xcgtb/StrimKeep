# -*- coding: utf-8 -*-
"""从 engine.py 拆出。跨层符号统一经 _eng() 惰性访问（monkeypatch 穿透 + 双导入兼容）。"""
if __package__:
    from . import state_store as _state
else:
    import state_store as _state
import os, re, sys, json, threading, shutil, fcntl, time, argparse, datetime, hashlib, logging, traceback, stat
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
    from .lib import _root_identity, LibraryScanError
except ImportError:
    from lib import _root_identity, LibraryScanError

_STATS_STATUS_LOCK = threading.Lock()
_STATS_STATUS = {'running': False, 'error': '', 'checked_at': 0, 'success_at': 0}


def scan_status():
    with _STATS_STATUS_LOCK:
        return dict(_STATS_STATUS)


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


CATS = [
    ('\U0001F476 儿童节目', ['儿童']),
    ('\U0001F3A4 演唱会',       ['演唱会']),
    ('\U0001F3AA 综艺剧',       ['综艺']),
    ('\u26e9\ufe0f 动漫剧',     ['动漫']),
    ('\U0001F9F8 动画电影',   ['动画电影']),
    ('\U0001F4FD\ufe0f 纪录片',  ['纪录']),
    ('\U0001F1E8\U0001F1F3 国产剧', ['国产剧集', '国产剧']),
    ('\U0001F1FA\U0001F1F8 欧美剧', ['欧美剧集', '欧美剧', '其他剧集']),
    ('\U0001F1EF\U0001F1F5 日韩剧', ['日韩剧集', '日韩剧']),
    ('\U0001F1E8\U0001F1F3 华语电影', ['国产电影', '华语电影']),
    ('\U0001F310 外语电影', ['欧美电影', '日韩电影', '外语电影', '其他电影']),
]

def _recompute_all_stats(allow_root_change=False):
    """Publish only a complete scan; keep previous totals on mount/IO failure."""
    with _STATS_STATUS_LOCK:
        _STATS_STATUS.update(running=True, checked_at=time.time())
    try:
        result = _verified_stats(allow_root_change)
    except Exception as error:
        with _STATS_STATUS_LOCK:
            _STATS_STATUS.update(running=False, error='统计扫描未完成，原数据保留：' + str(error),
                                 checked_at=time.time())
        raise
    with _STATS_STATUS_LOCK:
        _STATS_STATUS.update(running=False, error='', checked_at=time.time(), success_at=time.time())
    return result


def _statistics_files(root):
    """Stream verified paths, rejecting incomplete traversal like governance."""
    def walk_error(error):
        raise LibraryScanError(root, error.filename or root, error) from error
    visited = False
    for directory, children, names in os.walk(root, onerror=walk_error):
        visited = True
        for name in children:
            child = Path(directory) / name
            if child.is_symlink():
                raise LibraryScanError(root, child, '不支持跳过目录软链接')
        for name in names:
            if name.endswith('.strm'):
                yield Path(directory) / name
    if not visited:
        raise LibraryScanError(root, root, '根目录未完成遍历')


def _verified_stats(allow_root_change):
    """单次遍历双库，同时更新「分类统计」与「STRM 计数」两套缓存。
    之前两处各自独立遍历，时间不同/网络挂载抖动会导致数字对不上；
    统一从同一次遍历取数，保证总览卡片与总库分类统计永远一致。"""
    cat_names = [c[0] for c in CATS]

    out = {
        'local': {c: 0 for c in cat_names},
        'share': {c: 0 for c in cat_names},
        'local_other': 0,
        'share_other': 0,
        'local_total': 0,
        'share_total': 0,
    }

    directories = {}
    buckets = {}
    roots = (_eng().L_ROOT, _eng().S_ROOT)
    identities = {str(root): list(_root_identity(root)) for root in roots}
    previous = _state.read(_eng().STATE_DIR / 'strm_directory_counts.json', {})
    if previous.get('roots') == list(map(str, roots)) and not allow_root_change:
        for root in roots:
            old = previous.get('identities', {}).get(str(root))
            if old and old != identities[str(root)]:
                raise LibraryScanError(root, root, '根目录挂载身份已变化，请确认挂载后手动刷新总览')

    for root, key in ((_eng().L_ROOT, 'local'), (_eng().S_ROOT, 'share')):
        for f in _statistics_files(root):
            try:
                if not stat.S_ISREG(f.lstat().st_mode):
                    raise OSError('STRM 不是普通文件或是软链接')
            except OSError as error:
                raise LibraryScanError(root, f, error) from error
            try:
                parts = f.relative_to(root).parts
            except ValueError:
                continue
            directories[str(f.parent)] = directories.get(str(f.parent), 0) + 1
            matched = None
            for part in parts:
                for cat_name, kws in CATS:
                    hit = False
                    for kw in kws:
                        if kw in part:
                            hit = True
                            break
                    if hit:
                        matched = cat_name
                        break
                if matched:
                    break
            if matched:
                out[key][matched] += 1
            else:
                out[key + '_other'] += 1
            per_directory = buckets.setdefault(str(f.parent), {})
            label = matched or 'other'
            per_directory[label] = per_directory.get(label, 0) + 1
            out[key + '_total'] += 1

    rows = []
    for cat in cat_names:
        rows.append({
            'name': cat,
            'local': out['local'][cat],
            'share': out['share'][cat],
            'total': out['local'][cat] + out['share'][cat],
        })
    result = {
        'status': 'success',
        'rows': rows,
        'local_total': out['local_total'],
        'share_total': out['share_total'],
        'local_other': out['local_other'],
        'share_other': out['share_other'],
    }
    now = time.time()
    result['ts'] = now
    result['verified_ts'] = now
    # Verify both roots again after traversing both libraries, before any write.
    if any(list(_root_identity(root)) != identities[str(root)] for root in roots):
        raise LibraryScanError(roots[0], roots[0], '扫描期间根目录挂载身份已变化')
    ledger = {'roots': list(map(str, roots)), 'identities': identities,
              'directories': directories, 'buckets': buckets, 'verified_ts': now}
    _state.save_many([(_eng().STATE_DIR / 'library_stats_cache.json', result),
                      (_eng().STATE_DIR / 'strm_directory_counts.json', ledger),
                      (_eng()._STRM_COUNT_CACHE_FILE,
                       {'ts': now, 'local': out['local_total'], 'share': out['share_total']})])
    _eng()._lib_stats_cache['ts'] = now
    _eng()._lib_stats_cache['data'] = result
    # 同一次遍历的总数同步写入 STRM 计数缓存（内存+磁盘），两处数字永远一致
    _eng()._strm_count_cache.update({'ts': now, 'local': out['local_total'], 'share': out['share_total']})
    return result

_STATS_AUTO_TTL = 3600  # periodic dashboard use; manual invalidate/recheck stays available


def refresh_event_directories(paths):
    """Recount only callback-touched directories against a full-scan baseline.

    Idempotent for duplicate/retried events. No count is inferred from episode
    numbers or callbacks. A missing baseline waits for explicit initialization.
    """
    roots = [Path(_eng().L_ROOT), Path(_eng().S_ROOT)]
    with _eng()._strm_count_refreshing:
        ledger_path = _eng().STATE_DIR / 'strm_directory_counts.json'
        cache_path = _eng().STATE_DIR / 'library_stats_cache.json'
        ledger = _state.read(ledger_path, {})
        data = _state.read(cache_path, {})
        if ledger.get('roots') != list(map(str, roots)) or not data.get('rows'):
            return False
        counts = ledger.get('directories') or {}
        buckets = ledger.get('buckets') or {}
        changed = False
        for value in sorted(set(paths)):
            directory = Path(value)
            for root, lib in zip(roots, ['local', 'share']):
                try:
                    relative = directory.resolve().relative_to(root.resolve())
                except ValueError:
                    continue
                identity = ledger.get('identities', {}).get(str(root))
                if not root.is_dir() or (identity and identity != [root.stat().st_dev, root.stat().st_ino]):
                    raise RuntimeError('STRM 根目录不可见，保留统计')
                if not identity and not any(root.iterdir()):
                    raise RuntimeError('STRM 根目录尚未核实，保留统计')
                # Direct children are bounded below, including root-level STRM.
                current = 0
                current_buckets = {}
                if directory.is_dir():
                    for i, file in enumerate(directory.iterdir()):
                        if i >= 10000:
                            raise RuntimeError('单目录过大，等待全量统计')
                        if file.name.endswith('.strm') and file.is_file():
                            current += 1
                            category = next((name for part in file.relative_to(root).parts for name, words in CATS
                                             if any(word in part for word in words)), 'other')
                            current_buckets[category] = current_buckets.get(category, 0) + 1
                previous = counts.get(str(directory), 0)
                previous_buckets = buckets.get(str(directory), {})
                if previous and not previous_buckets:
                    return False  # upgrade requires a new full baseline
                difference = current - previous
                if not difference and current_buckets == previous_buckets:
                    break
                data[lib + '_total'] += difference
                for category in set(current_buckets) | set(previous_buckets):
                    delta = current_buckets.get(category, 0) - previous_buckets.get(category, 0)
                    if category == 'other':
                        data[lib + '_other'] += delta
                    else:
                        row = next(row for row in data['rows'] if row['name'] == category)
                        row[lib] += delta
                        row['total'] = row['local'] + row['share']
                counts[str(directory)] = current
                buckets[str(directory)] = current_buckets
                changed = True
                break
        if not changed:
            return True
        if min(data['local_total'], data['share_total']) < 0:
            raise RuntimeError('STRM 统计基线冲突，保留旧值')
        data['ts'] = time.time()
        ledger['directories'] = counts
        ledger['buckets'] = buckets
        count_path = _eng()._STRM_COUNT_CACHE_FILE
        _state.save_many([(cache_path, data), (ledger_path, ledger), (count_path,
                          {'ts': data['ts'], 'local': data['local_total'], 'share': data['share_total']})])
        _eng()._lib_stats_cache.update(ts=data['ts'], data=data)
        _eng()._strm_count_cache.update(ts=data['ts'], local=data['local_total'], share=data['share_total'])
        return True

def action_library_stats(args):
    # 缓存命中则直接返回
    now = time.time()
    from . import config
    if config.load_config().get('ingest_mode') == 'webhook':
        disk = _state.read(_eng().STATE_DIR / 'library_stats_cache.json', {})
        if isinstance(disk.get('rows'), list):
            _eng()._lib_stats_cache.update(ts=disk.get('ts', 0), data=disk)
            return disk
    if _eng()._lib_stats_cache['data'] is not None and (now - _eng()._lib_stats_cache['ts']) < _STATS_AUTO_TTL:
        return _eng()._lib_stats_cache['data']
    disk = _state.read(_eng().STATE_DIR / 'library_stats_cache.json', {})
    if isinstance(disk, dict) and isinstance(disk.get('rows'), list):
        _eng()._lib_stats_cache.update(ts=disk.get('ts', 0), data=disk)
        if now - float(disk.get('ts') or 0) >= _STATS_AUTO_TTL:
            threading.Thread(target=_strm_count_bg_refresh, daemon=True,
                             name='library-stats-refresh').start()
        return disk
    with _eng()._strm_count_refreshing:
        if _eng()._lib_stats_cache['data'] is not None and now - _eng()._lib_stats_cache['ts'] < _STATS_AUTO_TTL:
            return _eng()._lib_stats_cache['data']
        return _recompute_all_stats()

def _load_strm_count_disk():
    try:
        data = _state.read(_eng()._STRM_COUNT_CACHE_FILE)
        local, share = int(data['local']), int(data['share'])
        if local < 0 or share < 0:
            return None
        return local, share, float(data['ts'])
    except (OSError, ValueError, TypeError, KeyError):
        return None

def _save_strm_count_disk(local, share):
    try:
        _eng().STATE_DIR.mkdir(parents=True, exist_ok=True)
        _state.save(_eng()._STRM_COUNT_CACHE_FILE, {'ts': time.time(), 'local': local, 'share': share})
    except OSError as e:
        log.warning('STRM 计数缓存写入失败: %s', e)

def _strm_count_bg_refresh():
    """后台重算统计（单飞）。走 _recompute_all_stats 统一遍历，
    同时更新分类统计与 STRM 计数，保证两处数字一致。"""
    if not _eng()._strm_count_refreshing.acquire(blocking=False):
        return
    try:
        _recompute_all_stats()
        log.info('后台刷新统计完成: local=%d share=%d',
                 _eng()._strm_count_cache['local'], _eng()._strm_count_cache['share'])
    except Exception as e:
        log.warning('统计后台刷新失败: %s', e)
    finally:
        _eng()._strm_count_refreshing.release()

def _get_strm_counts():
    """STRM 总数三级缓存：内存(5分钟) → 磁盘(立即返回+后台刷新) → 同步首算。
    大库 rglob 全量遍历很慢，磁盘缓存保证总览页秒开，后台静默刷新。"""
    now = time.time()
    from . import config
    if config.load_config().get('ingest_mode') == 'webhook':
        disk = _load_strm_count_disk()
        if disk is not None:
            return disk[:2]
    if now - _eng()._strm_count_cache['ts'] < _STATS_AUTO_TTL and _eng()._strm_count_cache['ts'] > 0:
        return _eng()._strm_count_cache['local'], _eng()._strm_count_cache['share']
    disk = _load_strm_count_disk()
    if disk is not None:
        l, s_, ts = disk
        _eng()._strm_count_cache.update({'ts': ts or (now - _STATS_AUTO_TTL), 'local': l, 'share': s_})
        if time.time() - _eng()._strm_count_cache['ts'] >= _STATS_AUTO_TTL:
            threading.Thread(target=_strm_count_bg_refresh, daemon=True,
                             name='strm-count-refresh').start()
        return _eng()._strm_count_cache['local'], _eng()._strm_count_cache['share']
    # 首次无任何缓存：同步算一次并落盘
    with _eng()._strm_count_refreshing:
        result = _recompute_all_stats()
    l, s_ = result['local_total'], result['share_total']
    log.info('缓存刷新 STRM 计数: local=%d share=%d', l, s_)
    return l, s_

def invalidate_stats_cache(preserve_persisted=False):
    """清空统计缓存（删除后调用）"""
    _eng()._strm_count_cache['ts'] = 0
    _eng()._lib_stats_cache['ts'] = 0
    _eng()._lib_stats_cache['data'] = None
    if preserve_persisted:
        # Manual refresh ignores these values while rebuilding. Keep the last
        # successful counts across an interrupted refresh or process restart.
        return
    # A confirmed deletion must invalidate SQLite totals, not just RAM values.
    for path in (_eng().STATE_DIR / 'library_stats_cache.json', _eng()._STRM_COUNT_CACHE_FILE):
        try:
            _state.remove(path)
        except OSError as error:
            # Failure to invalidate a statistic must not fail an already
            # committed media deletion; surface it for later reconciliation.
            log.warning('统计缓存持久失效失败 %s: %s', path, error)

def invalidate_media_caches(keep_emby_lib=False):
    """文件变动后统一失效内存缓存（分集 / Emby 索引 / _eng().Lib 快照 / 统计）。
    keep_emby_lib=True 时保留片库映射缓存（单剧删除会就地修补它，避免整页重跑 TMDB 对照）。"""
    _eng()._ep_cache['ts'] = 0
    _eng()._ep_cache['data'] = None
    _eng()._emby_index_cache['ts'] = 0
    _eng()._emby_index_cache['data'] = None
    if not keep_emby_lib:
        _eng()._emby_lib_cache['ts'] = 0
        _eng()._emby_lib_cache['data'] = None
    _eng()._invalidate_lib_cache()
    invalidate_stats_cache()
