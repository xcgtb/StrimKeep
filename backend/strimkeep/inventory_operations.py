"""Translate explicit work selections into deletion-plan entries."""


def _reject_unsupported(actions, unsupported):
    """来源核不上的文件不能进计划：宁可明确拒绝，也不猜着删。"""
    blocked = {(item.get('side'), item.get('path')) for item in unsupported or ()
               if isinstance(item, dict)}
    if not blocked:
        return
    hits = [action for action in actions if (action['side'], action['path']) in blocked]
    if hits:
        raise ValueError('有 %d 个文件来源不可核实，不能删除（%s）。'
                         '网盘侧需要补登记；分享侧可单独选择「分享库」清理。'
                         % (len(hits), hits[0]['path']))


def manual_actions(projection, selections, unsupported=()):
    if not selections or len(selections) > 100:
        raise ValueError('请选择 1 至 100 部作品')
    works = {}
    for item in projection['items']:
        works.setdefault((item['kind'], item['tmdb_id']), []).append(item)
    seen, actions = set(), []
    for selection in selections:
        key = (selection['kind'], selection['tmdb_id'])
        side = selection['side']
        if (key, side) in seen or side not in ('cloud', 'share', 'both'):
            raise ValueError('作品选择重复或库类型无效')
        seen.add((key, side))
        matches = works.get(key) or []
        if not matches:
            raise ValueError('作品不在当前库存中')
        if len(matches) > 1:
            raise ValueError('同一 TMDB 编号对应多个作品目录，请先核对身份')
        item = matches[0]
        for library in ('cloud', 'share') if side == 'both' else (side,):
            for file in item[library]:
                actions.append({'side': library, 'id': str(file['id']), 'path': file['path']})
    keys = [(x['side'], x['id'], x['path']) for x in actions]
    if not actions or len(actions) > 10000 or len(set(keys)) != len(keys):
        raise ValueError('删除目标为空、重复或超过 10000 条')
    _reject_unsupported(actions, unsupported)
    return actions


def dual_actions(projection, preview, selections, unsupported=()):
    verdicts = {}
    for verdict in preview['dual']:
        key = (verdict['kind'], verdict['tmdb_id'], verdict.get('season'))
        verdicts.setdefault(key, []).append(verdict)
    works = {}
    for item in projection['items']:
        works.setdefault((item['kind'], item['tmdb_id']), []).append(item)
    actions, seen = [], {}
    if not selections or len(selections) > 10000:
        raise ValueError('请选择 1 至 10000 项去重结果')
    for selected in selections:
        key = (selected['kind'], selected['tmdb_id'], selected.get('season'))
        action_kind = selected.get('action_kind')
        winner = selected.get('winner')
        if not action_kind and winner in ('cloud', 'share'):
            action_kind = 'loc' if winner == 'share' else 'shr'
        selected_key = (*key, action_kind)
        if selected_key in seen:
            raise ValueError('去重结果重复')
        candidates = [v for v in verdicts.get(key, [])
                      if (v.get('action_kind') or ('loc' if v.get('winner') == 'share' else 'shr')) == action_kind
                      and (not selected.get('action_reason') or v.get('action_reason') == selected['action_reason'])]
        verdict = candidates[0] if len(candidates) == 1 else None
        matches = works.get(key[:2]) or []
        work = matches[0] if len(matches) == 1 else None
        if not verdict or not work or verdict.get('winner') not in ('cloud', 'share'):
            raise ValueError('去重结果发生变化，请重新预览')
        if selected.get('winner') != verdict['winner']:
            raise ValueError('去重胜方发生变化，请重新预览')
        opposite = 'shr' if action_kind == 'loc' else 'loc'
        prior_reason = seen.get((*key, opposite))
        if (*key, opposite) in seen:
            # 1.2.12: explicit S00 delete is the sole case where both
            # sides are intentionally cleared. Trust actual verdicts,
            # never just a reason supplied by the client.
            pair_reasons = {prior_reason, verdict.get('action_reason')}
            special_pair = (key[0] == 'tv' and key[2] == 0 and
                            pair_reasons == {'special_delete_local', 'special_delete_share'})
            # 1.2.12 `_emit_season_residuals`：两侧各自残次时就是各删各的（文件互不相交）。
            residual_pair = pair_reasons == {'incomplete_delete_local',
                                             'incomplete_delete_share'}
            if not (special_pair or residual_pair):
                raise ValueError('同一季不能在一次清理中同时删除两库，请分别核实。')
        seen[selected_key] = verdict.get('action_reason')
        loser = 'share' if verdict['winner'] == 'cloud' else 'cloud'
        for file in work[loser]:
            if key[0] == 'tv' and file.get('season') != key[2]:
                continue
            actions.append({'side': loser, 'id': str(file['id']), 'path': file['path']})
    keys = [(x['side'], x['id'], x['path']) for x in actions]
    if not actions or len(actions) > 10000 or len(set(keys)) != len(keys):
        raise ValueError('去重目标为空、重复或超过 10000 条')
    _reject_unsupported(actions, unsupported)
    return actions


def single_actions(projection, preview, selections, unsupported=()):
    if not selections or len(selections) > 10000:
        raise ValueError('请选择 1 至 10000 项单库结果')
    verdicts = {(v['kind'], v['tmdb_id'], v.get('season'), v.get('episode'), v['side'], v['reason']): v
                for v in preview['single']}
    works = {}
    for item in projection['items']:
        works.setdefault((item['kind'], item['tmdb_id']), []).append(item)
    actions, seen = [], set()
    for selected in selections:
        key = (selected['kind'], selected['tmdb_id'], selected.get('season'), selected.get('episode'),
               selected['side'], selected.get('reason'))
        if key in seen:
            raise ValueError('单库结果重复')
        seen.add(key)
        matches = works.get(key[:2]) or []
        result, work = verdicts.get(key), matches[0] if len(matches) == 1 else None
        if not result or not work or key[4] not in ('cloud', 'share'):
            raise ValueError('单库结果发生变化，请重新预览')
        if key[5] != 'quality':
            # 预览现在只产出「库内多版本」这一类单库结果；缺集季已经统一走双库动作
            # （残次品-删本地/删分享），旧的 episode_gap / covered_episode 已退场。
            raise ValueError('此结果没有确定的清理目标')
        files, winner, losers = result.get('files'), result.get('winner'), result.get('losers')
        if isinstance(losers, list) and losers:
            # 剧集多版本：一条结果覆盖该季所有重复集，按预览给出的路径逐一对齐。
            wanted = {str(item.get('path') or '') for item in losers}
            selected_files = [file for file in work[key[4]]
                              if str(file.get('path') or '') in wanted]
            if len(selected_files) != len(wanted) or len(wanted) != len(losers):
                raise ValueError('多版本目标已变化，请重新预览')
        elif isinstance(files, list) and type(winner) is int and 0 <= winner < len(files):
            selected_files = [file for index, file in enumerate(files) if index != winner]
        else:
            raise ValueError('多版本结果无法核实')
        for file in selected_files:
            actions.append({'side': key[4], 'id': str(file['id']), 'path': file['path']})
    keys = [(x['side'], x['id'], x['path']) for x in actions]
    if len(set(keys)) != len(keys):
        raise ValueError('所选单库多版本和缺集清理包含相同文件，请分开处理。')
    if not actions or len(actions) > 10000:
        raise ValueError('清理目标为空或超过 10000 条')
    _reject_unsupported(actions, unsupported)
    return actions
