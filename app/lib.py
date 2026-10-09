# -*- coding: utf-8 -*-
"""磁盘扫描与治理身份（Lib 类 + 双库遍历缓存 + tmdb 磁盘索引），从 engine.py 拆出。

注意：L_ROOT / S_ROOT / _CATEGORY_NAMES 是 engine 里的运行时可变全局，这里一律通过
_engine_ns() 惰性访问，保证 reload_config / monkeypatch 能穿透。
"""
import os
import stat
import re
import time
import logging
import threading
from pathlib import Path
from collections import defaultdict

log = logging.getLogger('strimkeep')

try:
    from . import tasks as _tasks
    from .core import parse_season_dir, get_ep, governance_title_key
except ImportError:
    import tasks as _tasks
    from core import parse_season_dir, get_ep, governance_title_key


def _engine_ns():
    try:
        from . import engine as _e
        return _e
    except ImportError:
        try:
            import engine as _e
            return _e
        except ImportError:
            return None


_eng = _engine_ns  # 兼容后续修复中的简写


class LibraryScanError(RuntimeError):
    """目录事实不完整：禁止发布本次结果或删除旧索引。"""
    def __init__(self, root, path, error):
        self.root, self.path = str(root), str(path)
        super().__init__(f'媒体库扫描失败：{path}；{error}。旧索引已保留，请检查挂载/权限后重新扫描')


def _root_identity(root):
    try:
        st = root.stat()
        if not stat.S_ISDIR(st.st_mode):
            raise NotADirectoryError('媒体库根路径不是目录')
        return st.st_dev, st.st_ino
    except OSError as error:
        raise LibraryScanError(root, root, error) from error


class Lib:
    def __init__(self, root):
        self.root = root
        self.mov = defaultdict(list)
        self.tv = defaultdict(lambda: defaultdict(list))
        self.meta = {}
        self.by_base = defaultdict(set)
        # TMDB 的电影与剧集是两个独立编号空间（movie:103 ≠ tv:103），
        # 因此键带类型前缀，避免跨类型误聚合/误配对。
        self.tmdb_refs = defaultdict(set)  # 键: 'movie:<id>' / 'tv:<id>' -> 治理 key 集合
        self.key_tmdb_movie = {}  # 治理 key -> 电影类文件携带的 TMDB ID（tmdb_first 策略用）
        self.key_tmdb_tv = {}     # 治理 key -> 剧集类文件携带的 TMDB ID
        self.path_tmdb = {}       # 标题目录绝对路径(容器内) -> tmdb_id（片库映射/探索兜底用）
        self.strm_count = 0
        _tasks.checkpoint()
        root_identity = _root_identity(root)
        # P1：媒体索引。文件没有变化时复用上次解析结果；新增/修改文件才重新解析。
        # os.walk 仍然负责发现文件，但最重的文件名/身份解析不再每次全量执行。
        root_key = str(root)
        try:
            media_index = _eng().db_media_index_load(root_key)
        except Exception:
            media_index = {}
        changed_rows = []
        seen_paths = set()
        visited_root = False
        def walk_error(error):
            raise LibraryScanError(root, error.filename or root, error) from error
        for dirpath, _dirs, names in os.walk(str(root), onerror=walk_error):
            visited_root = True
            # os.walk 默认不遍历目录软链接；不能把被跳过的媒体目录当成空目录。
            for name in _dirs:
                child = Path(dirpath) / name
                if child.is_symlink():
                    raise LibraryScanError(root, child, '不支持跳过目录软链接')
            strms = sorted(n for n in names if n.endswith('.strm'))
            if not strms:
                continue
            d = Path(dirpath)
            pname = d.name
            folder = d.parent.name if parse_season_dir(pname) is not None else pname
            allow_bare_ep = (parse_season_dir(pname) is not None or _under_tv_category(d, root))
            for n in strms:
                f = d / n
                path_key = str(f)
                seen_paths.add(path_key)
                try:
                    st = f.stat()
                    if f.is_symlink() or not stat.S_ISREG(st.st_mode):
                        raise OSError('STRM 不是普通文件或是软链接')
                    mtime_ns, size = int(st.st_mtime_ns), int(st.st_size)
                except OSError as error:
                    raise LibraryScanError(root, f, error) from error
                self.strm_count += 1
                cached = media_index.get(path_key)
                payload = cached.get('payload') if cached else None
                if not cached or cached.get('mtime_ns') != mtime_ns or cached.get('size') != size or not isinstance(payload, dict):
                    key, disp, base, year = governance_title_key(folder)
                    tm = re.search(r'(?i)tmdb(?:id)?[=\-: ]*(\d+)', folder or '')
                    ep = get_ep(n, pname, allow_bare_ep=allow_bare_ep)
                    payload = {
                        'key': key, 'disp': disp, 'base': base, 'year': year,
                        'tmdb': tm.group(1) if tm else '',
                        'season': int(ep[0]) if ep else 0,
                        'episode': int(ep[1]) if ep else 0,
                    }
                    changed_rows.append((path_key, mtime_ns, size, payload))
                key = payload.get('key') or ''
                disp = payload.get('disp') or ''
                base = payload.get('base') or ''
                year = payload.get('year')
                tmdb_id = str(payload.get('tmdb') or '')
                sn = int(payload.get('season') or 0)
                en = int(payload.get('episode') or 0)
                if not key:
                    continue
                self.meta[key] = (disp, base, year)
                self.by_base[base].add(key)
                if tmdb_id:
                    title_dir = d.parent if parse_season_dir(pname) is not None else d
                    self.path_tmdb[str(title_dir)] = tmdb_id
                # 只要解析出了季/集证据就是剧集：S00 特别篇(sn=0)、E00 第0集(en=0)
                # 也属于剧集，分别由 special_action 策略与逐集对照（跳过 E00）处理。
                # 只有「季集都解析不出」(sn=en=0) 才是电影；否则特别篇会被当成电影，
                # 与对侧任意文件比画质并整体删除（向往的生活 S04E00 误删事故）。
                if sn > 0 or en > 0:
                    self.tv[key][sn].append(f)
                    if tmdb_id:
                        self.tmdb_refs['tv:' + tmdb_id].add(key)
                        self.key_tmdb_tv.setdefault(key, tmdb_id)
                else:
                    self.mov[key].append(f)
                    if tmdb_id:
                        self.tmdb_refs['movie:' + tmdb_id].add(key)
                        self.key_tmdb_movie.setdefault(key, tmdb_id)
        _tasks.checkpoint()
        if not visited_root or _root_identity(root) != root_identity:
            raise LibraryScanError(root, root, '扫描期间根目录已更换或未完成遍历')
        try:
            if changed_rows:
                _eng().db_media_index_upsert(root_key, changed_rows)
            _eng().db_media_index_delete_missing(root_key, seen_paths)
        except Exception as e:
            log.debug('媒体增量索引更新失败: %s', e)

    def find(self, meta, container):
        key, (_, base, year) = meta
        if key in container: return key
        # 治理身份是「剧名 + 年份」。年份已知时绝不允许跨年模糊配对；
        # 年份未知时也只允许与同样未知年份的目录配对。
        cands = [k for k in self.by_base.get(base, ())
                 if k in container and self.meta[k][2] == year]
        return cands[0] if len(cands) == 1 else None

def _disk_tmdb_lookup():
    """合并两库磁盘扫描的「标题目录绝对路径 -> tmdb_id」兜底表。

    上游 TgtoDrive 把 tmdb 写在目录名 ``{tmdb-xxx}``，Emby 刮削不写
    ``ProviderIds.Tmdb``，导致片库映射/探索索引拿不到 tmdb_id。这里返回
    磁盘目录名里的事实 tmdb，供两处在 Emby ProviderIds 缺失时兜底。
    """
    lookup = {}
    try:
        for lib in (_get_lib(_engine_ns().L_ROOT), _get_lib(_engine_ns().S_ROOT)):
            lookup.update(lib.path_tmdb)
    except Exception as e:
        log.warning('磁盘 tmdb 兜底表构建失败: %s', e)
    return lookup

def _disk_eps_by_tmdb():
    """按目录名 tmdb 聚合两库磁盘分集：tmdb_id -> {episodes/local_eps/share_eps}。

    上游 TgtoDrive 用 SxxExx 命名分集，磁盘文件名是权威；Emby 识别可能漏集/错集，
    导致片库映射的缺集对照误判。这里复用 Lib 磁盘索引，把磁盘事实分集按 tmdb_id
    聚合，供片库映射在 Emby 分集之上 union 兜底。
    """
    out = defaultdict(lambda: {'episodes': set(), 'local_eps': set(), 'share_eps': set()})
    try:
        for lib, tag in ((_get_lib(_engine_ns().L_ROOT), 'local'), (_get_lib(_engine_ns().S_ROOT), 'share')):
            for key, tid in lib.key_tmdb_tv.items():
                for sn, files in lib.tv.get(key, {}).items():
                    if sn <= 0:
                        continue
                    for f in files:
                        try:
                            ep = get_ep(f.name, f.parent.name, allow_bare_ep=True)
                        except Exception:
                            continue
                        if ep and ep[0] > 0 and ep[1] > 0:
                            out[tid]['episodes'].add(ep)
                            (out[tid]['local_eps'] if tag == 'local'
                             else out[tid]['share_eps']).add(ep)
    except Exception as e:
        log.warning('磁盘分集表构建失败: %s', e)
    return out

def _match_governance_key(S, L, skey, container, strategy, kind):
    """跨库配对（分享 key -> 本地 key）。

    ``title_year``（默认）：按「剧名 + 年份」配对，TMDB 完全不参与（安全档）。
    ``tmdb_first``：优先按 TMDB ID 配对——两边 TMDB 一致且本地唯一时直接配对
    （可跨越译名/命名差异）；同一 TMDB 在本地对应多个不同身份时判定为冲突、
    不自动配，交给 ``_identity_conflicts`` 人工处理；无 TMDB 或本地无对应时
    回退到「剧名 + 年份」。

    ``kind``：'movie' 或 'tv'。TMDB 的电影与剧集是两个独立编号空间
    （movie:103 ≠ tv:103），必须同类型匹配，否则会把不同作品误判成冲突。
    """
    if strategy == 'tmdb_first':
        s_map = S.key_tmdb_movie if kind == 'movie' else S.key_tmdb_tv
        stm = s_map.get(skey)
        if stm:
            cands = [k for k in L.tmdb_refs.get(kind + ':' + stm, ()) if k in container]
            if len(cands) == 1:
                return cands[0]
            if len(cands) > 1:
                # 同 TMDB 多个身份：存疑，宁可不配也不配错
                return None
            # 0 个候选 -> 回退剧名 + 年份
    return L.find((skey, S.meta[skey]), container)

_lib_cache = {}
_lib_cache_lock = threading.Lock()
_lib_building = {}          # key -> threading.Event，正在构建中的库（single-flight）
_lib_building_lock = threading.Lock()
_lib_gen = 0                # 缓存世代：失效时递增，避免把过期构建结果写回缓存
LIB_CACHE_TTL = 30


def _get_lib(root: Path) -> Lib:
    key = str(root)
    while True:
        _tasks.checkpoint()
        with _lib_cache_lock:
            hit = _lib_cache.get(key)
            if hit and time.time() - hit[0] < LIB_CACHE_TTL:
                return hit[1]
            gen = _lib_gen
        with _lib_building_lock:
            event = _lib_building.get(key)
            builder = event is None
            if builder:
                event = threading.Event()
                _lib_building[key] = event
        if not builder:
            # 等待者：被唤醒（成功/失败/超时）后回到循环开头，重新竞争构建权
            while not event.wait(timeout=0.1):
                _tasks.checkpoint()
            continue
        try:
            lib = _eng().Lib(root)
            _tasks.checkpoint()
            with _lib_cache_lock:
                if gen == _lib_gen:
                    _lib_cache[key] = (time.time(), lib)
            return lib
        finally:
            with _lib_building_lock:
                _lib_building.pop(key, None)
            event.set()

def _invalidate_lib_cache():
    global _lib_gen
    with _lib_cache_lock:
        _lib_cache.clear()
        _lib_gen += 1

def _normalize_title(name: str) -> str:
    """规范化目录/文件名：去 tmdb 后缀、全角转半角、所有分隔符统一成单空格"""
    if not name: return ''
    name = re.sub(r'\{?tmdb[-_:=\s]*\d+\}?', '', name, flags=re.I)
    name = name.replace('：', ':').replace('（', '(').replace('）', ')')
    name = re.sub(r'[_\-\.:\s]+', ' ', name)
    name = re.sub(r'\s+', ' ', name).strip()
    return name

def _find_dir_fuzzy(parent, target_name):
    """
    在 parent 下找最接近 target_name 的子目录。
    返回 (dir_or_None, level)，level ∈ {exact, normalized, fuzzy, ambiguous, none}：
      - exact       目录名完全相同        -> 高置信度
      - normalized  标准化后完全相等      -> 高置信度
      - fuzzy       前缀匹配且唯一候选    -> 低置信度（保留发现能力）
      - ambiguous   前缀匹配多候选        -> 禁止自动删
      - none        找不到                -> 禁止
    """
    if not parent.is_dir():
        return None, 'none'
    target_norm = _normalize_title(target_name)
    if not target_norm:
        return None, 'none'

    exact = parent / target_name
    if exact.is_dir():
        return exact, 'exact'

    for child in parent.iterdir():
        if child.is_dir() and _normalize_title(child.name) == target_norm:
            return child, 'normalized'

    prefix_matches = []
    try:
        for child in parent.iterdir():
            if not child.is_dir():
                continue
            cn = _normalize_title(child.name)
            if cn and (cn.startswith(target_norm) or target_norm.startswith(cn)):
                prefix_matches.append(child)
    except OSError:
        return None, 'none'
    if len(prefix_matches) == 1:
        return prefix_matches[0], 'fuzzy'
    if len(prefix_matches) > 1:
        return None, 'ambiguous'
    return None, 'none'

def _under_tv_category(d, root):
    try:
        parts = d.relative_to(root).parts
    except ValueError:
        return False
    return any(p in _engine_ns()._CATEGORY_NAMES and '剧' in p for p in parts)
