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


# Event index is stored as one SQLite document, not a second JSON file.
# A 20-minute overlap avoids losing delayed writes near the last poll boundary;
# a six-hour authoritative pass corrects deletions/renames that DateCreated cannot detect.
_INGEST_OVERLAP = 20 * 60
_INGEST_RECONCILE = 6 * 3600
_ingest_refresh_lock = threading.RLock()
_INGEST_FIELDS = {
    'Movie': 'DateCreated,Path,Genres,ProviderIds,ProductionYear,Name',
    'Episode': 'DateCreated,Path,SeriesName,Genres,ProviderIds,SeriesId,ParentIndexNumber,IndexNumber,ProductionYear,Name',
}


def _ingest_index_path():
    return _eng().STATE_DIR / 'ingest_recent_items.json'


def _query_ingest_type(kind, since):
    min_date = since.strftime('%Y-%m-%dT%H:%M:%S.0000000Z')
    return _eng()._paged_items({
        'Recursive': 'true', 'SortBy': 'DateCreated', 'SortOrder': 'Descending',
        'MinDateCreated': min_date, 'IncludeItemTypes': kind,
        'Fields': _INGEST_FIELDS[kind]})


def _ingest_identity(item):
    # ItemId is stable through renames; Path is the conservative legacy fallback.
    return str(item.get('Id') or ('path:' + str(item.get('Path') or '')))


def _merge_recent(previous, incoming, cutoff):
    merged = {_ingest_identity(row): row for row in previous if _ingest_identity(row) != 'path:'}
    for row in incoming:
        ident = _ingest_identity(row)
        if ident != 'path:':
            merged[ident] = row
    rows = [( _eng().parse_dt(row.get('DateCreated')), row) for row in merged.values()]
    return [row for dt, row in sorted((v for v in rows if v[0] and v[0] >= cutoff),
                                       key=lambda item: item[0], reverse=True)]


def _fetch_ingest(hours=24, source_items=None):
    """实际拉取 Emby 近期入库。返回里 `ok=False` 表示至少一项拉取失败（结果不完整，不应覆盖好缓存）。"""
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)
    tv_tree = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    # 详情树：在原有“剧名 + 集数”统计之外保留季/集信息，供 Web 入库汇报展示。
    # 不改变 tv_tree 的旧结构，避免兼容 Telegram / 旧前端。
    tv_detail = defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: {'count': 0, 'seasons': defaultdict(lambda: {'count': 0, 'episodes': []})})))
    mov_tree = defaultdict(lambda: defaultdict(list))
    mov_detail = defaultdict(lambda: defaultdict(dict))
    movies_raw = []
    episodes_raw = []
    errors = []

    try:
        for m in (source_items['movies'] if source_items is not None else _query_ingest_type('Movie', cutoff)):
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
        for e in (source_items['episodes'] if source_items is not None else _query_ingest_type('Episode', cutoff)):
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
        '_recent_source': {'movies': movies_raw, 'episodes': episodes_raw},
    }

def refresh_ingest_cache(hours=24) -> dict:
    """持久化近期入库事实：常规只请求游标后 20 分钟重叠窗，六小时全量对账。

    保存成功前不移动游标；任何拉取失败都保留旧缓存和增量索引。
    """
    with _ingest_refresh_lock:
        now = time.time()
        index = _state.read(_ingest_index_path(), {})
        valid = (isinstance(index, dict) and index.get('host') == _eng().EMBY_HOST
                 and index.get('hours') == hours and 0 < now - float(index.get('ts') or 0) < 3600
                 and now - float(index.get('full_ts') or 0) < _INGEST_RECONCILE
                 and isinstance(index.get('movies'), list) and isinstance(index.get('episodes'), list))
        delta = None
        if valid:
            try:
                # Poll time, not max(DateCreated): Emby may import older timestamps late.
                since = datetime.datetime.fromtimestamp(
                    max(now - hours * 3600, float(index['ts']) - _INGEST_OVERLAP),
                    tz=datetime.timezone.utc)
                fresh_movies = _query_ingest_type('Movie', since)
                fresh_episodes = _query_ingest_type('Episode', since)
                cutoff = datetime.datetime.fromtimestamp(now - hours * 3600, tz=datetime.timezone.utc)
                delta = {'movies': _merge_recent(index['movies'], fresh_movies, cutoff),
                         'episodes': _merge_recent(index['episodes'], fresh_episodes, cutoff)}
                old_rows = {_ingest_identity(row): row for row in index['episodes']}
                changed = [row for row in fresh_episodes if old_rows.get(_ingest_identity(row)) != row]
                old_movies = {_ingest_identity(row): row for row in index['movies']}
                changed_movies = [row for row in fresh_movies if old_movies.get(_ingest_identity(row)) != row]
                log.info('入库增量拉取：电影 %d / 分集 %d；变化分集 %d',
                         len(fresh_movies), len(fresh_episodes), len(changed))
                data = _eng()._fetch_ingest(hours=hours, source_items=delta)
            except Exception as error:
                log.warning('入库增量拉取失败，保留旧缓存: %s', error)
                data = {'ts': time.time(), 'ok': False, 'error': str(error), 'stats': {}, 'tree': {}}
        else:
            log.info('入库完整时间窗对账开始（%sh）', hours)
            data = _eng()._fetch_ingest(hours=hours)
            changed = None
            changed_movies = None

        if not data.get('ok', True):
            old = read_ingest_cache()
            if old and old.get('ok', True):
                old['stale_error'] = data.get('error', '')
                old['stale_error_ts'] = time.time()
                old['from_cache'] = True
                _write_ingest_cache(old)
                log.warning('入库缓存刷新失败，保留旧缓存: %s', data.get('error'))
                return old
        rows = data.pop('_recent_source', None)
        if data.get('ok', True):
            if rows is not None:
                _state.save(_ingest_index_path(), {
                    'host': _eng().EMBY_HOST, 'hours': hours, 'ts': now,
                    'full_ts': float(index['full_ts']) if valid else now,
                    'movies': rows['movies'], 'episodes': rows['episodes']})
            # Avoid re-fetching the same affected series every five minutes.
            # On the authoritative pass, preserve the historical complete update path.
            mapping_data = data
            if changed is None and rows is not None:
                # The first authoritative pass must update ALL recently changed
                # series, not only the 500 detail rows exposed to the UI.
                mapping_data = dict(data, episodes_raw=[{
                    'series_id': e.get('SeriesId'), 'episode': e.get('IndexNumber'),
                    'season': e.get('ParentIndexNumber')} for e in rows['episodes']],
                    movies_raw=[{'id': m.get('Id')} for m in rows['movies']])
            if changed is not None:
                mapping_data = dict(data, episodes_raw=[{
                    'series_id': e.get('SeriesId'), 'episode': e.get('IndexNumber'),
                    'season': e.get('ParentIndexNumber')} for e in changed],
                    movies_raw=[{'id': m.get('Id')} for m in changed_movies])
        _write_ingest_cache(data)
        if data.get('ok', True):
            try:
                _eng().refresh_mapping_cache_after_ingest(mapping_data)
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
