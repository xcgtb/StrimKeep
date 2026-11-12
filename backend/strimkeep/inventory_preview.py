"""双库治理判定：按 1.2.12 的季级规则并增加用户要求的治理顺序。

来源：`app/domain/governance.py`（纯规则）+ `app/governance.py`
（`_build_plan_with_libs` / `_emit_season_act` / `_emit_multi_protect_compare` /
`_emit_season_acts_full` / `_emit_share_protect_acts` / `_dedupe_lib_versions`）。

判定顺序：

电影
  - 同一身份在任一库里有分集 → 这是一部剧，不按电影整体比画质。
  - 两侧各取最优文件，`share_wins`：先看决策模型，再按画质，全平才用平局开关。

剧集
  - 白名单：标题或任一文件路径命中 → 整部豁免（按标题聚合成一条）。
  - 静默期：任一侧任一季目录近期有变动 → 整部跳过；stat 不到按"没变动"处理。
  - 特别篇 S00：`delete`（两库都删）/ `compare`（比画质，多季保护未接管时）/ `ignore`。
  - 决策模型 `keep_local` / `keep_share`：逐共同季直接取舍（keep_share 要求分享覆盖本地）。
  - 多季保护 `compare`（至少一侧 ≥2 季）：share_protect / full / overlap 三档。
  - 否则逐季：残次品优先（两侧独立），`incomplete` / `no_common` 到此为止；
    完整才比画质：share_wins → 删本地，local_wins_fewer / local_wins_quality → 删分享。

库内多版本及缺集季先处理；同季未清理则不产生双库删除动作。
多季保护依赖整组季数据，任一相关季不干净时整组等待。双库删除要求保留侧覆盖删除侧。
"""
from collections import defaultdict
import math
import time

from .inventory_rules import (best_index, cmp_versions, compare_outcome, episode_gaps, multi_protect_mode,
                              protect_s00, share_covers_local,
                              share_protect_verdict)
from .media import NeedsReview


def _best(rows, policy):
    """同侧多份取最优（1.2.12 `_best`）。"""
    return rows[best_index([row['name'] for row in rows], policy)]


def _share_wins(share_name, cloud_name, policy, decision, tie_share_wins):
    """1.2.12 `share_wins`：决策模型优先，其次画质，最后平局开关。"""
    if decision == 'keep_local':
        return False
    if decision == 'keep_share':
        return True
    outcome = cmp_versions(share_name, cloud_name, policy)
    return outcome > 0 or (outcome == 0 and tie_share_wins)


def _fmt_nums(numbers):
    """集号压缩成 E01-E05,E07（1.2.12 `core.fmt_nums`）。"""
    numbers = sorted(set(numbers))
    if not numbers:
        return ''
    ranges, start, prev = [], numbers[0], numbers[0]
    for number in numbers[1:]:
        if number == prev + 1:
            prev = number
            continue
        ranges.append('E%02d-E%02d' % (start, prev) if start != prev else 'E%02d' % start)
        start = prev = number
    ranges.append('E%02d-E%02d' % (start, prev) if start != prev else 'E%02d' % start)
    return ','.join(ranges)


def _gap_text(episodes):
    """缺集描述（1.2.12 `core.analyze_season_episodes` 的口径）。

    分清两种残次品：前序缺失（从 E02 起、缺 E01）与中间断层（缺 E59，疑似被和谐）。
    """
    numbers = sorted({episode for episode in episodes or [] if isinstance(episode, int)
                      and not isinstance(episode, bool) and episode > 0})
    if not numbers:
        return '空剧集'
    head = list(range(1, numbers[0])) if numbers[0] > 1 else []
    middle = sorted(set(range(numbers[0], numbers[-1] + 1)) - set(numbers))
    if not head and not middle:
        return '完整'
    parts = []
    if head:
        parts.append('缺失前序 (缺 %s)' % _fmt_nums(head))
    if middle:
        parts.append('中间断层 (缺 %s，疑似被和谐)' % _fmt_nums(middle))
    return ' 且 '.join(parts)


def _gap_desc(episodes):
    text = _gap_text(episodes)
    return '' if text in ('完整', '空剧集') else text


def _action(side_to_delete, reason, label, season, **extra):
    """一条动作：`side_to_delete` 是被删的那一侧（cloud=本地/CD2，share=分享库）。"""
    return {'delete_side': side_to_delete, 'action_reason': reason, 'reason_label': label,
            'season': season, **extra}


def _season_compare(share_rows, cloud_rows, policy, tie):
    """逐集对齐比较（1.2.12 `season_compare`）：同集多份取各自最优，S00/E00 不参与。"""
    def best_by_episode(rows):
        grouped = defaultdict(list)
        for row in rows:
            episodes = row.get('episodes') or []
            if len(episodes) != 1 or episodes[0] <= 0:
                continue
            grouped[episodes[0]].append(row)
        return {episode: _best(files, policy) for episode, files in grouped.items()}

    share_best, cloud_best = best_by_episode(share_rows), best_by_episode(cloud_rows)
    share_keys, cloud_keys = set(share_best), set(cloud_best)
    common = sorted(share_keys & cloud_keys)
    scores = {'share': 0, 'cloud': 0}
    sample = None
    comparisons = []
    for episode in common:
        outcome, evidence = compare_outcome(share_best[episode]['name'], cloud_best[episode]['name'], policy)
        winner = 'share' if outcome > 0 else 'cloud' if outcome < 0 else tie
        scores[winner] += 1
        comparisons.append({'episode': episode, 'share_name': share_best[episode]['name'],
                            'cloud_name': cloud_best[episode]['name'], 'result': outcome,
                            'winner': winner, 'tie': outcome == 0, 'rules': evidence})
        if sample is None or (sample['result'] == 0 and outcome != 0):
            sample = {'episode': episode,
                      'share_name': share_best[episode]['name'],
                      'cloud_name': cloud_best[episode]['name'], 'result': outcome, 'rules': evidence}
    return {'share': share_best, 'cloud': cloud_best, 'common': common,
            'scores': scores,
            'ratio': (scores['share'] / len(common)) if common else 0.0,
            'sample': sample,
            'comparisons': comparisons,
            'complete': {'share': bool(share_keys) and episode_gaps(share_keys)['complete'],
                         'cloud': bool(cloud_keys) and episode_gaps(cloud_keys)['complete']}}


def _season_actions(compare, season, threshold):
    """单季裁决（1.2.12 `_emit_season_act`）：残次品优先，完整才比画质。"""
    actions = []
    if compare['cloud'] and not compare['complete']['cloud']:
        actions.append(_action('cloud', 'incomplete_delete_local', '残次品-删本地', season,
                               gap=_gap_desc(compare['cloud'])))
    if compare['share'] and not compare['complete']['share']:
        actions.append(_action('share', 'incomplete_delete_share', '残次品-删分享', season,
                               gap=_gap_desc(compare['share'])))
    if not (compare['complete']['share'] and compare['complete']['cloud']):
        return actions, 'incomplete'
    if not compare['common']:
        return actions, 'no_common'
    ratio, share_total, cloud_total = (compare['ratio'], len(compare['share']),
                                       len(compare['cloud']))
    if ratio >= threshold and share_total >= cloud_total:
        actions.append(_action('cloud', 'share_wins', '分享择优-删本地', season,
                               outcome='share_wins', ratio=ratio, sample=compare['sample']))
        return actions, 'share_wins'
    if share_total < cloud_total:
        actions.append(_action('share', 'local_wins', '本地更全-删分享', season,
                               outcome='local_wins_fewer',
                               ratio=ratio, sample=compare['sample']))
        return actions, 'local_wins_fewer'
    actions.append(_action('share', 'local_wins', '本地择优-删分享', season,
                           outcome='local_wins_quality', ratio=ratio, sample=compare['sample']))
    return actions, 'local_wins_quality'


def _multi_all_pass(sp, lp, policy, tie, threshold):
    """1.2.12 `multi_all_pass`：分享是否对本地每一季都逐集达标（整剧零和的前提）。"""
    for season, cloud_rows in lp.items():
        if season not in sp:
            return False
        compare = _season_compare(sp[season], cloud_rows, policy, tie)
        if not compare['common']:
            return False
        if not (compare['complete']['share'] and compare['complete']['cloud']):
            return False
        if compare['ratio'] < threshold:
            return False
        if len(compare['share']) < len(compare['cloud']):
            return False
    return True


def _multi_actions(mode, sp, lp, policy, tie, threshold):
    """多季保护三档（1.2.12 `_emit_multi_protect_compare` 及其三个子函数）。

    sp / lp 为不含 S00 的季映射（S00 由调用方按"跟随"规则处理）。
    返回 (动作列表, 是否整剧零和)。
    """
    common = sorted(set(sp) & set(lp))
    actions = []
    if mode == 'overlap':
        # 各有对方没有的季：同季两边都不动，只清各自的残次品
        for season in common:
            compare = _season_compare(sp[season], lp[season], policy, tie)
            acts, _outcome = _season_actions(compare, season, threshold)
            actions.extend(act for act in acts
                           if act['action_reason'].startswith('incomplete_'))
        return actions, False
    if mode == 'share_protect':
        # 本地季 ⊂ 分享季：保护分享合集，本地同季副本纳入清理
        for season in common:
            compare = _season_compare(sp[season], lp[season], policy, tie)
            acts, _outcome = _season_actions(compare, season, threshold)
            residual = [act for act in acts if act['action_reason'].startswith('incomplete_')]
            if residual:
                actions.extend(residual)
                continue
            verdict = share_protect_verdict(set(compare['share']), set(compare['cloud']))
            if verdict == 'skip':
                continue
            if verdict == 'share_fewer':
                actions.append(_action('share', 'local_wins', '本地更全-删分享', season,
                                       ratio=compare['ratio'], sample=compare['sample']))
                continue
            actions.append(_action('cloud', 'multi_protect_partial_local', '多季保护-淘汰本地',
                                   season, ratio=compare['ratio'], sample=compare['sample']))
        return actions, False
    # full：分享季 ⊆ 本地季（含相等）。全部本地季达标 → 整剧零和删本地；否则无条件淘汰分享。
    if _multi_all_pass(sp, lp, policy, tie, threshold):
        for season in sorted(lp):
            actions.append(_action('cloud', 'multi_full_share', '整剧零和-分享替代', season))
        return actions, True
    for season in common:
        compare = _season_compare(sp[season], lp[season], policy, tie)
        if not compare['complete']['cloud']:
            actions.append(_action('cloud', 'incomplete_delete_local', '残次品-删本地', season,
                                   gap=_gap_desc(compare['cloud'])))
        reasons = []
        if not compare['complete']['cloud']:
            reasons.append('本地' + _gap_desc(compare['cloud']))
        if not compare['complete']['share']:
            reasons.append('分享' + _gap_desc(compare['share']))
        if compare['complete']['cloud'] and compare['complete']['share']:
            if len(compare['share']) < len(compare['cloud']):
                reasons.append('分享副本集数不足 (%d/%d集)' % (len(compare['share']), len(compare['cloud'])))
            elif compare['scores']['cloud'] > compare['scores']['share']:
                reasons.append('分享画质次级 (%d/%d集)' % (compare['scores']['cloud'], len(compare['common'])))
        actions.append(_action('share', 'multi_protect_partial_share', '多季保护-淘汰分享', season,
                               ratio=compare['ratio'], sample=compare['sample'], reasons=reasons))
    return actions, False


def _season_map(item, side):
    seasons = defaultdict(list)
    for row in item[side]:
        seasons[row.get('season', 0)].append(row)
    return dict(seasons)


def _media_key(kind, title, season):
    if kind == 'movie' or season is None:
        return 'movie:%s' % title
    return 'tv:%s:S%02d' % (title, int(season))


def _uncovered_episodes(sp, lp, season, delete_side):
    """要删的一侧里、另一侧并不持有的集号：删掉之后这部剧在两库都没有这些集。

    这不是拦截条件（1.2.12 里残次品照样剔除），只是让页面把后果说清楚：
    对被判残次品的一侧，通常会出现「本季只剩这一库」的情况。
    """
    def episodes(rows):
        return {episode for row in rows or [] for episode in row.get('episodes') or []
                if isinstance(episode, int) and not isinstance(episode, bool) and episode > 0}
    own = episodes(lp.get(season)) if delete_side == 'cloud' else episodes(sp.get(season))
    other = episodes(sp.get(season)) if delete_side == 'cloud' else episodes(lp.get(season))
    return sorted(own - other)


def _to_dual(common, season, action, sp, lp, policy, tie, threshold, decision, mode,
             proper_sp, proper_lp, zero_sum, multi_checks):
    """把一条动作换成 dual 行（执行层按季删被淘汰一侧的文件）。"""
    delete_side = action['delete_side']
    keeper = 'share' if delete_side == 'cloud' else 'cloud'
    detail = None
    if season > 0 and sp.get(season) and lp.get(season):
        comp = _season_compare(sp[season], lp[season], policy, tie)
        detail = {'common_episodes': comp['common'], 'common_count': len(comp['common']),
                  'share_episodes': sorted(comp['share']), 'cloud_episodes': sorted(comp['cloud']),
                  'share_only': sorted(set(comp['share']) - set(comp['cloud'])),
                  'cloud_only': sorted(set(comp['cloud']) - set(comp['share'])),
                  'share_total': len(comp['share']), 'cloud_total': len(comp['cloud']),
                  'share_better': comp['scores']['share'], 'cloud_better': comp['scores']['cloud'],
                  'share_ratio': comp['ratio'] if comp['common'] else None,
                  'threshold': threshold, 'tie': tie,
                  'threshold_applied': decision == 'quality_first' and mode in (None, 'full'),
                  'sample': comp['sample'], 'comparisons': comp['comparisons']}
    incomplete_sides = []
    if season and season > 0:
        for side, seasons in (('cloud', lp), ('share', sp)):
            nums = {ep for row in seasons.get(season, []) for ep in row.get('episodes', []) if ep > 0}
            if nums and not episode_gaps(nums)['complete']:
                incomplete_sides.append(side)
    lost = _uncovered_episodes(sp, lp, season, delete_side) if season and season > 0 else []
    keeper_files = (lp.get(season) if keeper == 'cloud' else sp.get(season)) or []
    # 另一侧根本没有这一季：删掉就是整季消失（1.2.12 对这种季静默，我们按用户要求
    # 仍给动作，但必须显式标出来，页面据此提示风险）。
    single_side = bool(season and season > 0 and not keeper_files)
    sample = action.get('sample') or ((detail or {}).get('sample') if decision == 'quality_first' else None)
    return {**common, 'season': season, 'winner': keeper,
            'action_kind': 'loc' if delete_side == 'cloud' else 'shr',
            'outcome': action.get('outcome') or action['action_reason'],
            'reason': action.get('outcome') or action['action_reason'],
            'action_reason': action['action_reason'],
            'reason_label': action['reason_label'],
            'share_ratio': action.get('ratio'), 'sample': sample,
            'rules': (sample or {}).get('rules', []),
            'gap': action.get('gap', ''), 'reasons': action.get('reasons', []),
            'comparison': detail, 'comparisons': (detail or {}).get('comparisons', []),
            'decision': decision, 'multi_mode': mode,
            'cloud_seasons': sorted(proper_lp), 'share_seasons': sorted(proper_sp),
            'multi_zero_sum': zero_sum if mode is not None else None,
            'multi_checks': multi_checks,
            'incomplete_sides': incomplete_sides,
            'lost_episodes': lost, 'clears_show': bool(lost) and not keeper_files,
            'single_side': single_side,
            'cloud_files': lp.get(season) or [], 'share_files': sp.get(season) or []}


def _version_verdict(rows, policy):
    """库内多版本选取（1.2.12 `_dedupe_lib_versions` 的比较方式）。

    参考实现用严格 `>` 逐个替换，完全打平保留先出现的那一份（这里先按文件名
    排序，结果与遍历顺序无关）。`tie=True` 表示有候选与保留项判不出高下，
    页面需要提示"按平局保留"。
    """
    names = [row['name'] for row in rows]
    winner = best_index(names, policy)
    tie = any(index != winner and cmp_versions(names[winner], name, policy) == 0
              for index, name in enumerate(names))
    return {'winner': winner, 'reason': 'quality', 'tie': tie}


def _dedupe_candidates(item, sp, lp, covered, policy):
    """库内多版本（1.2.12 `_dedupe_lib_versions`）：同片/同集多份 → 各留最优。

    粒度跟参考实现一致：
      · 电影：同一作品一条（`lib.mov[key]` 多份 → 留最优）；
      · 剧集：同一季一条，把该季所有重复集合并进来（参考实现 Act 的
        `dup_eps`），而不是每集各出一条 —— 否则一季十几集重复会在列表里
        冒出十几行，用户根本看不出这是同一个问题。
    已被库间治理覆盖（整组删除）的标题跳过；S00 不参与。
    """
    rows = []
    if item['kind'] == 'movie':
        for side, step in (('cloud', 'loc'), ('share', 'shr')):
            files = item[side]
            if len(files) < 2:
                continue
            if (step, _media_key('movie', item['title'], None)) in covered:
                continue
            verdict = _version_verdict(files, policy)
            rows.append({'side': side, 'season': None, 'episode': None, 'files': files,
                         'losers': [row for index, row in enumerate(files) if index != verdict['winner']],
                         'reason_label': '库内多版本-删低版', 'dup_count': len(files), **verdict})
        return rows
    for side, step, seasons in (('cloud', 'loc', lp), ('share', 'shr', sp)):
        for season in sorted(seasons):
            files = seasons[season]
            if season <= 0 or len(files) < 2:
                continue
            if (step, _media_key('tv', item['title'], season)) in covered:
                continue
            grouped = defaultdict(list)
            for row in files:
                episodes = row.get('episodes') or []
                if len(episodes) == 1 and episodes[0] > 0:
                    grouped[episodes[0]].append(row)
            dupes = {episode: group for episode, group in grouped.items() if len(group) > 1}
            if not dupes:
                continue
            losers, keeps, tie = [], [], False
            for episode in sorted(dupes):
                group = dupes[episode]
                verdict = _version_verdict(group, policy)
                tie = tie or verdict['tie']
                losers.extend(row for index, row in enumerate(group) if index != verdict['winner'])
                keeps.append({'episode': episode, 'name': group[verdict['winner']]['name']})
            rows.append({'side': side, 'season': season, 'episode': None, 'files': files,
                         'losers': losers, 'keeps': keeps, 'dup_episodes': sorted(dupes),
                         'reason_label': '库内多版本-删低版', 'dup_count': len(files),
                         'winner': None, 'reason': 'quality', 'tie': tie})
    return rows


def governance_preview(snapshot, policy, tie='cloud', threshold=0.9,
                       quiet_minutes=0, now=None, exempt_keywords=(), decision='quality_first',
                       special_action='compare', multi_season_protect='compare'):
    output = {'single': [], 'dual': [], 'blocked': [], 'pairs': [], 'multi': [],
              'exempt': [], 'scan_complete': bool(snapshot.get('complete', True)),
              'issues': snapshot['issues'], 'issue_count': snapshot.get('issue_count', len(snapshot['issues'])),
              'unsupported': snapshot.get('unsupported', []), 'tie': tie, 'executable': False}
    keywords = [str(keyword) for keyword in (exempt_keywords or ()) if str(keyword or '')]
    if decision not in ('quality_first', 'keep_local', 'keep_share'):
        raise ValueError('invalid decision model')
    if special_action not in ('compare', 'delete', 'ignore'):
        raise ValueError('invalid special action')
    if multi_season_protect not in ('compare', 'off'):
        raise ValueError('invalid multi-season protection')
    tie_share_wins = tie == 'share'
    quality_enabled = any(rule.get('enabled') and rule.get('key') != 'size'
                          for rule in policy.get('rules', []))
    tv_identities = {(item['title'], item.get('year')) for item in snapshot['items']
                     if item['kind'] == 'tv'}
    cutoff = ((time.time() if now is None else now) - quiet_minutes * 60) if quiet_minutes else None

    for item in snapshot['items']:
        tmdb_ids = sorted(item.get('tmdb_ids') or [item['tmdb_id']])
        identity_source = item.get('identity_source') or 'title_year'
        if identity_source == 'tmdb':
            basis, detail = 'TMDB 编号', 'TMDB %s（同一编号无冲突）' % item['tmdb_id']
        elif item.get('fallback'):
            basis, detail = '剧名 + 年份（TMDB 冲突回退）', '同一 TMDB 对应多个剧名/年份，改用规范化剧名「%s」 + 年份「%s」' % (
                item['title'], item.get('year') or '未知')
        else:
            basis, detail = '剧名 + 年份', '规范化剧名「%s」 + 年份「%s」' % (
                item['title'], item.get('year') or '未知')
        common = {'kind': item['kind'], 'tmdb_id': item['tmdb_id'], 'tmdb_ids': tmdb_ids,
                  'title': item['title'], 'year': item.get('year'),
                  'identity_source': identity_source, 'match_basis': basis,
                  # 目录有、台账没有的文件数：用于提示「网盘源未登记，暂不能删本地」。
                  'unregistered': {'cloud': sum(1 for row in item['cloud'] if not row.get('registered', True)),
                                   'share': sum(1 for row in item['share'] if not row.get('registered', True))},
                  'match_basis_detail': detail}
        sp, lp = _season_map(item, 'share'), _season_map(item, 'cloud')
        # ── 白名单：标题或任一文件路径命中 → 整部豁免（1.2.12 `_group_exempt_acts`）──
        hits = [keyword for keyword in keywords if keyword in str(item.get('title') or '')]
        if not hits:
            hits = sorted({keyword for keyword in keywords
                           for row in item['share'] + item['cloud']
                           if keyword in str(row.get('path') or '')})
        if hits:
            seasons = sorted(set(sp) | set(lp))
            output['exempt'].append({**common, 'season': None, 'keywords': hits,
                                     'action_kind': 'exempt', 'reason': 'whitelist',
                                     'member_count': len(seasons),
                                     'members': [{'season': season} for season in seasons],
                                     'cloud_files': item['cloud'], 'share_files': item['share']})
            continue
        # ── 入库静默期：刚入库（STRM 写入）不满 N 分钟 → 整部先不进治理清单 ──
        # 只看「文件本身」的入库时间（STRM 写入时间）。
        # 不能用目录 mtime：删除/清理也会刷新目录 mtime，那会把刚清完的作品又静默一遍。
        ingested = [stamp / 1e9 for stamp in (row.get('file_mtime_ns')
                                              for row in item['cloud'] + item['share'])
                    if type(stamp) is int and stamp > 0]
        if cutoff is not None and ingested and max(ingested) >= cutoff:
            newest = max(ingested)
            output['blocked'].append({**common, 'reason': 'recent_activity',
                                      'quiet_minutes': quiet_minutes,
                                      'quiet_remaining': max(0, int(math.ceil(
                                          (newest + quiet_minutes * 60 - (time.time() if now is None else now)) / 60))),
                                      'ingested_at': int(newest),
                                      'detail': '最近入库的文件还在静默期内（入库 %s，静默 %s 分钟）。' % (
                                          time.strftime('%m-%d %H:%M', time.localtime(newest)), quiet_minutes),
                                      'cloud_files': item['cloud'],
                                      'share_files': item['share']})
            continue

        # Check every filename before generating any deletion action: an
        # unsupported configured quality tier must never silently rank low.
        try:
            for row in item['cloud'] + item['share']:
                compare_outcome(row['name'], row['name'], policy)
        except NeedsReview as exc:
            output['blocked'].append({**common, 'reason': 'unknown_quality', 'detail': str(exc)})
            continue

        if item['kind'] == 'movie':
            # 同一身份在任一库里有分集 → 这是一部剧，剩下的无季集文件（花絮等）
            # 不能按电影整体比画质/删除，交给剧集分支（1.2.12 直接跳过）。
            if (common['title'], common.get('year')) in tv_identities:
                continue
            covered = set()
            if item['cloud'] and item['share']:
                output['pairs'].append({**common, 'cloud_files': item['cloud'],
                                        'share_files': item['share']})
                share_best, cloud_best = _best(item['share'], policy), _best(item['cloud'], policy)
                outcome, evidence = compare_outcome(share_best['name'], cloud_best['name'], policy)
                decisive = next((rule['label'] for rule in evidence if rule.get('result')), '')
                sample = {'share_name': share_best['name'], 'cloud_name': cloud_best['name'],
                          'result': outcome, 'rules': evidence}
                if _share_wins(share_best['name'], cloud_best['name'], policy, decision,
                               tie_share_wins):
                    row = {**common, 'season': None, 'winner': 'share', 'action_kind': 'loc',
                           'outcome': 'share_better', 'reason': 'share_better',
                           'reason_label': ('指定保留分享，剔除网盘' if decision != 'quality_first' else
                                            '各维度平局，按设置保留分享、剔除网盘' if outcome == 0 else
                                            '分享画质达标'),
                           'decisive': decisive,
                           'decision': decision, 'tie': tie, 'sample': sample, 'rules': evidence,
                           'cloud_files': item['cloud'], 'share_files': item['share']}
                else:
                    row = {**common, 'season': None, 'winner': 'cloud', 'action_kind': 'shr',
                           'outcome': 'local_better', 'reason': 'local_better',
                           'reason_label': ('指定保留网盘，剔除分享' if decision != 'quality_first' else
                                            '各维度平局，按设置保留网盘、剔除分享' if outcome == 0 else
                                            '本地画质更优'),
                           'decisive': decisive,
                           'decision': decision, 'tie': tie, 'sample': sample, 'rules': evidence,
                           'cloud_files': item['cloud'], 'share_files': item['share']}
                output['dual'].append(row)
                covered.add((row['action_kind'], _media_key('movie', item['title'], None)))
            # Run the independent single-library pass even for one-sided movies.
            output['single'].extend({**common, **row} for row in
                                    _dedupe_candidates(item, {}, {}, covered, policy))
            continue


        # A range file can cover several episodes. It cannot be silently
        # dropped from the comparison or grouped with every other range file.
        merged = [row for row in item['cloud'] + item['share'] if len(row.get('episodes') or []) != 1]
        if merged:
            output['blocked'].append({**common, 'reason': 'merged_episodes', 'files': merged})
            continue

        # 残次品（1.2.12 `_emit_season_residuals`）的独立成单只在下面两种情形发生，
        # 具体位置见 `_emit_residual_actions` 的调用处：
        #   · 单季剧 / 关闭多季保护 → 逐季判定，缺集的一侧整季剔除；
        #   · 多季保护开启 → 由 share_protect / full / overlap 三个分支各自判定，
        #     同一季的「残次品」与「多季保护淘汰」必须同时给出，不能互相抵消。
        # 决策模型「保留本地／保留分享」不产生残次品动作（1.2.12 直接按决策取舍）。
        # 残次品（1.2.12 `_emit_season_residuals`）：两侧各自独立判定，谁不完整删谁。
        # 与参考实现的一处**有意差异**：1.2.12 只对两侧都有的季判残次品；用户要求
        # 「另一侧没有这一季时也照样给出可执行的清理动作」。这种情况用 single_side
        # 标出来，并由 lost_episodes / clears_show 说明「删完这季就没了」，不静默吞掉。
        def _emit_residual_actions(sides):
            found, hit = [], set()
            for side, own in sides:
                for season, files in own.items():
                    if season <= 0:
                        continue
                    present = sorted({row['episodes'][0] for row in files
                                      if row['episodes'][0] > 0})
                    missing = episode_gaps(present)['missing'] if present else []
                    if not present or not missing:
                        continue
                    hit.add(season)
                    found.append({
                        'delete_side': side,
                        'action_reason': ('incomplete_delete_local' if side == 'cloud'
                                          else 'incomplete_delete_share'),
                        'reason_label': '残次品-删本地' if side == 'cloud' else '残次品-删分享',
                        'season': season,
                        'gap': _gap_text(present)})
            return found, hit

        # ── 剧集：共同季清单（供页面展示）──
        for season in sorted(set(sp) | set(lp)):
            if season >= 0 and sp.get(season) and lp.get(season):
                cloud_episodes = {episode for row in lp[season] for episode in row.get('episodes') or []}
                share_episodes = {episode for row in sp[season] for episode in row.get('episodes') or []}
                output['pairs'].append({**common, 'season': season,
                                        'cloud_episodes': sorted(cloud_episodes),
                                        'share_episodes': sorted(share_episodes),
                                        'common_episodes': sorted(cloud_episodes & share_episodes),
                                        'cloud_only': sorted(cloud_episodes - share_episodes),
                                        'share_only': sorted(share_episodes - cloud_episodes),
                                        'cloud_files': lp[season], 'share_files': sp[season]})

        protect = protect_s00(multi_season_protect, special_action, decision, set(lp), set(sp))
        floor = 0 if protect else 1
        proper_sp = {season: rows for season, rows in sp.items() if season >= floor}
        proper_lp = {season: rows for season, rows in lp.items() if season >= floor}
        s00, l00 = sp.get(0), lp.get(0)
        actions = []
        if not protect:
            # 特别篇独立处理（1.2.12：delete 两库都删 / compare 比画质 / ignore 不动）
            if special_action == 'delete':
                if l00:
                    actions.append(_action('cloud', 'special_delete_local', '特别篇清理-删本地', 0))
                if s00:
                    actions.append(_action('share', 'special_delete_share', '特别篇清理-删分享', 0))
            elif special_action == 'compare' and s00 and l00:
                share_best, cloud_best = _best(s00, policy), _best(l00, policy)
                _, evidence = compare_outcome(share_best['name'], cloud_best['name'], policy)
                sample = {'share_name': share_best['name'], 'cloud_name': cloud_best['name'], 'rules': evidence}
                if _share_wins(share_best['name'], cloud_best['name'], policy, decision,
                               tie_share_wins):
                    actions.append(_action('cloud', 'special_share_better', '特别篇分享更优', 0,
                                           sample=sample))
                else:
                    actions.append(_action('share', 'special_local_better', '特别篇本地更优', 0,
                                           sample=sample))

        mode, zero_sum = None, False
        if decision in ('keep_local', 'keep_share'):
            # 决策模型：逐共同季直接取舍，不比画质，优先级高于多季保护
            for season in sorted(set(proper_sp) & set(proper_lp)):
                if decision == 'keep_local':
                    actions.append(_action('share', 'decision_keep_local', '保留本地-删分享', season))
                    continue
                compare = _season_compare(proper_sp[season], proper_lp[season], policy, tie)
                if not share_covers_local(set(compare['share']), set(compare['cloud'])):
                    continue
                actions.append(_action('cloud', 'decision_keep_share', '保留分享-删本地', season))
        elif multi_season_protect == 'compare' and (len(proper_lp) >= 2 or len(proper_sp) >= 2):
            mode = multi_protect_mode(set(proper_sp), set(proper_lp))
            inner_sp = {season: rows for season, rows in proper_sp.items() if season > 0}
            inner_lp = {season: rows for season, rows in proper_lp.items() if season > 0}
            acts, zero_sum = _multi_actions(mode, inner_sp, inner_lp, policy, tie, threshold)
            actions.extend(acts)
            # S00 跟随多季保护结论（1.2.12 `_emit_s00_follow`），overlap 不动
            if s00 and l00 and mode != 'overlap':
                if mode == 'share_protect':
                    actions.append(_action('cloud', 'multi_protect_partial_local',
                                           '多季保护-淘汰本地', 0))
                elif zero_sum:
                    actions.append(_action('cloud', 'multi_full_share', '整剧零和-分享替代', 0))
                else:
                    actions.append(_action('share', 'multi_protect_partial_share',
                                           '多季保护-淘汰分享', 0))
        else:
            for season in sorted(set(proper_sp) & set(proper_lp)):
                compare = _season_compare(proper_sp[season], proper_lp[season], policy, tie)
                acts, _outcome = _season_actions(compare, season, threshold)
                actions.extend(acts)

        multi_active = (multi_season_protect == 'compare'
                        and (len(proper_lp) >= 2 or len(proper_sp) >= 2))
        if decision == 'quality_first' and not multi_active:
            # 单季剧 / 关闭多季保护：缺集的一侧整季剔除，该季不再比画质。
            residual_actions, residual_seasons = _emit_residual_actions(
                (('cloud', lp), ('share', sp)))
            if residual_seasons:
                actions = [action for action in actions
                           if action['season'] not in residual_seasons]
            actions = residual_actions + actions


        # Explain the actual full-group test, including failures on a season
        # other than the one represented by an action.
        multi_checks = []
        if mode == 'full':
            for sn, cloud_rows in sorted(proper_lp.items()):
                if sn <= 0:
                    continue
                if sn not in proper_sp:
                    multi_checks.append({'season': sn, 'passed': False, 'reasons': ['分享缺少该季']})
                    continue
                compare = _season_compare(proper_sp[sn], cloud_rows, policy, tie)
                why = []
                if not compare['common']:
                    why.append('没有共同集')
                if not compare['complete']['cloud'] or not compare['complete']['share']:
                    why.append('至少一侧季集不连续')
                if compare['common'] and compare['ratio'] < threshold:
                    why.append('分享达标率低于阈值')
                if len(compare['share']) < len(compare['cloud']):
                    why.append('分享集数少于网盘')
                multi_checks.append({'season': sn, 'passed': not why, 'reasons': why,
                                     'common_count': len(compare['common']),
                                     'share_better': compare['scores']['share'],
                                     'share_total': len(compare['share']), 'cloud_total': len(compare['cloud']),
                                     'share_ratio': compare['ratio'] if compare['common'] else None,
                                     'threshold': threshold})
        dual_rows = [_to_dual(common, action['season'], action, sp, lp, policy, tie,
                              threshold, decision, mode, proper_sp, proper_lp, zero_sum, multi_checks)
                     for action in actions]
        output['dual'].extend(dual_rows)
        if mode is not None:
            output['multi'].append({**common, 'mode': mode,
                                    'cloud_seasons': sorted(proper_lp),
                                    'share_seasons': sorted(proper_sp),
                                    'decision': 'delete_show_local' if zero_sum else 'per_season',
                                    'reason': 'multi_full_share' if zero_sum else '',
                                    'checks': multi_checks,
                                    'protected': mode == 'overlap'})
        covered = {(row['action_kind'], _media_key('tv', item['title'], row['season']))
                   for row in dual_rows if row['action_kind'] in ('loc', 'shr')}
        # 1.2.12 在多季保护 / 决策模型命中后直接 continue：这些剧不再做库内去重，
        # 否则同一部剧会同时出现「整组动作」和「库内多版本」两类条目。
        if decision == 'quality_first' and not multi_active:
            output['single'].extend({**common, **row} for row in
                                    _dedupe_candidates(item, sp, lp, covered, policy))

    output['executable'] = bool(output['dual'] or output['single'])
    return output
