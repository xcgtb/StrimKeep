# -*- coding: utf-8 -*-
"""从 engine.py 拆出。跨层符号统一经 _eng() 惰性访问（monkeypatch 穿透 + 双导入兼容）。"""
if __package__:
    from . import state_store as _state
else:
    import state_store as _state
import os, re, sys, json, threading, shutil, fcntl, time, argparse, datetime, hashlib, logging, traceback
import urllib.parse, urllib.request, urllib.error
from pathlib import Path
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import contextlib, dataclasses, html, copy
import math
import copy

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


def _save_overview_disk(out):
    try:
        _eng().STATE_DIR.mkdir(parents=True, exist_ok=True)
        _state.save(_eng()._EMBY_OVERVIEW_CACHE_FILE, {'ts': time.time(), 'data': out})
    except (OSError, TypeError) as e:
        log.warning('片库映射缓存写入失败: %s', e)

def _load_overview_disk():
    """返回 {'ts': float, 'data': {...}} 或 None"""
    try:
        raw = _state.read(_eng()._EMBY_OVERVIEW_CACHE_FILE)
        data = raw.get('data')
        if isinstance(data, dict) and 'series' in data:
            return {'ts': float(raw.get('ts', 0)), 'data': data}
    except (OSError, ValueError):
        pass
    return None

def _overview_bg_refresh():
    """后台重建片库映射数据（单飞），完成后更新内存+磁盘缓存"""
    if not _eng()._overview_refresh_lock.acquire(blocking=False):
        return
    try:
        out = _build_emby_library_overview()
        _eng()._emby_lib_cache['ts'] = time.time()
        _eng()._emby_lib_cache['data'] = out
        _save_overview_disk(out)
        log.info('片库映射缓存后台刷新完成：剧集 %d / 电影 %d',
                 len(out.get('series', [])), len(out.get('movies', [])))
    except Exception as e:
        log.warning('片库映射后台刷新失败: %s', e)
    finally:
        _eng()._overview_refresh_lock.release()

def emby_library_overview(force=False):
    """片库映射数据三级缓存：内存(5分钟) → 磁盘(立即返回+后台刷新) → 同步构建。
    全量拉取 Emby 分集很慢，磁盘缓存保证每次进页面秒开，后台静默更新。"""
    if not force:
        if _eng()._emby_lib_cache['data'] and time.time() - _eng()._emby_lib_cache['ts'] < _eng()._CACHE_TTL:
            return _eng()._emby_lib_cache['data']
        disk = _load_overview_disk()
        if disk is not None:
            _eng()._emby_lib_cache['data'] = disk['data']
            _eng()._emby_lib_cache['ts'] = disk['ts']
            if time.time() - disk['ts'] >= _eng()._CACHE_TTL:
                threading.Thread(target=_overview_bg_refresh, daemon=True,
                                 name='emby-overview-refresh').start()
            return _eng()._emby_lib_cache['data']
    out = _build_emby_library_overview()
    _eng()._emby_lib_cache['ts'] = time.time()
    _eng()._emby_lib_cache['data'] = out
    _save_overview_disk(out)
    return out

def _build_emby_library_overview():
    """构建统一媒体身份的片库快照。

    关键口径：双库事实身份优先由「剧名 + 年份」确定，TMDB 只作元数据。
    这样即使 Emby 某条目误写了 TMDB ID，也不会把另一部作品错误合并；
    同一身份的季/集按 ``(season, episode)`` union。
    """
    out = {'series': [], 'movies': []}
    lib_of = _eng().EMBY_PATHS.lib_of
    disk_lookup = _eng()._disk_tmdb_lookup()  # 上游目录名 {tmdb-xxx} 兜底表
    series_data = _eng().emby_request('/Items', {
        'Recursive': 'true', 'IncludeItemTypes': 'Series',
        'Fields': 'ProviderIds,Path,ProductionYear,CommunityRating,ImageTags,Genres',
        'Limit': 50000,
    }) or {}
    episodes_all = _eng()._fetch_all_episodes()
    alive = _eng()._alive_dir_map(ep.get('Path') for ep in episodes_all)

    def _ep_alive(path):
        p = (path or '').replace('\\', '/')
        return alive.get(p.rsplit('/', 1)[0] if '/' in p else '', True)

    # 先把真实分集按 Emby SeriesId 收好，再按 TMDB 身份聚合。
    eps_by_series = defaultdict(list)
    for ep in episodes_all:
        sid = ep.get('SeriesId')
        if sid and _ep_alive(ep.get('Path')):
            eps_by_series[sid].append(ep)

    groups = {}
    for s in series_data.get('Items', []):
        sid = s.get('Id')
        if not sid or not eps_by_series.get(sid):
            continue  # 空壳 Series 不进入片库映射
        tmdb_id = str((s.get('ProviderIds') or {}).get('Tmdb') or '')
        if not tmdb_id:
            _cp = _eng().emby_path_to_container(s.get('Path', '') or '')
            tmdb_id = disk_lookup.get(str(_cp) if _cp else '', '')
        key = f'tv:{tmdb_id}' if tmdb_id else f'id:{sid}'
        g = groups.setdefault(key, {
            'id': sid, 'series_ids': [], 'name': s.get('Name'),
            'year': s.get('ProductionYear'), 'rating': s.get('CommunityRating'),
            'tmdb_id': tmdb_id or None, 'genres': list(s.get('Genres') or []),
            'paths': [], 'libs': set(), 'has_image': False,
            'episodes': set(), 'local_eps': set(), 'share_eps': set(),
        })
        if sid not in g['series_ids']:
            g['series_ids'].append(sid)
        path = s.get('Path', '') or ''
        if path and path not in g['paths']:
            g['paths'].append(path)
        lib = lib_of(path)
        if lib:
            g['libs'].add(lib)
        g['has_image'] = g['has_image'] or ('Primary' in (s.get('ImageTags') or {}))
        if not g.get('name') and s.get('Name'):
            g['name'] = s.get('Name')
        if not g.get('year') and s.get('ProductionYear'):
            g['year'] = s.get('ProductionYear')
        for ep in eps_by_series[sid]:
            sn = ep.get('ParentIndexNumber'); en = ep.get('IndexNumber')
            if sn is None or en is None:
                continue
            try:
                pair = (int(sn), int(en))
            except (TypeError, ValueError):
                continue
            if pair[0] < 0 or pair[1] <= 0:
                continue
            g['episodes'].add(pair)
            ep_lib = lib_of(ep.get('Path', '') or '')
            if ep_lib == 'local': g['local_eps'].add(pair)
            elif ep_lib == 'share': g['share_eps'].add(pair)

    # 分集磁盘兜底：上游 TgtoDrive 用 SxxExx 命名，Emby 识别可能漏集/错集。
    # 把磁盘事实分集 union 进 Emby 分集，补上 Emby 漏识别的集，避免缺集对照误判。
    disk_eps = _eng()._disk_eps_by_tmdb()
    for g in groups.values():
        _tid = g.get('tmdb_id')
        if not _tid:
            continue
        _de = disk_eps.get(_tid)
        if _de and _de['episodes']:
            g['episodes'] |= _de['episodes']
            g['local_eps'] |= _de['local_eps']
            g['share_eps'] |= _de['share_eps']

    for g in groups.values():
        season_map = defaultdict(set)
        for sn, en in g['episodes']:
            season_map[sn].add(en)
        seasons = []
        total_missing = 0
        for sn in sorted(season_map):
            eps_set = sorted(season_map[sn])
            if not eps_set:
                continue
            lo, hi = eps_set[0], eps_set[-1]
            pre = list(range(1, lo)) if lo > 1 else []
            mid = sorted(set(range(lo, hi + 1)) - set(eps_set))
            miss = sorted(set(pre + mid))
            total_missing += len(miss)
            seasons.append({'season': sn, 'episodes': len(eps_set), 'max_ep': hi,
                            'missing': miss, 'complete': not miss,
                            'local_eps': sum(1 for ss, _ in g['local_eps'] if ss == sn),
                            'share_eps': sum(1 for ss, _ in g['share_eps'] if ss == sn)})
        if not seasons:
            continue
        libs = g['libs']
        out['series'].append({
            'id': g['id'], 'series_ids': g['series_ids'], 'name': g['name'],
            'year': g['year'], 'rating': g['rating'], 'tmdb_id': g['tmdb_id'],
            'genres': g['genres'], 'in_local': 'local' in libs, 'in_share': 'share' in libs,
            'path': g['paths'][0] if g['paths'] else '', 'paths': g['paths'],
            'has_image': g['has_image'], 'seasons': seasons,
            'total_seasons': len(seasons), 'total_episodes': len(g['episodes']),
            'local_eps': len(g['local_eps']), 'share_eps': len(g['share_eps']),
            'have_eps': len(g['episodes']), 'missing_eps': total_missing,
            'complete': total_missing == 0 and len(seasons) > 0,
        })

    movie_data = _eng().emby_request('/Items', {
        'Recursive': 'true', 'IncludeItemTypes': 'Movie',
        'Fields': 'ProviderIds,Path,ProductionYear,CommunityRating,ImageTags,Genres',
        'Limit': 50000,
    }) or {}
    movie_items = movie_data.get('Items', [])
    m_alive = _eng()._alive_dir_map(m.get('Path') for m in movie_items)
    movie_groups = {}
    for m in movie_items:
        path = m.get('Path', '') or ''
        _pp = path.replace('\\', '/')
        if not m_alive.get(_pp.rsplit('/', 1)[0] if '/' in _pp else '', True):
            continue
        tmdb_id = str((m.get('ProviderIds') or {}).get('Tmdb') or '')
        if not tmdb_id:
            _cp = _eng().emby_path_to_container(m.get('Path', '') or '')
            tmdb_id = disk_lookup.get(str(_cp) if _cp else '', '')
        key = f'movie:{tmdb_id}' if tmdb_id else f'id:{m.get("Id")}'
        g = movie_groups.setdefault(key, {
            'id': m.get('Id'), 'ids': [], 'name': m.get('Name'),
            'year': m.get('ProductionYear'), 'rating': m.get('CommunityRating'),
            'tmdb_id': tmdb_id or None, 'genres': list(m.get('Genres') or []),
            'paths': [], 'libs': set(), 'has_image': False,
        })
        if m.get('Id') and m.get('Id') not in g['ids']:
            g['ids'].append(m.get('Id'))
        if path and path not in g['paths']:
            g['paths'].append(path)
        lib = lib_of(path)
        if lib: g['libs'].add(lib)
        g['has_image'] = g['has_image'] or ('Primary' in (m.get('ImageTags') or {}))
    for g in movie_groups.values():
        libs = g['libs']
        out['movies'].append({
            'id': g['id'], 'ids': g['ids'], 'name': g['name'], 'year': g['year'],
            'rating': g['rating'], 'tmdb_id': g['tmdb_id'], 'genres': g['genres'],
            'in_local': 'local' in libs, 'in_share': 'share' in libs,
            'path': g['paths'][0] if g['paths'] else '', 'paths': g['paths'],
            'has_image': g['has_image'],
        })
    return out

def read_manual_done() -> dict:
    data = _state.read(_eng().MANUAL_DONE_FILE, {})
    return data if isinstance(data, dict) else {}

def _apply_manual_done_to_series(series, done=None):
    """统一应用手动完结标记，并返回不修改原对象的健康统计。"""
    if done is None:
        done = read_manual_done()
    rows = []
    for src in series or []:
        s = dict(src)
        ti = dict(s.get('tmdb_info') or {})
        ids = [str(x) for x in (s.get('series_ids') or [s.get('id')]) if x]
        if any(x in done for x in ids) and ti.get('match_status') in ('missing', 'ongoing'):
            ti['match_status'] = 'aligned'
            ti['manual_done'] = True
        s['tmdb_info'] = ti
        s['_md'] = bool(ti.get('manual_done'))
        rows.append(s)
    return rows

def _episode_count(row):
    # 0 是有效事实；不能用 or 退回历史集数。
    value = row.get('have_eps')
    if value is None:
        value = row.get('total_episodes')
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _health_snapshot(data, manual_done=None):
    """只从同一份缓存行生成汇总、缺集榜和事实版本，不访问网络。"""
    series = _apply_manual_done_to_series(data.get('series') or [], done=manual_done)
    movies = data.get('movies') or []
    stats = {'total_series': len(series), 'total_movies': len(movies),
             'aligned': 0, 'missing': 0, 'extra': 0, 'ongoing': 0,
             'unmatched': 0, 'no_tmdb': 0, 'manual_done': 0}
    top = []
    for row in series:
        info = row.get('tmdb_info') or {}
        status = info.get('match_status', 'unmatched')
        if status in stats:
            stats[status] += 1
        if row.get('_md'):
            stats['manual_done'] += 1
        if status == 'missing':
            top.append({'name': row.get('name'), 'year': row.get('year'),
                        'diff': abs(int(info.get('diff') or 0)),
                        'tot': int(info.get('tmdb_total') or 0), 'have': _episode_count(row)})
    top.sort(key=lambda row: row['diff'], reverse=True)
    episodes = sum(_episode_count(row) for row in series)
    # 兼容旧的纯汇总快照；有明细时始终按明细重新计算。
    if not series and ('series' not in data or ('episodes' not in data and 'episode_total' in data)):
        episodes = int(data.get('episodes', data.get('episode_total', 0)) or 0)
        stats.update(data.get('stats') or {})
        top = list(data.get('top_missing') or [])
    raw = json.dumps({'series': series, 'movies': movies, 'episodes': episodes},
                     sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    version = hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]
    return {'schema_version': 1, 'ts': data.get('ts') or 0,
            'facts_ts': data.get('facts_ts') or data.get('ts') or 0,
            'facts_version': version, 'series': series, 'movies': movies, 'stats': stats,
            'episodes': episodes, 'episode_total': episodes, 'top_missing': top[:10],
            'source': data.get('source') or 'emby_tmdb_cache'}


def build_library_health_snapshot(max_age=1800):
    """从片库对照缓存生成统一汇总；已有空库缓存也是有效事实。"""
    data = read_emby_lib_cache(max_age=None)
    if data is None:
        data = emby_library_overview(force=False) or {}
    return _health_snapshot(data)

def save_library_snapshot(snapshot):
    try:
        _eng().STATE_DIR.mkdir(parents=True, exist_ok=True)
        _state.save(_eng().LIBRARY_SNAPSHOT_FILE, snapshot)
    except (OSError, TypeError) as e:
        log.warning('统一片库快照写入失败: %s', e)

def load_library_snapshot(max_age=None, background_refresh=True):
    try:
        data = _state.read(_eng().LIBRARY_SNAPSHOT_FILE)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if max_age is not None and time.time() - float(data.get('ts') or 0) > max_age:
        if background_refresh:
            threading.Thread(target=refresh_library_snapshot_background, daemon=True, name='library-snapshot-refresh').start()
        return data
    return data

def refresh_library_snapshot_background():
    """从已存在的 TMDB 对照缓存生成轻量快照；不触发网络扫描。"""
    try:
        data = read_emby_lib_cache(max_age=None)
        if data is not None:
            save_library_snapshot(_health_snapshot(data))
    except Exception as e:
        log.warning('统一片库快照刷新失败: %s', e)

_health_read_lock = threading.RLock()
_health_read_cache = {'key': None, 'data': None}


def _health_source_stamp():
    return tuple(_state.stamp(path) for path in (_eng().LIBRARY_SNAPSHOT_FILE, _eng().EMBY_LIB_CACHE_FILE))


def _read_shared_health():
    """只保留当前文件版本的一份结果；返回副本，写入后凭文件身份立即失效。"""
    with _health_read_lock:
        # 人工完结很小，仍每次读取，兼容损坏文件的数据库兜底。
        done = read_manual_done()
        stamps = _health_source_stamp()
        key = (stamps, json.dumps(done, sort_keys=True, ensure_ascii=False),
               id(_eng().load_library_snapshot), id(read_emby_lib_cache))
        if key == _health_read_cache['key']:
            return copy.deepcopy(_health_read_cache['data'])
        snap = _eng().load_library_snapshot(max_age=None, background_refresh=False)
        cached = read_emby_lib_cache(max_age=None)
        def facts_time(data):
            return float((data or {}).get('facts_ts') or (data or {}).get('ts') or 0)
        data = cached if cached is not None and (not snap or facts_time(cached) >= facts_time(snap)) else snap
        health = _health_snapshot(data, manual_done=done) if data is not None else {}
        if stamps == _health_source_stamp():
            _health_read_cache.update(key=key, data=health)
        else:
            _health_read_cache.update(key=None, data=None)
        return copy.deepcopy(health)


def unified_health(max_age=1800):
    """只读同一份最新缓存事实；入库/删除的局部更新不伪装成完整 TMDB 对照。"""
    try:
        health = _read_shared_health()
        if not health:
            return {}
        now = time.time()
        ts = float(health.get('ts') or 0)
        facts_ts = float(health.get('facts_ts') or 0)
        health.update({'age_sec': max(0, int(now - ts)) if ts else None,
                       'facts_age_sec': max(0, int(now - facts_ts)) if facts_ts else None,
                       'stale': not ts or (max_age is not None and now - ts > max_age)})
        return health
    except Exception as error:
        log.warning('读取统一片库快照失败: %s', error)
        return {}

def daily_consistency_snapshot(force_refresh: bool = False) -> dict:
    """轻量统一事实快照：晨报、总览、执行记录口径使用同一批缓存与规则指纹。"""
    # 晨报只引用后台 5 分钟轮询维护的 24h 入库缓存；生成晨报时绝不再次查询。
    ingest = _eng().read_ingest_cache() or {}
    health = unified_health(max_age=1800) or {}
    gov = _eng().load_latest_scan() or {}
    rule_snapshot = _eng()._current_rule_snapshot()
    return {
        'schema_version': 2, 'ts': time.time(), 'rule_sig': rule_snapshot['sig'],
        'ingest': {'ts': ingest.get('ts', 0), 'ok': ingest.get('ok', True), 'stats': ingest.get('stats', {}),
                   'warning': ingest.get('stale_error') or ('' if ingest.get('ok', True) else ingest.get('error', ''))},
        'library': {'ts': health.get('ts', 0), 'stats': health.get('stats', {}), 'episode_total': health.get('episodes', health.get('episode_total', 0)),
                    'facts_version': health.get('facts_version'), 'facts_ts': health.get('facts_ts', 0),
                    'source': health.get('source'), 'stale': health.get('stale', True),
                    'top_missing': (health.get('top_missing') or [])[:10]},
        'governance': {'ts': gov.get('ts', 0), 'scan_id': gov.get('plan_id', ''), 'result': gov.get('result') or {}},
        'rules': rule_snapshot['rules'],
    }


def cached_library_view(max_age=1800):
    """探索、映射及其详情共用最新片库事实；只读，不补建或逐剧现查。"""
    facts = unified_health(max_age=max_age)
    if not facts:
        return None
    result = dict(facts, status='success', from_cache=True, emby_host=_eng().EMBY_HOST)
    metadata = {k: facts.get(k) for k in ('facts_version', 'facts_ts', 'source', 'stale', 'ts')}
    result['series'] = [dict(row, **metadata) for row in facts.get('series') or []]
    stats = dict(facts.get('stats') or {})
    stats['complete_series'] = sum(bool(row.get('complete') or (row.get('tmdb_info') or {}).get('match_status') == 'aligned')
                                   for row in result['series'])
    stats['incomplete_series'] = len(result['series']) - stats['complete_series']
    result['stats'] = stats
    return result

def gap_report(max_age=30 * 60, force_refresh=False, cache_only=False):
    """缺集检测统一口径。

    cache_only=True：严格只读现有 Emby→TMDB 缓存，绝不触发网络扫描；
    force_refresh=True：严格现场重建，绕过内存/磁盘片库缓存。
    普通调用仍允许在缓存缺失时补建一次，兼容定时任务。
    """
    data = None if force_refresh else cached_library_view(max_age=1800 if max_age is None else max_age)
    from_cache = bool(data)
    if not data and not cache_only:
        from argparse import Namespace as _NS
        data = action_emby_library(_NS(force=force_refresh, with_tmdb=True))
        if data.get('status') == 'error':
            return {'status': 'error', 'message': data.get('message', '')}
        try:
            save_emby_lib_cache(data)  # 落盘，让网页读到的也是这一份，两边数据一致
            data['ts'] = time.time()
            data = cached_library_view(max_age=1800 if max_age is None else max_age) or data
        except Exception:
            pass
    if not data:
        return {'status': 'empty', 'message': '暂无可用的片库对照缓存，请先执行「预览（现场扫）」或等待后台片库扫描完成。',
                'missing': [], 'stats': {}, 'movies_total': 0, 'from_cache': False, 'cache_ts': 0}
    series = _apply_manual_done_to_series(data.get('series') or [])
    stats = {'total': len(series), 'aligned': 0, 'missing': 0, 'extra': 0,
             'ongoing': 0, 'unmatched': 0, 'unavailable': 0}
    missing = []
    done = read_manual_done()  # 手动完结的剧不再算缺集 / 在更，与网页「片库映射」口径一致
    for s_ in series:
        st = (s_.get('tmdb_info') or {}).get('match_status', 'unmatched')
        if st == 'no_tmdb': st = 'unmatched'
        ids = [str(x) for x in (s_.get('series_ids') or [s_.get('id')]) if x]
        if any(x in done for x in ids) and st in ('missing', 'ongoing'):
            st = 'aligned'
        if st in stats: stats[st] += 1
        if st in ('unmatched', 'pending', 'pending_tmdb', 'unknown'): stats['unavailable'] += 1
        if st == 'missing': missing.append(s_)
    missing.sort(key=lambda x: (x.get('tmdb_info') or {}).get('diff') or 0)  # diff 为负，越小缺得越多
    return {'status': 'success', 'missing': missing, 'stats': stats,
            'movies_total': len(data.get('movies') or []),
            'from_cache': from_cache, 'cache_ts': data.get('ts') or 0,
            **{key: data.get(key) for key in ('facts_ts', 'facts_version', 'source', 'stale')}}

def _tmdb_bulk_get(t, requests, max_workers=8):
    """主线程复用一次加载的缓存；仅缺失/过期条目并发联网，worker 不读写缓存文件。"""
    uniq = []; seen = set()
    for kind, tid in requests:
        key = (str(kind), str(tid))
        if not tid or key in seen:
            continue
        seen.add(key); uniq.append(key)
    results = {}; errors = 0; cache_rows = {}; pending = []
    now = time.time(); language = _eng().TMDB_LANG
    ttl = _eng().TMDB_INFO_TTL
    params = {'language': language}
    def cache_key(key):
        return f'/{key[0]}/{key[1]}?' + urllib.parse.urlencode(sorted(params.items()))
    for key in uniq:
        ck = cache_key(key); hit = t.cache.get(ck)
        try:
            stamp = float(hit['ts']) if isinstance(hit, dict) else 0
            fresh = isinstance(hit, dict) and 'data' in hit and math.isfinite(stamp) and stamp > 0 and now - stamp < ttl
        except (KeyError, TypeError, ValueError):
            fresh = False
        if fresh:
            results[key] = hit['data']; cache_rows[ck] = hit
            if hasattr(t, 'hits'): t.hits += 1
        else:
            pending.append(key)
    def one(key):
        try:
            # 兼容已有替身/兼容客户端；实际 Tmdb 使用不读盘的轻量 worker。
            factory = getattr(t, 'new_worker', None)
            worker = factory() if callable(factory) else _eng().Tmdb()
            info = worker.get(f'/{key[0]}/{key[1]}', ttl=ttl, language=language)
            ck = cache_key(key)
            return key, info, None, (ck, worker.cache.get(ck)), getattr(worker, 'calls', 0), getattr(worker, 'hits', 0)
        except Exception as error:
            return key, None, error, None, 0, 0
    if pending:
        workers = max(1, min(int(max_workers), len(pending), 8))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for key, info, error, cache_row, calls, hits in ex.map(one, pending):
                if error:
                    errors += 1
                    log.warning('TMDB 查询失败 %s/%s: %s', key[0], key[1], error)
                results[key] = info
                if hasattr(t, 'calls'): t.calls += calls
                if hasattr(t, 'hits'): t.hits += hits
                if cache_row and cache_row[1]: cache_rows[cache_row[0]] = cache_row[1]
    return results, errors, cache_rows


def action_emby_library(args):
    force = bool(getattr(args, 'force', False))
    with_tmdb = getattr(args, 'with_tmdb', True)
    if with_tmdb is None: with_tmdb = True
    try:
        data = emby_library_overview(force=force)
    except Exception as e:
        return {'status': 'error', 'message': f'拉取 Emby 库失败: {e}'}
    series = data['series']; movies = data['movies']
    tmdb_errors = 0
    # 所有 series 先标 pending
    for s in series:
        s['tmdb_info'] = {'match_status': 'pending', 'tmdb_status': None,
                          'local_total': s.get('total_episodes', 0),
                          'tmdb_total': None, 'diff': None, 'seasons': []}

    if with_tmdb:
        t = _eng().Tmdb()
        has_key = bool(t.key)
        reqs = [('tv', s.get('tmdb_id')) for s in series if s.get('tmdb_id')]
        reqs += [('movie', m.get('tmdb_id')) for m in movies if m.get('tmdb_id')]
        infos, tmdb_errors, cache_rows = _tmdb_bulk_get(t, reqs, max_workers=8) if has_key else ({}, 0, {})
        for s in series:
            tmdb_id = s.get('tmdb_id')
            if not tmdb_id:
                s['tmdb_info'] = {'match_status': 'no_tmdb' if has_key else 'unmatched',
                                  'tmdb_status': None, 'local_total': s.get('total_episodes', 0),
                                  'tmdb_total': None, 'diff': None, 'seasons': []}
                continue
            info = infos.get(('tv', str(tmdb_id)))
            s['tmdb_info'] = _eng().classify_series_by_tmdb(s.get('seasons', []), info)
            if info and info.get('poster_path'):
                s['tmdb_info']['poster'] = info.get('poster_path')
        if has_key:
            for m in movies:
                _mid = m.get('tmdb_id')
                if not _mid:
                    continue
                _minfo = infos.get(('movie', str(_mid)))
                if _minfo and _minfo.get('poster_path'):
                    m['poster_tmdb'] = _minfo.get('poster_path')
        t.cache.update(cache_rows)
        t.save()
    # 手动完结统一应用到后端快照，避免治理总览/晨报与片库映射各算一套。
    done = read_manual_done()
    for s in series:
        ti = s.get('tmdb_info') or {}
        ids = [str(x) for x in (s.get('series_ids') or [s.get('id')]) if x]
        if any(x in done for x in ids) and ti.get('match_status') in ('missing', 'ongoing'):
            ti = dict(ti)
            ti['match_status'] = 'aligned'
            ti['diff'] = 0
            s['tmdb_info'] = ti
            s['_md'] = True
    stats = {'total_series': len(series), 'total_movies': len(movies),
             'aligned': 0, 'missing': 0, 'extra': 0, 'ongoing': 0, 'unmatched': 0, 'no_tmdb': 0,
             'complete_series': 0, 'incomplete_series': 0}
    for s in series:
        st = (s.get('tmdb_info') or {}).get('match_status', 'unmatched')
        if st in stats: stats[st] += 1
        if s.get('complete') or st == 'aligned': stats['complete_series'] += 1
        else: stats['incomplete_series'] += 1
    result = {'status': 'success', 'stats': stats, 'series': series, 'movies': movies,
              'tmdb_errors': tmdb_errors, 'emby_host': _eng().EMBY_HOST}
    if with_tmdb:
        try:
            save_emby_lib_cache(result)
            save_library_snapshot(build_library_health_snapshot())
        except Exception as e:
            log.warning('保存统一片库对照缓存失败: %s', e)
    return result

def build_morning_report(items: list, force_refresh: bool = False, cache_only: bool = False) -> str:
    now = datetime.datetime.fromtimestamp(time.time())
    lines = [_eng().tg_title('☀️', 'StrimKeep 晨报', f'{now:%Y-%m-%d} 周{_eng()._WEEK[now.weekday()]}')]
    snap = daily_consistency_snapshot(force_refresh=force_refresh)
    lines.append(f"🧭 <i>统一快照 · 规则 {html.escape(str(snap.get('rule_sig') or ''))}</i>")

    if 'stats' in items:
        try:
            st = (snap.get('ingest') or {}).get('stats') or {}
            lines += ['', '📊 <b>近 24 小时入库</b>',
                      f"🎬 电影　<b>+{st.get('movies', 0)}</b> 部",
                      f"📺 剧集　<b>+{st.get('series', 0)}</b> 部 · <b>+{st.get('episodes', 0)}</b> 集"]
            # 折叠明细（对齐 Web 入库汇报）：最近入库置顶，TG 里点开即看，不用回 Web
            ing = _eng().read_ingest_cache() or {}
            tree = ing.get('tree') or {}
            tvd = tree.get('tv_detail') or {}
            movd = tree.get('mov_detail') or {}
            rows = []
            src_short = {'本地影视库': '本地', '分享影视库': '分享', '其它库': '其它'}
            for src, cats in tvd.items():
                for cat, shows in cats.items():
                    for name, info in shows.items():
                        rows.append((float(info.get('last_ts') or 0), src, cat, name,
                                     int(info.get('count') or 0)))
            for src, cats in movd.items():
                for cat, names in cats.items():
                    for name, ts in names.items():
                        rows.append((float(ts or 0), src, cat, name, 0))
            rows.sort(key=lambda x: -x[0])
            if rows:
                det = [f"• [{src_short.get(src, src)}·{html.escape(str(cat))}]《{html.escape(str(name))}》"
                       + (f" +{cnt}集" if cnt else '')
                       for _ts, src, cat, name, cnt in rows[:_eng().MORNING_INGEST_TOP]]
                lines.append('<blockquote expandable>' + '\n'.join(det) + '</blockquote>')
                if len(rows) > len(det):
                    lines.append(f'<i>…另有 {len(rows) - len(det)} 条，完整清单见 Web「每日简报」</i>')
        except Exception as e:
            lines += ['', f'📊 入库统计失败: {html.escape(str(e))}']

    if 'subscriptions' in items:
        try:
            # 晨报只引用当天「追更订阅」真正成功推送的汇报；不重新执行订阅检查。
            rep = _eng()._load_subscription_report(today_only=True)
            ups = (rep or {}).get('updates') or []
            lines += ['', f"🔔 <b>订阅更新</b>　<i>{len(ups)} 部</i>"]
            if ups:
                for u in ups[:10]:
                    name = html.escape(str(u.get('name') or ''))
                    if u.get('refilled'):
                        lines.append(f"✅ 《{name}》已补齐 <b>{_eng()._fmt_ep_ranges(_eng()._keys_to_eps(u['refilled']))}</b>")
                    if u.get('new_eps'):
                        lines.append(f"📺 《{name}》新增 <b>{_eng()._fmt_ep_ranges(_eng()._keys_to_eps(u['new_eps']))}</b>")
                    if u.get('newly_missing'):
                        lines.append(f"⚠️ 《{name}》缺集 <b>{_eng()._fmt_ep_ranges(_eng()._keys_to_eps(u['newly_missing']))}</b>")
                if len(ups) > 10:
                    lines.append(f'<i>…另有 {len(ups) - 10} 部有变化</i>')
            else:
                lines.append('今日尚无成功推送的订阅汇报')
            check = _eng().load_subscription_check_status() or {}
            if check.get('status') in ('error', 'partial'):
                lines.append(f"⚠️ 最近检查失败/不完整：{html.escape(str(check.get('error') or '请查看逐部原因'))}")
        except Exception as e:
            lines += ['', f'🔔 订阅汇报读取失败: {html.escape(str(e))}']

    if 'emby_gap' in items:
        try:
            # 手动「现场扫」必须绕过缺集缓存；定时晨报继续复用 30 分钟缓存。
            rep = gap_report(max_age=None, force_refresh=force_refresh, cache_only=(cache_only or not force_refresh))
            if rep.get('status') == 'error':
                raise RuntimeError(rep.get('message'))
            if rep.get('status') == 'empty':
                lines += ['', f"🧩 <b>Emby 缺集</b>　<i>{html.escape(str(rep.get('message') or '暂无缓存'))}</i>"]
                return '\n'.join(lines)

            def _diff(x):
                return abs((x.get('tmdb_info') or {}).get('diff') or 0)
            broken = sorted(rep['missing'], key=_diff, reverse=True)
            total_gap = sum(_diff(x) for x in broken)
            lines += ['', f"🧩 <b>Emby 缺集</b>　<i>{len(broken)} 部 · 共缺 {total_gap} 集</i>"]
            lines.append(f"<i>片库事实版本 {html.escape(str(rep.get('facts_version') or '未知'))} · "
                         + ('TMDB 对照已过期，以上为缓存结果' if rep.get('stale') else '当前缓存对照') + '</i>')
            if broken:
                shown = broken[:_eng().MORNING_GAP_TOP]
                # 折叠引用：默认只露前几行（缺得最多的排最前），点一下展开，再点收起
                lines.append('<blockquote expandable>' + '\n'.join(
                    f"• 《{html.escape(str(x.get('name') or ''))}》缺 <b>{_diff(x)}</b> 集" for x in shown)
                    + '</blockquote>')
                if len(broken) > len(shown):
                    lines.append(f'<i>…另有 {len(broken) - len(shown)} 部，完整清单见 Web「片库映射」</i>')
            else:
                if (rep.get('stats') or {}).get('unavailable'):
                    lines.append(f"⚠️ {rep['stats']['unavailable']} 部暂无有效 TMDB 对照，不能判定是否缺集")
                lines.append('缓存中未发现已确认缺集' + ('（对照已过期）' if rep.get('stale') else ''))
        except Exception as e:
            lines += ['', f'🧩 Emby 检查失败: {html.escape(str(e))}']

    return '\n'.join(lines)

def send_morning_report(items: list, force_refresh: bool = False, mark_sent: bool = True) -> bool:
    """发送晨报。

    force_refresh=True：现场刷新（仅现场预览/显式调用使用）。
    mark_sent=True：记为当天定时晨报已发送；手动测试发送应传 False，
    避免一次手动测试把当天 08:00 的定时晨报标记掉。
    """
    text = build_morning_report(items, force_refresh=force_refresh)
    ok = _eng().notify_telegram(text)
    if ok and mark_sent:
        _cfg.mark_morning_report_sent(time.strftime('%Y-%m-%d', time.localtime()))
    return ok

def _live_series_episodes(series_id: str):
    """某剧当前「真实存在」的分集：Emby 返回的分集里，路径能映射到容器内且文件已不在磁盘上的，
    视为 Emby 尚未清理的残留，直接剔除。删除后 Emby 的刷新是异步的，
    如果直接信 Emby，刚删完的剧集还会被当成\"仍在库里\"，海报就不会消失。
    路径映射不上的（其它库）无法核实，保留。"""
    items = (_eng().emby_request('/Items', {
        'ParentId': series_id, 'Recursive': 'true',
        'IncludeItemTypes': 'Episode',
        'Fields': 'Path,ParentIndexNumber,IndexNumber', 'Limit': 5000,
    }) or {}).get('Items') or []
    live = []
    for e in items:
        conv = _eng().emby_path_to_container(e.get('Path') or '')
        if conv is not None:
            try:
                if not conv.exists():
                    continue
            except OSError:
                pass
        live.append(e)
    return live

def _resync_series_entry(entry: dict, live: list):
    """用真实存在的分集重算缓存里这部剧的分集统计，并按原 TMDB 数据重新判定缺集。"""
    lib_of = _eng().EMBY_PATHS.lib_of
    season_map, local_set, share_set = {}, set(), set()
    for ep in live:
        sn, en = ep.get('ParentIndexNumber'), ep.get('IndexNumber')
        if sn is None or en is None:
            continue
        season_map.setdefault(sn, set()).add(en)
        lib = lib_of(ep.get('Path') or '')
        if lib == 'local':
            local_set.add((sn, en))
        elif lib == 'share':
            share_set.add((sn, en))
    seasons, total_missing = [], 0
    for sn in sorted(season_map):
        eps_set = sorted(season_map[sn])
        lo, hi = eps_set[0], eps_set[-1]
        miss = sorted(set(range(1, lo)) | (set(range(lo, hi + 1)) - set(eps_set)))
        total_missing += len(miss)
        seasons.append({'season': sn, 'episodes': len(eps_set), 'max_ep': hi,
                        'missing': miss, 'complete': not miss,
                        'local_eps': sum(1 for ss, _ in local_set if ss == sn),
                        'share_eps': sum(1 for ss, _ in share_set if ss == sn)})
    entry.update({
        'in_local': bool(local_set), 'in_share': bool(share_set),
        'seasons': seasons, 'total_seasons': len(seasons),
        'total_episodes': sum(x['episodes'] for x in seasons),
        'local_eps': len(local_set), 'share_eps': len(share_set),
        'have_eps': len(local_set | share_set),
        'missing_eps': total_missing,
        'complete': total_missing == 0 and len(seasons) > 0,
    })
    old = entry.get('tmdb_info')
    if old:
        if old.get('tmdb_total'):
            fake = {'status': old.get('tmdb_status') or '',
                    'seasons': [{'season_number': se['season'], 'episode_count': se['tmdb']}
                                for se in (old.get('seasons') or []) if se.get('tmdb')]}
            updated = _eng().classify_series_by_tmdb(seasons, fake)
            # 局部集数更新沿用原对照的标称总数/海报，不把已播总数当标称总数。
            entry['tmdb_info'] = dict(old, **updated)
            if old.get('declared_total') is not None:
                entry['tmdb_info']['declared_total'] = old['declared_total']
        else:
            old['local_total'] = entry['total_episodes']

def _patch_all_caches(patch):
    """对 内存 / 总览磁盘 / TMDB 对照磁盘 三份缓存各调用一次 patch(data)->bool，改动了就落盘（保留原时间戳）。"""
    if _eng()._emby_lib_cache.get('data'):
        patch(_eng()._emby_lib_cache['data'])
    disk = _load_overview_disk()
    if disk and patch(disk['data']):
        _save_overview_disk(disk['data'])
    tmdb_cache = read_emby_lib_cache()
    if tmdb_cache and patch(tmdb_cache):
        save_emby_lib_cache(tmdb_cache, keep_ts=True)


def _recompute_mapping_stats(data: dict):
    """根据当前 series 的 tmdb_info 重算片库映射统计，避免就地刷新后统计仍停留在旧快照。"""
    series = (data or {}).get('series') or []
    stats = (data or {}).setdefault('stats', {})
    stats.update({
        'total_series': len(series),
        'aligned': 0, 'missing': 0, 'extra': 0, 'ongoing': 0,
        'unmatched': 0, 'no_tmdb': 0, 'complete_series': 0,
        'incomplete_series': 0,
    })
    for s in series:
        st = (s.get('tmdb_info') or {}).get('match_status', 'unmatched')
        if st in stats:
            stats[st] += 1
        if s.get('complete') or st == 'aligned':
            stats['complete_series'] += 1
        else:
            stats['incomplete_series'] += 1


def refresh_mapping_cache_after_ingest(ingest_data: dict) -> dict:
    """入库轮询成功后，只刷新本轮确实发生入库的剧集。

    不重新跑全量 TMDB 对照，也不重新搜索身份；沿用片库映射缓存里的
    series_ids / tmdb_id，只重新读取这些剧当前真实存在的分集并重算 tmdb_info。
    这样补齐缺集后，片库映射的卡片状态可在下一轮 5 分钟入库扫描后自动变为
    「对齐」，无需手工点击「重新对照」。
    """
    result = {'status': 'skipped', 'affected': 0, 'updated': 0}
    if not ingest_data or not ingest_data.get('ok', True):
        return result

    raw = ingest_data.get('episodes_raw') or []
    affected_ids = {
        str(e.get('series_id') or '').strip()
        for e in raw
        if str(e.get('series_id') or '').strip()
    }
    if not affected_ids:
        return result

    cache = read_emby_lib_cache(max_age=None)
    if not cache:
        return result

    updated_entries = []
    for entry in (cache.get('series') or []):
        ids = {str(x).strip() for x in (entry.get('series_ids') or [entry.get('id')]) if str(x).strip()}
        if not (ids & affected_ids):
            continue
        result['affected'] += 1

        # 双库同一剧可能有两个 Emby SeriesId，必须 union 两边的实时分集。
        live = []
        seen = set()
        for sid in ids:
            for ep in _live_series_episodes(sid):
                key = (str(ep.get('Id') or ''), str(ep.get('Path') or ''),
                       ep.get('ParentIndexNumber'), ep.get('IndexNumber'))
                if key in seen:
                    continue
                seen.add(key)
                live.append(ep)

        before = (
            int(entry.get('have_eps') or 0),
            int((entry.get('tmdb_info') or {}).get('diff') or 0),
            (entry.get('tmdb_info') or {}).get('match_status'),
        )
        _resync_series_entry(entry, live)
        after = (
            int(entry.get('have_eps') or 0),
            int((entry.get('tmdb_info') or {}).get('diff') or 0),
            (entry.get('tmdb_info') or {}).get('match_status'),
        )
        if before != after:
            result['updated'] += 1
            updated_entries.append((ids, copy.deepcopy(entry)))

    if not result['affected']:
        return result
    if not result['updated']:
        return {'status': 'checked', 'affected': result['affected'], 'updated': 0}

    _recompute_mapping_stats(cache)
    save_emby_lib_cache(cache, keep_ts=True)

    # 将已经算好的条目同步到内存/总览/TMDB 对照缓存；这里不再重新访问 Emby。
    def _patch(data):
        touched = False
        for src in (data or {}).get('series') or []:
            src_ids = {str(x).strip() for x in (src.get('series_ids') or [src.get('id')]) if str(x).strip()}
            for ids, fresh in updated_entries:
                if src_ids & ids:
                    src.clear()
                    src.update(copy.deepcopy(fresh))
                    touched = True
                    break
        if touched:
            _recompute_mapping_stats(data)
        return touched

    _patch_all_caches(_patch)

    # 探索/总览还有一层 library_snapshot，必须一起失效/更新，否则会继续显示旧缺集。
    snap = load_library_snapshot(max_age=None, background_refresh=False)
    if snap and _patch(snap):
        snap['facts_ts'] = time.time()
        save_library_snapshot(_health_snapshot(snap))

    result['status'] = 'updated'
    return result


def patch_emby_lib_cache_after_series_delete(series_id: str, deleted_target: str = ''):
    """删除 / 同步后就地修补缓存里这部剧（以磁盘真实文件为准）。
    返回 {'remaining': 剩余分集数, 'entry': 新条目或 None, 'series_path': Emby 侧剧目录}。"""
    live = _live_series_episodes(series_id)
    info = {'remaining': len(live), 'entry': None, 'series_path': ''}

    def patch(data):
        series_list = (data or {}).get('series') or []
        idx = next((i for i, x in enumerate(series_list) if x.get('id') == series_id), None)
        if idx is None:
            return False
        info['series_path'] = series_list[idx].get('path') or info['series_path']
        if not live:
            series_list.pop(idx)
        else:
            _resync_series_entry(series_list[idx], live)
            info['entry'] = series_list[idx]
        data['series'] = series_list
        if isinstance(data.get('stats'), dict):
            data['stats']['total_series'] = len(series_list)
        return True

    _patch_all_caches(patch)
    return info

def patch_emby_lib_cache_after_movie_delete(tmdb_id: str, target: str):
    """按 tmdb_id 删电影后，把缓存里「位于被删库」的电影条目移除。返回移除数。"""
    flag = 'in_local' if target == 'local' else 'in_share'
    removed = [0]

    def patch(data):
        movies = (data or {}).get('movies') or []
        keep = [m for m in movies if not (str(m.get('tmdb_id') or '') == str(tmdb_id) and m.get(flag))]
        if len(keep) == len(movies):
            return False
        removed[0] = max(removed[0], len(movies) - len(keep))
        data['movies'] = keep
        if isinstance(data.get('stats'), dict):
            data['stats']['total_movies'] = len(keep)
        return True

    _patch_all_caches(patch)
    return removed[0]

def save_emby_lib_cache(data: dict, keep_ts: bool = False):
    _eng().STATE_DIR.mkdir(parents=True, exist_ok=True)
    data = dict(data)
    if not (keep_ts and data.get('ts')):
        data['ts'] = time.time()
    data['facts_ts'] = time.time()
    _state.save(_eng().EMBY_LIB_CACHE_FILE, data)

def read_emby_lib_cache(max_age=None):
    try:
        data = _state.read(_eng().EMBY_LIB_CACHE_FILE)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if max_age is not None and time.time() - data.get('ts', 0) > max_age:
        return None
    return data

def get_tmdb_scan_progress() -> dict:
    p = dict(_eng()._tmdb_scan_progress)
    if p['running'] and p['started_at']:
        p['elapsed_sec'] = int(time.time() - p['started_at'])
        p['eta_sec'] = int(p['elapsed_sec'] / p['done'] * (p['total'] - p['done'])) if p['done'] > 0 else None
        p['percent'] = int(p['done'] / p['total'] * 100) if p['total'] else 0
    else:
        p['elapsed_sec'] = (p['finished_at'] - p['started_at']) if p['finished_at'] and p['started_at'] else 0
        p['eta_sec'] = None
        p['percent'] = 100 if p['finished_at'] else 0
    return p

def refresh_tmdb_scan(background=False):
    """后台跑一次完整 TMDB 对照并落盘（带进度上报）。单飞：网页「强制对照」和定时预热
    可能前后脚触发，已在跑时直接返回，不再并发两份全量对照。"""
    if not _eng()._tmdb_scan_lock.acquire(blocking=False):
        return {'status': 'running', 'message': 'TMDB 对照已在后台运行'}
    # 请求返回前预留任务并清除旧错误，不能等线程获得调度后才更新进度。
    _eng()._tmdb_scan_progress.update({
        'running': True, 'total': 0, 'done': 0,
        'stage': '拉取 Emby 库...',
        'started_at': time.time(), 'finished_at': 0,
        'stats': {}, 'error': '',
    })

    def run():
        try:
            return _refresh_tmdb_scan()
        finally:
            _eng()._tmdb_scan_lock.release()

    if not background:
        return run()
    try:
        threading.Thread(target=run, daemon=True, name='tmdb-scan').start()
    except Exception as e:
        _eng()._tmdb_scan_progress.update({'running': False, 'finished_at': time.time(),
                                         'stage': '启动失败', 'error': str(e)})
        _eng()._tmdb_scan_lock.release()
        log.exception('TMDB 对照启动失败')
        return {'status': 'error', 'message': str(e)}
    return {'status': 'started', 'message': 'TMDB 对照已在后台启动'}

def _refresh_tmdb_scan():
    log.info('TMDB 对照开始（后台）')
    try:
        data = emby_library_overview(force=True)
        series = data['series']
        movies = data['movies']
        total = len(series)
        _eng()._tmdb_scan_progress['total'] = total
        _eng()._tmdb_scan_progress['stage'] = f'对照 TMDB（共 {total} 部）...'
        t = _eng().Tmdb()
        has_key = bool(t.key)
        reqs = [('tv', s_.get('tmdb_id')) for s_ in series if s_.get('tmdb_id')]
        infos, tmdb_errors, cache_rows = _tmdb_bulk_get(t, reqs, max_workers=8) if has_key else ({}, 0, {})
        for i, s_ in enumerate(series):
            tmdb_id = s_.get('tmdb_id')
            if not tmdb_id:
                s_['tmdb_info'] = {'match_status': 'no_tmdb' if has_key else 'unmatched',
                                   'tmdb_status': None,
                                   'local_total': s_.get('total_episodes', 0),
                                   'tmdb_total': None, 'diff': None, 'seasons': []}
            else:
                info = infos.get(('tv', str(tmdb_id)))
                s_['tmdb_info'] = _eng().classify_series_by_tmdb(s_.get('seasons', []), info)
                if info and info.get('poster_path'):
                    s_['tmdb_info']['poster'] = info.get('poster_path')
            _eng()._tmdb_scan_progress['done'] = i + 1
            if (i + 1) % 20 == 0 or (i + 1) == total:
                el = time.time() - _eng()._tmdb_scan_progress['started_at']
                eta = el / (i + 1) * (total - i - 1)
                log.info('TMDB 进度 %d/%d (%.0f%%) ETA %.0f 秒', i + 1, total,
                         (i + 1) / total * 100 if total else 100, eta)
        t.cache.update(cache_rows)
        t.save()
        stats = {'total_series': len(series), 'total_movies': len(movies),
                 'aligned': 0, 'missing': 0, 'extra': 0, 'ongoing': 0,
                 'unmatched': 0, 'no_tmdb': 0, 'pending': 0,
                 'complete_series': 0, 'incomplete_series': 0}
        for s_ in series:
            st = (s_.get('tmdb_info') or {}).get('match_status', 'unmatched')
            if st in stats: stats[st] += 1
            if s_.get('complete'): stats['complete_series'] += 1
            else: stats['incomplete_series'] += 1
        res = {'status': 'success', 'stats': stats, 'series': series, 'movies': movies,
               'tmdb_errors': tmdb_errors, 'with_tmdb': True, 'emby_host': _eng().EMBY_HOST}
        save_emby_lib_cache(res)
        save_library_snapshot(build_library_health_snapshot())
        cfg = _cfg.load_config()
        cfg['tmdb_scan_last_ts'] = str(time.time())
        _cfg.save_config(cfg)
        _eng()._tmdb_scan_progress.update({'running': False, 'finished_at': time.time(),
                                    'stage': '完成', 'stats': stats})
        log.info('TMDB 对照完成：对齐 %d / 缺集 %d / 超集 %d / 在更 %d',
                 stats['aligned'], stats['missing'], stats['extra'], stats['ongoing'])
        return res
    except Exception as e:
        log.exception('TMDB 对照失败')
        _eng()._tmdb_scan_progress.update({'running': False, 'finished_at': time.time(),
                                    'stage': '失败', 'error': str(e)})
        return {'status': 'error', 'message': str(e)}

def scan_exempt_matches():
    """扫描双库，返回命中白名单的条目。
    口径与 build_plan 一致：剧名、所在目录、文件路径任一命中都算；
    同一个命中目录（如「百家讲坛」）下的多个 Season 目录聚合成一条。"""
    kws = _eng()._exempt_keywords()
    if not kws:
        return []
    results = {}

    def hit_of(disp, files):
        h = _eng()._exempt_hit(disp)
        if h:
            return h
        for f in files:
            h = _eng()._exempt_hit(str(f))
            if h:
                return h
        return []

    for root, lib_name in ((_eng().L_ROOT, '本地'), (_eng().S_ROOT, '分享')):
        if not root.exists():
            continue
        lib = _eng()._get_lib(root)
        entries = []  # (key, [files], 季号或None)
        for key, files in lib.mov.items():
            entries.append((key, files, None))
        for key, seasons in lib.tv.items():
            for sn, files in seasons.items():
                entries.append((key, files, sn))
        for key, files, sn in entries:
            disp = lib.meta[key][0]
            hits = hit_of(disp, files)
            if not hits:
                continue
            gname = None
            for f in files[:200]:
                gname, _k = _eng()._exempt_group_of(f, kws)
                if gname:
                    break
            gname = gname or disp
            r = results.setdefault(gname, {
                'title': gname, 'keyword': hits[0], 'libs': [],
                'members': set(), 'seasons': set(), 'strm': 0})
            if lib_name not in r['libs']:
                r['libs'].append(lib_name)
            r['members'].add(disp)
            if sn is not None:
                r['seasons'].add(sn)
            r['strm'] += len(files)
    out = []
    for r in results.values():
        members = sorted(r['members'], key=_eng()._nat_key)
        out.append({
            'title': r['title'], 'keyword': r['keyword'], 'libs': r['libs'],
            # 只有单个节目时季号才有意义；多个 Season 目录时用 members 展示
            'seasons': sorted(r['seasons']) if len(members) == 1 else [],
            'member_count': len(members), 'members': members[:300],
            'strm_count': r['strm'],
        })
    out.sort(key=lambda x: _eng()._nat_key(x['title']))
    return out
