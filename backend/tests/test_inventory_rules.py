import pytest

from strimkeep.inventory_rules import compare_filenames, episode_gaps, season_decision, single_library_versions
from strimkeep.media import NeedsReview
from strimkeep.quality import public_policy


def policy(*keys):
    result = public_policy()
    for rule in result['rules']:
        rule['enabled'] = rule['key'] in keys
    return result


def test_gaps_distinguish_prefix_and_internal_gaps():
    assert episode_gaps([0, 3, 4, 5])['missing'] == [1, 2]
    assert episode_gaps([*range(1, 26), 27])['missing'] == [26]
    assert episode_gaps([1, 2, 2])['complete']
    assert not episode_gaps([0])['complete']


def test_filename_comparison_ignores_size_and_keeps_unknown():
    p = policy('resolution', 'size')
    assert compare_filenames('x.2160p.strm', 'x.1080p.strm', p)[0] == 1
    with pytest.raises(NeedsReview):
        compare_filenames('x.strm', 'x.1080p.strm', p)
    with pytest.raises(NeedsReview):
        compare_filenames('x.1080p.strm', 'x.1080p.strm', policy('size'))


def test_season_never_deletes_uncovered_episodes():
    p = policy('resolution')
    share = {1: 'x.2160p.strm', 2: 'x.2160p.strm'}
    cloud = {1: 'x.1080p.strm', 2: 'x.1080p.strm', 3: 'x.1080p.strm'}
    # 分享集数少 → 集数优先，淘汰分享（不会因为画质好就删掉本地独有的 E03）
    assert season_decision(share, cloud, p)['winner'] == 'cloud'
    # 反过来：分享覆盖了本地全部集且画质更好，删本地不会丢任何一集 → share_wins
    assert season_decision({ep: 'x.2160p.strm' for ep in (1, 2, 3)},
                           {1: 'x.1080p.strm', 2: 'x.1080p.strm'}, p)['winner'] == 'share'
    # 两侧各有对方没有的集时，至少一侧必然不完整 → 落到 incomplete，只清残次品
    assert season_decision({1: 'x.2160p.strm', 3: 'x.2160p.strm'},
                           {1: 'x.1080p.strm', 2: 'x.1080p.strm'}, p)['outcome'] == 'incomplete'


def test_season_ties_and_incomplete_sides():
    p = policy('resolution')
    files = {1: 'x.1080p.strm', 2: 'x.1080p.strm'}
    assert season_decision(files, files, p, tie='share')['winner'] == 'share'
    assert season_decision(files, files, p, tie='cloud')['winner'] == 'cloud'
    assert season_decision({2: 'x.2160p.strm'}, files, p)['reason'] == 'incomplete'


def test_mixed_quality_and_unknown_follow_reference_tie_rule():
    """1.2.12：判不出画质就是平局，平局按开关走，不额外中止裁决。"""
    p = policy('resolution')
    share = {1: 'x.2160p.strm', 2: 'x.720p.strm'}
    cloud = {1: 'x.1080p.strm', 2: 'x.1080p.strm'}
    assert season_decision(share, cloud, p)['winner'] == 'cloud'
    assert season_decision(share, cloud, p)['outcome'] == 'local_wins_quality'
    # 1.2.12 将未知分辨率排在已识别分辨率之后，平局开关不应覆盖已决胜的维度。
    unknown = season_decision({1: 'x.strm'}, {1: 'x.1080p.strm'}, p)
    assert unknown['outcome'] == 'local_wins_quality' and unknown['winner'] == 'cloud'
    assert season_decision({1: 'x.strm'}, {1: 'x.1080p.strm'}, p, tie='share')['winner'] == 'cloud'


def test_both_unknown_dimension_continues_to_framerate():
    from strimkeep.inventory_rules import compare_outcome
    from reference_1212_quality import compare_cover, cover_default_strategy
    p = policy('resolution', 'dynamic_range', 'fps')
    left, right = 'x.1080p.60fps.strm', 'x.1080p.24fps.strm'
    old = cover_default_strategy()
    for rule in old['rules']:
        rule['enabled'] = rule['key'] in ('resolution', 'dolby', 'fps')
    result, evidence = compare_outcome(left, right, p)
    assert result == compare_cover(left, right, old)
    assert result == 1
    assert any(item['rule'] == 'fps' and item['result'] == 1 for item in evidence)


def test_vivid_and_eac3_follow_reference_tiers_and_user_order():
    from strimkeep.inventory_rules import compare_outcome
    from reference_1212_quality import compare_cover, cover_default_strategy
    for dimension, old_dimension, order, pairs in [
        ('dynamic_range', 'dolby', ['hdr10plus', 'hdr10', 'sdr'],
         [('x.HDR.Vivid.strm', 'x.HDR10.strm'), ('x.HDR10+.strm', 'x.HDR.Vivid.strm')]),
        ('audio', 'audio', ['flac', 'eac3', 'aac'],
         [('x.AV3A.strm', 'x.E-AC-3.strm'), ('x.E-AC-3.strm', 'x.AAC.strm')]),
    ]:
        p = policy(dimension)
        next(rule for rule in p['rules'] if rule['key'] == dimension)['order'] = order
        old = cover_default_strategy()
        for rule in old['rules']:
            rule['enabled'] = rule['key'] == old_dimension
        for left, right in pairs:
            assert compare_outcome(left, right, p)[0] == compare_cover(left, right, old) == 1


def test_user_explicit_vivid_order_is_preserved():
    from strimkeep.inventory_rules import compare_outcome
    p = policy('dynamic_range')
    next(rule for rule in p['rules'] if rule['key'] == 'dynamic_range')['order'] = ['hdr10', 'hdrvivid']
    assert compare_outcome('x.HDR.Vivid.strm', 'x.HDR10.strm', p)[0] == -1


def test_unconfigured_vivid_without_anchor_requires_review():
    from strimkeep.inventory_rules import compare_outcome
    p = policy('dynamic_range')
    next(rule for rule in p['rules'] if rule['key'] == 'dynamic_range')['order'] = ['sdr']
    with pytest.raises(NeedsReview, match='HDR Vivid'):
        compare_outcome('x.HDR.Vivid.strm', 'x.SDR.strm', p)
    p = policy('audio')
    next(rule for rule in p['rules'] if rule['key'] == 'audio')['order'] = ['aac']
    with pytest.raises(NeedsReview, match='AV3A'):
        compare_outcome('x.AV3A.strm', 'x.AAC.strm', p)


def test_new_tiers_roundtrip_through_policy_and_filename_parser():
    from strimkeep.media import media_from_filename
    p = policy('dynamic_range', 'audio')
    next(rule for rule in p['rules'] if rule['key'] == 'dynamic_range')['order'] = ['hdrvivid', 'hdr10']
    next(rule for rule in p['rules'] if rule['key'] == 'audio')['order'] = ['av3a', 'eac3']
    result = public_policy(p)
    assert next(rule for rule in result['rules'] if rule['key'] == 'audio')['order'] == ['av3a', 'eac3']
    media = media_from_filename('x.HDR.Vivid.E-AC-3.5.1.strm')
    assert media['dynamic_range'] == 'HDR Vivid' and media['audio'][0]['codec'] == 'eac3'
    assert media_from_filename('x.Audio.Vivid.2.0.strm')['audio'][0]['codec'] == 'av3a'


def test_share_qualified_ratio_and_coverage_match_reference():
    p = policy('resolution')
    cloud = {ep: 'x.1080p.strm' for ep in range(1, 11)}
    share = {ep: ('x.720p.strm' if ep == 10 else 'x.2160p.strm') for ep in range(1, 11)}
    verdict = season_decision(share, cloud, p, threshold=.9)
    assert verdict['winner'] == 'share' and verdict['share_ratio'] == .9
    assert season_decision(share, cloud, p, threshold=1)['winner'] == 'cloud'
    # 分享多出一集但达标率不够：1.2.12 判 local_wins_quality（淘汰分享），不是交人工
    share[11] = 'x.720p.strm'
    assert season_decision(share, cloud, p, threshold=1)['outcome'] == 'local_wins_quality'


def test_single_library_winner_and_uncertain_versions():
    p = policy('resolution')
    assert single_library_versions(['x.1080p.strm', 'x.2160p.strm', 'x.720p.strm'], p)['winner'] == 1
    assert single_library_versions(['x.1080p.strm', 'y.1080p.strm'], p)['reason'] == 'tie_or_conflict'
    assert single_library_versions(['x.1080p.strm', 'x.strm'], p)['reason'] == 'unknown_quality'


def test_outcome_vocabulary_matches_reference_1_2_12():
    """裁决词汇对齐 1.2.12 season_verdict：share_wins / local_wins_fewer / local_wins_quality。"""
    p = policy('resolution')
    share = {ep: 'x.2160p.strm' for ep in range(1, 11)}
    cloud = {ep: 'x.1080p.strm' for ep in range(1, 11)}
    assert season_decision(share, cloud, p)['outcome'] == 'share_wins'
    # 分享集数少（1-8 vs 1-10）：集数优先，淘汰分享
    assert season_decision({ep: 'x.2160p.strm' for ep in range(1, 9)}, cloud,
                           p)['outcome'] == 'local_wins_fewer'
    # 同集数但分享画质次级：画质优先
    assert season_decision({ep: 'x.720p.strm' for ep in range(1, 11)}, cloud,
                           p)['outcome'] == 'local_wins_quality'
    # 两侧各有对方没有的集 → 必然至少一侧不完整（E01 起有断层），
    # 所以这里落到 incomplete，而不是去比画质：这是"只清残次品"的前提。
    gapped = {1: 'x.2160p.strm', 2: 'x.2160p.strm', 5: 'x.2160p.strm'}
    assert season_decision(gapped, cloud, p)['outcome'] == 'incomplete'


def test_sample_points_at_the_episode_that_decided():
    """1.2.12 的 sample：第一个真正分出胜负的共同集，供页面展示决胜依据。"""
    p = policy('resolution')
    share = {1: 'x.1080p.strm', 2: 'x.2160p.strm', 3: 'x.2160p.strm'}
    cloud = {1: 'x.1080p.strm', 2: 'x.1080p.strm', 3: 'x.1080p.strm'}
    verdict = season_decision(share, cloud, p, tie='cloud')
    assert verdict['sample']['episode'] == 2

    tied = {1: 'x.1080p.strm', 2: 'x.1080p.strm'}
    assert season_decision(tied, tied, p, tie='cloud')['sample'] is None


def test_residual_and_share_coverage_helpers():
    from strimkeep.inventory_rules import residual_flags, share_covers_local

    assert share_covers_local({1, 2, 3}, {1, 2})
    assert not share_covers_local({1, 2}, {1, 2, 3})
    flags = residual_flags({1, 2}, {2, 3})
    assert flags == {'share': False, 'cloud': True}


def test_multi_season_modes_match_reference():
    from strimkeep.inventory_rules import multi_protect_mode, protect_s00, share_protect_verdict

    assert multi_protect_mode({1, 2, 3}, {1, 2}) == 'share_protect'
    assert multi_protect_mode({1, 2}, {1, 2}) == 'full'
    assert multi_protect_mode({1}, {1, 2}) == 'full'
    assert multi_protect_mode({1, 3}, {1, 2}) == 'overlap'

    assert share_protect_verdict({1, 2, 3}, set()) == 'skip'
    assert share_protect_verdict({1, 2}, {1, 2, 3}) == 'share_fewer'
    assert share_protect_verdict({1, 2, 3}, {1, 2}) == 'delete_local'

    assert protect_s00('compare', 'compare', 'quality_first', [0, 1], [1]) is True
    assert protect_s00('compare', 'compare', 'quality_first', [1], [1]) is False
    assert protect_s00('off', 'compare', 'quality_first', [0, 1], [1]) is False


def test_partial_share_reasons_list_every_cause():
    from strimkeep.inventory_rules import partial_share_reasons

    p = policy('resolution')
    cloud = {ep: 'x.1080p.strm' for ep in range(1, 11)}
    assert partial_share_reasons({ep: 'x.2160p.strm' for ep in range(1, 9)}, cloud, p) == \
        ['分享副本集数不足 (8/10集)']
    assert partial_share_reasons({ep: 'x.720p.strm' for ep in range(1, 11)}, cloud, p) == \
        ['分享画质次级 (10/10集)']
    assert partial_share_reasons({1: 'x.2160p.strm'}, cloud, p)[0].startswith('分享副本集数不足')
    # 从第 2 集开始就是"残次品"（E01 缺失），原因要说的是缺集而不是集数
    assert partial_share_reasons({2: 'x.2160p.strm'}, cloud, p)[0].startswith('分享缺集')
