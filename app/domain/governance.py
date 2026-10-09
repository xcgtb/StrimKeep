# -*- coding: utf-8 -*-
"""
domain.governance —— 双库治理的纯决策规则

从 governance.py 原样迁出，行为零变化。
约束（由 tests/test_domain_quality.py 的纯度守卫强制）：
  - 不做 IO，不读环境变量，不 import 任何项目内基础设施模块；
  - 所有外部依赖（版本比较函数、平局开关、决策模型、集号解析、证据挂载）
    都以显式参数注入，由 services / governance.py 的包装层提供；
  - 「文件」只按鸭子类型使用 `.name`，不依赖 pathlib。

对外函数：
  share_wins / ep_share_ok     分享版是否胜出 / 逐集是否达标
  season_compare               同一季逐集对齐比较
  side_complete                一侧正片是否 E01 起连续无断层
  exempt_hit / nat_key         白名单命中 / 自然排序键
"""
import re


def nat_key(text):
    """自然排序：Season 9 < Season 10"""
    return [int(t) if t.isdigit() else t for t in re.split(r'(\d+)', str(text))]


def exempt_hit(name, kws):
    """白名单关键词命中列表（kws 为空返回 []）。"""
    if not kws:
        return []
    s = str(name)
    return [k for k in kws if k in s]


def share_wins(s_name, l_name, decision, cmp, tie_share_wins):
    """分享版本是否胜出：先看决策模型，再按 7 维对比，全部打平才用平局开关。

    decision: 'keep_local' / 'keep_share' / 其他（按画质）
    cmp(a_name, b_name) -> 1 / -1 / 0
    tie_share_wins: 全部打平时分享是否胜出
    """
    if decision == 'keep_local':
        return False
    if decision == 'keep_share':
        return True
    r = cmp(s_name, l_name)
    return r > 0 or (r == 0 and tie_share_wins)


def ep_share_ok(s_name, l_name, cmp, tie_share_wins):
    """剧集逐集：分享版是否「达标」——7 维分胜负；全部打平按「平局保留本地/分享」开关。"""
    r = cmp(s_name, l_name)
    return r > 0 or (r == 0 and tie_share_wins)


def side_complete(keys):
    """某一侧正片（E01 起）是否连续无断层。
    E00（第0集）是特别集，不参与；前序缺失（min>1）或中间断层均为残次品。"""
    ks = {k for k in keys if k > 0}
    if not ks:
        return False
    lo, hi = min(ks), max(ks)
    return lo == 1 and len(ks) == (hi - lo + 1)


def season_compare(s_files, l_files, ep_of, cmp, tie_share_wins, compare_meta):
    """
    逐集对齐比较分享与本地同一季（单向择优）。同集号多份时取各自最高画质参与比较。

    ep_of(f) -> (season, episode) | None
    cmp(a_name, b_name) -> 1 / -1 / 0
    compare_meta(s_name, l_name) -> dict   （季级对比依据，用于 sample 展示）
    返回 dict：s_eps / l_eps / s_only / l_only / common / s_better / l_better /
              sample / s_complete / l_complete / complete
    """
    s_eps, l_eps = {}, {}
    for f in (s_files or []):
        ep = ep_of(f)
        # ep[0]<=0: S00 目录；ep[1]<=0: E00 第0集（特别集）——均不参与对照与完整性判定
        if not ep or ep[0] <= 0 or ep[1] <= 0:
            continue
        old = s_eps.get(ep[1])
        if old is None or cmp(f.name, old.name) > 0:
            s_eps[ep[1]] = f
    for f in (l_files or []):
        ep = ep_of(f)
        if not ep or ep[0] <= 0 or ep[1] <= 0:
            continue
        old = l_eps.get(ep[1])
        if old is None or cmp(f.name, old.name) > 0:
            l_eps[ep[1]] = f
    s_keys, l_keys = set(s_eps), set(l_eps)
    common = s_keys & l_keys
    # 代表集：优先取第一个「非平局」的共同集，让 Web 能展示决胜依据；全平则取第一集
    sample = None
    for e in sorted(common):
        if cmp(s_eps[e].name, l_eps[e].name) != 0:
            sample = e
            break
    if sample is None and common:
        sample = min(common)
    s_better = l_better = 0
    for e in common:
        if ep_share_ok(s_eps[e].name, l_eps[e].name, cmp, tie_share_wins):
            s_better += 1
        else:
            l_better += 1
    s_complete, l_complete = side_complete(s_keys), side_complete(l_keys)
    return {
        's_eps': s_eps, 'l_eps': l_eps,
        's_only': sorted(s_keys - l_keys),
        'l_only': sorted(l_keys - s_keys),
        'common': common, 's_better': s_better, 'l_better': l_better,
        'sample': (dict(compare_meta(s_eps[sample].name, l_eps[sample].name), episode=sample)
                   if sample is not None else None),
        's_complete': s_complete,
        'l_complete': l_complete,
        'complete': s_complete and l_complete,
    }


# ═══════════════════ 剧集整季裁决（从 governance._emit_* 抽出的分支判断） ═══════════════════
# 这些函数只回答「该怎么办」，不构造任何 Act / 文案，也不碰文件。
# 输入的 cmp 即 season_compare() 的返回值。

def residual_flags(cmp, has_local, has_share):
    """残次品独立判定：谁不完整删谁。返回 (删本地, 删分享)。
    has_* 表示该侧确实有文件（没有文件就无从删起）。"""
    return (bool(not cmp['l_complete'] and has_local),
            bool(not cmp['s_complete'] and has_share))


def share_covers_local(cmp):
    """分享这一季是否覆盖本地全部集号（keep_share 删本地的安全前提）。"""
    return set(cmp['l_eps']) <= set(cmp['s_eps'])


def season_verdict(cmp, has_local, has_share, replace_ratio):
    """单季治理裁决（原 _emit_season_act 的全部分支）。

    返回 dict：
      residual_local / residual_share : 是否产生「残次品」删除动作（可同时为真）
      outcome :
        'incomplete'          任一侧不完整 → 只处理残次品，不再比画质
        'no_common'           无共同集号 → 静默
        'share_wins'          分享达标率 ≥ 阈值且集数不少于本地 → 删本地
        'local_wins_fewer'    分享集数少于本地 → 淘汰分享（集数优先）
        'local_wins_quality'  其余 → 本地画质更优，淘汰分享
    """
    res_l, res_s = residual_flags(cmp, has_local, has_share)
    out = {'residual_local': res_l, 'residual_share': res_s}
    if not cmp['l_complete'] or not cmp['s_complete']:
        out['outcome'] = 'incomplete'
        return out
    common = len(cmp['common'])
    if common == 0:
        out['outcome'] = 'no_common'
        return out
    s_total, l_total = len(cmp['s_eps']), len(cmp['l_eps'])
    share_ratio = cmp['s_better'] / common if common else 0.0
    if share_ratio >= replace_ratio and s_total >= l_total:
        out['outcome'] = 'share_wins'
    elif s_total < l_total:
        out['outcome'] = 'local_wins_fewer'
    else:
        out['outcome'] = 'local_wins_quality'
    return out


def multi_all_pass(l_proper, s_proper, season_cmp, replace_ratio):
    """多季保护「两边季集合包含」时：分享是否对本地每一季都逐集达标（整剧零和，整体删本地的前提）。

    l_proper / s_proper : {季号: 文件列表}
    season_cmp(s_files, l_files) -> cmp
    任一季缺失 / 无共同集 / 任一侧不完整 / 达标率不足 / 分享集数少于本地 → False。
    """
    for sn, l_files in l_proper.items():
        if sn not in s_proper:
            return False
        cmp = season_cmp(s_proper[sn], l_files)
        common = len(cmp['common'])
        if common == 0:
            return False
        if not cmp['l_complete'] or not cmp['s_complete']:
            return False
        if cmp['s_better'] / common < replace_ratio:
            return False
        if len(cmp['s_eps']) < len(cmp['l_eps']):
            return False
    return True


def multi_protect_mode(s_keys, l_keys):
    """多季保护「开启」档下，按两边季集合关系选择处理模式：
      'share_protect' 本地季是分享季的真子集 → 保护分享合集
      'full'          分享季是本地季的子集（含相等）→ 整体比较 / 保护本地合集
      'overlap'       双方各有对方没有的季 → 同季两边都不动，只处理残次品
    """
    S, L = set(s_keys), set(l_keys)
    if L < S:
        return 'share_protect'
    if S <= L:
        return 'full'
    return 'overlap'


def share_protect_verdict(cmp):
    """分享保护模式下、已排除残次品之后的单季裁决：
      'skip'          本地这一季没有可比集 → 不动
      'share_fewer'   分享没覆盖本地全部集 → 淘汰分享（本地更全）
      'delete_local'  分享覆盖本地 → 淘汰本地同季副本（保护分享合集）
    """
    if not cmp['l_eps']:
        return 'skip'
    if not share_covers_local(cmp):
        return 'share_fewer'
    return 'delete_local'


def partial_share_reasons(cmp, gap_desc):
    """多季保护「分享未全覆盖」时，列出该季被淘汰的全部原因（残次品 / 集数不足 / 画质次级）。
    gap_desc(keys) -> 缺集描述文本（注入，保持本模块无依赖）。"""
    common = len(cmp['common'])
    reasons = []
    if not cmp['l_complete']:
        reasons.append('本地' + gap_desc(cmp['l_eps'].keys()))
    if not cmp['s_complete']:
        reasons.append('分享' + gap_desc(cmp['s_eps'].keys()))
    if cmp['l_complete'] and cmp['s_complete']:
        if len(cmp['s_eps']) < len(cmp['l_eps']):
            reasons.append('分享副本集数不足 (%d/%d集)' % (len(cmp['s_eps']), len(cmp['l_eps'])))
        elif cmp['l_better'] > cmp['s_better']:
            reasons.append('分享画质次级 (%d/%d集)' % (cmp['l_better'], common))
    return reasons


def protect_s00(multi_season_protect, special_action, decision, l_season_keys, s_season_keys):
    """多季保护开启 + 特别篇「画质对比」+ 画质优先 + 任一侧 ≥2 季（含 S00）时，
    S00 算作一季参与多季保护，不再单独比画质。"""
    return bool(multi_season_protect == 'compare' and special_action == 'compare'
                and decision == 'quality_first'
                and (len([k for k in l_season_keys if k >= 0]) >= 2
                     or len([k for k in s_season_keys if k >= 0]) >= 2))
