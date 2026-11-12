"""差分测试：我们的双库判定 vs 参考实现 1.2.12（原样收藏在 tests 里）。

参考实现的纯规则模块 `app/domain/governance.py` 直接收录为
`reference_1212_governance.py`（无 IO、无依赖），这里用穷举/随机样例把两边的
裁决结论逐一比对，任何一处分支漂移都会立刻失败。
"""
import itertools
from types import SimpleNamespace

import pytest

import reference_1212_governance as ref
from strimkeep.inventory_preview import _multi_all_pass, _season_actions, _season_compare
from strimkeep.inventory_rules import (cmp_versions, multi_protect_mode, protect_s00,
                                       share_protect_verdict)
from strimkeep.quality import public_policy


def build_policy():
    value = public_policy()
    for rule in value['rules']:
        rule['enabled'] = rule['key'] == 'resolution'
    return value


POLICY = build_policy()
RATIO = 0.8


def rows(episodes, quality, season=1):
    return [{'name': 'x.%sp.strm' % quality, 'episodes': [episode], 'season': season}
            for episode in episodes]


def ref_files(episode_rows, season=1):
    return [SimpleNamespace(name=row['name'], season=season, episode=row['episodes'][0])
            for row in episode_rows]


def ref_compare(share_rows, cloud_rows, tie_share_wins):
    """用同一套比较函数跑参考实现，保证差异只来自裁决分支。"""
    return ref.season_compare(
        ref_files(share_rows), ref_files(cloud_rows),
        ep_of=lambda file: (file.season, file.episode),
        cmp=lambda left, right: cmp_versions(left, right, POLICY),
        tie_share_wins=tie_share_wins,
        compare_meta=lambda left, right: {'left': left, 'right': right})


QUALITY_PATTERNS = (
    ('2160p', '1080p'),   # 分享画质更好
    ('1080p', '2160p'),   # 本地画质更好
    ('1080p', '1080p'),   # 完全打平
    ('2160p', '720p'),    # 分享更好（差距更大）
)


@pytest.mark.parametrize('tie_share_wins', (True, False))
@pytest.mark.parametrize('share_quality,cloud_quality', QUALITY_PATTERNS)
def test_season_outcome_matches_reference_for_every_episode_combination(
        tie_share_wins, share_quality, cloud_quality):
    """枚举 E01..E04 的所有覆盖组合（含空集、断档、集数不等），逐例比对裁决。"""
    episodes = (1, 2, 3, 4)
    for size_s in range(len(episodes) + 1):
        for size_c in range(len(episodes) + 1):
            for sub_s in itertools.combinations(episodes, size_s):
                for sub_c in itertools.combinations(episodes, size_c):
                    share_rows, cloud_rows = rows(sub_s, share_quality), rows(sub_c, cloud_quality)
                    expected = ref.season_verdict(
                        ref_compare(share_rows, cloud_rows, tie_share_wins),
                        bool(cloud_rows), bool(share_rows), RATIO)
                    compare = _season_compare(share_rows, cloud_rows, POLICY,
                                              'share' if tie_share_wins else 'cloud')
                    actions, outcome = _season_actions(compare, 1, RATIO)
                    reasons = [action['action_reason'] for action in actions]
                    label = '分 %s / 云 %s / %s/%s' % (sub_s, sub_c, share_quality, cloud_quality)
                    assert outcome == expected['outcome'], label
                    assert ('incomplete_delete_local' in reasons) == expected['residual_local'], label
                    assert ('incomplete_delete_share' in reasons) == expected['residual_share'], label


def test_multi_protect_mode_matches_reference():
    for share in itertools.chain.from_iterable(
            itertools.combinations((1, 2, 3), size) for size in range(1, 4)):
        for cloud in itertools.chain.from_iterable(
                itertools.combinations((1, 2, 3), size) for size in range(1, 4)):
            assert multi_protect_mode(set(share), set(cloud)) == \
                ref.multi_protect_mode(share, cloud), (share, cloud)


def test_protect_s00_matches_reference():
    for protect in ('compare', 'off'):
        for special in ('compare', 'delete', 'ignore'):
            for decision in ('quality_first', 'keep_local', 'keep_share'):
                for local in ({1}, {0, 1}, {1, 2}, {0, 1, 2}):
                    for share in ({1}, {0, 1}, {1, 2, 3}):
                        assert protect_s00(protect, special, decision, local, share) == \
                            ref.protect_s00(protect, special, decision, local, share), \
                            (protect, special, decision, local, share)


def test_share_protect_verdict_matches_reference():
    for local in (set(), {1}, {1, 2}):
        for share in (set(), {1}, {1, 2}, {1, 2, 3}):
            ours = share_protect_verdict(share, local)
            theirs = ref.share_protect_verdict({'l_eps': dict.fromkeys(local),
                                                's_eps': dict.fromkeys(share)})
            assert ours == theirs, (share, local)


def test_multi_all_pass_matches_reference():
    seasons = (('a', (1,)), ('b', (2,)), ('ab', (1, 2)))
    for share_key in seasons:
        for cloud_key in seasons:
            sp = {season: rows((1, 2), '2160p', season) for season in share_key[1]}
            lp = {season: rows((1, 2), '1080p', season) for season in cloud_key[1]}
            ours = _multi_all_pass(sp, lp, POLICY, 'cloud', RATIO)
            theirs = ref.multi_all_pass(
                lp, sp,
                lambda share_rows, cloud_rows: ref_compare(share_rows, cloud_rows, False),
                RATIO)
            assert ours == theirs, (share_key[0], cloud_key[0])
