# -*- coding: utf-8 -*-
"""
Golden 场景矩阵：为剧集治理的每个决策分支各造一个最小场景，固化 build_plan() 的实际输出。

用途：拆分 governance.py 的 _emit_* 决策时的安全网（比 test_golden_plan.py 的单一综合场景细得多）。
  - 对比 dev/tests/golden/build_plan_matrix.json；有差异 = 治理行为变了。
  - 有意修改后：UPDATE_GOLDEN=1 python -m pytest dev/tests，并单独 commit 说明原因。
  - 「分支覆盖守卫」确保这些场景持续命中所有关键 reason，避免 golden 悄悄失效。
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix='strimkeep_matrix_'))
os.environ['L_ROOT'] = str(_TMP / 'local')
os.environ['S_ROOT'] = str(_TMP / 'share')
os.environ['CLOUD_L_ROOT'] = str(_TMP / 'cloud')
os.environ['AGENT_DATA'] = str(_TMP / 'data')
os.environ['TMDB_KEY'] = ''
os.environ['TG_BOT_TOKEN'] = ''
os.environ['INGEST_QUIET_MINUTES'] = '0'

sys.path.insert(0, str(Path(__file__).parents[2] / 'app'))
import engine  # noqa: E402

GOLDEN = Path(__file__).parent / 'golden' / 'build_plan_matrix.json'

BASE = {
    'decision': 'quality_first',
    'match_strategy': 'title_year',
    'multi_season_protect': 'compare',
    'tie_keep_local': False,
    'exempt_keywords': [],
    'special_action': 'compare',
}


def season(show, sn, eps, q):
    """一季：eps 为集号列表，q 为画质标签（如 '1080p'）；返回 [(folder, filename)]"""
    return [('%s (2020)/Season %02d' % (show, sn), '%s.S%02dE%02d.%s.strm' % (show, sn, e, q)) for e in eps]


def season_mixed(show, sn, eps_q):
    """一季里每集画质不同：eps_q = [(集号, 画质)]"""
    return [('%s (2020)/Season %02d' % (show, sn), '%s.S%02dE%02d.%s.strm' % (show, sn, e, q)) for e, q in eps_q]


R = lambda a, b: list(range(a, b + 1))  # noqa: E731

# 场景：name -> (策略覆盖, 本地文件列表, 分享文件列表)
SCENARIOS = {
    # ── 单季：完整性（残次品） ──
    'residual_local_gap': ({}, season('剧', 1, [1, 3], '1080p'), season('剧', 1, R(1, 3), '1080p')),
    'residual_share_head_missing': ({}, season('剧', 1, R(1, 3), '1080p'), season('剧', 1, [2, 3], '1080p')),
    'residual_both': ({}, season('剧', 1, [1, 3], '1080p'), season('剧', 1, [2, 3], '1080p')),
    # ── 单季：择优 ──
    'single_share_better': ({}, season('剧', 1, R(1, 4), '1080p'), season('剧', 1, R(1, 4), '2160p')),
    'single_local_better': ({}, season('剧', 1, R(1, 4), '2160p'), season('剧', 1, R(1, 4), '720p')),
    'single_share_more_eps': ({}, season('剧', 1, R(1, 2), '1080p'), season('剧', 1, R(1, 3), '2160p')),
    'single_share_fewer_eps': ({}, season('剧', 1, R(1, 3), '1080p'), season('剧', 1, R(1, 2), '2160p')),
    'single_ratio_below_threshold': (
        {}, season('剧', 1, R(1, 10), '1080p'),
        season_mixed('剧', 1, [(e, '2160p' if e <= 8 else '720p') for e in R(1, 10)])),
    'single_ratio_at_threshold': (
        {}, season('剧', 1, R(1, 10), '1080p'),
        season_mixed('剧', 1, [(e, '2160p' if e <= 9 else '720p') for e in R(1, 10)])),
    'single_tie_share_wins': ({}, season('剧', 1, R(1, 2), '1080p'), season('剧', 1, R(1, 2), '1080p')),
    'single_tie_keep_local': ({'tie_keep_local': True}, season('剧', 1, R(1, 2), '1080p'),
                              season('剧', 1, R(1, 2), '1080p')),
    'single_no_common_eps': ({}, season('剧', 1, [1, 2], '1080p'), season('剧', 2, [1, 2], '1080p')),
    # ── 多季保护 compare ──
    'multi_full_share_with_s00': (
        {}, season('剧', 1, R(1, 2), '1080p') + season('剧', 2, R(1, 2), '1080p') + season('剧', 0, [1], '720p'),
        season('剧', 1, R(1, 2), '2160p') + season('剧', 2, R(1, 2), '2160p') + season('剧', 0, [1], '2160p')),
    'multi_partial_share_with_s00': (
        {}, season('剧', 1, R(1, 2), '1080p') + season('剧', 2, R(1, 2), '1080p') + season('剧', 0, [1], '720p'),
        season('剧', 1, R(1, 2), '2160p') + season('剧', 0, [1], '2160p')),
    'multi_partial_share_reasons': (
        {}, season('剧', 1, R(1, 3), '2160p') + season('剧', 2, R(1, 2), '1080p'),
        season('剧', 1, R(1, 2), '720p') + season('剧', 2, [2], '720p')),
    'multi_share_protect_fewer': (
        {}, season('剧', 1, R(1, 3), '1080p'),
        season('剧', 1, R(1, 2), '2160p') + season('剧', 2, R(1, 2), '2160p')),
    'multi_share_protect_delete_local_with_s00': (
        {}, season('剧', 1, R(1, 2), '1080p') + season('剧', 0, [1], '1080p'),
        season('剧', 1, R(1, 2), '2160p') + season('剧', 2, R(1, 2), '2160p') + season('剧', 0, [1], '2160p')),
    'multi_share_protect_residual': (
        {}, season('剧', 1, [1, 3], '1080p'),
        season('剧', 1, R(1, 3), '2160p') + season('剧', 2, R(1, 2), '2160p')),
    'multi_partial_overlap': (
        {}, season('剧', 1, R(1, 2), '1080p') + season('剧', 2, [1, 3], '1080p'),
        season('剧', 2, R(1, 3), '2160p') + season('剧', 3, R(1, 2), '2160p')),
    'multi_full_not_all_pass_residual': (
        {}, season('剧', 1, R(1, 2), '1080p') + season('剧', 2, [1, 3], '1080p'),
        season('剧', 1, R(1, 2), '2160p') + season('剧', 2, R(1, 3), '2160p')),
    # ── 多季保护 off ──
    'protect_off_two_seasons': (
        {'multi_season_protect': 'off'},
        season('剧', 1, R(1, 2), '1080p') + season('剧', 2, R(1, 2), '2160p'),
        season('剧', 1, R(1, 2), '2160p') + season('剧', 2, R(1, 2), '1080p')),
    # ── 决策模型 ──
    'decision_keep_local': (
        {'decision': 'keep_local'}, season('剧', 1, R(1, 2), '720p') + season('剧', 2, R(1, 2), '720p'),
        season('剧', 1, R(1, 2), '2160p') + season('剧', 2, R(1, 2), '2160p') + season('剧', 3, [1], '2160p')),
    'decision_keep_share': (
        {'decision': 'keep_share'}, season('剧', 1, R(1, 2), '2160p') + season('剧', 2, R(1, 3), '2160p'),
        season('剧', 1, R(1, 2), '720p') + season('剧', 2, R(1, 2), '720p')),
    'decision_keep_share_covers': (
        {'decision': 'keep_share'}, season('剧', 1, R(1, 2), '2160p'), season('剧', 1, R(1, 3), '720p')),
    # ── 特别篇 S00 ──
    # S00 单独比画质只在「多季保护关闭」时发生；开启时 S00 算作一季参与多季保护（见 multi_* 场景）
    'special_compare_share_better': (
        {'multi_season_protect': 'off'}, season('剧', 1, [1], '1080p') + season('剧', 0, [1], '720p'),
        season('剧', 1, [1], '1080p') + season('剧', 0, [1], '2160p')),
    'special_compare_local_better': (
        {'multi_season_protect': 'off'}, season('剧', 1, [1], '1080p') + season('剧', 0, [1], '2160p'),
        season('剧', 1, [1], '1080p') + season('剧', 0, [1], '720p')),
    'special_delete': (
        {'special_action': 'delete'}, season('剧', 1, [1], '1080p') + season('剧', 0, [1], '720p'),
        season('剧', 1, [1], '1080p') + season('剧', 0, [1], '2160p')),
    'special_ignore': (
        {'special_action': 'ignore'}, season('剧', 1, [1], '1080p') + season('剧', 0, [1], '720p'),
        season('剧', 1, [1], '1080p') + season('剧', 0, [1], '2160p')),
    # ── 白名单 / 库内多版本（剧集） ──
    'whitelist_tv': (
        {'exempt_keywords': ['剧']}, season('剧', 1, R(1, 2), '1080p'), season('剧', 1, R(1, 2), '2160p')),
    'dup_versions_in_share_season': (
        {}, season('剧', 1, R(1, 2), '1080p'),
        season('剧', 1, R(1, 2), '2160p') + season('剧', 1, [1], '720p')),
}

# 这些 reason 必须被矩阵命中（守卫 golden 不会悄悄失效）
REQUIRED_REASONS = {
    'incomplete_delete_local', 'incomplete_delete_share', 'share_wins', 'local_wins',
    'multi_full_share', 'multi_protect_partial_share', 'multi_protect_partial_local',
    'decision_keep_local', 'decision_keep_share',
    'special_delete_local', 'special_delete_share', 'special_share_better', 'special_local_better',
    'whitelist',
}


def _rel(v):
    s = str(v)
    for root, tag in ((engine.L_ROOT, 'L'), (engine.S_ROOT, 'S'), (engine.CLOUD_L_ROOT, 'C')):
        s = s.replace(str(root), '<%s>' % tag)
    return s


def _jsonable(v):
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in sorted(v.items(), key=lambda kv: str(kv[0]))}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, set):
        return sorted((_jsonable(x) for x in v), key=lambda x: json.dumps(x, ensure_ascii=False))
    if isinstance(v, (int, float, bool)) or v is None:
        return v
    return _rel(v)


def _run(strategy, local, share):
    for root in (engine.L_ROOT, engine.S_ROOT, engine.CLOUD_L_ROOT):
        if root.exists():
            shutil.rmtree(root)
        root.mkdir(parents=True, exist_ok=True)
    for root, files in ((engine.L_ROOT, local), (engine.S_ROOT, share)):
        for folder, name in files:
            d = root / folder
            d.mkdir(parents=True, exist_ok=True)
            (d / name).write_text('http://127.0.0.1:12366/fake', encoding='utf-8')
    engine._invalidate_lib_cache()
    st = dict(BASE)
    st.update(strategy)
    engine._strategy = lambda: dict(st)
    acts = engine.build_plan()
    rows = [{'kind': a.kind, 'text': _rel(a.text), 'detail': _rel(a.detail),
             'files': sorted(_rel(f) for f in a.files), 'meta': _jsonable(a.meta)} for a in acts]
    rows.sort(key=lambda r: json.dumps(r, ensure_ascii=False, sort_keys=True))
    return rows


def _snapshot_all():
    return {name: _run(*spec) for name, spec in sorted(SCENARIOS.items())}


class TestGoldenMatrix:
    def test_matrix_matches_golden(self):
        got = _snapshot_all()
        if os.environ.get('UPDATE_GOLDEN') == '1' or not GOLDEN.exists():
            GOLDEN.parent.mkdir(parents=True, exist_ok=True)
            GOLDEN.write_text(json.dumps(got, ensure_ascii=False, indent=1, sort_keys=True) + '\n',
                              encoding='utf-8')
            return
        want = json.loads(GOLDEN.read_text(encoding='utf-8'))
        bad = [n for n in sorted(set(got) | set(want)) if got.get(n) != want.get(n)]
        assert not bad, ('治理行为与 golden 不一致的场景: %s\n有意修改请 UPDATE_GOLDEN=1 重新生成并单独 commit。\n%s'
                         % (bad, json.dumps({n: got.get(n) for n in bad[:2]}, ensure_ascii=False, indent=1)[:3000]))

    def test_every_scenario_produces_actions_or_is_a_known_noop(self):
        got = _snapshot_all()
        noop_ok = {'single_no_common_eps', 'special_ignore'}
        empty = [n for n, rows in got.items() if not rows and n not in noop_ok]
        assert not empty, '这些场景没有产生任何动作（fixture 可能失效）: %s' % empty

    def test_branch_coverage_guard(self):
        got = _snapshot_all()
        hit = {r['meta'].get('reason') for rows in got.values() for r in rows}
        missing = REQUIRED_REASONS - hit
        assert not missing, '场景矩阵未命中关键分支: %s（命中: %s）' % (sorted(missing), sorted(x for x in hit if x))
