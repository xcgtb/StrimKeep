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
import math
from contextvars import ContextVar
from email.utils import parsedate_to_datetime

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
    from . import network as _network
except ImportError:
    import config as _cfg
    import logger
    import network as _network


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


class TmdbError(Exception):
    pass

_cache_write_lock = threading.RLock()
TMDB_REQUEST_BUDGET = 45.0  # 单次查询（含重试与退避）的最长等待预算。
_subscription_tmdb_client = ContextVar('subscription_tmdb_client', default=None)
_explore_request_window = ContextVar('explore_request_window', default=None)



@contextlib.contextmanager
def subscription_tmdb_session():
    """每轮订阅惰性加载一次缓存，退出时只保存新增结果；不跨轮保留客户端。"""
    if _subscription_tmdb_client.get() is not None:
        yield
        return
    session = {}
    token = _subscription_tmdb_client.set(session)
    try:
        yield
    finally:
        _subscription_tmdb_client.reset(token)
        client = session.get('client')
        if client is not None and getattr(client, 'calls', 1):
            try:
                client.save()
            except Exception as error:
                log.warning('TMDB 订阅缓存写入失败: %s', error)


def _cache_stamp(path):
    return _state.stamp(path)


def _cache_time(row):
    try:
        value = float(row['ts'])
        return value if math.isfinite(value) else 0.0
    except (KeyError, TypeError, ValueError):
        return 0.0


class Tmdb:
    def __init__(self, cache=None):
        self.key = (_eng().RUNTIME_CFG.get('tmdb_key') or '').strip()
        self.cache_file = _eng().STATE_DIR / 'tmdb_cache.json'
        self.calls = self.hits = 0
        self._cache_stamp = None
        if cache is not None:
            # 批量查询 worker 只持有自己的新结果，不读取整份缓存。
            self.cache = cache
        else:
            with _cache_write_lock:
                try:
                    loaded = _state.read(self.cache_file)
                    self.cache = loaded if isinstance(loaded, dict) else {}
                except (OSError, ValueError):
                    self.cache = {}
                self._cache_stamp = _cache_stamp(self.cache_file)

    def new_worker(self):
        worker = Tmdb(cache={})
        worker.key = self.key
        worker.cache_file = self.cache_file
        return worker

    def get(self, path, ttl=6 * 3600, **params):
        params.setdefault('language', _eng().TMDB_LANG)
        ck = path + '?' + urllib.parse.urlencode(sorted(params.items()))
        hit = self.cache.get(ck)
        if isinstance(hit, dict) and 'data' in hit and _cache_time(hit) and time.time() - _cache_time(hit) < ttl:
            self.hits += 1
            return hit['data']
        headers = {}
        if self.key.startswith('eyJ'):
            headers['Authorization'] = f'Bearer {self.key}'
        else:
            params['api_key'] = self.key
        url = f'{_eng().TMDB_BASE}{path}?{urllib.parse.urlencode(params)}'
        data = None
        window = _explore_request_window.get()
        deadline = min(time.monotonic() + TMDB_REQUEST_BUDGET, window['deadline']) if window else time.monotonic() + TMDB_REQUEST_BUDGET
        attempts = window['attempts'] if window else 3
        socket_timeout = window['timeout'] if window else 15.0
        for attempt in range(attempts):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TmdbError('TMDB 查询超过本次等待预算')
            delay = 1.5
            try:
                with _network.open_external(urllib.request.Request(url, headers=headers), timeout=min(socket_timeout, remaining)) as r:
                    data = json.loads(r.read().decode('utf-8'))
                break
            except urllib.error.HTTPError as e:
                if e.code in (401, 403): raise TmdbError(f'TMDB Key 无效或无权限 (HTTP {e.code})')
                if e.code == 429:
                    try:
                        delay = max(0.0, float(e.headers.get('Retry-After', '2'))) + 1
                    except (TypeError, ValueError):
                        try:
                            retry_at = parsedate_to_datetime(e.headers.get('Retry-After', '')).timestamp()
                            delay = max(0.0, retry_at - time.time()) + 1
                        except (TypeError, ValueError, OverflowError, AttributeError):
                            delay = 3.0
                    if not math.isfinite(delay) or delay >= deadline - time.monotonic():
                        raise TmdbError('TMDB 限流等待超过本次查询预算，请稍后重试') from e
                    if attempt == attempts - 1:
                        raise TmdbError('TMDB 多次限流，请稍后重试') from e
                elif e.code == 404:
                    return None
                elif attempt == attempts - 1:
                    raise TmdbError(f'TMDB 返回 HTTP {e.code}') from e
            except (urllib.error.URLError, OSError, ValueError) as e:
                if attempt == attempts - 1: raise TmdbError(f'无法访问 TMDB: {e}') from e
            if delay >= deadline - time.monotonic():
                raise TmdbError('TMDB 查询超过本次等待预算')
            time.sleep(delay)
        if data is None: raise TmdbError('TMDB 多次请求失败')
        self.cache[ck] = {'ts': time.time(), 'data': data}
        self.calls += 1
        time.sleep(0.03)
        return data

    def save(self):
        try:
            with _cache_write_lock:
                if _cache_stamp(self.cache_file) != self._cache_stamp:
                    disk = _state.read(self.cache_file, {})
                    for key, value in disk.items():
                        if _cache_time(value) > _cache_time(self.cache.get(key)):
                            self.cache[key] = value
                cut = time.time() - 86400
                self.cache = {k: v for k, v in self.cache.items() if _cache_time(v) > cut}
                _state.save(self.cache_file, self.cache)
                self._cache_stamp = _cache_stamp(self.cache_file)
        except OSError as e:
            log.warning('TMDB 缓存写入失败: %s', e)

def _load_emby_index_disk():
    try:
        raw = _state.read(_eng()._EMBY_INDEX_CACHE_FILE)
        data = raw.get('data')
        if isinstance(data, dict): return {'ts': float(raw.get('ts', 0)), 'data': data}
    except (OSError, ValueError, TypeError):
        pass
    return None

def _save_emby_index_disk(data):
    try:
        _eng().STATE_DIR.mkdir(parents=True, exist_ok=True)
        _state.save(_eng()._EMBY_INDEX_CACHE_FILE, {'ts': time.time(), 'data': data})
    except (OSError, TypeError) as e:
        log.warning('Emby 探索索引缓存写入失败: %s', e)

def _build_emby_library_index():
    out = {}
    disk_lookup = _eng()._disk_tmdb_lookup()  # 上游目录名 {tmdb-xxx} 兜底表
    for item_type in ('Movie', 'Series'):
        media_key = 'tv' if item_type == 'Series' else 'movie'
        try:
            data = _eng().emby_request('/Items', {
                'Recursive': 'true', 'IncludeItemTypes': item_type,
                'Fields': 'ProviderIds,Path,ProductionYear,CommunityRating,ImageTags',
                'Limit': 50000,
            })
            if not isinstance(data, dict) or not isinstance(data.get('Items'), list):
                raise ValueError('Emby 身份索引返回不完整')
            if int(data.get('TotalRecordCount', len(data['Items']))) > len(data['Items']):
                raise ValueError('Emby 身份索引被截断')
            for it in data['Items']:
                tmdb_id = str((it.get('ProviderIds') or {}).get('Tmdb') or '')
                if not tmdb_id:
                    _cp = _eng().emby_path_to_container(it.get('Path', '') or '')
                    tmdb_id = disk_lookup.get(str(_cp) if _cp else '', '')
                if not tmdb_id: continue
                key = f'{media_key}:{tmdb_id}'
                path = it.get('Path', '') or ''
                lib = _eng().emby_lib_of(path)
                row = out.setdefault(key, {
                    'id': it.get('Id'), 'ids': [], 'name': it.get('Name'), 'type': it.get('Type'),
                    'path': path, 'paths': [], 'year': it.get('ProductionYear'),
                    'rating': it.get('CommunityRating'), 'has_image': False, 'libs': set(),
                })
                if it.get('Id') and it.get('Id') not in row['ids']: row['ids'].append(it.get('Id'))
                if path and path not in row['paths']: row['paths'].append(path)
                if lib: row['libs'].add(lib)
                row['has_image'] = row['has_image'] or ('Primary' in (it.get('ImageTags') or {}))
                if not row.get('path') and path: row['path'] = path
        except Exception as e:
            raise RuntimeError('Emby 身份索引拉取失败 (%s)' % item_type) from e
    for row in out.values():
        row['libs'] = sorted(row['libs'])
        row['in_local'] = 'local' in row['libs']
        row['in_share'] = 'share' in row['libs']
    return out

def _emby_index_bg_refresh():
    # 仅由已取得刷新锁的启动函数调用，避免重复请求生成大量等待线程。
    try:
        out = _build_emby_library_index()
        _eng()._emby_index_cache.update({'ts': time.time(), 'data': out, 'error_ts': 0})
        _save_emby_index_disk(out)
    except Exception as e:
        _eng()._emby_index_cache['error_ts'] = time.time()
        log.warning('Emby 探索索引后台刷新失败，保留上次完整索引: %s', e)
    finally:
        _eng()._emby_index_refresh_lock.release()


def _start_emby_index_refresh():
    cache = _eng()._emby_index_cache
    if time.time() - cache.get('error_ts', 0) < 30:
        return
    lock = _eng()._emby_index_refresh_lock
    if not lock.acquire(blocking=False):
        return
    try:
        threading.Thread(target=_emby_index_bg_refresh, daemon=True,
                         name='emby-index-refresh').start()
    except Exception:
        lock.release()
        raise


def emby_library_index(force=False):
    """探索身份索引始终先读缓存；首次构建也在后台，成功空库是有效结果。"""
    cache = _eng()._emby_index_cache
    if force:
        with _eng()._emby_index_refresh_lock:
            out = _build_emby_library_index()
            cache.update({'ts': time.time(), 'data': out, 'error_ts': 0})
            _save_emby_index_disk(out)
            return out
    if cache.get('data') is None:
        disk = _load_emby_index_disk()
        if disk:
            cache.update(disk)
    if cache.get('data') is None or time.time() - cache.get('ts', 0) >= 300:
        _start_emby_index_refresh()
    return cache.get('data') or {}


EXPLORE_PAGE_LIMIT = 32
EXPLORE_PAGE_TTL = 6 * 3600
_explore_page_lock = threading.RLock()
_explore_page_state = None
_explore_slots = threading.BoundedSemaphore(2)
# 浏览/预取最多占一个名额，避免占满首屏搜索名额。
_explore_background_slots = threading.BoundedSemaphore(1)
_EXPLORE_FIELDS = ('id', 'title', 'name', 'release_date', 'first_air_date',
                   'vote_average', 'poster_path', 'media_type')


def _explore_state():
    global _explore_page_state
    path = _eng().STATE_DIR / 'explore_pages.json'
    with _explore_page_lock:
        if _explore_page_state is None or _explore_page_state['path'] != path:
            pages = {}
            try:
                raw = _state.read(path)
                if isinstance(raw, dict):
                    for key, row in raw.items():
                        if (isinstance(row, dict) and _cache_time(row) and
                                isinstance(row.get('data'), dict) and
                                isinstance(row['data'].get('results'), list)):
                            pages[key] = row
            except (OSError, ValueError, TypeError):
                pass
            pages = dict(sorted(pages.items(), key=lambda kv: _cache_time(kv[1]))[-EXPLORE_PAGE_LIMIT:])
            _explore_page_state = {'path': path, 'pages': pages, 'jobs': set(), 'errors': {}}
        return _explore_page_state


def _explore_failure(error):
    """只返回固定错误分类，urllib 异常中的 URL / API key 不进入页面或日志。"""
    cause = error
    seen = set()
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        if isinstance(cause, urllib.error.HTTPError):
            if cause.code == 407:
                return 'proxy', 'HTTP 代理认证失败，请在服务连接中检查用户名和密码'
            if cause.code in (401, 403):
                return 'auth', 'TMDB 密钥无效或无权限，请在规则设置中检查 TMDB 配置'
            if cause.code == 429:
                return 'rate_limit', 'TMDB 请求限流，请稍后重试'
            return 'http', 'TMDB 服务暂时不可用（HTTP %s），请稍后重试' % cause.code
        if isinstance(cause, TimeoutError) or (isinstance(cause, urllib.error.URLError) and
                                               isinstance(cause.reason, TimeoutError)):
            return 'timeout', '连接 TMDB 超时，请检查 NAS 网络或代理后重试'
        if isinstance(cause, (urllib.error.URLError, OSError)):
            if 'HTTP 代理' in str(getattr(cause, 'reason', cause)):
                return 'proxy', 'HTTP 代理连接失败，请在服务连接中检查地址、端口和鉴权'
            return 'network', '无法连接 TMDB，请检查 NAS 网络、DNS 或代理后重试'
        cause = cause.__cause__ or cause.__context__
    if isinstance(error, TmdbError) and '预算' in str(error):
        return 'timeout', 'TMDB 响应超时，请稍后重试'
    return 'response', 'TMDB 返回异常，暂时无法加载，请稍后重试'


def _refresh_explore_page(state, key, path, params, client_key, slots=None, background_slot=None):
    slots = slots if slots is not None else _explore_slots
    token = _explore_request_window.set({'deadline': time.monotonic() + 10,
                                         'attempts': 2, 'timeout': 4.0})
    try:
        # 分页已有独立小缓存：搜索不读取/回写整个 TMDB 明细缓存。
        client = _eng().Tmdb(cache={})
        client.key = client_key
        first = client.get(path, **params)
        if not isinstance(first, dict) or not isinstance(first.get('results'), list):
            raise TmdbError('TMDB 探索返回不完整')
        total_pages = int(first.get('total_pages') or 1)
        if any(not isinstance(item, dict) or not item.get('id') for item in first['results']):
            raise TmdbError('TMDB 探索条目不完整')
        data = {'results': [{k: item[k] for k in _EXPLORE_FIELDS if k in item}
                            for item in first['results'][:20]],
                'total_pages': max(1, min(total_pages, 500)),
                'total_results': int(first.get('total_results') or 0)}
        with _explore_page_lock:
            state['pages'][key] = {'ts': time.time(), 'data': data}
            state['errors'].pop(key, None)
            while len(state['pages']) > EXPLORE_PAGE_LIMIT:
                oldest = min(state['pages'], key=lambda k: _cache_time(state['pages'][k]))
                del state['pages'][oldest]
            try:
                state['path'].parent.mkdir(parents=True, exist_ok=True)
                _state.save(state['path'], state['pages'])
            except OSError as error:
                log.warning('探索分页缓存保存失败: %s', error)
    except Exception as error:
        # 不把失败写成成功空结果。错误文本不含请求 URL 或密钥。
        code, message = _explore_failure(error)
        with _explore_page_lock:
            state['errors'][key] = {'ts': time.time(), 'message': message, 'code': code}
            while len(state['errors']) > EXPLORE_PAGE_LIMIT:
                del state['errors'][next(iter(state['errors']))]
        log.warning('探索后台刷新失败，保留上次分页结果 (%s: %s)', code, message)
    finally:
        _explore_request_window.reset(token)
        with _explore_page_lock:
            state['jobs'].discard(key)
        slots.release()
        if background_slot is not None:
            background_slot.release()


def _explore_page(path, params, media, retry=False):
    # 轻客户端只取配置，不在 HTTP 线程读取整份 TMDB 缓存。
    light = _eng().Tmdb(cache={})
    if not light.key:
        return None, {'status': 'error', 'message': '未配置 TMDB_KEY'}
    params = dict(params, language=_eng().TMDB_LANG)
    # 新分页协议独立键，避免旧的双页合并缓存造成跳页和重复。
    identity = ['single-page-v1', path, params, media, _eng().TMDB_BASE, light.key]
    key = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    state = _explore_state()
    with _explore_page_lock:
        row = state['pages'].get(key)
        stale = row is None or time.time() - row['ts'] >= EXPLORE_PAGE_TTL
        error = state['errors'].get(key)
        if retry and key not in state['jobs']:
            state['errors'].pop(key, None)
            error = None
        cooling = error and time.time() - error['ts'] < 30
        slots = _explore_slots
        background_slot = None if path.startswith('/search/') and params.get('page', 1) == 1 else _explore_background_slots
        acquired = False
        if stale and key not in state['jobs'] and not cooling:
            background_acquired = background_slot is None or background_slot.acquire(blocking=False)
            if background_acquired:
                acquired = slots.acquire(blocking=False)
                if not acquired and background_slot is not None:
                    background_slot.release()
        if acquired:
            state['jobs'].add(key)
            try:
                threading.Thread(target=_refresh_explore_page,
                                 args=(state, key, path, params, light.key, slots, background_slot),
                                 daemon=True, name='explore-page-refresh').start()
            except Exception:
                state['jobs'].discard(key)
                slots.release()
                if background_slot is not None:
                    background_slot.release()
                raise
        meta = {'page_ts': row['ts'] if row else None, 'page_stale': stale,
                'refreshing': key in state['jobs'], 'refresh_error': error['message'] if cooling else None,
                'retry_after': 1}
        if row is None:
            meta.update(status='error' if cooling else 'pending',
                        error_code=error.get('code') if cooling else None,
                        message=error['message'] if cooling else
                        (('正在搜索 TMDB…' if path.startswith('/search/') else '正在加载探索结果…')
                         if key in state['jobs'] else '正在等待探索请求名额…'))
            return None, meta
        return row['data'], meta

def _explore_library_status(cards):
    """Reapply shared cached facts without discovery/TMDB requests or per-card scans."""
    emby_index = emby_library_index()
    index_cache = _eng()._emby_index_cache
    index_known = index_cache.get('data') is not None
    index_stale = time.time() - index_cache.get('ts', 0) >= 300
    refreshing = _eng()._emby_index_refresh_lock.locked()
    refresh_error = None
    if index_cache.get('error_ts') and index_stale:
        refresh_error = ('片库索引更新失败，保留上次完整索引' if index_known else '片库索引更新失败，暂未取得完整索引')
    # 集数和对照数据与片库映射/详情共用同一批事实，不再逐卡覆盖。
    facts = _eng().cached_library_view() if any(c['type'] == 'tv' for c in cards) else None
    rows_by_tmdb = defaultdict(list)
    for row in (facts or {}).get('series') or []:
        if row.get('tmdb_id'):
            rows_by_tmdb[str(row['tmdb_id'])].append(row)
    updates = []
    for card in cards:
        item_media, tmdb_id = card['type'], str(card['tmdb_id'])
        emby_hit = emby_index.get(item_media + ':' + tmdb_id)
        in_emby = emby_hit is not None
        in_local = bool(emby_hit and emby_hit.get('in_local'))
        in_share = bool(emby_hit and emby_hit.get('in_share'))

        eps = None
        eps_source = None
        if item_media == 'tv':
            rows = rows_by_tmdb.get(tmdb_id) or []
            eps_source = 'ambiguous' if len(rows) > 1 else 'unavailable'
            if len(rows) == 1:
                row = rows[0]
                info = row.get('tmdb_info') or {}
                have = row.get('have_eps')
                if have is None:
                    have = row.get('total_episodes', 0)
                total = info.get('declared_total')
                if total is None:
                    total = info.get('tmdb_total')
                eps = {'local': int(row.get('local_eps') or 0),
                       'share': int(row.get('share_eps') or 0),
                       'have': int(have or 0), 'total': total,
                       'match_status': info.get('match_status', 'unmatched'),
                       'manual_done': bool(row.get('_md'))}
                eps_source = 'shared_cache'
                in_emby = True
                in_local = bool(row.get('in_local', eps['local'] > 0))
                in_share = bool(row.get('in_share', eps['share'] > 0))
                emby_hit = dict(emby_hit or {}, id=row.get('id'))

        updates.append({'type': item_media, 'tmdb_id': tmdb_id,
            'in_emby': in_emby, 'in_local': in_local, 'in_share': in_share,
            'emby_id': emby_hit['id'] if emby_hit else None,
            '_emby_has_image': bool(emby_hit and emby_hit.get('has_image')),
            'eps': eps, 'eps_source': eps_source,
            'library_status': ('stale' if index_stale else 'available') if index_known else ('available' if in_emby else 'unavailable'),
            'facts_version': (facts or {}).get('facts_version'),
            'facts_ts': (facts or {}).get('facts_ts'),
            'facts_stale': (facts or {}).get('stale', True),
        })
    return {'status': 'success', 'cards': updates,
            'library_refreshing': refreshing, 'library_error': refresh_error,
            'facts_version': (facts or {}).get('facts_version'),
            'facts_ts': (facts or {}).get('facts_ts')}


def action_explore(args):
    region = getattr(args, 'region', 'all') or 'all'
    year = (getattr(args, 'year', '') or '').strip()
    sort = getattr(args, 'sort', 'popularity') or 'popularity'
    media = getattr(args, 'media', 'movie') or 'movie'
    page = max(1, min(int(getattr(args, 'page', 1) or 1), 500))
    query = (getattr(args, 'q', '') or '').strip()
    genre = (getattr(args, 'genre', '') or '').strip()

    if query:
        # 搜索：media=all 走 /search/multi 一次搜到电影+剧集（结果带 media_type，可后切），
        # 明确选了 movie/tv 时仍用精确的分类型搜索。
        if media == 'all':
            tmdb_path = '/search/multi'
        else:
            tmdb_path = '/search/tv' if media == 'tv' else '/search/movie'
        params = {'query': query, 'page': page, 'include_adult': 'false'}
    else:
        # 浏览：TMDB discover 没有 all，媒体类型回落到 movie。
        if media == 'all':
            media = 'movie'
        tmdb_path = '/discover/tv' if media == 'tv' else '/discover/movie'
        params = {'page': page, 'sort_by': _eng().SORT_MAP.get(sort, 'popularity.desc'), 'vote_count.gte': 5}
        if region != 'all' and region in _eng().REGION_MAP: params.update(_eng().REGION_MAP[region])
        if year:
            params['first_air_date_year' if media == 'tv' else 'primary_release_year'] = year
        if genre and genre in _eng().GENRE_MAP:
            gid = _eng().GENRE_MAP[genre].get(media)
            if gid:
                params['with_genres'] = str(gid)

    res, page_meta = _explore_page(tmdb_path, params, media, retry=bool(getattr(args, 'retry', False)))
    if res is None:
        return page_meta

    cards = []
    for item in (res.get('results') or [])[:20]:
        item_media = item.get('media_type') or media
        if item_media == 'person':
            continue
        if item_media not in ('movie', 'tv'):
            item_media = media
        poster = item.get('poster_path')
        rating = item.get('vote_average') or 0
        cards.append({'type': item_media, 'tmdb_id': str(item.get('id', '')),
                      'title': item.get('title') or item.get('name') or '',
                      'year': (item.get('release_date') or item.get('first_air_date') or '')[:4],
                      'rating': round(rating, 1) if rating else None,
                      'poster': '/api/tmdb/poster/' + poster.lstrip('/') if poster else ''})
    library = _explore_library_status(cards)
    for card, update in zip(cards, library['cards']):
        card.update(update)
        if not card['poster'] and card.pop('_emby_has_image', False) and card['emby_id']:
            card['poster'] = '/api/emby/poster/' + str(card['emby_id'])
        card.pop('_emby_has_image', None)
    page_meta['page_refreshing'] = page_meta['refreshing']
    page_meta['library_error'] = library['library_error']
    page_meta['refreshing'] = page_meta['refreshing'] or library['library_refreshing']
    page_meta['refresh_error'] = page_meta.get('refresh_error') or library['library_error']
    return dict(page_meta, status='success', page=page,
                total_pages=min(res.get('total_pages', 1), 500),
                total_results=res.get('total_results', 0), cards=cards, is_search=bool(query),
                facts_version=library['facts_version'], facts_ts=library['facts_ts'],
                tmdb_calls=0, tmdb_hits=0)


def classify_series_by_tmdb(local_seasons, tmdb_info):
    """按 TMDB 已播集数对照片库，避免连载未播集造成假缺集。"""
    local_map = {s['season']: s['episodes'] for s in local_seasons if s['season'] > 0}
    local_total = sum(local_map.values())
    if not tmdb_info:
        return {'match_status': 'unmatched', 'tmdb_status': None,
                'local_total': local_total, 'tmdb_total': None, 'declared_total': None, 'diff': None,
                'seasons': [{'season': sn, 'local': local_map[sn], 'tmdb': None, 'diff': None, 'status': 'unknown'} for sn in sorted(local_map)]}
    tmdb_status = tmdb_info.get('status', '') or ''
    tmdb_map = {}
    season_rows = tmdb_info.get('seasons') or []
    for ss in season_rows:
        sn = ss.get('season_number')
        if sn is None or sn <= 0:
            continue
        ec = int(ss.get('episode_count', 0) or 0)
        if ec <= 0:
            continue
        tmdb_map[int(sn)] = ec
    # 标称总集数（含未播集，与上游 TgtoDrive 显示口径一致，如 18/24）；仅用于展示，缺集判断仍按已播集
    declared_total = sum(tmdb_map.values())
    # 对于正在播出的最后一季，只统计 last_episode_to_air 之前已经播出的集；
    # 更后的季直接忽略。没有该字段时保留旧口径。
    last = tmdb_info.get('last_episode_to_air') or {}
    try:
        last_s = int(last.get('season_number') or 0)
        last_e = int(last.get('episode_number') or 0)
    except (TypeError, ValueError):
        last_s = last_e = 0
    if last_s > 0 and last_e > 0:
        tmdb_map = {sn: (last_e if sn == last_s else ec)
                    for sn, ec in tmdb_map.items()
                    if sn < last_s or sn == last_s}
    tmdb_total = sum(tmdb_map.values())
    season_diff = []
    for sn in sorted(set(local_map.keys()) | set(tmdb_map.keys())):
        l = local_map.get(sn, 0); t_ = tmdb_map.get(sn, 0)
        if l == t_: st = 'aligned'
        elif l < t_: st = 'missing'
        else: st = 'extra'
        season_diff.append({'season': sn, 'local': l, 'tmdb': t_, 'diff': l - t_, 'status': st})
    is_ongoing = tmdb_status in ('Returning Series', 'In Production', 'Planned', 'Pilot')
    if is_ongoing:
        status = 'aligned' if local_total >= tmdb_total else 'ongoing'
    else:
        if local_total == tmdb_total: status = 'aligned'
        elif local_total < tmdb_total: status = 'missing'
        else: status = 'extra'
    return {'match_status': status, 'tmdb_status': tmdb_status,
            'local_total': local_total, 'tmdb_total': tmdb_total, 'declared_total': declared_total,
            'diff': local_total - tmdb_total, 'seasons': season_diff}

def _emby_series_ids_by_tmdb(series_tmdb_id, fallback_id=None):
    """按 TMDB ID 找 Emby Series 条目 ID 列表（实时，不读缓存）。

    优先级与 _emby_series_latest_ep 一致：① AnyProviderIdEquals 精确查询；
    ② 全量 Series 按 ProviderIds.Tmdb 过滤。两次都空时用 fallback_id 兜底
    （调用方从探索索引拿到的 Emby 条目 id，避免 ProviderIds 缺失时查不到）。
    """
    ids = []
    try:
        data = _eng().emby_request('/Items', {
            'Recursive': 'true', 'IncludeItemTypes': 'Series',
            'Fields': 'ProviderIds,Name', 'Limit': 50000,
            'AnyProviderIdEquals': f'Tmdb.{series_tmdb_id}',
        }) or {}
        ids = [it.get('Id') for it in (data.get('Items') or []) if it.get('Id')]
    except Exception as e:
        log.warning('实时集数：Series 精确查询失败 %s: %s', series_tmdb_id, e)
    if not ids:
        try:
            data = _eng().emby_request('/Items', {
                'Recursive': 'true', 'IncludeItemTypes': 'Series',
                'Fields': 'ProviderIds,Name', 'Limit': 50000,
            }) or {}
            ids = [it.get('Id') for it in (data.get('Items') or [])
                   if str((it.get('ProviderIds') or {}).get('Tmdb') or '') == str(series_tmdb_id)
                   and it.get('Id')]
        except Exception as e:
            log.warning('实时集数：Series 全量过滤失败 %s: %s', series_tmdb_id, e)
    if not ids and fallback_id:
        ids = [fallback_id]
    return ids

def _emby_series_live_eps(series_tmdb_id, fallback_id=None, use_cache=True):
    """实时查某剧在 Emby 的分集集合（不读任何快照）。

    返回 {'episodes': set[(季,集)], 'local_eps': int, 'share_eps': int,
          'have_eps': int, 'empty': bool}；完全查不到时返回 None。
    ``empty=True`` 表示「查到了 Series 但集数为 0」——调用方应保留旧值而不是
    用 0 覆盖（避免 Emby 短暂异常时把卡片打成「未入库」）。

    只统计 ``emby_lib_of(path)`` 能识别的分集：与 _build_emby_library_overview
    口径一致（只算落在本地/分享两个库根内的），顺带排除 CD2 已删源、Emby 还没
    刷掉的幽灵条目。
    """
    if not series_tmdb_id:
        return None
    ck = str(series_tmdb_id)
    if use_cache:
        with _eng()._live_eps_lock:
            hit = _eng()._live_eps_cache.get(ck)
            if hit and time.time() - hit[0] < _eng()._LIVE_EPS_TTL:
                return hit[1]

    ids = _emby_series_ids_by_tmdb(series_tmdb_id, fallback_id)
    if not ids:
        return None
    have, local_eps, share_eps = set(), set(), set()
    for sid in ids:
        try:
            data = _eng().emby_request('/Items', {
                'ParentId': sid, 'Recursive': 'true', 'IncludeItemTypes': 'Episode',
                'Fields': 'ParentIndexNumber,IndexNumber,IndexNumberEnd,Path',
                'Limit': 50000,
            }) or {}
        except Exception as e:
            log.warning('实时集数：拉取分集失败 series=%s: %s', sid, e)
            continue
        for ep in (data.get('Items') or []):
            try:
                sn = int(ep.get('ParentIndexNumber') or 0)
                en = int(ep.get('IndexNumber') or 0)
                en_end = int(ep.get('IndexNumberEnd') or en)
            except (TypeError, ValueError):
                continue
            if sn <= 0 or en <= 0:
                continue
            lib = _eng().emby_lib_of(ep.get('Path') or '')
            if not lib:
                continue  # 不在两个库根内：不算入库（与片库映射口径一致）
            for e in range(en, max(en, en_end) + 1):
                have.add((sn, e))
                (local_eps if lib == 'local' else share_eps).add((sn, e))

    out = {'episodes': have, 'local_eps': len(local_eps), 'share_eps': len(share_eps),
           'have_eps': len(have), 'empty': not have}
    if use_cache:
        with _eng()._live_eps_lock:
            _eng()._live_eps_cache[ck] = (time.time(), out)
            # 轻量清理：只保留最近 2000 条，避免长跑进程内存无界增长
            if len(_eng()._live_eps_cache) > 2000:
                for k, _v in sorted(_eng()._live_eps_cache.items(), key=lambda kv: kv[1][0])[:500]:
                    _eng()._live_eps_cache.pop(k, None)
    return out

def _tmdb_aired_set_from_info(info):
    """从已取得的 TMDB /tv/{id} 原始响应计算已播 (季,集)，不重复请求 TMDB。"""
    if not info:
        return set()
    aired_seasons = {}
    for ss in (info.get('seasons') or []):
        sn = ss.get('season_number')
        if sn is None or sn <= 0:
            continue
        try:
            aired_seasons[int(sn)] = int(ss.get('episode_count', 0) or 0)
        except (TypeError, ValueError):
            continue
    last = info.get('last_episode_to_air') or {}
    try:
        last_s = int(last.get('season_number') or 0)
        last_e = int(last.get('episode_number') or 0)
    except (TypeError, ValueError):
        last_s = last_e = 0
    aired = set()
    for sn, ec in aired_seasons.items():
        if last_s > 0 and last_e > 0:
            if sn < last_s: n = ec
            elif sn == last_s: n = last_e
            else: continue
        else:
            n = ec
        aired.update((sn, e) for e in range(1, n + 1))
    return aired

def _tmdb_series_info(tmdb_id):
    """查 TMDB 已播集数、状态、季结构。
    已播集以 last_episode_to_air 为界：之前的季整季计入，最后播出的季只计到该集；
    连载季 episode_count 含未播集，直接求和会让在更的剧永远「缺集」。
    缺 last_episode_to_air 时退回旧口径（各季 episode_count 全部计入）。"""
    try:
        session = _subscription_tmdb_client.get()
        if session is None:
            t = _eng().Tmdb()
        else:
            if 'client' not in session:
                session['client'] = _eng().Tmdb()
            t = session['client']
        hits_before = getattr(t, 'hits', 0)
        info = t.get(f'/tv/{tmdb_id}', ttl=_eng().TMDB_INFO_TTL)
        if session is None:
            t.save()
        if not info: return None
        if not isinstance(info, dict) or info.get('success') is False or not isinstance(info.get('seasons'), list):
            raise ValueError('TMDB 未返回有效季结构')
        aired_seasons = {}
        for s in (info.get('seasons') or []):
            sn = s.get('season_number')
            if sn is None or sn <= 0: continue
            aired_seasons[sn] = {
                'episode_count': s.get('episode_count', 0) or 0,
                'air_date': s.get('air_date') or '',
            }
        last = info.get('last_episode_to_air') or {}
        aired = _tmdb_aired_set_from_info(info)
        key = f'/tv/{tmdb_id}?' + urllib.parse.urlencode(sorted({'language': _eng().TMDB_LANG}.items()))
        cache_row = (getattr(t, 'cache', {}) or {}).get(key) or {}
        return {
            'source': 'tmdb_cache' if getattr(t, 'hits', 0) > hits_before else 'tmdb',
            'ts': cache_row.get('ts'),
            'name': info.get('name'),
            'status': info.get('status', ''),
            'last_episode_to_air': last,
            'seasons': aired_seasons,
            'total_episodes': len(aired),   # 已播集数（不含未播集 / S00）
            'declared_total': sum(v['episode_count'] for v in aired_seasons.values()),   # 标称总集数（展示用）
            'aired': aired,                 # {(季, 集)}
        }
    except Exception as e:
        log.warning('TMDB 订阅查询失败 %s: %s', tmdb_id, e)
        return None
