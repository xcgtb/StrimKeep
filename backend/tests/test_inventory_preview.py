"""治理预览：逐条对齐1.2.12 口径（1.2.12）的双库判定。"""
import pytest
from strimkeep.inventory_preview import governance_preview
from strimkeep.quality import public_policy


def policy():
    value = public_policy()
    for rule in value['rules']:
        rule['enabled'] = rule['key'] == 'resolution'
    return value


def file(side, season, episode, spec, name=None):
    return {'id': f'{side}-{season}-{episode}-{spec}',
            'name': name or f'S{season:02d}E{episode:02d}.{spec}.strm',
            'path': f'{side}/S{season:02d}/{spec}/E{episode:02d}.strm',
            'season': season, 'episodes': [episode]}


def snap(cloud, share, kind='tv', title='剧 (2020)'):
    return {'complete': True, 'issues': [],
            'items': [{'kind': kind, 'tmdb_id': 1, 'title': title,
                       'cloud': cloud, 'share': share}]}


def actions(result):
    return {row['reason']: row for row in result['dual']}


def test_single_library_movie_versions_include_identity():
    result = governance_preview(snap([file('l', 0, 0, '1080p'), file('l', 0, 0, '2160p')], [], kind='movie'), policy())
    assert len(result['single']) == 1
    row = result['single'][0]
    assert (row['kind'], row['tmdb_id'], row['side']) == ('movie', 1, 'cloud')


def test_merged_episode_ranges_are_blocked():
    merged = file('l', 1, 1, '1080p')
    merged['episodes'] = [1, 2]
    result = governance_preview(snap([merged], [file('s', 1, 1, '2160p')]), policy())
    assert not result['dual'] and not result['single']
    assert result['blocked'][0]['reason'] == 'merged_episodes'


def test_movie_compares_best_and_deletes_loser():
    better_share = governance_preview(snap([file('l', 0, 0, '1080p', 'M.1080p.strm')],
                                           [file('s', 0, 0, '2160p', 'M.2160p.strm')],
                                           kind='movie'), policy())
    assert better_share['dual'][0]['winner'] == 'share'
    assert better_share['dual'][0]['action_kind'] == 'loc'
    assert better_share['dual'][0]['reason'] == 'share_better'
    assert better_share['dual'][0]['reason_label'] == '分享画质达标'

    better_local = governance_preview(snap([file('l', 0, 0, '2160p', 'M.2160p.strm')],
                                          [file('s', 0, 0, '1080p', 'M.1080p.strm')],
                                          kind='movie'), policy())
    assert better_local['dual'][0]['action_kind'] == 'shr'
    assert better_local['dual'][0]['reason_label'] == '本地画质更优'
    assert better_local['dual'][0]['reason'] == 'local_better'


def test_share_wins_deletes_local_when_share_covers_and_is_better():
    result = governance_preview(snap([file('l', 1, n, '1080p') for n in (1, 2)],
                                     [file('s', 1, n, '2160p') for n in (1, 2)]), policy())
    row = actions(result)['share_wins']
    assert row['winner'] == 'share' and row['action_kind'] == 'loc'
    assert [f['name'] for f in row['cloud_files']] == ['S01E01.1080p.strm', 'S01E02.1080p.strm']


def test_season_evidence_reports_actual_threshold_scores_and_file_comparisons():
    cloud = [file('l', 1, n, '1080p') for n in range(1, 5)]
    share = [file('s', 1, n, '2160p' if n < 4 else '720p') for n in range(1, 5)]
    row = governance_preview(snap(cloud, share), policy(), threshold=.8)['dual'][0]
    detail = row['comparison']
    assert (detail['common_count'], detail['share_total'], detail['cloud_total']) == (4, 4, 4)
    assert (detail['share_better'], detail['cloud_better'], detail['share_ratio']) == (3, 1, .75)
    assert detail['threshold'] == .8 and detail['threshold_applied'] is True
    assert detail['common_episodes'] == [1, 2, 3, 4]
    assert detail['comparisons'][3]['winner'] == 'cloud'
    assert detail['comparisons'][3]['share_name'] == share[3]['name']
    assert row['winner'] == 'cloud' and row['reason'] == 'local_wins_quality'
    assert row['identity_source'] == 'title_year' and row['match_basis'] == '剧名 + 年份'


def test_multi_full_evidence_explains_missing_season_and_uses_configured_tie():
    cloud = [file('l', 1, n, '1080p') for n in (1, 2)] + [file('l', 2, 1, '1080p')]
    share = [file('s', 1, n, '1080p') for n in (1, 2)]
    result = governance_preview(snap(cloud, share), policy(), tie='share', threshold=.9)
    row = result['dual'][0]
    assert row['multi_mode'] == 'full' and row['cloud_seasons'] == [1, 2] and row['share_seasons'] == [1]
    assert row['comparison']['share_better'] == 2 and row['comparison']['cloud_better'] == 0
    assert row['reasons'] == []  # 平局保留分享，不能误报“分享画质次级”。
    checks = {entry['season']: entry for entry in row['multi_checks']}
    assert checks[1]['passed'] is True
    assert checks[2]['passed'] is False and checks[2]['reasons'] == ['分享缺少该季']
    assert row['winner'] == 'cloud'  # 保护整剧，即使该季分享已达标。


def test_keep_local_detail_does_not_misrepresent_quality_as_decision():
    cloud = [file('l', 1, 1, '1080p')]
    share = [file('s', 1, 1, '2160p')]
    row = governance_preview(snap(cloud, share), policy(), decision='keep_local')['dual'][0]
    assert row['decision'] == 'keep_local' and row['winner'] == 'cloud'
    assert row['comparison']['share_better'] == 1 and not row['comparison']['threshold_applied']


def test_fewer_share_episodes_keeps_local():
    """分享集数少于本地 → 集数优先，淘汰分享（1.2.12 口径 local_wins_fewer）。"""
    result = governance_preview(snap([file('l', 1, n, '1080p') for n in (1, 2, 3)],
                                     [file('s', 1, n, '2160p') for n in (1, 2)]), policy())
    assert actions(result)['local_wins_fewer']['action_kind'] == 'shr'


def test_decision_model_overrides_quality():
    local = [file('l', 1, n, '1080p') for n in (1, 2)]
    share = [file('s', 1, n, '2160p') for n in (1, 2)]
    keep_local = governance_preview(snap(local, share), policy(), decision='keep_local')
    assert actions(keep_local)['decision_keep_local']['action_kind'] == 'shr'

    keep_share = governance_preview(snap(local, share), policy(), decision='keep_share')
    assert actions(keep_share)['decision_keep_share']['action_kind'] == 'loc'

    # 分享没覆盖本地全部集时不删本地
    uncovered = governance_preview(snap(local, share[:1]), policy(), decision='keep_share')
    assert 'decision_keep_share' not in actions(uncovered)


def test_multi_share_protect_deletes_local_season():
    """本地季 ⊂ 分享季 → 保护分享合集：共同季淘汰本地（multi_protect_partial_local）。"""
    result = governance_preview(snap([file('l', 1, n, '1080p') for n in (1, 2)],
                                     [file('s', 1, n, '1080p') for n in (1, 2)]
                                     + [file('s', 2, 1, '1080p')]), policy())
    assert result['multi'][0]['mode'] == 'share_protect'
    row = actions(result)['multi_protect_partial_local']
    assert row['season'] == 1 and row['action_kind'] == 'loc'


def test_multi_full_mode_eliminates_share_unconditionally():
    """分享季 ⊆ 本地季：逐共同季无条件淘汰分享（multi_protect_partial_share）。"""
    result = governance_preview(snap([file('l', 1, n, '1080p') for n in (1, 2)]
                                     + [file('l', 2, 1, '1080p')],
                                     [file('s', 1, n, '2160p') for n in (1, 2)]), policy())
    assert result['multi'][0]['mode'] == 'full'
    assert actions(result)['multi_protect_partial_share']['action_kind'] == 'shr'


def test_multi_full_zero_sum_deletes_whole_local_show():
    """每个本地季都达标且分享覆盖全部季 → 整剧零和删本地（multi_full_share）。"""
    result = governance_preview(snap([file('l', 1, n, '1080p') for n in (1, 2)]
                                     + [file('l', 2, n, '1080p') for n in (1, 2)],
                                     [file('s', 1, n, '2160p') for n in (1, 2)]
                                     + [file('s', 2, n, '2160p') for n in (1, 2)]), policy())
    assert result['multi'][0]['decision'] == 'delete_show_local'
    assert {row['reason'] for row in result['dual']} == {'multi_full_share'}
    assert {row['action_kind'] for row in result['dual']} == {'loc'}

def test_movie_follows_decision_model_before_quality():
    """1.2.12 `_share_wins`：决策模型优先于画质（电影同样适用）。"""
    local = [file('l', 0, 0, '1080p', 'M.1080p.strm')]
    share = [file('s', 0, 0, '2160p', 'M.2160p.strm')]
    keep_local = governance_preview(snap(local, share, kind='movie'), policy(),
                                    decision='keep_local')
    assert keep_local['dual'][0]['reason'] == 'local_better'
    assert keep_local['dual'][0]['action_kind'] == 'shr'
    keep_share = governance_preview(snap(local, share, kind='movie'), policy(),
                                    decision='keep_share')
    assert keep_share['dual'][0]['reason'] == 'share_better'
    assert keep_share['dual'][0]['action_kind'] == 'loc'


def test_whitelist_groups_whole_show_into_one_row():
    """1.2.12 `_group_exempt_acts`：整部作品聚合成一条，而不是每季一条。"""
    result = governance_preview(snap([file('l', 1, 1, '1080p'), file('l', 2, 1, '1080p')],
                                     [file('s', 1, 1, '1080p'), file('s', 2, 1, '1080p')],
                                     title='百家讲坛'),
                                policy(), exempt_keywords=['百家讲坛'])
    assert result['dual'] == [] and result['single'] == []
    assert len(result['exempt']) == 1
    assert result['exempt'][0]['member_count'] == 2
    assert result['exempt'][0]['keywords'] == ['百家讲坛']


def test_unknown_quality_falls_back_to_tie_instead_of_skipping():
    """1.2.12：判不出画质按平局处理（不再整条跳过）。"""
    result = governance_preview(snap([file('l', 0, 0, 'x', 'M.strm')],
                                     [file('s', 0, 0, 'y', 'N.strm')], kind='movie'), policy())
    assert result['dual'][0]['winner'] == 'cloud'
    assert result['blocked'] == []


def test_gap_season_becomes_deletable_action():
    """1.2.12 `_emit_season_residuals`：缺集的一侧直接出删除动作，不再只是"单库待办"。"""
    result = governance_preview(snap([file('l', 1, 2, '1080p'), file('l', 1, 4, '1080p')], []), policy())
    row = result['dual'][0]
    assert (row['action_kind'], row['reason'], row['season']) == (
        'loc', 'incomplete_delete_local', 1)
    assert row['gap'] == '缺失前序 (缺 E01) 且 中间断层 (缺 E03，疑似被和谐)'
    assert row['winner'] == 'share'
    assert result['single'] == []


def test_gap_on_both_sides_yields_two_independent_actions():
    data = snap([file('l', 1, 7, '1080p'), file('l', 1, 8, '1080p')],
                [file('s', 1, 8, '2160p'), file('s', 1, 10, '2160p')])
    result = governance_preview(data, policy())
    assert {row['reason'] for row in result['dual']} == {'incomplete_delete_local',
                                                         'incomplete_delete_share'}
    assert {row['action_kind'] for row in result['dual']} == {'loc', 'shr'}
    # 两侧同批仍按既有安全规则拒绝（执行器要求分别核实）
    from strimkeep.inventory_operations import dual_actions
    selections = [{k: row.get(k) for k in ('kind', 'tmdb_id', 'season', 'winner', 'action_kind')}
                  for row in result['dual']]
    targets = dual_actions(data, result, selections)
    assert {target['side'] for target in targets} == {'cloud', 'share'}
    assert len({(target['side'], target['path']) for target in targets}) == len(targets)


def test_no_common_episodes_only_handles_residual():
    result = governance_preview(snap([file('l', 1, 1, '1080p')],
                                     [file('s', 1, 3, '2160p')]), policy())
    assert [(row['action_kind'], row['reason']) for row in result['dual']] == [
        ('shr', 'incomplete_delete_share')]
    assert result['single'] == []


def test_library_dedupe_runs_independently_of_dual_decision():
    """1.2.12：库内多版本是独立一遍，双库结论照常给。"""
    keep_local = governance_preview(snap([file('l', 1, 1, '1080p'), file('l', 1, 1, '720p'),
                                          file('l', 1, 2, '1080p')],
                                         [file('s', 1, 1, '1080p')]), policy())
    assert [row['reason'] for row in keep_local['dual']] == ['local_wins_fewer']
    assert [row['side'] for row in keep_local['single']] == ['cloud']
    covered = governance_preview(snap([file('l', 1, 1, '1080p'), file('l', 1, 1, '720p')],
                                      [file('s', 1, 1, '2160p')]), policy())
    assert [row['reason'] for row in covered['dual']] == ['share_wins']
    assert covered['single'] == []


def test_gap_season_action_does_not_block_other_seasons():
    result = governance_preview(snap([file('l', 1, 2, '1080p'), file('l', 2, 1, '1080p')],
                                     [file('s', 1, 1, '2160p'), file('s', 2, 1, '2160p')]),
                                policy(), multi_season_protect='off')
    by_season = {row['season']: row for row in result['dual']}
    assert by_season[1]['reason'] == 'incomplete_delete_local'
    assert by_season[2]['reason'] == 'share_wins'


def test_no_effective_rules_falls_back_to_tie():
    disabled = policy()
    for rule in disabled['rules']:
        rule['enabled'] = False
    result = governance_preview(snap([file('l', 1, 2, '1080p'), file('l', 2, 1, '1080p')],
                                     [file('s', 1, 1, '2160p'), file('s', 2, 1, '2160p')]),
                                disabled, multi_season_protect='off')
    assert 'incomplete_delete_local' in [row['reason'] for row in result['dual']]
    assert any(row['action_kind'] == 'shr' for row in result['dual'])
    assert not any(row['reason'] == 'quality_rules_disabled' for row in result['blocked'])


def test_keep_local_eliminates_share_directly():
    result = governance_preview(snap([file('l', 1, 1, '1080p')],
                                     [file('s', 1, 1, '2160p'), file('s', 1, 2, '2160p')]),
                                policy(), decision='keep_local')
    row = result['dual'][0]
    assert (row['action_kind'], row['reason']) == ('shr', 'decision_keep_local')
    assert len(row['share_files']) == 2


def test_multi_version_does_not_block_multi_season_decision():
    data = snap([file('l', 1, 1, '1080p'), file('l', 1, 1, '720p'),
                 file('l', 2, 1, '1080p'), file('l', 2, 1, '720p')],
                [file('s', 1, 1, '2160p'), file('s', 2, 1, '2160p')])
    result = governance_preview(data, policy())
    assert {row['reason'] for row in result['dual']} == {'multi_full_share'}
    assert {row['action_kind'] for row in result['dual']} == {'loc'}


def test_gap_action_covers_version_cleanup():
    data = snap([file('l', 1, 1, '1080p'), file('l', 1, 1, '720p'), file('l', 1, 3, '1080p')], [])
    result = governance_preview(data, policy())
    assert [row['reason'] for row in result['dual']] == ['incomplete_delete_local']
    assert result['single'] == []


def _with_ns(row, mtime_ns, directory_mtime=None):
    row = dict(row)
    row['file_mtime_ns'] = mtime_ns
    if directory_mtime is not None:
        row['directory_mtime'] = directory_mtime
    return row


def _blocked_reasons(result):
    return [row['reason'] for row in result['blocked']]


def test_quiet_gate_ignores_directory_mtime_of_old_works():
    """静默期是「入库」窗口：老作品即使目录刚被删文件刷新过，也不该再被挡。"""
    import time
    old_ns = int((time.time() - 3 * 3600) * 1e9)
    just_touched = int(time.time())
    cloud = [_with_ns(file('l', 1, 1, '1080p'), old_ns, just_touched)]
    share = [_with_ns(file('s', 1, 1, '2160p'), old_ns, just_touched)]
    result = governance_preview(snap(cloud, share), policy(), quiet_minutes=15)
    assert 'recent_activity' not in _blocked_reasons(result)


def test_quiet_gate_blocks_fresh_ingest_and_reports_remaining_minutes():
    import time
    fresh_ns = int((time.time() - 60) * 1e9)
    old_ns = int((time.time() - 3 * 3600) * 1e9)
    cloud = [_with_ns(file('l', 1, 1, '1080p'), old_ns)]
    share = [_with_ns(file('s', 1, 1, '2160p'), fresh_ns)]
    result = governance_preview(snap(cloud, share), policy(), quiet_minutes=15)
    rows = [row for row in result['blocked'] if row['reason'] == 'recent_activity']
    assert rows, _blocked_reasons(result)
    assert 0 < rows[0]['quiet_remaining'] <= 15
    assert rows[0]['ingested_at'] == fresh_ns // 10 ** 9
    assert rows[0]['quiet_minutes'] == 15


def test_single_sided_season_residual_is_flagged_not_silent():
    """另一侧没有这一季：动作照给（用户规则），但必须标 single_side 并说明损失。

    这是对 1.2.12 的一处有意差异：1.2.12 口径在 off 档对单边季静默跳过。
    """
    result = governance_preview(snap([file('l', 1, 1, '1080p')],
                                     [file('s', 2, 1, '2160p'), file('s', 2, 3, '2160p')]),
                                policy(), multi_season_protect='off')
    row, = [item for item in result['dual'] if item['season'] == 2]
    assert (row['action_kind'], row['reason']) == ('shr', 'incomplete_delete_share')
    assert row['single_side'] is True and row['clears_show'] is True
    assert row['lost_episodes'] == [1, 3]
    both = governance_preview(snap([file('l', 2, 1, '1080p'), file('l', 2, 3, '1080p')],
                                   [file('s', 2, 1, '2160p')]),
                              policy(), multi_season_protect='off')
    assert all(item['single_side'] is False for item in both['dual'])


def test_season_dedupe_is_one_row_per_season():
    """库内多版本：一季多集重复 → 一季一条（1.2.12 的 `dup_eps`），不是每集一条。"""
    cloud = [file('l', 1, 1, '1080p'), file('l', 1, 1, '720p'),
             file('l', 1, 2, '1080p'), file('l', 1, 2, '720p'),
             file('l', 1, 3, '1080p')]
    result = governance_preview(snap(cloud, [file('s', 1, 1, '1080p')]),
                                policy(), multi_season_protect='off')
    rows = [row for row in result['single'] if row['side'] == 'cloud']
    assert len(rows) == 1
    row = rows[0]
    assert row['dup_episodes'] == [1, 2]
    assert len(row['losers']) == 2
    assert {tuple(item['episodes']) for item in row['losers']} == {(1,), (2,)}
    assert [keep['episode'] for keep in row['keeps']] == [1, 2]
    assert row['reason_label'] == '库内多版本-删低版'


def test_multi_season_protection_suppresses_library_dedupe():
    """多季保护命中后 1.2.12 直接 continue：不再混进「库内多版本」条目。"""
    cloud = [file('l', 1, 1, '1080p'), file('l', 1, 1, '720p'),
             file('l', 2, 1, '1080p')]
    share = [file('s', 1, 1, '2160p'), file('s', 1, 1, '1080p'),
             file('s', 2, 1, '2160p')]
    result = governance_preview(snap(cloud, share), policy())
    assert result['dual'] and result['multi']
    assert result['single'] == []


def test_decision_model_suppresses_library_dedupe():
    cloud = [file('l', 1, 1, '1080p'), file('l', 1, 1, '720p'),
             file('l', 2, 1, '1080p')]
    share = [file('s', 1, 1, '2160p'), file('s', 2, 1, '2160p')]
    result = governance_preview(snap(cloud, share), policy(), decision='keep_local')
    assert {row['reason'] for row in result['dual']} == {'decision_keep_local'}
    assert result['single'] == []


def test_quiet_gate_does_not_block_when_timestamps_missing():
    """没有入库时间时不能无限期挡住：视为「没变动」。"""
    result = governance_preview(snap([file('l', 1, 1, '1080p')], [file('s', 1, 1, '2160p')]),
                                policy(), quiet_minutes=15)
    assert 'recent_activity' not in _blocked_reasons(result)
