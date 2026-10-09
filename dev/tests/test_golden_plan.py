# -*- coding: utf-8 -*-
"""
Golden 特征测试：固化 build_plan() 当前的实际输出，作为架构重构的安全网。

用法：
  - 正常运行：对比 dev/tests/golden/build_plan.json，有任何差异即失败（说明治理行为变了）。
  - 有意改变行为后重新生成：UPDATE_GOLDEN=1 python -m pytest dev/tests
    （重构 PR 里不应该出现 golden 文件变化；变化必须单独成 commit 并说明原因）

fixture 覆盖：电影画质对比 / 平局 / 白名单 / 库内多版本；剧集分享更优 / 本地更优；
分享独有季保护；本地独有；S00 特别篇。
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix='strimkeep_golden_'))
os.environ['L_ROOT'] = str(_TMP / 'local')
os.environ['S_ROOT'] = str(_TMP / 'share')
os.environ['CLOUD_L_ROOT'] = str(_TMP / 'cloud')
os.environ['AGENT_DATA'] = str(_TMP / 'data')
os.environ['TMDB_KEY'] = ''
os.environ['TG_BOT_TOKEN'] = ''
os.environ['INGEST_QUIET_MINUTES'] = '0'

sys.path.insert(0, str(Path(__file__).parents[2] / 'app'))
import engine  # noqa: E402

GOLDEN = Path(__file__).parent / 'golden' / 'build_plan.json'

STRATEGY = {
    'decision': 'quality_first',
    'match_strategy': 'title_year',
    'multi_season_protect': 'compare',
    'tie_keep_local': False,
    'exempt_keywords': ['白名单剧'],
    'special_action': 'compare',
}

# (库, 目录, 文件名)
FIXTURE = [
    # 电影：分享更优 → 删本地
    ('L', '电影甲 (2020)', '电影甲.1080p.strm'),
    ('S', '电影甲 (2020)', '电影甲.2160p.strm'),
    # 电影：本地更优 → 删分享
    ('L', '电影乙 (2021)', '电影乙.2160p.strm'),
    ('S', '电影乙 (2021)', '电影乙.720p.strm'),
    # 电影：平局
    ('L', '电影丙 (2022)', '电影丙.1080p.strm'),
    ('S', '电影丙 (2022)', '电影丙.1080p.strm'),
    # 电影：白名单
    ('L', '白名单剧 (2019)', '白名单剧.2160p.strm'),
    ('S', '白名单剧 (2019)', '白名单剧.720p.strm'),
    # 电影：库内多版本
    ('L', '电影丁 (2018)', '电影丁.720p.strm'),
    ('L', '电影丁 (2018)', '电影丁.2160p.strm'),
    # 电影：本地独有
    ('L', '电影戊 (2017)', '电影戊.1080p.strm'),
    # 剧集：分享整季更优
    ('L', '剧集甲 (2020)/Season 01', '剧集甲.S01E01.1080p.strm'),
    ('L', '剧集甲 (2020)/Season 01', '剧集甲.S01E02.1080p.strm'),
    ('S', '剧集甲 (2020)/Season 01', '剧集甲.S01E01.2160p.strm'),
    ('S', '剧集甲 (2020)/Season 01', '剧集甲.S01E02.2160p.strm'),
    # 剧集：本地更优
    ('L', '剧集乙 (2021)/Season 01', '剧集乙.S01E01.2160p.strm'),
    ('L', '剧集乙 (2021)/Season 01', '剧集乙.S01E02.2160p.strm'),
    ('S', '剧集乙 (2021)/Season 01', '剧集乙.S01E01.720p.strm'),
    ('S', '剧集乙 (2021)/Season 01', '剧集乙.S01E02.720p.strm'),
    # 剧集：分享独有第二季（应受保护）+ 缺集
    ('L', '剧集丙 (2019)/Season 01', '剧集丙.S01E01.1080p.strm'),
    ('S', '剧集丙 (2019)/Season 01', '剧集丙.S01E01.1080p.strm'),
    ('S', '剧集丙 (2019)/Season 02', '剧集丙.S02E01.1080p.strm'),
    ('S', '剧集丙 (2019)/Season 02', '剧集丙.S02E03.1080p.strm'),
    # S00 特别篇
    ('L', '剧集丁 (2020)/Season 01', '剧集丁.S01E01.1080p.strm'),
    ('S', '剧集丁 (2020)/Season 01', '剧集丁.S01E01.1080p.strm'),
    ('L', '剧集丁 (2020)/Season 00', '剧集丁.S00E01.720p.strm'),
    ('S', '剧集丁 (2020)/Season 00', '剧集丁.S00E01.2160p.strm'),
]


def _build_fixture():
    for root in (engine.L_ROOT, engine.S_ROOT, engine.CLOUD_L_ROOT):
        if root.exists():
            shutil.rmtree(root)
        root.mkdir(parents=True, exist_ok=True)
    for lib, folder, name in FIXTURE:
        d = (engine.L_ROOT if lib == 'L' else engine.S_ROOT) / folder
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text('http://127.0.0.1:12366/fake', encoding='utf-8')
    engine._invalidate_lib_cache()


def _rel(p):
    s = str(p)
    for root in (engine.L_ROOT, engine.S_ROOT, engine.CLOUD_L_ROOT):
        s = s.replace(str(root), '<%s>' % ('L' if root == engine.L_ROOT
                                           else 'S' if root == engine.S_ROOT else 'C'))
    return s


def _jsonable(v):
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in sorted(v.items(), key=lambda kv: str(kv[0]))}
    if isinstance(v, (list, tuple, set)):
        items = [_jsonable(x) for x in v]
        return sorted(items, key=lambda x: json.dumps(x, ensure_ascii=False)) if isinstance(v, set) else items
    if isinstance(v, (str, int, float, bool)) or v is None:
        return _rel(v) if isinstance(v, str) else v
    return _rel(v)


def _snapshot():
    acts = engine.build_plan()
    rows = [{
        'kind': a.kind,
        'text': _rel(a.text),
        'detail': _rel(a.detail),
        'files': sorted(_rel(f) for f in a.files),
        'meta': _jsonable(a.meta),
    } for a in acts]
    rows.sort(key=lambda r: json.dumps(r, ensure_ascii=False, sort_keys=True))
    return rows


class TestGoldenPlan:
    def test_build_plan_matches_golden(self):
        _build_fixture()
        engine._strategy = lambda: dict(STRATEGY)
        got = _snapshot()
        assert got, 'fixture 没有产生任何 Act，golden 测试失效'

        if os.environ.get('UPDATE_GOLDEN') == '1' or not GOLDEN.exists():
            GOLDEN.parent.mkdir(parents=True, exist_ok=True)
            GOLDEN.write_text(json.dumps(got, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
                              encoding='utf-8')
            return

        want = json.loads(GOLDEN.read_text(encoding='utf-8'))
        assert got == want, (
            'build_plan 输出与 golden 不一致：治理行为发生了变化。\n'
            '若是有意修改，请 UPDATE_GOLDEN=1 重新生成并单独 commit。\n'
            'got=%s' % json.dumps(got, ensure_ascii=False, indent=1)[:3000])

    def test_build_plan_is_deterministic(self):
        _build_fixture()
        engine._strategy = lambda: dict(STRATEGY)
        assert _snapshot() == _snapshot()
