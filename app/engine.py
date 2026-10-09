# -*- coding: utf-8 -*-
"""strimkeep.py —— 飞牛 NAS 端治理引擎"""
import contextlib
import dataclasses
import html
import os, re, sys, json, threading, shutil, fcntl, time, argparse, datetime, hashlib, logging, traceback
import urllib.parse, urllib.request, urllib.error
from pathlib import Path
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

# Old Web/Bot/CLI/test entry names remain aliases to their owning modules.
# Shared configuration, caches and locks below deliberately stay on engine.
if __package__:
    from . import config as _cfg
    from .compat import bind_engine_exports as _bind_engine_exports
else:
    import config as _cfg
    from compat import bind_engine_exports as _bind_engine_exports
_bind_engine_exports(globals(), __package__)


def _p(env, default):
    return Path(os.environ.get(env, default))

# 容器内路径固定：用户只需在 docker-compose 里把宿主机目录挂载到这三个位置。
# 环境变量覆盖仅供测试使用（tests/ 用它指向临时目录），不对用户开放、不写入文档。
L_ROOT = _p('L_ROOT', '/media/local')
S_ROOT = _p('S_ROOT', '/media/share')
CLOUD_L_ROOT = _p('CLOUD_L_ROOT', '/media/cloud')
# DATA_DIR 以 config 为准（config.py 是路径的唯一定义处），避免两处默认值漂移
DATA_DIR = _cfg.DATA_DIR
LOG_TXT = DATA_DIR / '媒体治理明细.log'
STATE_DIR = DATA_DIR / 'state'
LOCK_FILE = DATA_DIR / 'agent.lock'
REPORT_DIR = DATA_DIR / 'reports'
SUB_STATE_FILE = STATE_DIR / 'subscriptions_state.json'
EMBY_LIB_CACHE_FILE = STATE_DIR / 'emby_library_with_tmdb.json'
INGEST_CACHE_FILE = STATE_DIR / 'ingest_cache.json'
WASH_RESIDUAL_FILE = STATE_DIR / 'wash_residuals.json'
LIBRARY_SNAPSHOT_FILE = STATE_DIR / 'library_snapshot.json'

VIDEO_EXTS = ['.mkv', '.mp4', '.ts', '.mov', '.iso', '.m2ts']

# ═══════════════ 目录生命周期 / 孤儿治理（白皮书 §15+§16）═══════════════
_CATEGORY_NAMES = {
    '电影', '剧集', '儿童节目', '综艺', '动漫', '纪录片', '演唱会',
    '国产电影', '华语电影', '欧美电影', '日韩电影', '动画电影', '外语电影', '其他电影',
    '国产剧集', '欧美剧集', '日韩剧集', '其他剧集',
    '国产剧', '欧美剧', '日韩剧',
}
_METADATA_EXTS = {
    '.nfo', '.jpg', '.jpeg', '.png', '.gif', '.webp',
    '.srt', '.ass', '.ssa', '.sub', '.idx', '.sup',
    '.json', '.url', '.xml', '.md5', '.sha1',
}
PLAN_TTL = 2 * 3600
PRUNE_MIN_DEPTH = int(os.environ.get('PRUNE_MIN_DEPTH', '3'))

logging.basicConfig(stream=sys.stderr, level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('strimkeep')


def _ensure_tz():
    """统一时区：读 TZ 环境变量，缺省 Asia/Shanghai。
    镜像里没有 tzdata 时 TZ=Asia/Shanghai 会静默退回 UTC，这里检测到就改用 POSIX 写法 CST-8（不依赖 tzdata）。"""
    tz = (os.environ.get('TZ') or '').strip() or 'Asia/Shanghai'
    os.environ['TZ'] = tz
    try:
        time.tzset()
        if tz == 'Asia/Shanghai' and time.localtime().tm_gmtoff != 8 * 3600:
            log.warning('时区 Asia/Shanghai 未生效（镜像缺少 tzdata？），改用 CST-8')
            os.environ['TZ'] = 'CST-8'
            time.tzset()
    except (AttributeError, OSError) as e:
        log.warning('设置时区失败: %s', e)


_ensure_tz()


def tz_info():
    """当前进程使用的时区，供前端/健康检查核对（晨报、巡检都按这个时区算）"""
    lt = time.localtime()
    off = int(getattr(lt, 'tm_gmtoff', 0) or 0)
    sign = '+' if off >= 0 else '-'
    off = abs(off)
    return {'name': os.environ.get('TZ', ''), 'offset': f'UTC{sign}{off // 3600:02d}:{off % 3600 // 60:02d}',
            'now': time.strftime('%Y-%m-%d %H:%M:%S', lt)}


def _norm_emby_path(p):
    """Emby 路径统一成 / 分隔、去掉末尾 /；空值返回 ''"""
    p = str(p or '').strip().replace('\\', '/')
    return p.rstrip('/')


_LEGACY_LIB_NAMES = ('影视媒体库', '分享影视库')


class EmbyPathMap:
    """Emby 路径 → 本地/分享库 的唯一映射处（根目录来自配置 emby_local_path / emby_share_path）。
    片库总览要对 ~10 万个分集路径分类，所以这里只做字符串前缀比较，构造时把能算的都算好。"""

    def __init__(self, local_root, share_root):
        self.local = _norm_emby_path(local_root)
        self.share = _norm_emby_path(share_root)
        # (根目录, 根目录+'/', 库) —— 长的优先（嵌套时取更精确的那个），等长时本地在前，与旧逻辑一致
        roots = [(r, r + '/', lib) for r, lib in ((self.local, 'local'), (self.share, 'share')) if r]
        self._roots = sorted(roots, key=lambda x: -len(x[0]))
        # 兼容兜底（只用于展示）：路径里出现根目录的末级目录名。原来写死的两个名字保持旧版
        # '影视媒体库' in path 的子串判断，老用户行为不变；其它名字要求整段目录名相等，
        # 免得 tv 误中 tv2。两个根末级名相同时无法区分，干脆不兜底
        lb = self.local.rsplit('/', 1)[-1] if self.local else ''
        sb = self.share.rsplit('/', 1)[-1] if self.share else ''
        self._bases = [(b if b in _LEGACY_LIB_NAMES else '/' + b + '/', lib)
                       for b, lib in ((lb, 'local'), (sb, 'share')) if b] if lb != sb else []

    def lib_of(self, path, fallback=True):
        """展示/统计用：'local' | 'share' | ''"""
        if not path:
            return ''
        p = path.replace('\\', '/') if '\\' in path else path
        for root, pre, lib in self._roots:
            if p == root or p.startswith(pre):
                return lib
        if fallback and self._bases:
            padded = '/' + p + '/'
            for needle, lib in self._bases:
                if needle in padded:
                    return lib
        return ''

    def to_container(self, path):
        """删除用：严格前缀匹配（不走末级目录名兜底），转成容器内路径；对不上或含 .. 返回 None"""
        if not path:
            return None
        p = str(path).replace('\\', '/')
        for root, pre, lib in self._roots:
            if p.startswith(pre):
                parts = [x for x in p[len(pre):].split('/') if x and x != '.']
                if not parts or '..' in parts:
                    return None
                return (L_ROOT if lib == 'local' else S_ROOT).joinpath(*parts)
        return None


RUNTIME_CFG = _cfg.load_config()
EMBY_HOST = RUNTIME_CFG['emby_host']
EMBY_KEY  = RUNTIME_CFG['emby_key']


def emby_path_map(local_root=None, share_root=None):
    """按配置构造 EmbyPathMap；留空的一侧回落到默认值（设置页输入框的占位符就是默认值，
    清空后若变成「什么都匹配不上」，与界面提示不符）"""
    return EmbyPathMap((local_root or '').strip() or _cfg.DEFAULTS['emby_local_path'],
                       (share_root or '').strip() or _cfg.DEFAULTS['emby_share_path'])


EMBY_PATHS = emby_path_map(RUNTIME_CFG.get('emby_local_path'), RUNTIME_CFG.get('emby_share_path'))
TMDB_BASE = os.environ.get('TMDB_BASE', 'https://api.themoviedb.org/3')
TMDB_LANG = os.environ.get('TMDB_LANG', 'zh-CN')
TMDB_IMG  = os.environ.get('TMDB_IMG', 'https://image.tmdb.org/t/p/w500')
TMDB_INFO_TTL = 24 * 3600


def reload_config():
    global RUNTIME_CFG, EMBY_HOST, EMBY_KEY, EMBY_PATHS
    RUNTIME_CFG = dict(_cfg.DEFAULTS, **_cfg.load_config())
    EMBY_HOST = RUNTIME_CFG['emby_host'] or 'http://127.0.0.1:8096'
    EMBY_KEY = RUNTIME_CFG['emby_key'] or ''
    EMBY_PATHS = emby_path_map(RUNTIME_CFG.get('emby_local_path'), RUNTIME_CFG.get('emby_share_path'))
    return RUNTIME_CFG


def emby_lib_of(path):
    """Emby 路径属于哪个库（展示/统计用，带末级目录名兜底）：'local' | 'share' | ''"""
    return EMBY_PATHS.lib_of(path)


# ═══════════════════ 电视剧逐集比较（白皮书 §7）═══════════════════


# 逐集择优阈值：交集内分享画质达标集数占比 >= 该比例才删本地（否则删分享或保留）。
SEASON_REPLACE_RATIO = 0.9


# ═══════════════════ Emby ═══════════════════
EMBY_TIMEOUT = int(os.environ.get('EMBY_TIMEOUT', '30') or 30)   # 秒；大库 / NAS 繁忙时 15 秒容易超时
EMBY_RETRIES = 2                                                  # 仅 GET 在超时 / 5xx / 连接错误时重试


# ═══════════════════ Telegram 排版助手 ═══════════════════
_WEEK = '一二三四五六日'


# ═══════════════════ Lib 短时效缓存 ═══════════════════
# 作用：短时间内多个只读入口（白名单命中预览等）连续调用 build_plan/_get_lib 时，
# 30 秒内复用同一份双库遍历结果，大库上省几秒到几十秒。
# 注意：「扫描双库」完成后和「清理成功」后都会主动失效缓存——
# 执行清理前的二次校验必须基于最新磁盘状态，不能吃扫描时的快照。


_ep_cache = {'ts': 0, 'data': None}
_WASH_RESIDUAL_LOCK = threading.Lock()
_ep_lock = threading.Lock()


_SIDECAR_SEPS = '.-_ [('


_RE_DIR_TMDB = re.compile(r'(?i)tmdb(?:id)?[-_=: ]*(\d+)')


_ORPHAN_IGNORE_NAMES = {'thumbs.db', 'desktop.ini', '.ds_store'}


# ═══════════════════ 入库静默期 ═══════════════════
# 剧集/电影刚入库时往往只入了一部分（分享转存、STRM 还在陆续生成），
# 此时对比双库会得到"分享仅 3 集、本地 6 集 → 淘汰分享"这类偏差结论。
# 规则：一个标题（含双库、所有季）的任一目录在 ingest_quiet_minutes 分钟内有新增/变动，
# 就整体不进入治理队列（静默跳过，不出现在清单里）；过了静默期再自然纳入。
# 判定用目录 mtime（每个季目录只 stat 一次，几千次即可覆盖整库），不逐文件 stat。
_QUIET_LAST = {'n': 0}


GOV_LATEST_FILE = STATE_DIR / 'gov_latest.json'


_emby_index_cache = {'ts': 0, 'data': None}
_EMBY_INDEX_CACHE_FILE = STATE_DIR / 'emby_index_cache.json'
_emby_index_refresh_lock = threading.Lock()


REGION_MAP = {
    'cn': {'with_origin_country': 'CN'}, 'hk': {'with_origin_country': 'HK'},
    'tw': {'with_origin_country': 'TW'}, 'jp': {'with_origin_country': 'JP'},
    'kr': {'with_origin_country': 'KR'}, 'us': {'with_origin_country': 'US'},
}
SORT_MAP = {
    'popularity': 'popularity.desc', 'release': 'primary_release_date.desc',
    'rating': 'vote_average.desc',
}

GENRE_MAP = {
    # 与 TMDB 官方 genre 列表对齐（movie: /genre/movie/list, tv: /genre/tv/list）
    '动作':   {'movie': 28,    'tv': 10759},
    '冒险':   {'movie': 12,    'tv': None},
    '喜剧':   {'movie': 35,    'tv': 35},
    '犯罪':   {'movie': 80,    'tv': 80},
    '纪录':   {'movie': 99,    'tv': 99},
    '剧情':   {'movie': 18,    'tv': 18},
    '家庭':   {'movie': 10751, 'tv': 10751},
    '奇幻':   {'movie': 14,    'tv': None},
    '历史':   {'movie': 36,    'tv': None},
    '恐怖':   {'movie': 27,    'tv': None},
    '音乐':   {'movie': 10402, 'tv': None},
    '悬疑':   {'movie': 9648,  'tv': 9648},
    '爱情':   {'movie': 10749, 'tv': None},
    '科幻':   {'movie': 878,   'tv': 10765},
    '惊悚':   {'movie': 53,    'tv': None},
    '战争':   {'movie': 10752, 'tv': 10768},
    '西部':   {'movie': 37,    'tv': 37},
    '动画':   {'movie': 16,    'tv': 16},
    '儿童':   {'movie': None,  'tv': 10762},
    '新闻':   {'movie': None,  'tv': 10763},
    '真人秀': {'movie': None,  'tv': 10764},
    '肥皂剧': {'movie': None,  'tv': 10766},
    '脱口秀': {'movie': None,  'tv': 10767},
    '电视电影': {'movie': 10770, 'tv': None},
}


_emby_lib_cache = {'ts': 0, 'data': None}


_EMBY_OVERVIEW_CACHE_FILE = STATE_DIR / 'emby_overview_cache.json'
_overview_refresh_lock = threading.Lock()


_MEDIA_EXT = {'.strm', '.mkv', '.mp4', '.ts', '.m2ts', '.avi', '.mov', '.wmv', '.flv', '.rmvb', '.iso'}


MANUAL_DONE_FILE = STATE_DIR / 'manual_done.json'


# ═══════════════════ 入库监控（缓存层） ═══════════════════


# ═══════════════ 探索页实时集数（绕过统一快照）═══════════════
# 背景：探索页此前依赖 library_snapshot.json（30 分钟强制缓存），快照本身又建立在
# emby_library_overview 的产物之上，而那个磁盘缓存只在「片库映射」全量对照时才写。
# 三者新鲜度互不相同，导致刚入库的集在探索页迟迟不更新，且刷新页面永远不自愈。
# 这里的实时查询只问「这一部剧现在有哪些集」，1 次 Series + 1~2 次 Episode，
# 单剧毫秒级；配合并发 + 短 TTL 微缓存，既跟得上入库又能扛住连点刷新。
_LIVE_EPS_TTL = 15
_live_eps_cache = {}
_live_eps_lock = threading.Lock()


# ═══════════════════ 晨报 ═══════════════════
MORNING_GAP_TOP = 50   # 晨报里最多列出多少部缺集剧（按缺得最多排序），完整清单看 Web
MORNING_INGEST_TOP = 60  # 晨报里最多列出多少条入库明细（最近入库置顶），完整清单看 Web


_tmdb_scan_progress = {
    'running': False, 'total': 0, 'done': 0, 'stage': '',
    'started_at': 0, 'finished_at': 0, 'stats': {}, 'error': '',
}


_tmdb_scan_lock = threading.Lock()


# ============ 性能缓存（5 分钟 TTL）============
_strm_count_cache = {'ts': 0, 'local': 0, 'share': 0}
_lib_stats_cache = {'ts': 0, 'data': None}
_CACHE_TTL = 300


_STRM_COUNT_CACHE_FILE = STATE_DIR / 'strm_count_cache.json'
_strm_count_refreshing = threading.Lock()


ACTIONS = {
    'inter_check':  action_inter_check,
    'inter_clean':  action_inter_clean,
    'stats':        action_stats,
    'played':       action_played,
    'search':       action_search,
    'logs':         action_logs,
    'explore':      action_explore,
    'emby_library': action_emby_library,
    'library_stats': action_library_stats,
    'orphans':       action_scan_orphans,
    'clean_orphans': action_clean_orphan_dirs,
}
MUTATING = {'inter_clean', 'clean_orphans'}


def main():

    ap = argparse.ArgumentParser()
    ap.add_argument('--action', required=True, choices=list(ACTIONS))
    ap.add_argument('--kw', default='')
    ap.add_argument('--plan', default='')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()
    # 互斥锁现在统一由 action_inter_clean 自己持有（见该函数），
    # 这样 CLI / Web / Telegram Bot 三个入口共用同一把锁，这里不再重复加锁。
    try:
        res = ACTIONS[args.action](args)
    except Exception as e:
        traceback.print_exc(file=sys.stderr)
        res = {'status': 'error', 'message': f'{type(e).__name__}: {e}'}
    print(json.dumps(res, ensure_ascii=False))


if __name__ == '__main__':
    main()
