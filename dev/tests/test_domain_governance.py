# -*- coding: utf-8 -*-
"""
domain.governance 的纯函数测试：不碰文件系统 / SQLite / Emby / TMDB / 全局配置。
文件对象用最小的假对象（只有 .name），版本比较与集号解析全部注入。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2] / 'app'))
from domain import governance as g  # noqa: E402


class F:
    """最小文件替身：领域层只允许使用 .name"""
    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return 'F(%s)' % self.name


# 假比较：名字里带的 q<数字> 越大越好
def cmp_q(a, b):
    qa, qb = int(a.split('q')[-1].split('.')[0]), int(b.split('q')[-1].split('.')[0])
    return (qa > qb) - (qa < qb)


def ep_of(f):
    # 文件名形如 S01E03.q2.strm
    n = f.name
    return int(n[1:3]), int(n[4:6])


class TestShareWins:
    def test_keep_local_never_wins_for_share(self):
        assert g.share_wins('x.q9', 'x.q1', 'keep_local', cmp_q, True) is False

    def test_keep_share_always_wins(self):
        assert g.share_wins('x.q1', 'x.q9', 'keep_share', cmp_q, False) is True

    def test_quality_first_better_share_wins(self):
        assert g.share_wins('x.q9', 'x.q1', 'quality_first', cmp_q, False) is True
        assert g.share_wins('x.q1', 'x.q9', 'quality_first', cmp_q, True) is False

    def test_tie_follows_switch(self):
        assert g.share_wins('x.q5', 'x.q5', 'quality_first', cmp_q, True) is True
        assert g.share_wins('x.q5', 'x.q5', 'quality_first', cmp_q, False) is False

    def test_ep_share_ok_matches_share_wins_in_quality_mode(self):
        for a, b in (('x.q9', 'x.q1'), ('x.q1', 'x.q9'), ('x.q5', 'x.q5')):
            for tie in (True, False):
                assert g.ep_share_ok(a, b, cmp_q, tie) == g.share_wins(a, b, 'quality_first', cmp_q, tie)


class TestSideComplete:
    def test_continuous_from_one_is_complete(self):
        assert g.side_complete({1, 2, 3}) is True

    def test_missing_head_is_incomplete(self):
        assert g.side_complete({2, 3, 4}) is False

    def test_gap_in_middle_is_incomplete(self):
        assert g.side_complete({1, 2, 4}) is False

    def test_empty_and_zero_only_incomplete(self):
        assert g.side_complete(set()) is False
        assert g.side_complete({0}) is False

    def test_e00_is_ignored(self):
        assert g.side_complete({0, 1, 2}) is True


class TestSeasonCompare:
    @staticmethod
    def _run(s, l, tie=False):
        return g.season_compare([F(n) for n in s], [F(n) for n in l], ep_of, cmp_q, tie,
                                lambda a, b: {'share_name': a, 'local_name': b})

    def test_share_better_counts(self):
        r = self._run(['S01E01.q9.strm', 'S01E02.q9.strm'], ['S01E01.q1.strm', 'S01E02.q1.strm'])
        assert r['s_better'] == 2 and r['l_better'] == 0
        assert r['complete'] is True
        assert r['sample']['episode'] == 1

    def test_local_better_counts(self):
        r = self._run(['S01E01.q1.strm'], ['S01E01.q9.strm'])
        assert r['s_better'] == 0 and r['l_better'] == 1

    def test_only_sets_and_gap_detection(self):
        r = self._run(['S01E01.q5.strm', 'S01E03.q5.strm'], ['S01E01.q5.strm', 'S01E02.q5.strm'])
        assert r['s_only'] == [3] and r['l_only'] == [2]
        assert r['s_complete'] is False and r['l_complete'] is True and r['complete'] is False

    def test_duplicate_episode_keeps_best(self):
        r = self._run(['S01E01.q1.strm', 'S01E01.q7.strm'], ['S01E01.q5.strm'])
        assert r['s_eps'][1].name == 'S01E01.q7.strm'
        assert r['s_better'] == 1

    def test_season_zero_and_episode_zero_are_excluded(self):
        r = self._run(['S00E01.q9.strm', 'S01E00.q9.strm'], ['S01E01.q1.strm'])
        assert r['s_eps'] == {} and r['common'] == set()
        assert r['sample'] is None

    def test_tie_goes_by_switch(self):
        s, l = ['S01E01.q5.strm'], ['S01E01.q5.strm']
        assert self._run(s, l, tie=True)['s_better'] == 1
        assert self._run(s, l, tie=False)['l_better'] == 1


class TestSmallHelpers:
    def test_exempt_hit(self):
        assert g.exempt_hit('综艺/百家讲坛/x', ['百家讲坛', '不存在']) == ['百家讲坛']
        assert g.exempt_hit('x', []) == []
        assert g.exempt_hit('x', None) == []

    def test_nat_key_orders_numbers_naturally(self):
        assert sorted(['Season 10', 'Season 9', 'Season 2'], key=g.nat_key) == ['Season 2', 'Season 9', 'Season 10']


# ═══════════════════ 剧集整季裁决（纯函数） ═══════════════════

def _cmp(l_eps=(1, 2), s_eps=(1, 2), s_better=None, l_better=None, common=None,
         l_complete=True, s_complete=True):
    common = set(l_eps) & set(s_eps) if common is None else set(common)
    n = len(common)
    return {
        'l_eps': {e: object() for e in l_eps}, 's_eps': {e: object() for e in s_eps},
        'common': common,
        's_better': n if s_better is None else s_better,
        'l_better': 0 if l_better is None else l_better,
        'l_complete': l_complete, 's_complete': s_complete,
    }


class TestResidualFlags:
    def test_only_the_incomplete_side_is_flagged(self):
        assert g.residual_flags(_cmp(l_complete=False), True, True) == (True, False)
        assert g.residual_flags(_cmp(s_complete=False), True, True) == (False, True)
        assert g.residual_flags(_cmp(l_complete=False, s_complete=False), True, True) == (True, True)

    def test_no_files_means_nothing_to_delete(self):
        assert g.residual_flags(_cmp(l_complete=False, s_complete=False), False, False) == (False, False)


class TestSeasonVerdict:
    R = 0.9

    def test_incomplete_side_stops_quality_compare(self):
        v = g.season_verdict(_cmp(l_complete=False), True, True, self.R)
        assert v['outcome'] == 'incomplete' and v['residual_local'] is True

    def test_incomplete_with_no_files_still_returns_incomplete(self):
        v = g.season_verdict(_cmp(l_complete=False), False, True, self.R)
        assert v['outcome'] == 'incomplete' and v['residual_local'] is False

    def test_no_common_eps_is_silent(self):
        v = g.season_verdict(_cmp(l_eps=(1, 2), s_eps=(3, 4), common=()), True, True, self.R)
        assert v['outcome'] == 'no_common'

    def test_share_wins_when_ratio_met_and_not_fewer(self):
        assert g.season_verdict(_cmp(), True, True, self.R)['outcome'] == 'share_wins'
        # 分享集数更多也算
        assert g.season_verdict(_cmp(s_eps=(1, 2, 3)), True, True, self.R)['outcome'] == 'share_wins'

    def test_ratio_threshold_is_inclusive(self):
        eps = tuple(range(1, 11))
        at = _cmp(l_eps=eps, s_eps=eps, s_better=9, l_better=1)
        below = _cmp(l_eps=eps, s_eps=eps, s_better=8, l_better=2)
        assert g.season_verdict(at, True, True, 0.9)['outcome'] == 'share_wins'
        assert g.season_verdict(below, True, True, 0.9)['outcome'] == 'local_wins_quality'

    def test_fewer_share_eps_wins_for_local_even_if_quality_better(self):
        v = g.season_verdict(_cmp(l_eps=(1, 2, 3), s_eps=(1, 2)), True, True, self.R)
        assert v['outcome'] == 'local_wins_fewer'


class TestMultiSeason:
    def test_protect_mode_from_season_sets(self):
        assert g.multi_protect_mode({1, 2}, {1}) == 'share_protect'     # 本地是分享的真子集
        assert g.multi_protect_mode({1}, {1, 2}) == 'full'              # 分享是本地子集
        assert g.multi_protect_mode({1, 2}, {1, 2}) == 'full'           # 相等
        assert g.multi_protect_mode({1, 2}, {2, 3}) == 'overlap'        # 部分重叠
        assert g.multi_protect_mode({}, {1, 2}) == 'full'               # 分享没有任何季

    def test_multi_all_pass(self):
        ok = _cmp()
        bad_ratio = _cmp(s_better=0, l_better=2)
        bad_complete = _cmp(s_complete=False)
        fewer = _cmp(l_eps=(1, 2, 3), s_eps=(1, 2))
        l = {1: ['x'], 2: ['y']}
        s = {1: ['x'], 2: ['y']}
        assert g.multi_all_pass(l, s, lambda a, b: ok, 0.9) is True
        assert g.multi_all_pass(l, s, lambda a, b: bad_ratio, 0.9) is False
        assert g.multi_all_pass(l, s, lambda a, b: bad_complete, 0.9) is False
        assert g.multi_all_pass(l, s, lambda a, b: fewer, 0.9) is False
        assert g.multi_all_pass(l, {1: ['x']}, lambda a, b: ok, 0.9) is False   # 缺一季
        assert g.multi_all_pass(l, s, lambda a, b: _cmp(common=()), 0.9) is False

    def test_share_protect_verdict(self):
        assert g.share_protect_verdict(_cmp(l_eps=(), s_eps=(1,), common=())) == 'skip'
        assert g.share_protect_verdict(_cmp(l_eps=(1, 2, 3), s_eps=(1, 2))) == 'share_fewer'
        assert g.share_protect_verdict(_cmp(l_eps=(1, 2), s_eps=(1, 2, 3))) == 'delete_local'

    def test_partial_share_reasons(self):
        gap = lambda keys: '缺集(%s)' % ','.join(map(str, sorted(keys)))  # noqa: E731
        assert g.partial_share_reasons(_cmp(l_complete=False), gap) == ['本地缺集(1,2)']
        assert g.partial_share_reasons(_cmp(l_eps=(1, 2, 3), s_eps=(1, 2)), gap) == ['分享副本集数不足 (2/3集)']
        assert g.partial_share_reasons(_cmp(s_better=0, l_better=2), gap) == ['分享画质次级 (2/2集)']
        assert g.partial_share_reasons(_cmp(), gap) == []

    def test_protect_s00_rule(self):
        args = ('compare', 'compare', 'quality_first')
        assert g.protect_s00(*args, {0: 1, 1: 1}, {1: 1}) is True      # S00 + S01 = 2 季
        assert g.protect_s00(*args, {1: 1}, {1: 1}) is False
        assert g.protect_s00('off', 'compare', 'quality_first', {0: 1, 1: 1}, {1: 1}) is False
        assert g.protect_s00('compare', 'ignore', 'quality_first', {0: 1, 1: 1}, {1: 1}) is False
        assert g.protect_s00('compare', 'compare', 'keep_local', {0: 1, 1: 1}, {1: 1}) is False
        assert g.share_covers_local(_cmp(l_eps=(1, 2), s_eps=(1, 2, 3))) is True
        assert g.share_covers_local(_cmp(l_eps=(1, 2, 3), s_eps=(1, 2))) is False
