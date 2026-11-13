"""按用户给的规则逐条验证双库治理（对齐1.2.12 口径 的判定分支）。"""
import pytest

from strimkeep.inventory_preview import governance_preview
from strimkeep.quality import public_policy


def policy():
    value = public_policy()
    for rule in value['rules']:
        rule['enabled'] = rule['key'] == 'resolution'
    return value


def tv_file(side, season, episode, spec='1080p'):
    return {'id': f'{side}-{season}-{episode}-{spec}',
            'name': f'S{season:02d}E{episode:02d}.{spec}.strm',
            'path': f'{side}/S{season:02d}/{spec}/E{episode:02d}.strm',
            'season': season, 'episodes': [episode]}


def movie_file(side, spec):
    return {'id': f'{side}-{spec}', 'name': f'M.{spec}.strm', 'path': f'{side}/{spec}/M.strm',
            'season': None, 'episodes': []}


def snap(cloud, share, kind='tv', title='伟大的长征 (2026)'):
    return {'complete': True, 'issues': [],
            'items': [{'kind': kind, 'tmdb_id': 9, 'title': title,
                       'cloud': cloud, 'share': share}]}


def by_reason(result):
    return {row['reason']: row for row in result['dual']}


# ── 规则 1：前序缺失（S01E25 起、缺 E01–E24）= 残次品，纳入剔除 ──
def test_prefix_gap_is_residual_and_deleted_from_its_own_library():
    cloud = [tv_file('l', 1, n) for n in range(25, 61)]
    share = [tv_file('s', 1, n) for n in range(1, 61)]
    row = by_reason(governance_preview(snap(cloud, share), policy()))['incomplete_delete_local']
    assert (row['action_kind'], row['season']) == ('loc', 1)
    assert len(row['cloud_files']) == 36
    assert row['gap'] == '缺失前序 (缺 E01-E24)'


# ── 规则 2：中间缺集（E01–E58 + E60，缺 E59）= 残次品 ──
def test_internal_gap_is_residual_and_lists_the_missing_episode():
    cloud = [tv_file('l', 1, n) for n in list(range(1, 59)) + [60]]
    share = [tv_file('s', 1, n) for n in range(1, 61)]
    row = by_reason(governance_preview(snap(cloud, share), policy()))['incomplete_delete_local']
    assert row['gap'] == '中间断层 (缺 E59，疑似被和谐)'
    assert len(row['cloud_files']) == 59


# ── 规则 3：分享库同样两种情形 → 残次品-删分享，只动分享库 ──
def test_share_side_prefix_gap_cleans_only_the_share_library():
    cloud = [tv_file('l', 1, n) for n in range(1, 61)]
    share = [tv_file('s', 1, n) for n in range(25, 61)]
    row = by_reason(governance_preview(snap(cloud, share), policy()))['incomplete_delete_share']
    assert row['action_kind'] == 'shr'
    assert len(row['share_files']) == 36
    assert {file['id'] for file in row['share_files']} == {f's-1-{n}-1080p' for n in range(25, 61)}



# ── 规则 4：单库多版本（杜比/SDR）= 库内去重，绝不跨库 ──
def test_single_library_versions_are_deduped_inside_one_library_only():
    share = [tv_file('s', 1, 1, '1080p'), tv_file('s', 1, 1, '2160p'),
             tv_file('s', 1, 2, '1080p'), tv_file('s', 1, 2, '2160p')]
    result = governance_preview(snap([], share), policy())
    assert result['dual'] == []
    rows = [row for row in result['single'] if row['side'] == 'share']
    # 1.2.12 粒度：同一季一条，重复集合并（dup_eps），不是每集一条
    assert len(rows) == 1
    row = rows[0]
    assert row['season'] == 1 and row['reason'] == 'quality'
    assert row['dup_episodes'] == [1, 2]
    assert len(row['losers']) == 2
    assert {keep['name'] for keep in row['keeps']} == {'S01E01.2160p.strm', 'S01E02.2160p.strm'}
    assert {file['name'] for file in row['losers']} == {'S01E01.1080p.strm', 'S01E02.1080p.strm'}


# ── 规则 5：两库都不完整 → 只做残次品，不做洗版 ──
def test_two_incomplete_libraries_only_produce_residual_actions():
    cloud = [tv_file('l', 1, n) for n in range(1, 61) if n != 59]   # 缺 E59（中间缺集）
    share = [tv_file('s', 1, n) for n in range(1, 61) if n != 30]   # 缺 E30
    result = governance_preview(snap(cloud, share), policy())
    assert {row['reason'] for row in result['dual']} == {'incomplete_delete_local',
                                                         'incomplete_delete_share'}
    assert {row['action_kind'] for row in result['dual']} == {'loc', 'shr'}


# ── 规则 6：电影跨库洗版 + 决策模型 + 平局 ──
def test_movie_wash_priority_across_libraries_with_decision_and_tie():
    shrink = governance_preview(snap([movie_file('l', '2160p')], [movie_file('s', '1080p')],
                                     kind='movie'), policy())
    assert shrink['dual'][0]['action_kind'] == 'shr'   # 网盘 4K 更优 → 删 1080p 分享
    grow = governance_preview(snap([movie_file('l', '1080p')], [movie_file('s', '2160p')],
                                   kind='movie'), policy())
    assert grow['dual'][0]['action_kind'] == 'loc'     # 分享 4K 更优 → 删网盘
    keep_share = governance_preview(snap([movie_file('l', '2160p')], [movie_file('s', '1080p')],
                                         kind='movie'), policy(), decision='keep_share')
    assert keep_share['dual'][0]['action_kind'] == 'loc'
    keep_local = governance_preview(snap([movie_file('l', '1080p')], [movie_file('s', '2160p')],
                                         kind='movie'), policy(), decision='keep_local')
    assert keep_local['dual'][0]['action_kind'] == 'shr'
    tie_share = governance_preview(snap([movie_file('l', '2160p')], [movie_file('s', '2160p')],
                                        kind='movie'), policy(), tie='share')
    assert tie_share['dual'][0]['action_kind'] == 'loc'
    tie_cloud = governance_preview(snap([movie_file('l', '2160p')], [movie_file('s', '2160p')],
                                        kind='movie'), policy(), tie='cloud')
    assert tie_cloud['dual'][0]['action_kind'] == 'shr'


# ── 规则 7：多季保护防止掏空（乡村爱情：18 季 vs 8 季）──
def test_multi_season_protection_never_hollows_out_the_fuller_library():
    cloud = [tv_file('l', s, 1) for s in range(1, 19)]
    share = [tv_file('s', s, 1) for s in range(1, 9)]
    result = governance_preview(snap(cloud, share), policy())
    assert {row['action_kind'] for row in result['dual']} == {'shr'}
    assert {row['season'] for row in result['dual']} == set(range(1, 9))


def test_share_collection_is_protected_when_share_is_the_fuller_one():
    cloud = [tv_file('l', s, 1) for s in range(1, 9)]
    share = [tv_file('s', s, 1) for s in range(1, 19)]
    result = governance_preview(snap(cloud, share), policy())
    assert {row['action_kind'] for row in result['dual']} == {'loc'}
    assert {row['season'] for row in result['dual']} == set(range(1, 9))


def test_zero_sum_replaces_the_whole_show_only_when_every_season_passes():
    cloud = [tv_file('l', s, 1, '1080p') for s in range(1, 19)]
    share = [tv_file('s', s, 1, '2160p') for s in range(1, 19)]
    all_pass = governance_preview(snap(cloud, share), policy())
    assert {row['reason'] for row in all_pass['dual']} == {'multi_full_share'}
    assert {row['action_kind'] for row in all_pass['dual']} == {'loc'}

    share[5]['name'] = 'S06E01.720p.strm'   # 第 6 季分享更差 → 整体达不到门槛
    partial = governance_preview(snap(cloud, share), policy())
    assert {row['action_kind'] for row in partial['dual']} == {'shr'}
    assert len(partial['dual']) == 18


# ── 规则 8：不开多季保护 → 来一季完整就逐季替换，不完整的一季仍按残次品 ──
def test_multi_season_protection_off_replaces_season_by_season():
    cloud = [tv_file('l', s, 1, '1080p') for s in range(1, 4)]
    share = [tv_file('s', 1, 1, '2160p'), tv_file('s', 2, 1, '2160p')]
    result = governance_preview(snap(cloud, share), policy(), multi_season_protect='off')
    assert {row['season']: row['action_kind'] for row in result['dual']} == {1: 'loc', 2: 'loc'}

    broken = [tv_file('l', 1, n) for n in (1, 2, 4)]
    share_full = [tv_file('s', 1, n) for n in range(1, 5)]
    off = governance_preview(snap(broken, share_full), policy(), multi_season_protect='off')
    assert by_reason(off)['incomplete_delete_local']['gap'] == '中间断层 (缺 E03，疑似被和谐)'


# ── 残次品季已整季清理时，不再重复给该季的库内多版本 ──
def test_residual_season_suppresses_duplicate_version_item():
    cloud = [tv_file('l', 1, 1, '1080p'), tv_file('l', 1, 1, '2160p'),
             tv_file('l', 1, 3, '1080p')]
    share = [tv_file('s', 1, n) for n in (1, 2, 3)]
    result = governance_preview(snap(cloud, share), policy())
    assert [row['reason'] for row in result['dual']] == ['incomplete_delete_local']
    assert result['single'] == []



# ── 决策模型「保留本地」：本地这季不完整也不删本地（1.2.12 直接按决策取舍）──
def test_keep_local_never_deletes_an_incomplete_local_season():
    cloud = [tv_file('l', 1, n) for n in (1, 2, 4)]
    share = [tv_file('s', 1, n) for n in range(1, 5)]
    rows = by_reason(governance_preview(snap(cloud, share), policy(), decision='keep_local'))
    assert set(rows) == {'decision_keep_local'}
    assert rows['decision_keep_local']['action_kind'] == 'shr'
    assert len(rows['decision_keep_local']['share_files']) == 4


# ── 多季保护 full 档：本地某季残次时，「删本地残次」和「淘汰分享该季」两条都要在 ──
def test_multi_season_full_keeps_both_residual_and_share_elimination():
    cloud = [tv_file('l', 1, 1), tv_file('l', 1, 3), tv_file('l', 2, 1)]
    share = [tv_file('s', 1, n) for n in (1, 2, 3)] + [tv_file('s', 2, 1)]
    rows = {(row['season'], row['action_kind'], row['reason'])
            for row in governance_preview(snap(cloud, share), policy())['dual']}
    assert (1, 'loc', 'incomplete_delete_local') in rows
    assert (1, 'shr', 'multi_protect_partial_share') in rows
    assert (2, 'shr', 'multi_protect_partial_share') in rows


# ── 后果提示：哪些集会因为这次清理在两库都不再存在 ──
def test_action_reports_episodes_that_disappear_from_both_libraries():
    cloud = [tv_file('l', 1, n) for n in range(4, 8)]
    row = by_reason(governance_preview(snap(cloud, []), policy()))['incomplete_delete_local']
    assert row['lost_episodes'] == [4, 5, 6, 7]
    assert row['clears_show'] is True


def test_covered_residual_reports_no_loss():
    cloud = [tv_file('l', 1, n) for n in (1, 2, 4)]
    share = [tv_file('s', 1, n) for n in range(1, 5)]
    row = by_reason(governance_preview(snap(cloud, share), policy()))['incomplete_delete_local']
    assert row['lost_episodes'] == []
    assert row['clears_show'] is False



# ── 多季保护 overlap 档：各有独有季 → 两边都不动，只清残次品 ──
def test_overlap_mode_only_cleans_residuals():
    cloud = [tv_file('l', 1, n) for n in (1, 2, 4)] + [tv_file('l', 2, 1)]
    share = [tv_file('s', 1, n) for n in range(1, 5)] + [tv_file('s', 3, 1)]
    result = governance_preview(snap(cloud, share), policy())
    assert [row['reason'] for row in result['dual']] == ['incomplete_delete_local']
    assert result['dual'][0]['action_kind'] == 'loc'
