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


def _fetch_ingest(hours=24):
    """实际拉取 Emby 近期入库。返回里 `ok=False` 表示至少一项拉取失败（结果不完整，不应覆盖好缓存）。"""
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)
    min_date = cutoff.strftime('%Y-%m-%dT%H:%M:%S.0000000Z')
    tv_tree = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    # 详情树：在原有“剧名 + 集数”统计之外保留季/集信息，供 Web 入库汇报展示。
    # 不改变 tv_tree 的旧结构，避免兼容 Telegram / 旧前端。
    tv_detail = defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: {'count': 0, 'seasons': defaultdict(lambda: {'count': 0, 'episodes': []})})))
    mov_tree = defaultdict(lambda: defaultdict(list))
    mov_detail = defaultdict(lambda: defaultdict(dict))
    movies_raw = []
    episodes_raw = []
    errors = []

    base = {'Recursive': 'true', 'SortBy': 'DateCreated', 'SortOrder': 'Descending', 'MinDateCreated': min_date}
    try:
        for m in _eng()._paged_items(dict(base, IncludeItemTypes='Movie', Fields='DateCreated,Path,Genres,ProviderIds,ProductionYear,Name')):
            dt = _eng().parse_dt(m.get('DateCreated'))
            if not dt or dt < cutoff: continue
            movies_raw.append(m)
            n = m.get('Name')
            bucket = mov_tree[_eng()._src(m.get('Path', ''))][parse_emby_library(m, True)]
            if n and n not in bucket: bucket.append(n)
            if n:
                try:
                    _mts = dt.timestamp()
                except Exception:
                    _mts = 0.0
                _md = mov_detail[_eng()._src(m.get('Path', ''))][parse_emby_library(m, True)]
                if _mts > (_md.get(n) or 0):
                    _md[n] = _mts
    except Exception as e:
        log.warning('入库电影拉取失败: %s', e)
        errors.append('电影: %s' % e)

    try:
        for e in _eng()._paged_items(dict(base, IncludeItemTypes='Episode', Fields='DateCreated,Path,SeriesName,Genres,ProviderIds,SeriesId,ParentIndexNumber,IndexNumber,ProductionYear,Name')):
            dt = _eng().parse_dt(e.get('DateCreated'))
            if not dt or dt < cutoff: continue
            episodes_raw.append(e)
            src = _eng()._src(e.get('Path', ''))
            cat = parse_emby_library(e, False)
            title = e.get('SeriesName') or '未知剧集'
            tv_tree[src][cat][title] += 1
            d = tv_detail[src][cat][title]
            d['count'] += 1
            # 记录该剧最新一集的入库时间（供「最近入库置顶」排序 / 晨报明细用）
            try:
                _ts = dt.timestamp()
            except Exception:
                _ts = 0.0
            if _ts > (d.get('last_ts') or 0):
                d['last_ts'] = _ts
            try:
                season = int(e.get('ParentIndexNumber'))
            except (TypeError, ValueError):
                season = None
            try:
                episode = int(e.get('IndexNumber'))
            except (TypeError, ValueError):
                episode = None
            sk = str(season) if season is not None and season >= 0 else 'unknown'
            sd = d['seasons'][sk]
            sd['count'] += 1
            if episode is not None and episode > 0:
                sd['episodes'].append(episode)
    except Exception as e:
        log.warning('入库剧集拉取失败: %s', e)
        errors.append('剧集: %s' % e)

    # 统计口径：按媒体身份去重。双库同一 TMDB 电影只算 1 部；剧集按
    # 规范化剧名+年份识别跨库同一剧，再按 (season, episode) union。
    def _ingest_series_key(e):
        name = _eng()._normalize_title(str(e.get('SeriesName') or e.get('Name') or '')).casefold()
        year = str(e.get('ProductionYear') or '')
        # Episode 的 ProviderIds 常常是 episode 自身 ID，不能拿它当 Series ID。
        return f"name:{name}|year:{year}" if name else f"sid:{e.get('SeriesId') or ''}"

    movie_ids, movie_fallback = set(), set()
    series_ids, episode_ids = set(), set()
    for m in movies_raw:
        tid = str((m.get('ProviderIds') or {}).get('Tmdb') or '')
        if tid: movie_ids.add(tid)
        else: movie_fallback.add((_eng()._normalize_title(str(m.get('Name') or '')).casefold(), str(m.get('ProductionYear') or '')))
    for e in episodes_raw:
        sid = _ingest_series_key(e)
        if sid: series_ids.add(sid)
        try:
            pair = (int(e.get('ParentIndexNumber')), int(e.get('IndexNumber')))
        except (TypeError, ValueError):
            pair = None
        if pair and pair[0] >= 0 and pair[1] > 0 and sid:
            episode_ids.add((sid, pair))
        elif sid:
            # 旧版/测试数据没有季集字段时，退回资源路径去重，避免把统计变成 0。
            episode_ids.add((sid, 'item:' + str(e.get('Path') or e.get('Id') or e.get('Name') or '')))
    total_mov = len(movie_ids) + len(movie_fallback)
    total_series = len(series_ids)
    total_eps = len(episode_ids)

    # 保留原始分类树供详情展示，但 stats 使用身份去重后的数字。
    return {
        'ts': time.time(),
        'hours': hours,
        'ok': not errors,
        'error': '；'.join(errors),
        'stats': {'movies': total_mov, 'series': total_series, 'episodes': total_eps},
        'tree': {
            'tv': {k: {c: dict(s) for c, s in v.items()} for k, v in tv_tree.items()},
            'tv_detail': {
                src: {cat: {title: {
                    'count': int(info.get('count') or 0),
                    'last_ts': float(info.get('last_ts') or 0),
                    'seasons': {sk: {
                        'count': int(sd.get('count') or 0),
                        'episodes': sorted(set(int(x) for x in (sd.get('episodes') or []) if isinstance(x, int) or str(x).isdigit()))
                    } for sk, sd in info.get('seasons', {}).items()}
                } for title, info in shows.items()} for cat, shows in cats.items()} for src, cats in tv_detail.items()
            },
            'mov': {k: {c: list(ns) for c, ns in v.items()} for k, v in mov_tree.items()},
            'mov_detail': {k: {c: dict(v2) for c, v2 in v.items()} for k, v in mov_detail.items()},
        },
        'movies_raw': [{'name': m.get('Name'), 'path': m.get('Path',''), 'created': m.get('DateCreated')} for m in movies_raw[:200]],
        'episodes_raw': [{'name': e.get('Name'), 'series': e.get('SeriesName'), 'series_id': e.get('SeriesId'), 'season': e.get('ParentIndexNumber'), 'episode': e.get('IndexNumber'), 'path': e.get('Path',''), 'created': e.get('DateCreated')} for e in episodes_raw[:500]],
    }

def refresh_ingest_cache(hours=24) -> dict:
    """立即拉取并覆盖缓存。拉取失败（超时等）时保留上一份完整缓存，只附上失败信息，
    避免把「+0 部 / +0 集」的残缺结果写成最新数据。"""
    log.info('入库缓存刷新开始（%sh）', hours)
    data = _eng()._fetch_ingest(hours=hours)
    if not data.get('ok', True):
        old = read_ingest_cache()
        if old and old.get('ok', True):
            old['stale_error'] = data.get('error', '')
            old['stale_error_ts'] = time.time()
            old['from_cache'] = True
            _write_ingest_cache(old)
            log.warning('入库缓存刷新失败，保留旧缓存（%d 秒前）: %s',
                        int(time.time() - old.get('ts', 0)), data.get('error'))
            return old
        # 没有旧缓存可退：仍把失败结果落盘，但带 ok=False，界面据此提示而不是当成「0 新增」
    _write_ingest_cache(data)
    # 本轮确实有新入库时，只就地刷新受影响剧集的片库映射缓存。
    # 不触发全量 TMDB 对照，避免为了「补齐缺集」重新扫整个片库。
    if data.get('ok', True):
        try:
            _eng().refresh_mapping_cache_after_ingest(data)
        except Exception as e:
            log.warning('入库后片库映射缓存刷新失败: %s', e)
    st = data.get('stats', {})
    if not data.get('ok', True):
        log.warning('入库缓存刷新失败且无旧缓存可保留: %s', data.get('error'))
        return data
    log.info('入库缓存刷新完成：电影 %d / 剧集 %d 部 / 集 %d',
             st.get('movies', 0), st.get('series', 0), st.get('episodes', 0))
    return data

def _write_ingest_cache(data):
    try:
        _eng().STATE_DIR.mkdir(parents=True, exist_ok=True)
        _state.save(_eng().INGEST_CACHE_FILE, data)
    except OSError as e:
        log.warning('入库缓存写入失败: %s', e)

def read_ingest_cache(max_age=None) -> dict:
    """读缓存；max_age 为 None 时不做时效判断，返回 None 表示无缓存"""
    try:
        data = _state.read(_eng().INGEST_CACHE_FILE)
    except (OSError, ValueError):
        return None
    if max_age is not None and time.time() - data.get('ts', 0) > max_age:
        return None
    return data

def get_ingest(hours=24, force_refresh=False) -> dict:
    """
    统一入口：
      force_refresh=True  → 立即拉最新
      force_refresh=False → 优先读缓存（10 分钟时效），过期则刷新
    """
    if force_refresh:
        return refresh_ingest_cache(hours=hours)
    cached = read_ingest_cache(max_age=600)
    if cached and cached.get('ok', True):
        cached['from_cache'] = True
        return cached
    data = refresh_ingest_cache(hours=hours)
    data['from_cache'] = False
    return data

def action_stats(args):
    """
    force_refresh kw: 'force' 时立即拉
    默认读缓存，无缓存刷新
    """
    kw = getattr(args, 'kw', '') or ''
    force = kw == 'force' or 'force' in kw
    full = 'full' in kw
    data = get_ingest(force_refresh=force)
    st = data.get('stats') or {}
    warn = data.get('stale_error') or ('' if data.get('ok', True) else data.get('error', ''))
    if not full:
        return {'status': 'success', 'has_more': True,
                'stats': st, 'from_cache': data.get('from_cache', False),
                'cache_ts': data.get('ts', 0), 'ok': data.get('ok', True), 'warning': warn,
                'text': '\n'.join([
                    '📊 **近 24 小时入库速报**', '━━━━━━━━━━━━━━━━━━━',
                    f'🎬 单片/电影新增：`+{st.get("movies", 0)}` 部',
                    f'📺 连载/剧集新增：`+{st.get("series", 0)}` 部共 `+{st.get("episodes", 0)}` 集'])}
    tree = data.get('tree') or {}
    tv_tree = tree.get('tv') or {}
    mov_tree = tree.get('mov') or {}
    rep = [f'📊 **24小时入库清单** (单片 `+{st.get("movies", 0)}` | 连载 `+{st.get("series", 0)}` 部 `+{st.get("episodes", 0)}` 集)', '━━━━━━━━━━━━━━━━━━━']
    rep.append(f'\n📺 **【连载剧集明细】** (共 {st.get("series", 0)} 部)')
    if st.get('series'):
        for src in ('本地影视库', '分享影视库'):
            for cat in sorted(tv_tree.get(src, {})):
                shows = tv_tree[src][cat]
                lines = [f'《{esc(n)}》`+{c}集`' for n, c in sorted(shows.items(), key=lambda x: x[1], reverse=True)]
                rep.append(f'┌ 📂 **{src} · {cat}** ({len(shows)}部)')
                rep += ['│  • ' + '  • '.join(lines[i:i + 2]) for i in range(0, len(lines), 2)]
                rep.append('└')
    else: rep.append('  • 暂无新增剧集')
    rep.append(f'\n🎬 **【单片电影明细】** (共 {st.get("movies", 0)} 部)')
    if st.get('movies'):
        for src in ('本地影视库', '分享影视库'):
            for cat in sorted(mov_tree.get(src, {})):
                names = mov_tree[src][cat]
                rep.append(f'┌ 📂 **{src} · {cat}** ({len(names)}部)')
                tags = [f'《{esc(n)}》' for n in names]
                rep += ['│  • ' + '、'.join(tags[i:i + 3]) for i in range(0, len(tags), 3)]
                rep.append('└')
    else: rep.append('  • 暂无新增电影')
    if warn:
        rep.insert(1, f'⚠️ 本次扫描失败（{esc(warn)}），以下为旧数据')
    return {'status': 'success', 'text': '\n'.join(rep),
            'ok': data.get('ok', True), 'warning': warn,
            'from_cache': data.get('from_cache', False), 'cache_ts': data.get('ts', 0),
            'stats': st, 'tree': tree,
            'movies_raw': data.get('movies_raw', []),
            'episodes_raw': data.get('episodes_raw', []),
            'from_cache': data.get('from_cache', False),
            'cache_ts': data.get('ts', 0)}

def action_played(args):
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=24)
    newest = defaultdict(lambda: defaultdict(int))
    for ep in _eng()._recent('Episode', 'DateCreated,SeriesName,ParentIndexNumber,IndexNumber', 3000, cutoff):
        if ep.get('SeriesName') and ep.get('IndexNumber'):
            s = ep.get('ParentIndexNumber', 1)
            newest[ep['SeriesName']][s] = max(newest[ep['SeriesName']][s], ep['IndexNumber'])
    records, alerts = [], []
    def check(name, s_idx, e_idx, who, watched):
        top = newest.get(name, {}).get(s_idx, 0)
        if top > e_idx:
            verb = f'已看完 S{s_idx:02d}E{e_idx:02d}' if watched else f'看到 E{e_idx:02d}'
            a = {'user': who, 'series': name, 'watched': verb, 'new_ep': top,
                 'text': f'👤 {who} {verb}，《{name}》今日已入库 E{top:02d}'}
            if not any(x['user'] == who and x['series'] == name for x in alerts):
                alerts.append(a)
    for u in _eng().emby_request('/Users') or []:
        uid, who = u['Id'], u.get('Name', '用户')
        try:
            items = (_eng().emby_request(f'/Users/{uid}/Items', {'Recursive': 'true', 'Filters': 'IsResumable',
                                                          'SortBy': 'DatePlayed', 'SortOrder': 'Descending',
                                                          'Limit': 5}) or {}).get('Items', [])
            for it in items:
                name = it.get('SeriesName') or it.get('Name')
                ud = it.get('UserData', {})
                pct = max(1, min(99, int(ud.get('PlaybackPositionTicks', 0) / (it.get('RunTimeTicks') or 1) * 100)))
                if it.get('Type') == 'Movie':
                    records.append({'user': who, 'type': 'movie', 'name': name, 'pct': pct,
                                    'text': f'👤 {who} 正在看 🎬《{name}》 (进度 {pct}%)'})
                else:
                    s_idx, e_idx = it.get('ParentIndexNumber', 1), it.get('IndexNumber', 1)
                    records.append({'user': who, 'type': 'tv', 'name': name, 's': s_idx, 'e': e_idx, 'pct': pct,
                                    'text': f'👤 {who} 正在看 📺《{name}》S{s_idx:02d}E{e_idx:02d} (进度 {pct}%)'})
                    check(name, s_idx, e_idx, who, False)
            watched = (_eng().emby_request(f'/Users/{uid}/Items', {'Recursive': 'true', 'Filters': 'IsPlayed',
                                                            'SortBy': 'DatePlayed', 'SortOrder': 'Descending',
                                                            'Limit': 6, 'IncludeItemTypes': 'Episode'}) or {}).get('Items', [])
            for it in watched:
                if it.get('SeriesName'):
                    check(it['SeriesName'], it.get('ParentIndexNumber', 1), it.get('IndexNumber', 0), who, True)
        except Exception as e:
            log.warning('用户 %s 播放数据获取失败: %s', who, e)
    return {'status': 'success', 'records': records, 'alerts': alerts}

def action_search(args):
    tokens = [t for t in args.kw.casefold().split() if t]
    if not tokens:
        return {'status': 'success', 'text': '请输入片名关键词'}
    found = []
    for root, tag in ((_eng().L_ROOT, '本地影视库'), (_eng().S_ROOT, '分享影视库')):
        if not root.exists(): continue
        for dp, dns, fns in os.walk(root):
            dns[:] = [d for d in dns if parse_season_dir(d) is None]
            is_title = any(x.endswith('.strm') for x in fns) or any(parse_season_dir(d) is not None for d in os.listdir(dp) if os.path.isdir(os.path.join(dp, d)))
            base = os.path.basename(dp)
            if is_title and all(t in base.casefold() for t in tokens):
                p = Path(dp); rel = p.relative_to(root).parts
                m_type = rel[0] if rel else '影视库'; m_cat = rel[1] if len(rel) > 1 else '分类'
                icon = '🎬' if any(x in m_type for x in ('电影', '演唱会')) else '📺'
                found.append(f'{icon} **《{esc(p.name)}》**\n  ├ 📂 归属库: `{tag}`\n  ├ 🏷️ 分类: `{m_type} / {m_cat}`\n  └ 📍 路径: `{p}`')
    text = '\n\n'.join(found[:8]) if found else f'❌ 未在两库中检索到包含关键词《{esc(args.kw)}》的资源。'
    if len(found) > 8: text += f'\n\n… 共 {len(found)} 条，仅显示前 8 条，请补充关键词缩小范围'
    _eng().write_audit_log('模糊搜片', f'关键词: {args.kw}，命中 {len(found)} 条')
    return {'status': 'success', 'text': text}

def action_logs(args):
    n = int(args.kw) if args.kw.isdigit() else 35
    return {'status': 'success', 'text': logger.to_text(n)}
