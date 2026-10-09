# -*- coding: utf-8 -*-
"""
strimkeep.core —— 纯函数层
设计约束：不做任何 IO，不读取环境变量，给定相同输入必然返回相同输出。
"""
import re
from functools import lru_cache
from typing import Iterable, Optional, Sequence, Tuple

# ═══════════════════ 正则常量 ═══════════════════
RE_SEASON_DIR = re.compile(r'(?i)^(?:season[ ._-]*(\d{1,3})|s(\d{1,3})|第\s*(\d{1,3})\s*季)(?![0-9a-z])')
RE_SPECIAL_DIR = re.compile(r'(?i)^(?:specials?|特别篇|特典)(?![0-9a-z])')
RE_SXXEXX = re.compile(r'(?i)(?<![a-z0-9])s(\d{1,2})[ ._-]*e(?:p)?[ ._-]*(\d{1,4})(?!\d)')
RE_EP = re.compile(r'(?i)(?<![a-z0-9])(?:ep?|sp)[ ._-]*(\d{1,3})(?!\d)|第\s*(\d{1,4})\s*[集话話期]')
RE_SP_NAME = re.compile(r'(?i)特别篇|(?<![a-z0-9])sp[ ._-]*\d+(?![a-z0-9])')
RE_TMDB = re.compile(r'(?i)tmdb(?:id)?[=\-: ]*(\d+)')
RE_BRACKET = re.compile(r'\[.*?\]|\{.*?\}|\(.*?\)')
RE_YEAR = re.compile(r'\((\d{4})\)')


# ═══════════════════ 默认数据 ═══════════════════
DEFAULT_EXEMPT_KEYWORDS: Tuple[str, ...] = ()
DEFAULT_CATEGORIES: Tuple[str, ...] = (
    '👶 儿童节目', '🎤 演唱会', '🎪 综艺剧', '⛩️ 动漫剧', '🧸 动画电影', '📽️ 纪录片',
    '🇨🇳 国产剧', '🇺🇸 欧美剧', '🇯🇵 日韩剧', '🇨🇳 华语电影', '🌐 外语电影',
)
DEFAULT_KIDS_KW: Tuple[str, ...] = (
    '儿童', '少儿', '亲子', '早教', '朵拉', '佩奇', 'jojo', '布鲁伊', '熊出没',
    '喜羊羊', '贝瓦', '汪汪队', '宝宝巴士', '神厨小福贵', '虹猫蓝兔', '神兵小将',
    '葫芦兄弟', '大头儿子',
)

# ═══════════════════ 文本处理 ═══════════════════
def esc(s) -> str:
    """Markdown 转义"""
    return re.sub(r'([_*`\[])', r'\\\1', str(s))

@lru_cache(maxsize=8192)
def parse_season_dir(name: str) -> Optional[int]:
    """解析季目录名"""
    name = name.strip()
    m = RE_SEASON_DIR.match(name)
    if m:
        return int(next(g for g in m.groups() if g is not None))
    if RE_SPECIAL_DIR.match(name):
        return 0
    return None

def get_ep(name: str, parent_name: str = '', allow_bare_ep: bool = True) -> Optional[Tuple[int, int]]:
    """从文件名+父目录推断 (季, 集)。

    ``allow_bare_ep`` 只允许在明确的剧集上下文中打开。
    裸 ``EP04`` / ``Episode 4`` 本身并不足以证明文件是电视剧；电影
    （例如 ``Star Wars Ep 4``）不应因此被错误归入 S01E04。
    SxxExx、季目录和 Specials 仍是强证据。
    """
    sd = parse_season_dir(parent_name) if parent_name else None
    m = RE_SXXEXX.search(name)
    if m:
        return int(m.group(1)), int(m.group(2))
    em = RE_EP.search(name)
    ep = int(em.group(1) or em.group(2)) if em else None
    if sd == 0 or RE_SP_NAME.search(name):
        return 0, (ep if ep is not None else 1)
    if sd is not None:
        return (sd, ep) if ep is not None else None
    if allow_bare_ep and ep is not None:
        return 1, ep
    return None

@lru_cache(maxsize=16384)
def title_key(folder: str) -> Tuple[str, str, str, Optional[str]]:
    """返回历史兼容的 (key, 展示名, base, 年份)。

    注意：这里保留 TMDB key 语义给旧接口/工具使用；双库治理不得把 TMDB
    编号当作文件系统事实身份，治理侧使用 :func:`governance_title_key`。
    """
    t = RE_TMDB.search(folder)
    y = RE_YEAR.search(folder)
    year = y.group(1) if y else None
    base = re.sub(r'\s+', ' ', RE_BRACKET.sub('', folder)).strip() or folder
    disp = base + (f' ({year})' if year else '')
    return (f'tmdb:{t.group(1)}' if t else disp), disp, base, year


@lru_cache(maxsize=16384)
def governance_title_key(folder: str) -> Tuple[str, str, str, Optional[str]]:
    """双库治理的真实媒体身份：剧名 + 年份。

    TMDB 只是第三方元数据，Emby/整理工具把错误的 TMDB ID 写进目录名时，
    不能因此把《A》与《B》合并，更不能因此产生跨片治理动作。治理扫描
    必须先以用户可见的目录剧名和年份建立身份；TMDB ID 仅作为辅助信息。
    """
    y = RE_YEAR.search(folder)
    year = y.group(1) if y else None
    base = re.sub(r'\s+', ' ', RE_BRACKET.sub('', folder)).strip() or folder
    norm = re.sub(r'[._:\-]+', ' ', base)
    norm = re.sub(r'\s+', ' ', norm).strip().casefold()
    key = f'title:{norm}|year:{year or ""}'
    disp = base + (f' ({year})' if year else '')
    return key, disp, base, year

# ═══════════════════ 集数判定 ═══════════════════
def is_exempt(name: str, keywords: Optional[Sequence[str]] = None) -> bool:
    if keywords is None: keywords = DEFAULT_EXEMPT_KEYWORDS
    return any(k in str(name) for k in keywords)

def fmt_nums(nums: Sequence[int]) -> str:
    if not nums: return ''
    ranges, start, prev = [], nums[0], nums[0]
    for n in nums[1:]:
        if n == prev + 1: prev = n
        else:
            ranges.append(f'E{start:02d}-E{prev:02d}' if start != prev else f'E{start:02d}')
            start = prev = n
    ranges.append(f'E{start:02d}-E{prev:02d}' if start != prev else f'E{start:02d}')
    return ','.join(ranges)

def analyze_season_episodes(eps: Iterable[int], name: str = '', exempt_keywords: Optional[Sequence[str]] = None) -> Tuple[bool, str]:
    eps = list(eps)
    if not eps: return False, '空剧集'
    if is_exempt(name, exempt_keywords): return True, '白名单豁免'
    se = sorted(set(eps))
    lo, hi = se[0], se[-1]
    pre = list(range(1, lo)) if lo > 1 else []
    mid = sorted(set(range(lo, hi + 1)) - set(se))
    if not pre and not mid: return True, '完整'
    reasons = []
    if pre: reasons.append(f'缺失前序 (缺 {fmt_nums(pre)})')
    if mid: reasons.append(f'中间断层 (缺 {fmt_nums(mid)}，疑似被和谐)')
    return False, ' 且 '.join(reasons)

def is_seq(eps: Iterable[int], name: str = '', exempt_keywords: Optional[Sequence[str]] = None) -> bool:
    valid, _ = analyze_season_episodes(eps, name, exempt_keywords)
    return valid

# ═══════════════════ Emby 分类 ═══════════════════
def parse_emby_library(item: dict, is_movie: bool = False, categories: Sequence[str] = DEFAULT_CATEGORIES, kids_keywords: Sequence[str] = DEFAULT_KIDS_KW) -> str:
    path = item.get('Path', '') or ''
    for c in categories:
        parts = c.split(' ', 1)
        if len(parts) > 1 and parts[1] in path: return c
    name = item.get('SeriesName') or item.get('Name') or ''
    text = f"{path} {name} {' '.join(item.get('Genres', []))}".lower()
    if any(k in text for k in kids_keywords): return categories[0]
    if any(k in text for k in ['演唱会', '音乐会', 'concert', '左麟右李']) or re.search(r'\blive\b', text): return categories[1]
    if any(k in text for k in ['综艺', '真人秀', 'talk show', '乘风破浪', '奔跑吧']) or re.search(r'\breality\b', text): return categories[2]
    if any(k in text for k in ['动漫', '国漫', '日漫', 'anime', '动画', 'animation']): return categories[4] if is_movie else categories[3]
    if any(k in text for k in ['纪录', 'documentary', '纪实']): return categories[5]
    if not is_movie:
        if any(k in path for k in ['欧美', '美剧', '英剧']): return categories[7]
        if any(k in path for k in ['日韩', '日本', '韩剧', '韩国']): return categories[8]
        return categories[6]
    return categories[9] if any(k in path for k in ['华语', '国产', '大陆', '港台']) else categories[10]


# ═══════════════════ 画质对比（已迁移至 domain/quality.py，此处仅为兼容转发） ═══════════════════
# 旧代码/测试继续 `from core import compare_cover` 等即可；新代码请直接依赖 domain.quality。
try:
    from .domain.quality import (  # noqa: F401
        COVER_RULE_ORDER,
        COVER_RULE_META,
        COVER_TIERS_DEFAULT,
        COVER_ENABLED_DEFAULT,
        RE_DV,
        _cover_rule,
        cover_default_strategy,
        normalize_cover,
        _dim_source,
        _dim_resolution,
        _dim_dolby,
        _dim_bitdepth,
        _dim_audio,
        _dim_fps,
        _dim_release_group,
        recognize_dims,
        _tier_rank,
        _dim_rank,
        _active_rules,
        compare_cover,
        explain_compare,
        quality_label,
    )
except ImportError:
    from domain.quality import (  # noqa: F401
        COVER_RULE_ORDER,
        COVER_RULE_META,
        COVER_TIERS_DEFAULT,
        COVER_ENABLED_DEFAULT,
        RE_DV,
        _cover_rule,
        cover_default_strategy,
        normalize_cover,
        _dim_source,
        _dim_resolution,
        _dim_dolby,
        _dim_bitdepth,
        _dim_audio,
        _dim_fps,
        _dim_release_group,
        recognize_dims,
        _tier_rank,
        _dim_rank,
        _active_rules,
        compare_cover,
        explain_compare,
        quality_label,
    )
