"""Pure, non-destructive inventory decisions for STRM filename evidence.

The caller must verify identities, scan completeness and execution snapshots;
these functions neither enumerate nor delete files.
"""
from .media import NeedsReview, media_from_filename
from .quality import LABELS, compare, facts, rule_value
from .legacy_1212_quality import recognize_dims


def compare_filenames(left, right, policy):
    """Compare using the configured wash ordering, except file size.

    Unknown evidence remains unknown, including when all remaining rules tie.
    """
    rules = [dict(rule, enabled=False) if rule['key'] == 'size' else dict(rule)
             for rule in policy['rules']]
    scoped = dict(policy, rules=rules)
    return compare(facts(left, media_from_filename(left), None),
                   facts(right, media_from_filename(right), None), scoped)


def episode_gaps(episodes):
    """Detect missing predecessors and internal gaps; E00 is a special."""
    episodes = sorted(set(e for e in episodes if isinstance(e, int) and not isinstance(e, bool) and e > 0))
    if not episodes:
        return {'complete': False, 'missing': [], 'last': None}
    missing = sorted(set(range(1, episodes[-1] + 1)) - set(episodes))
    return {'complete': not missing, 'missing': missing, 'last': episodes[-1]}


def single_library_versions(filenames, policy):
    """Recommend a unique best version, keeping all candidates on uncertainty.

    Equal versions are left for an explicit tie policy. A tournament winner is
    checked against every candidate because incomplete evidence need not define
    a total ordering.
    """
    if len(filenames) < 2 or any(not isinstance(x, str) or not x for x in filenames):
        raise ValueError('at least two filenames are required')
    candidate = 0
    try:
        for index in range(1, len(filenames)):
            outcome, _ = compare_filenames(filenames[index], filenames[candidate], policy)
            if outcome > 0:
                candidate = index
        evidence = []
        for index, filename in enumerate(filenames):
            if index == candidate:
                continue
            outcome, details = compare_filenames(filenames[candidate], filename, policy)
            evidence.append({'index': index, 'result': outcome, 'rules': details})
            if outcome <= 0:
                return {'winner': None, 'reason': 'tie_or_conflict', 'comparisons': evidence}
        return {'winner': candidate, 'reason': 'quality', 'comparisons': evidence}
    except NeedsReview as exc:
        return {'winner': None, 'reason': 'unknown_quality', 'detail': str(exc)}


def _filename_facts(name):
    """Use the original 1.2.12 recognizer with the organizer's rule ordering."""
    media = media_from_filename(name)
    legacy = recognize_dims(name)
    sizes = {'4320p / 8K': (7680, 4320), '2160p / 4K': (3840, 2160),
             '1440p / 2K': (2560, 1440), '1080p': (1920, 1080),
             '720p': (1280, 720), 'SD（576p 及以下）': (1024, 576)}
    if legacy['resolution'] in sizes:
        media['width'], media['height'] = sizes[legacy['resolution']]
    rates = {'60 / 59.94 fps': 60, '50 fps': 50, '30 / 29.97 fps': 30,
             '25 fps': 25, '24 / 23.976 fps': 24}
    if legacy['fps'] in rates:
        media['fps'] = rates[legacy['fps']]
    depths = {'12bit': 12, '10bit': 10, '8bit': 8}
    if legacy['bitdepth'] in depths:
        media['bit_depth'] = depths[legacy['bitdepth']]
    ranges = {'HDR10+': 'HDR10+', 'HDR10': 'HDR10', 'HDR / HLG': 'HLG', 'SDR': 'SDR'}
    dv = legacy['dolby']
    if dv.startswith('Dolby Vision'):
        media['dynamic_range'] = 'Dolby Vision'
        media['dolby_vision_profile'] = next((n for n in (5, 7, 8) if 'Profile %d' % n in dv), None)
    elif dv in ranges:
        media['dynamic_range'] = ranges[dv]
    # Use the original filename recognizer for formats the organizer's
    # generic parser does not currently identify. No channel count is guessed.
    if not media.get('audio'):
        legacy_audio = legacy['audio']
        codec = {'Audio Vivid / AV3A': 'av3a', 'DD+ / E-AC-3': 'eac3',
                 'DD+ Atmos': 'eac3', 'LPCM / FLAC': 'pcm'}.get(legacy_audio)
        if codec:
            media['audio'] = [{'codec': codec, 'channels': None, 'estimated': True}]
    result = facts(name, media, None)
    if dv == 'HDR Vivid':
        result['dynamic_range'] = 'hdrvivid'
    return result


def _governance_rule(rule):
    """Add 1.2.12-only tiers to older organizer policies without reordering them.

    Explicitly configured tiers always win. The legacy defaults put Vivid
    immediately before HDR10, and AV3A immediately before DD+. If neither
    anchor exists the tier remains unconfigured instead of guessing a rank.
    """
    key = rule['key']
    token, before, after = (('hdrvivid', 'hdr10', 'hdr10plus') if key == 'dynamic_range'
                             else ('av3a', 'eac3', 'flac') if key == 'audio'
                             else (None, None, None))
    if not token:
        return rule
    field = 'order' if rule.get('order') else 'preferences'
    tiers = list(rule.get(field) or [])
    if token not in tiers:
        if before in tiers:
            tiers.insert(tiers.index(before), token)
        elif after in tiers:
            tiers.insert(tiers.index(after) + 1, token)
    return {**rule, field: tiers}


def compare_outcome(left, right, policy):
    """Compare each configured filename dimension; unknown is its last tier.

    This follows 1.2.12's per-dimension continuation while retaining the
    current organizer's configured order and additional codec dimension.
    """
    values = (_filename_facts(left), _filename_facts(right))
    evidence = []
    for original_rule in policy['rules']:
        rule = _governance_rule(original_rule)
        if not rule.get('enabled') or rule['key'] == 'size':
            continue
        tiers = rule.get('order') or rule.get('preferences') or []
        if rule['key'] == 'dynamic_range' and any(v['dynamic_range'] == 'hdrvivid' for v in values) and 'hdrvivid' not in tiers:
            raise NeedsReview('HDR Vivid 未配置且无法从 HDR10 档位确定排序，请在影视整理规则中配置。')
        if rule['key'] == 'audio' and any(any(t.get('codec') == 'av3a' for t in v.get('audio', [])) for v in values) and 'av3a' not in tiers:
            raise NeedsReview('Audio Vivid / AV3A 未配置且无法从音频档位确定排序，请在影视整理规则中配置。')
        ranked = []
        for value in values:
            try:
                score, label = rule_value(value, rule)
                ranked.append((True, score, label))
            except NeedsReview:
                # The reference recognizes a known audio codec even if its
                # channel count is absent; do not invent that count.
                order = rule.get('order') or rule.get('preferences') or []
                tracks = value.get('audio') or [] if rule['key'] == 'audio' else []
                known = [track for track in tracks if track.get('codec') in order]
                if known:
                    track = min(known, key=lambda x: order.index(x['codec']))
                    ranked.append((True, (0, -order.index(track['codec']), 0),
                                   {'codec': track['codec'], 'channels': None}))
                else:
                    ranked.append((False, None, '未识别'))
        a, b = ranked
        result = ((a[1] > b[1]) - (a[1] < b[1])) if a[0] and b[0] else int(a[0]) - int(b[0])
        evidence.append({'rule': rule['key'], 'label': LABELS[rule['key']], 'new': a[2],
                         'old': b[2], 'result': result, 'unknown': not a[0] or not b[0]})
        if result:
            return result, evidence
    return 0, evidence


def cmp_versions(left, right, policy):
    """1.2.12 ``_cmp_versions``：只比较画质维度，返回 1/0/-1。"""
    return compare_outcome(left, right, policy)[0]


def best_index(filenames, policy):
    """1.2.12 ``_best``：同侧多份取画质最优；平局保留先出现的那一份。

    1.2.12 口径用严格 ``>`` 比较，所以完全打平时保留第一份。这里先把名字排序再取，
    让结果与磁盘遍历顺序无关（同样的输入永远得到同样的保留项）。
    """
    order = sorted(range(len(filenames)), key=lambda index: (str(filenames[index]), index))
    best = order[0]
    for index in order[1:]:
        if cmp_versions(filenames[index], filenames[best], policy) > 0:
            best = index
    return best


def season_decision(share, cloud, policy, tie='cloud', threshold=0.9):
    """逐行对齐 1.2.12 ``domain.governance.season_verdict``。

    输入是已经清洗过的同一季：``{集号: 文件名}``。裁决顺序与1.2.12 口径一致：
    1. 任一侧不完整（E01 起断档）→ ``incomplete``（只清残次品，不比画质）；
    2. 没有共同集 → ``no_common``；
    3. 分享达标率 ≥ 阈值 **且** 分享集数不少于本地 → ``share_wins``；
    4. 分享集数少于本地 → ``local_wins_fewer``（集数优先）；
    5. 其余 → ``local_wins_quality``（画质优先）。

    1.2.12 口径不设"两侧各有对方没有的集就交人工"这一层：那种情况必然至少一侧
    不完整，会在第 1 步就终止（只清残次品），所以这里也不需要额外兜底。
    """
    if tie not in ('share', 'cloud') or not 0.5 <= threshold <= 1:
        raise ValueError('invalid tie or threshold')
    for side in (share, cloud):
        if any(not isinstance(k, int) or isinstance(k, bool) or k <= 0 or not isinstance(v, str) or not v
               for k, v in side.items()):
            raise ValueError('invalid episode evidence')
    s, c = set(share), set(cloud)
    gaps = {'share': episode_gaps(s), 'cloud': episode_gaps(c)}
    result = {'winner': None, 'reason': '', 'outcome': '', 'gaps': gaps, 'comparisons': [],
              'scores': {'share': 0, 'cloud': 0}, 'share_ratio': 0.0,
              'threshold': threshold, 'share_only': sorted(s - c), 'cloud_only': sorted(c - s),
              'sample': None}
    result['residual'] = {'share': not gaps['share']['complete'],
                          'cloud': not gaps['cloud']['complete']}
    if not s or not c or not all(x['complete'] for x in gaps.values()):
        result.update(reason='incomplete', outcome='incomplete')
        return result
    common = sorted(s & c)
    if not common:
        result.update(reason='no_common', outcome='no_common')
        return result
    scores = {'share': 0, 'cloud': 0}
    sample = None
    for episode in common:
        outcome, evidence = compare_outcome(share[episode], cloud[episode], policy)
        winner = 'share' if outcome > 0 else 'cloud' if outcome < 0 else tie
        scores[winner] += 1
        result['comparisons'].append({'episode': episode, 'winner': winner, 'rules': evidence})
        if sample is None and outcome != 0:
            # 代表集：第一个真正分出胜负的共同集，和1.2.12 口径的 sample 同义。
            sample = {'episode': episode, 'rules': evidence}
    share_ratio = scores['share'] / len(common)
    result.update(scores=scores, share_ratio=share_ratio, sample=sample)
    if share_ratio >= threshold and len(s) >= len(c):
        result.update(winner='share', reason='quality', outcome='share_wins')
    elif len(s) < len(c):
        result.update(winner='cloud', reason='quality', outcome='local_wins_fewer')
    else:
        result.update(winner='cloud', reason='quality', outcome='local_wins_quality')
    return result


def share_covers_local(share, cloud):
    """分享是否覆盖本地全部集（1.2.12 ``share_covers_local``）。"""
    return set(cloud) <= set(share)


def residual_flags(share, cloud, has_share=True, has_cloud=True):
    """哪一侧是残次品（不完整）：1.2.12 用它决定"只清残次、不比画质"。"""
    return {'share': bool(has_share) and not episode_gaps(set(share))['complete'],
            'cloud': bool(has_cloud) and not episode_gaps(set(cloud))['complete']}


def multi_protect_mode(share_seasons, cloud_seasons):
    """多季保护的处理模式（1.2.12 ``multi_protect_mode``）。

    ``share_protect`` 本地季是分享季的真子集 → 保护分享合集；
    ``full`` 分享季是本地季的子集（含相等）→ 整体比较；
    ``overlap`` 双方各有对方没有的季 → 同季两边都不动，只处理残次品。
    """
    share, cloud = set(share_seasons), set(cloud_seasons)
    if cloud < share:
        return 'share_protect'
    if share <= cloud:
        return 'full'
    return 'overlap'


def share_protect_verdict(share, cloud):
    """分享保护模式下、已排除残次品后的单季裁决（1.2.12 同名函数）。

    ``skip`` 本地这季没有可比集；``share_fewer`` 分享没覆盖本地全部集；
    ``delete_local`` 分享覆盖本地 → 淘汰本地同季副本。
    """
    if not cloud:
        return 'skip'
    if not share_covers_local(share, cloud):
        return 'share_fewer'
    return 'delete_local'


def protect_s00(multi_season_protect, special_action, decision,
                cloud_seasons, share_seasons):
    """S00 是否算作一季参与多季保护（1.2.12 ``protect_s00``）。

    「多季保护 + 特别篇按画质对比 + 画质优先 + 任一侧 ≥2 季」时成立，
    此时特别篇不再单独比画质。
    """
    return bool(multi_season_protect == 'compare' and special_action == 'compare'
                and decision == 'quality_first'
                and (len([k for k in cloud_seasons if k >= 0]) >= 2
                     or len([k for k in share_seasons if k >= 0]) >= 2))


def partial_share_reasons(share, cloud, policy):
    """分享未全覆盖时，列出该季被淘汰的全部原因（残次品 / 集数不足 / 画质次级）。

    对应 1.2.12 ``partial_share_reasons``：那里把"缺集描述"作为参数注入，
    我们直接用 ``episode_gaps`` 生成，保持本模块无外部依赖。
    """
    share_gaps, cloud_gaps = episode_gaps(set(share)), episode_gaps(set(cloud))
    reasons = []
    if not cloud_gaps['complete']:
        reasons.append('本地缺集 %s' % cloud_gaps['missing'])
    if not share_gaps['complete']:
        reasons.append('分享缺集 %s' % share_gaps['missing'])
    if cloud_gaps['complete'] and share_gaps['complete']:
        if len(share) < len(cloud):
            reasons.append('分享副本集数不足 (%d/%d集)' % (len(share), len(cloud)))
        else:
            try:
                verdict = season_decision(share, cloud, policy)
            except ValueError:
                return reasons
            if verdict.get('scores', {}).get('cloud', 0) > verdict.get('scores', {}).get('share', 0):
                reasons.append('分享画质次级 (%d/%d集)'
                               % (verdict['scores']['cloud'], len(set(share) & set(cloud))))
    return reasons
