# -*- coding: utf-8 -*-
"""
engine.py 治理逻辑测试（build_plan / action_inter_clean）。

覆盖白皮书第22节里点名、但当前完全没有测试保护的场景，
本轮先补：
  - 电影白名单豁免（此前是 bug：白名单只在电视剧分支生效，电影完全不检查）
  - 电影画质对比基线（确认修复白名单没有连带破坏正常删除逻辑）
  - 清理任务的跨入口互斥锁（action_inter_clean 内部的 flock）
  - 分享独有 Season 默认保护（此前是 bug：本地没有的季，只要分享库这边缺集/
    错集就会被判定为 gap 直接删掉分享 STRM——两边都没有的资源就再也找不回来了）

运行方式：在项目根目录执行 `pytest dev/tests/ -v`。
"""
from app import state_store as _state
import fcntl
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

# 在 import engine 之前把三个媒体根目录和数据目录都指到临时目录，
# 避免测试误碰到真实 NAS 上的 /media /data。
_TMP = Path(tempfile.mkdtemp(prefix='strimkeep_test_'))
os.environ['L_ROOT'] = str(_TMP / 'local')
os.environ['S_ROOT'] = str(_TMP / 'share')
os.environ['CLOUD_L_ROOT'] = str(_TMP / 'cloud')
os.environ['AGENT_DATA'] = str(_TMP / 'data')
os.environ['TMDB_KEY'] = ''
os.environ['TG_BOT_TOKEN'] = ''
# 测试里的 STRM 都是刚创建的，先关掉「入库静默期」，静默期本身在 TestIngestQuietPeriod 里单独测
os.environ['INGEST_QUIET_MINUTES'] = '0'

sys.path.insert(0, str(Path(__file__).parents[2] / 'app'))
import engine  # noqa: E402


def _strm(root: Path, folder: str, filename: str):
    """在 root/folder 下建一个空 .strm 文件（内容对治理逻辑无所谓，只看路径和文件名）"""
    d = root / folder
    d.mkdir(parents=True, exist_ok=True)
    (d / filename).write_text('http://127.0.0.1:12366/fake', encoding='utf-8')


def _reset_libs():
    """清掉两库目录内容 + 失效 Lib 缓存，保证每个测试互不干扰"""
    for root in (engine.L_ROOT, engine.S_ROOT, engine.CLOUD_L_ROOT):
        if root.exists():
            shutil.rmtree(root)
        root.mkdir(parents=True, exist_ok=True)
    engine._invalidate_lib_cache()


def _patch_strategy(monkeypatch_dict):
    """覆盖 engine._strategy()，避免测试依赖 /data/config.json 的真实内容"""
    base = {
        'decision': 'quality_first',
        'match_strategy': 'title_year',
        'multi_season_protect': 'compare',
        'tie_keep_local': False,
        'exempt_keywords': [],
        'special_action': 'compare',
    }
    base.update(monkeypatch_dict)
    engine._strategy = lambda: base


def _acts_for_title(acts, title_substr):
    return [a for a in acts if title_substr in a.meta.get('title', '')]


# ═══════════════════ 电影白名单 ═══════════════════
class TestMovieWhitelist:
    def test_whitelisted_movie_is_not_touched(self):
        """
        回归测试：修复前 build_plan() 的电影分支完全不检查白名单，
        命中白名单关键词的电影仍然会被正常按画质比较、生成删除 Action。
        本测试锁定：白名单命中后必须只产生一个 kind='exempt' 的 Act，
        不能出现 kind in ('loc', 'shr')。
        """
        _reset_libs()
        _patch_strategy({'exempt_keywords': ['白名单测试电影']})

        _strm(engine.L_ROOT, '白名单测试电影 (2020)', '白名单测试电影.2160p.strm')
        _strm(engine.S_ROOT, '白名单测试电影 (2020)', '白名单测试电影.720p.strm')

        acts = engine.build_plan()
        hits = _acts_for_title(acts, '白名单测试电影')

        assert len(hits) == 1
        assert hits[0].kind == 'exempt'
        assert not any(a.kind in ('loc', 'shr') for a in hits)

    def test_non_whitelisted_movie_still_compares_normally(self):
        """确认白名单修复没有误伤：没命中关键词的电影还是按画质正常比较"""
        _reset_libs()
        _patch_strategy({'exempt_keywords': ['这个关键词不会命中任何东西']})

        _strm(engine.L_ROOT, '画质对比电影 (2021)', '画质对比电影.1080p.strm')
        _strm(engine.S_ROOT, '画质对比电影 (2021)', '画质对比电影.2160p.strm')

        acts = engine.build_plan()
        hits = _acts_for_title(acts, '画质对比电影')

        assert len(hits) == 1
        # 分享 2160p > 本地 1080p，按默认策略应删本地、保留分享
        assert hits[0].kind == 'loc'
        assert hits[0].meta['reason'] == 'share_better'

    def test_tie_break_defaults_to_deleting_local(self):
        """画质相同时，默认 tie_keep_local=False，平局删本地保留分享"""
        _reset_libs()
        _patch_strategy({'exempt_keywords': []})

        _strm(engine.L_ROOT, '平局电影 (2022)', '平局电影.1080p.strm')
        _strm(engine.S_ROOT, '平局电影 (2022)', '平局电影.1080p.strm')

        acts = engine.build_plan()
        hits = _acts_for_title(acts, '平局电影')

        assert len(hits) == 1
        assert hits[0].kind == 'loc'


# ═══════════════════ 跨入口并发锁 ═══════════════════
class TestCleanupLock:
    def test_dry_run_does_not_take_the_lock(self):
        """dry-run 只读不写，不应该受锁影响，也不应该去抢锁"""
        _reset_libs()
        _patch_strategy({'exempt_keywords': []})

        class Args:
            plan = ''
            dry_run = True

        # 手动占住锁，确认 dry-run 完全不受影响
        engine.DATA_DIR.mkdir(parents=True, exist_ok=True)
        holder = open(engine.LOCK_FILE, 'w')
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            res = engine.action_inter_clean(Args())
            assert res['status'] == 'success'
        finally:
            fcntl.flock(holder, fcntl.LOCK_UN)
            holder.close()

    def test_second_real_cleanup_gets_busy_while_first_holds_lock(self):
        """
        模拟三个入口（CLI/Web/Bot）里任意两个同时发起真正的清理：
        第二个必须拿不到锁，返回 status=busy，而不是也去跑一遍 safe_delete_files。
        """
        _reset_libs()
        _patch_strategy({'exempt_keywords': []})

        class Args:
            plan = ''
            dry_run = False

        engine.DATA_DIR.mkdir(parents=True, exist_ok=True)
        holder = open(engine.LOCK_FILE, 'w')
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            res = engine.action_inter_clean(Args())
            assert res['status'] == 'busy'
        finally:
            fcntl.flock(holder, fcntl.LOCK_UN)
            holder.close()

    def test_lock_is_released_after_cleanup_so_next_call_succeeds(self):
        """锁必须在清理结束后释放，不能一直占着导致后续任务永远 busy"""
        _reset_libs()
        _patch_strategy({'exempt_keywords': []})

        class Args:
            plan = ''
            dry_run = False

        res1 = engine.action_inter_clean(Args())
        assert res1['status'] == 'success'
        res2 = engine.action_inter_clean(Args())
        assert res2['status'] == 'success'


# ═══════════════════ 分享独有 Season 静默 ═══════════════════
class TestShareOnlySeasonProtection:
    def test_complete_share_only_season_is_silent(self):
        """本地完全没有这部剧，分享库这季集数完整——静默，不生成任何 action"""
        _reset_libs()
        _patch_strategy({'exempt_keywords': []})

        for ep in (1, 2, 3):
            _strm(engine.S_ROOT, '分享独有剧集A (2020)/Season 01', f'E{ep:02d}.strm')

        acts = engine.build_plan()
        hits = _acts_for_title(acts, '分享独有剧集A')

        assert len(hits) == 0

    def test_incomplete_share_only_season_is_silent(self):
        """分享独有但缺集（断层）——同样静默，不生成任何 action（不再显示受保护）"""
        _reset_libs()
        _patch_strategy({'exempt_keywords': []})

        for ep in (1, 3):  # 缺 E02，中间断层
            _strm(engine.S_ROOT, '分享独有剧集B (2021)/Season 01', f'E{ep:02d}.strm')

        acts = engine.build_plan()
        hits = _acts_for_title(acts, '分享独有剧集B')

        assert len(hits) == 0

    def test_share_only_season_never_shows_up_as_deletable_in_check(self):
        """action_inter_check() 返回的字段里，分享独有的季不应出现在任何删除/保护列表里"""
        _reset_libs()
        _patch_strategy({'exempt_keywords': []})

        for ep in (1, 3):
            _strm(engine.S_ROOT, '分享独有剧集C (2022)/Season 01', f'E{ep:02d}.strm')

        class Args:
            kw = ''

        res = engine.action_inter_check(Args())
        share_titles = [i['title'] for i in res['del_share_items']]
        protected_titles = [i['title'] for i in res['protected_items']]

        assert not any('分享独有剧集C' in t for t in share_titles)
        assert not any('分享独有剧集C' in t for t in protected_titles)


# ═══════════════════ 电视剧逐集择优（新逻辑）═══════════════════
class TestSeasonCompare:
    """逐集对齐 + 达标率择优，取消双向保留"""

    def test_positive_share_wins(self):
        """分享逐集达标率 >= 阈值且集数不落后 → 删本地"""
        _reset_libs()
        _patch_strategy({'multi_season_protect': 'off', 'exempt_keywords': []})
        _strm(engine.L_ROOT, '逐集正例 (2020)/Season 01', 'E01.1080p.strm')
        _strm(engine.L_ROOT, '逐集正例 (2020)/Season 01', 'E02.1080p.strm')
        _strm(engine.L_ROOT, '逐集正例 (2020)/Season 01', 'E03.1080p.strm')
        _strm(engine.S_ROOT, '逐集正例 (2020)/Season 01', 'E01.2160p.strm')
        _strm(engine.S_ROOT, '逐集正例 (2020)/Season 01', 'E02.2160p.strm')
        _strm(engine.S_ROOT, '逐集正例 (2020)/Season 01', 'E03.2160p.strm')

        acts = engine.build_plan()
        hits = _acts_for_title(acts, '逐集正例')
        assert len(hits) == 1
        assert hits[0].kind == 'loc'
        assert hits[0].meta['reason'] == 'share_wins'

    def test_negative_local_wins(self):
        """本地画质整体更优 → 删分享"""
        _reset_libs()
        _patch_strategy({'multi_season_protect': 'off', 'exempt_keywords': []})
        _strm(engine.L_ROOT, '逐集反例 (2020)/Season 01', 'E01.2160p.strm')
        _strm(engine.L_ROOT, '逐集反例 (2020)/Season 01', 'E02.2160p.strm')
        _strm(engine.L_ROOT, '逐集反例 (2020)/Season 01', 'E03.2160p.strm')
        _strm(engine.S_ROOT, '逐集反例 (2020)/Season 01', 'E01.1080p.strm')
        _strm(engine.S_ROOT, '逐集反例 (2020)/Season 01', 'E02.1080p.strm')
        _strm(engine.S_ROOT, '逐集反例 (2020)/Season 01', 'E03.1080p.strm')

        acts = engine.build_plan()
        hits = _acts_for_title(acts, '逐集反例')
        assert len(hits) == 1
        assert hits[0].kind == 'shr'
        assert hits[0].meta['reason'] == 'local_wins'

    def test_share_more_episodes_wins(self):
        """分享集数更多且达标 → 删本地"""
        _reset_libs()
        _patch_strategy({'multi_season_protect': 'off', 'exempt_keywords': []})
        _strm(engine.L_ROOT, '分享更多 (2020)/Season 01', 'E01.2160p.strm')
        _strm(engine.L_ROOT, '分享更多 (2020)/Season 01', 'E02.2160p.strm')
        _strm(engine.S_ROOT, '分享更多 (2020)/Season 01', 'E01.2160p.strm')
        _strm(engine.S_ROOT, '分享更多 (2020)/Season 01', 'E02.2160p.strm')
        _strm(engine.S_ROOT, '分享更多 (2020)/Season 01', 'E03.2160p.strm')

        acts = engine.build_plan()
        hits = _acts_for_title(acts, '分享更多')
        assert len(hits) == 1
        assert hits[0].kind == 'loc'
        assert hits[0].meta['reason'] == 'share_wins'

    def test_all_tie_2160_share_wins(self):
        """画质全平局时默认平局保留分享 → 删本地"""
        _reset_libs()
        _patch_strategy({'multi_season_protect': 'off', 'exempt_keywords': []})
        _strm(engine.L_ROOT, '全平局 (2020)/Season 01', 'E01.2160p.strm')
        _strm(engine.L_ROOT, '全平局 (2020)/Season 01', 'E02.2160p.strm')
        _strm(engine.S_ROOT, '全平局 (2020)/Season 01', 'E01.2160p.strm')
        _strm(engine.S_ROOT, '全平局 (2020)/Season 01', 'E02.2160p.strm')

        acts = engine.build_plan()
        hits = _acts_for_title(acts, '全平局')
        assert len(hits) == 1
        assert hits[0].kind == 'loc'
        assert hits[0].meta['reason'] == 'share_wins'

    def test_share_only_season_silent(self):
        """分享独有 Season → 静默，不生成任何 action"""
        _reset_libs()
        _patch_strategy({'multi_season_protect': 'off', 'exempt_keywords': []})
        _strm(engine.S_ROOT, '分享独有回归 (2020)/Season 01', 'E01.2160p.strm')
        _strm(engine.S_ROOT, '分享独有回归 (2020)/Season 01', 'E03.2160p.strm')

        acts = engine.build_plan()
        hits = _acts_for_title(acts, '分享独有回归')
        assert len(hits) == 0

    def test_whitelist_still_wins(self):
        _reset_libs()
        _patch_strategy({'multi_season_protect': 'off', 'exempt_keywords': ['逐集白名单']})
        _strm(engine.L_ROOT, '逐集白名单 (2020)/Season 01', 'E01.2160p.strm')
        _strm(engine.S_ROOT, '逐集白名单 (2020)/Season 01', 'E01.1080p.strm')

        acts = engine.build_plan()
        hits = _acts_for_title(acts, '逐集白名单')
        assert len(hits) == 1
        assert hits[0].kind == 'exempt'

    def test_incomplete_season_deletes_both(self):
        """残次品：前序缺失（缺 E01）→ 本地+分享都删"""
        _reset_libs()
        _patch_strategy({'multi_season_protect': 'off', 'exempt_keywords': []})
        for ep in (3, 4, 5):
            _strm(engine.L_ROOT, '残次品测试 (2020)/Season 01', f'E{ep:02d}.1080p.strm')
            _strm(engine.S_ROOT, '残次品测试 (2020)/Season 01', f'E{ep:02d}.2160p.strm')

        acts = engine.build_plan()
        hits = _acts_for_title(acts, '残次品测试')
        kinds = {a.kind for a in hits}
        # 应产生删本地 + 删分享两个动作
        assert 'loc' in kinds and 'shr' in kinds
        assert all(a.meta['reason'] in ('incomplete_delete_local', 'incomplete_delete_share') for a in hits)

    def test_mid_gap_season_deletes_both(self):
        """残次品：中间断层（缺 E03）→ 本地+分享都删"""
        _reset_libs()
        _patch_strategy({'multi_season_protect': 'off', 'exempt_keywords': []})
        for ep in (1, 2, 4):
            _strm(engine.L_ROOT, '断层测试 (2020)/Season 01', f'E{ep:02d}.2160p.strm')
            _strm(engine.S_ROOT, '断层测试 (2020)/Season 01', f'E{ep:02d}.2160p.strm')

        acts = engine.build_plan()
        hits = _acts_for_title(acts, '断层测试')
        kinds = {a.kind for a in hits}
        assert 'loc' in kinds and 'shr' in kinds

    def test_multi_protect_partial_coverage_no_local_delete(self):
        """多季保护开启：分享只覆盖部分季（缺 S03）时——
        本地一季都不删（合集不被掏空）；分享来的这些季全部淘汰（不比画质）。"""
        _reset_libs()
        _patch_strategy({'multi_season_protect': 'compare', 'exempt_keywords': []})
        # 本地 S01-S03（1080p），分享只有 S01（2160p 达标）和 S02（720p 劣质）
        for sn in ('01', '02', '03'):
            for ep in (1, 2):
                _strm(engine.L_ROOT, f'多季保护剧 (2020)/Season {sn}', f'E{ep:02d}.1080p.strm')
        _strm(engine.S_ROOT, '多季保护剧 (2020)/Season 01', 'E01.2160p.strm')
        _strm(engine.S_ROOT, '多季保护剧 (2020)/Season 01', 'E02.2160p.strm')
        _strm(engine.S_ROOT, '多季保护剧 (2020)/Season 02', 'E01.720p.strm')
        _strm(engine.S_ROOT, '多季保护剧 (2020)/Season 02', 'E02.720p.strm')

        acts = engine.build_plan()
        hits = _acts_for_title(acts, '多季保护剧')
        # 本地不许出现任何删除动作（合集不被掏空）
        assert not any(a.kind == 'loc' for a in hits), [a.text for a in hits]
        # 分享未全覆盖 → 分享 S01、S02 都淘汰（不比画质，哪怕 S01 达标）
        shr_seasons = sorted(a.meta.get('season') for a in hits if a.kind == 'shr')
        assert shr_seasons == [1, 2], [a.text for a in hits]


# ═══════════════════ 云端源置信度（白皮书 §13）═══════════════════
class TestCloudVideoConfidence:
    """只有 exact / normalized 允许自动删；其他一律 STRM 保留"""

    def _setup(self, conf, vids=None):
        _reset_libs()
        _patch_strategy({'exempt_keywords': []})
        _strm(engine.L_ROOT, '云源测试 (2020)/Season 01', 'E01.1080p.strm')
        f = next(engine.L_ROOT.rglob('*.strm'))
        orig = engine.cloud_videos
        engine.cloud_videos = lambda *a, **k: (vids or [], conf)
        return f, orig

    def _teardown(self, orig):
        engine.cloud_videos = orig

    def test_exact_allows_deletion(self):
        f, orig = self._setup('exact', [])
        try:
            r = engine.safe_delete_files([f], engine.L_ROOT, engine.CLOUD_L_ROOT, dry_run=False)
            assert r['strm_removed'] == 1
            assert not f.exists()
        finally:
            self._teardown(orig)

    def test_normalized_allows_deletion(self):
        f, orig = self._setup('normalized', [])
        try:
            r = engine.safe_delete_files([f], engine.L_ROOT, engine.CLOUD_L_ROOT, dry_run=False)
            assert r['strm_removed'] == 1
            assert not f.exists()
        finally:
            self._teardown(orig)

    def test_missing_blocks_strm(self):
        f, orig = self._setup('none')
        try:
            r = engine.safe_delete_files([f], engine.L_ROOT, engine.CLOUD_L_ROOT, dry_run=False)
            assert r['strm_removed'] == 0
            assert r['cloud_missing'] == 1
            assert f.exists()
        finally:
            self._teardown(orig)

    def test_fallback_blocks_strm(self):
        f, orig = self._setup('unique_fallback')
        try:
            r = engine.safe_delete_files([f], engine.L_ROOT, engine.CLOUD_L_ROOT, dry_run=False)
            assert r['strm_removed'] == 0
            assert r['cloud_fallback'] == 1
            assert f.exists()
        finally:
            self._teardown(orig)

    def test_ambiguous_blocks_strm(self):
        f, orig = self._setup('ambiguous')
        try:
            r = engine.safe_delete_files([f], engine.L_ROOT, engine.CLOUD_L_ROOT, dry_run=False)
            assert r['strm_removed'] == 0
            assert r['cloud_ambiguous'] == 1
            assert f.exists()
        finally:
            self._teardown(orig)


import json as _json
import time as _time


class TestPlanLifecycle:

    def test_action_id_uses_stable_media_key(self):
        act = engine.Act('loc', '📺 《闪电侠 (2014)》S01 (整剧零和：分享全面达标 → 删本地)',
                         'detail', [], {'title': '闪电侠 (2014)', 'season': 1})
        assert act.action_id == 'loc:tv:闪电侠 (2014):S01'
        assert '整剧零和' not in act.action_id

    def test_movie_action_id_has_no_season(self):
        act = engine.Act('shr', '🎬 《测试电影》 (本地更优)',
                         'detail', [], {'title': '测试电影'})
        assert act.action_id == 'shr:movie:测试电影'

    def test_save_plan_writes_schema_v2(self):
        _reset_libs()
        _patch_strategy({'exempt_keywords': []})
        _strm(engine.L_ROOT, '剧A (2020)/Season 01', 'E01.1080p.strm')
        _strm(engine.S_ROOT, '剧A (2020)/Season 01', 'E01.1080p.strm')
        acts = engine.build_plan()
        pid = engine.save_plan(acts)
        assert pid is not None
        pf = engine.STATE_DIR / f'plan_{pid}.json'
        assert not pf.exists() and engine.db_load_plan(pid)
        data = _state.read(pf)
        assert data.get('schema_version') == 2
        assert data.get('state') == 'pending'
        assert isinstance(data.get('actions'), list)
        assert len(data['actions']) >= 1
        first = data['actions'][0]
        assert 'action_id' in first
        assert 'media_key' in first

    def test_save_plan_records_rule_signature(self, monkeypatch):
        _reset_libs()
        _patch_strategy({'exempt_keywords': []})
        _strm(engine.L_ROOT, '规则指纹剧 (2020)/Season 01', 'E01.1080p.strm')
        _strm(engine.S_ROOT, '规则指纹剧 (2020)/Season 01', 'E01.1080p.strm')
        monkeypatch.setattr(engine._cfg, 'load_config', lambda: {'strategy_exempt_keywords': ''})
        monkeypatch.setattr(engine._cfg, 'get_strategy', lambda: {'decision': 'quality_first'})
        acts = engine.build_plan()
        pid = engine.save_plan(acts)
        data = engine.load_plan(pid)
        assert len(data.get('rule_sig', '')) == 16

    def test_plan_rejected_when_rules_changed(self, monkeypatch):
        _reset_libs()
        _patch_strategy({'exempt_keywords': []})
        _strm(engine.L_ROOT, '规则变化剧 (2020)/Season 01', 'E01.1080p.strm')
        _strm(engine.S_ROOT, '规则变化剧 (2020)/Season 01', 'E01.1080p.strm')
        state = {'decision': 'quality_first'}
        monkeypatch.setattr(engine._cfg, 'load_config', lambda: {'strategy_exempt_keywords': state['decision']})
        monkeypatch.setattr(engine._cfg, 'get_strategy', lambda: {'decision': state['decision']})
        pid = engine.save_plan(engine.build_plan())
        state['decision'] = 'keep_local'

        class A: pass
        a = A(); a.plan = pid; a.dry_run = False
        res = engine.action_inter_clean(a)
        assert res.get('status') == 'error'
        assert res.get('code') == 'plan_rule_changed'
        assert engine.load_plan(pid).get('state') == 'stale'

    def test_load_plan_rejects_old_schema(self):
        engine.STATE_DIR.mkdir(parents=True, exist_ok=True)
        old_pf = engine.STATE_DIR / 'plan_deadbeef.json'
        old_pf.write_text(_json.dumps({'id': 'deadbeef', 'ts': _time.time(), 'keys': ['loc|x']}))
        try:
            assert engine.load_plan('deadbeef') is None
        finally:
            old_pf.unlink(missing_ok=True)

    def test_expired_plan_rejected(self):
        _reset_libs()
        _patch_strategy({'exempt_keywords': []})
        _strm(engine.L_ROOT, '剧B (2020)/Season 01', 'E01.1080p.strm')
        _strm(engine.S_ROOT, '剧B (2020)/Season 01', 'E01.1080p.strm')
        acts = engine.build_plan()
        pid = engine.save_plan(acts)
        assert pid is not None
        pf = engine.STATE_DIR / f'plan_{pid}.json'
        data = _state.read(pf)
        data['ts'] = _time.time() - (engine.PLAN_TTL + 3600)
        _state.save(pf, data)

        class A: pass
        a = A(); a.plan = pid; a.dry_run = False
        res = engine.action_inter_clean(a)
        assert res.get('status') == 'error'
        assert res.get('code') == 'plan_expired'

    def test_done_plan_cannot_be_reexecuted(self):
        _reset_libs()
        _patch_strategy({'exempt_keywords': []})
        _strm(engine.L_ROOT, '剧C (2020)/Season 01', 'E01.1080p.strm')
        _strm(engine.S_ROOT, '剧C (2020)/Season 01', 'E01.1080p.strm')
        acts = engine.build_plan()
        pid = engine.save_plan(acts)
        assert pid is not None
        engine.save_plan_state(pid, 'done')

        class A: pass
        a = A(); a.plan = pid; a.dry_run = False
        res = engine.action_inter_clean(a)
        assert res.get('status') == 'error'
        assert res.get('code') == 'plan_used'



def _aux_file(root, folder, filename, content='x'):
    d = root / folder
    d.mkdir(parents=True, exist_ok=True)
    (d / filename).write_text(content, encoding='utf-8')


class TestDirLifecycle:

    def test_movie_dir_cleaned_after_strm_delete(self):
        _reset_libs()
        _patch_strategy({'exempt_keywords': []})
        _strm(engine.L_ROOT, '电影/华语电影/测试电影 (2020)', '测试电影.1080p.strm')
        f = next(engine.L_ROOT.rglob('*.strm'))
        r = engine.safe_delete_files([f], engine.L_ROOT, None, dry_run=False)
        assert r['strm_removed'] == 1
        assert not (engine.L_ROOT / '电影/华语电影/测试电影 (2020)').exists()
        assert (engine.L_ROOT / '电影/华语电影').is_dir()
        assert (engine.L_ROOT / '电影').is_dir()

    def test_category_dir_never_deleted(self):
        _reset_libs()
        _patch_strategy({'exempt_keywords': []})
        _strm(engine.L_ROOT, '电影', 'x.strm')
        f = next(engine.L_ROOT.rglob('*.strm'))
        engine.safe_delete_files([f], engine.L_ROOT, None, dry_run=False)
        assert (engine.L_ROOT / '电影').is_dir()

    def test_unknown_file_blocks_dir_delete(self):
        _reset_libs()
        _patch_strategy({'exempt_keywords': []})
        _strm(engine.L_ROOT, '电影/华语电影/测试电影 (2020)', '测试电影.1080p.strm')
        _aux_file(engine.L_ROOT, '电影/华语电影/测试电影 (2020)', 'readme.log', 'x')
        f = next(engine.L_ROOT.rglob('*.strm'))
        engine.safe_delete_files([f], engine.L_ROOT, None, dry_run=False)
        assert (engine.L_ROOT / '电影/华语电影/测试电影 (2020)').is_dir()
        assert (engine.L_ROOT / '电影/华语电影/测试电影 (2020)/readme.log').exists()

    def test_metadata_deleted_with_media(self):
        _reset_libs()
        _patch_strategy({'exempt_keywords': []})
        _strm(engine.L_ROOT, '电影/华语电影/测试电影 (2020)', '测试电影.1080p.strm')
        _aux_file(engine.L_ROOT, '电影/华语电影/测试电影 (2020)', 'mediainfo.json', '{}')
        _aux_file(engine.L_ROOT, '电影/华语电影/测试电影 (2020)', 'poster.jpg', 'img')
        f = next(engine.L_ROOT.rglob('*.strm'))
        engine.safe_delete_files([f], engine.L_ROOT, None, dry_run=False)
        assert not (engine.L_ROOT / '电影/华语电影/测试电影 (2020)').exists()

    def test_series_root_cleaned_after_all_seasons(self):
        _reset_libs()
        _patch_strategy({'exempt_keywords': []})
        for sn in ('Season 01', 'Season 02'):
            _strm(engine.L_ROOT, '剧集/欧美剧集/测试剧 (2020)/' + sn, 'E01.1080p.strm')
        files = list(engine.L_ROOT.rglob('*.strm'))
        engine.safe_delete_files(files, engine.L_ROOT, None, dry_run=False)
        assert not (engine.L_ROOT / '剧集/欧美剧集/测试剧 (2020)').exists()
        assert (engine.L_ROOT / '剧集/欧美剧集').is_dir()

    def test_scan_orphans_finds_unknown_files(self):
        _reset_libs()
        _patch_strategy({'exempt_keywords': []})
        _aux_file(engine.L_ROOT, '电影/华语电影', 'weird.xyz', 'x')
        _aux_file(engine.L_ROOT, '电影/华语电影/电影A (2020)', 'mediainfo.json', '{}')
        _strm(engine.L_ROOT, '电影/华语电影/电影B (2020)', 'x.strm')
        items = engine.scan_orphans(max_depth=3)
        paths = [it['path'] for it in items]
        assert any('weird.xyz' in p for p in paths)
        assert not any('mediainfo.json' in p for p in paths)
        assert not any(p.endswith('.strm') for p in paths)



class TestOrphanPerf:

    def test_scan_orphans_respects_max_depth(self):
        _reset_libs()
        _patch_strategy({'exempt_keywords': []})
        # 3 层结构
        _aux_file(engine.L_ROOT, '电影/华语电影/剧A (2020)', 'x.xyz', 'x')
        # 5 层结构
        _aux_file(engine.L_ROOT, '电影/华语电影/剧B (2020)/Season 01/extra', 'y.xyz', 'y')
        # max_depth=3 应该只找到 3 层里的
        items = engine.scan_orphans(max_depth=3)
        paths = [it['path'] for it in items]
        assert any('x.xyz' in p for p in paths)
        assert not any('y.xyz' in p for p in paths)

    def test_scan_orphans_ignores_metadata(self):
        _reset_libs()
        _patch_strategy({'exempt_keywords': []})
        _aux_file(engine.L_ROOT, '电影/华语电影/电影A (2020)', 'mediainfo.json', '{}')
        _aux_file(engine.L_ROOT, '电影/华语电影/电影A (2020)', 'poster.jpg', 'img')
        _aux_file(engine.L_ROOT, '电影/华语电影/电影A (2020)', 'sub.srt', 'srt')
        _aux_file(engine.L_ROOT, '电影/华语电影/电影A (2020)', 'weird.xyz', 'x')
        items = engine.scan_orphans(max_depth=3)
        assert len(items) == 1
        assert items[0]['ext'] == '.xyz'



class TestCloudRootFailSafe:

    def test_missing_cloud_root_blocks_strm_delete(self):
        _reset_libs()
        _patch_strategy({'exempt_keywords': []})
        _strm(engine.L_ROOT, '电影/华语电影/测试片 (2020)', 'x.1080p.strm')
        f = next(engine.L_ROOT.rglob('*.strm'))
        bogus = engine.L_ROOT / '_no_such_cloud_root_'
        r = engine.safe_delete_files([f], engine.L_ROOT, bogus, dry_run=False)
        assert r['strm_removed'] == 0
        assert r['cloud_missing'] == 1
        assert f.exists()

    def test_none_cloud_root_means_share_lib_ok(self):
        _reset_libs()
        _patch_strategy({'exempt_keywords': []})
        _strm(engine.S_ROOT, '剧集/欧美剧集/剧A (2020)/Season 01', 'E01.strm')
        f = next(engine.S_ROOT.rglob('*.strm'))
        r = engine.safe_delete_files([f], engine.S_ROOT, None, dry_run=False)
        assert r['strm_removed'] == 1
        assert not f.exists()


# ═══════════════════ 回归：多季保护 UnboundLocalError('common') ═══════════════════
class TestMultiProtectCommonUnbound:
    def test_partial_share_with_local_better_does_not_crash(self):
        """本地 S01/S02，分享只有 S02 且画质更差：
        第一轮循环在 S01 处 break（common 未赋值），第二轮拼「分享画质次级 x/common集」
        时曾抛 UnboundLocalError，导致整个双库扫描失败。"""
        _reset_libs()
        _patch_strategy({'multi_season_protect': 'compare', 'exempt_keywords': []})
        for sn in ('01', '02'):
            for ep in (1, 2):
                _strm(engine.L_ROOT, f'未绑定回归剧 (2020)/Season {sn}', f'E{ep:02d}.2160p.strm')
        for ep in (1, 2):
            _strm(engine.S_ROOT, '未绑定回归剧 (2020)/Season 02', f'E{ep:02d}.720p.strm')

        acts = engine.build_plan()  # 修复前这里直接抛异常
        hits = _acts_for_title(acts, '未绑定回归剧')
        assert not any(a.kind == 'loc' for a in hits)
        shr = [a for a in hits if a.kind == 'shr']
        assert len(shr) == 1 and shr[0].meta['season'] == 2
        assert '2/2集' in shr[0].detail


# ═══════════════════ 扫描不应重复遍历双库 ═══════════════════
class TestScanSingleTraversal:
    class _Args:
        kw = ''
        silent = True

    def test_scan_builds_each_library_only_once(self):
        """action_inter_check 曾在 build_plan 之后又 Lib(S)/Lib(L) 重扫一遍只为拿计数，
        大库上扫描耗时翻倍。现在整个扫描每个库只允许遍历一次。"""
        _reset_libs()
        _patch_strategy({'exempt_keywords': []})
        _strm(engine.L_ROOT, '单次遍历剧 (2020)/Season 01', 'E01.1080p.strm')
        _strm(engine.S_ROOT, '单次遍历剧 (2020)/Season 01', 'E01.2160p.strm')

        built = []
        orig_lib = engine.Lib

        class CountingLib(orig_lib):
            def __init__(self, root):
                built.append(str(root))
                super().__init__(root)

        engine.Lib = CountingLib
        try:
            engine.action_inter_check(self._Args())
        finally:
            engine.Lib = orig_lib

        assert sorted(built) == sorted([str(engine.L_ROOT), str(engine.S_ROOT)]), built

    def test_scan_refreshes_strm_count_cache_from_same_traversal(self):
        _reset_libs()
        _patch_strategy({'exempt_keywords': []})
        _strm(engine.L_ROOT, '计数剧 (2020)/Season 01', 'E01.1080p.strm')
        _strm(engine.L_ROOT, '计数剧 (2020)/Season 01', 'E02.1080p.strm')
        _strm(engine.S_ROOT, '计数剧 (2020)/Season 01', 'E01.2160p.strm')
        _strm(engine.S_ROOT, '计数剧 (2020)/Season 01', 'E02.2160p.strm')
        _strm(engine.S_ROOT, '计数剧B (2021)/Season 01', 'E01.2160p.strm')

        engine.action_inter_check(self._Args())

        assert engine._strm_count_cache['local'] == 2
        assert engine._strm_count_cache['share'] == 3
        disk = engine._load_strm_count_disk()
        assert disk is not None and disk[0] == 2 and disk[1] == 3

    def test_lib_cache_is_invalidated_after_scan(self):
        """扫描后必须失效 Lib 缓存，保证随后的执行清理二次校验基于最新磁盘状态"""
        _reset_libs()
        _patch_strategy({'exempt_keywords': []})
        _strm(engine.L_ROOT, '缓存失效剧 (2020)/Season 01', 'E01.1080p.strm')
        engine.action_inter_check(self._Args())
        assert engine._lib_cache == {}


# ═══════════════════ 入库静默期 ═══════════════════
def _age_tree(root: Path, seconds: int = 3600):
    """把 root 下所有目录和文件的 mtime 改到 seconds 秒之前（模拟"早就入库完成"）"""
    t = time.time() - seconds
    for dp, _dn, fns in os.walk(root):
        os.utime(dp, (t, t))
        for fn in fns:
            os.utime(os.path.join(dp, fn), (t, t))


class _QuietEnv:
    """临时把静默期设成 15 分钟，测试结束恢复成 0"""
    def __enter__(self):
        os.environ['INGEST_QUIET_MINUTES'] = '15'
        engine._invalidate_lib_cache()

    def __exit__(self, *exc):
        os.environ['INGEST_QUIET_MINUTES'] = '0'
        engine._invalidate_lib_cache()


class TestIngestQuietPeriod:
    def test_recent_movie_is_silently_skipped_then_picked_up(self):
        """入库不满 15 分钟的电影：静默跳过，不出现在清单里；过了静默期自然纳入"""
        _reset_libs()
        _patch_strategy({})
        _strm(engine.L_ROOT, '静默电影 (2021)', '静默电影.1080p.strm')
        _strm(engine.S_ROOT, '静默电影 (2021)', '静默电影.2160p.strm')
        with _QuietEnv():
            assert _acts_for_title(engine.build_plan(), '静默电影') == []
            assert engine._QUIET_LAST['n'] >= 1
            _age_tree(engine.L_ROOT); _age_tree(engine.S_ROOT)
            engine._invalidate_lib_cache()
            acts = _acts_for_title(engine.build_plan(), '静默电影')
            assert len(acts) == 1 and acts[0].kind == 'loc'

    def test_partially_ingested_series_is_not_judged(self):
        """还没入完的剧（分享刚入 3 集、本地已有 6 集）不能被判成"分享落后 → 淘汰分享"，
        静默期过后才允许参与治理。"""
        _reset_libs()
        _patch_strategy({})
        for e in range(1, 7):
            _strm(engine.L_ROOT, '入库中的剧 (2026)/Season 01', f'入库中的剧.S01E{e:02d}.1080p.strm')
        _age_tree(engine.L_ROOT)                      # 本地早就入完了
        for e in range(1, 4):                          # 分享这边刚入了 3 集
            _strm(engine.S_ROOT, '入库中的剧 (2026)/Season 01', f'入库中的剧.S01E{e:02d}.1080p.strm')
        with _QuietEnv():
            assert _acts_for_title(engine.build_plan(), '入库中的剧') == []
            _age_tree(engine.S_ROOT)                   # 静默期过后
            engine._invalidate_lib_cache()
            assert _acts_for_title(engine.build_plan(), '入库中的剧') != []

    def test_only_recent_titles_are_skipped(self):
        """静默只针对刚入库的标题，同一次扫描里早已入库的标题照常治理"""
        _reset_libs()
        _patch_strategy({})
        _strm(engine.L_ROOT, '老电影 (2019)', '老电影.1080p.strm')
        _strm(engine.S_ROOT, '老电影 (2019)', '老电影.2160p.strm')
        _age_tree(engine.L_ROOT); _age_tree(engine.S_ROOT)
        _strm(engine.L_ROOT, '新电影 (2026)', '新电影.1080p.strm')
        _strm(engine.S_ROOT, '新电影 (2026)', '新电影.2160p.strm')
        with _QuietEnv():
            acts = engine.build_plan()
            assert len(_acts_for_title(acts, '老电影')) == 1
            assert _acts_for_title(acts, '新电影') == []

    def test_recent_duplicate_versions_are_skipped(self):
        """库内多版本去重也遵守静默期：刚入库的多版本先不动"""
        _reset_libs()
        _patch_strategy({})
        _strm(engine.S_ROOT, '多版本片 (2020)', '多版本片.720p.strm')
        _strm(engine.S_ROOT, '多版本片 (2020)', '多版本片.2160p.strm')
        with _QuietEnv():
            assert _acts_for_title(engine.build_plan(), '多版本片') == []
            _age_tree(engine.S_ROOT)
            engine._invalidate_lib_cache()
            assert _acts_for_title(engine.build_plan(), '多版本片') != []

    def test_zero_minutes_disables_the_filter(self):
        """静默期设为 0 = 关闭，刚创建的文件也立即参与治理"""
        _reset_libs()
        _patch_strategy({})
        _strm(engine.L_ROOT, '关闭静默 (2022)', '关闭静默.1080p.strm')
        _strm(engine.S_ROOT, '关闭静默 (2022)', '关闭静默.2160p.strm')
        assert len(_acts_for_title(engine.build_plan(), '关闭静默')) == 1

class TestNoTrashRetention:
    """STRM 删除即彻底删除，不保留任何副本、不产生回收站目录。

    该机制（TRASH_STRM / TRASH_DIR / TRASH_KEEP_DAYS）已被显式移除，
    本组测试用于防止它被无意间重新引入。
    """

    def test_no_trash_module_symbols(self):
        """模块层不应再暴露任何 TRASH 相关配置"""
        for attr in ('TRASH_STRM', 'TRASH_DIR', 'TRASH_KEEP_DAYS'):
            assert not hasattr(engine, attr), '%s 应已移除' % attr

    def test_remove_strm_deletes_permanently(self):
        """STRM 与同 stem 附属文件被真删，且不在数据目录留下任何副本"""
        _reset_libs()
        d = engine.L_ROOT / '删除测试 (2021)'
        d.mkdir(parents=True, exist_ok=True)
        strm = d / '删除测试.2160p.strm'
        strm.write_text('http://127.0.0.1/fake', encoding='utf-8')
        meta = d / '删除测试.2160p-mediainfo.json'
        meta.write_text('{}', encoding='utf-8')
        sub = d / '删除测试.2160p.zh.srt'
        sub.write_text('', encoding='utf-8')
        other_ep = d / '删除测试.2160p.E02.strm'
        other_ep.write_text('http://127.0.0.1/fake2', encoding='utf-8')

        engine._remove_strm(strm)

        assert not strm.exists(), 'STRM 应被彻底删除'
        assert not meta.exists(), '同名 mediainfo 应被删除'
        assert not sub.exists(), '同名字幕应被删除'
        assert other_ep.exists(), '同目录其他集不应被误删'
        assert not (engine.DATA_DIR / 'trash').exists(), '不应产生回收站目录'
        if engine.DATA_DIR.exists():
            assert list(engine.DATA_DIR.rglob('*.strm')) == [], '数据目录不应残留 STRM 副本'

    def test_purge_old_still_cleans_expired_plans(self):
        """移除回收站后，purge_old 仍须清理过期治理计划"""
        import json
        _reset_libs()
        engine.STATE_DIR.mkdir(parents=True, exist_ok=True)
        stale = engine.STATE_DIR / 'plan_stale.json'
        old = time.time() - 8 * 86400
        # Use a real plan timestamp: v2 file/database retention now shares creation time.
        _state.save(stale, {'schema_version': 2, 'id': 'stale', 'ts': old,
                                     'state': 'pending'})

        engine.purge_old()

        assert engine.db_load_plan('stale') is None, '超过 7 天的未执行计划应被清理'


def test_same_tmdb_different_title_year_never_cross_matches():
    """治理身份必须是剧名+年份；错误/重复 TMDB 不能把不同作品合并。"""
    _reset_libs()
    _strm(engine.L_ROOT, '奇异博士 (2016) {tmdb-64165}', '奇异博士.1080p.strm')
    _strm(engine.S_ROOT, '百分之十 (2015) {tmdb-64165}', '百分之十.1080p.strm')
    acts = engine.build_plan()
    assert not [a for a in acts if a.kind in ('loc', 'shr')]


def test_governance_action_exposes_match_evidence():
    """治理详情必须能看到匹配依据、本地/分享路径及 TMDB 辅助信息。"""
    _reset_libs()
    _strm(engine.L_ROOT, '百分之十 (2015) {tmdb-64165}', '百分之十.1080p.strm')
    _strm(engine.S_ROOT, '百分之十 (2015) {tmdb-64165}', '百分之十.2160p.strm')
    acts = [a for a in engine.build_plan() if a.kind in ('loc', 'shr')]
    assert acts
    m = acts[0].meta
    assert m['match_basis'] == '剧名 + 年份'
    assert '百分之十' in m['match_basis_detail']
    assert any('百分之十 (2015)' in x for x in m['local_paths'])
    assert any('百分之十 (2015)' in x for x in m['share_paths'])
    assert m['tmdb'] == ['64165']


# ═══════════════════ 配对策略切换（1.6.10） ═══════════════════
class TestMatchStrategy:
    def test_tmdb_first_pairs_across_different_titles(self):
        """tmdb_first：TMDB ID 一致时，可跨不同译名/命名配对；title_year 则配不上。"""
        _reset_libs()

        _strm(engine.L_ROOT, '奇异博士 (2016) [tmdb-284052]', '奇异博士.2160p.strm')
        _strm(engine.S_ROOT, 'Doctor Strange (2016) [tmdb-284052]', 'Doctor Strange.720p.strm')

        # title_year（默认）：剧名不同 -> 不配 -> 无删除动作
        _patch_strategy({'match_strategy': 'title_year'})
        assert not [a for a in engine.build_plan() if a.kind in ('loc', 'shr')]

        # tmdb_first：TMDB 一致 -> 配 -> 分享 720p 劣于本地 2160p，删分享
        _patch_strategy({'match_strategy': 'tmdb_first'})
        acts = [a for a in engine.build_plan() if a.kind in ('loc', 'shr')]
        assert len(acts) == 1
        assert acts[0].kind == 'shr'
        assert acts[0].meta['identity_source'] == 'tmdb_first'

    def test_tmdb_first_does_not_match_when_tmdb_conflicts(self):
        """tmdb_first：同一 TMDB 在本地对应多个不同身份时，判定冲突、不自动配。"""
        _reset_libs()

        _strm(engine.L_ROOT, '甲电影 (2020) [tmdb-999999]', '甲电影.2160p.strm')
        _strm(engine.L_ROOT, '乙电影 (2021) [tmdb-999999]', '乙电影.720p.strm')
        _strm(engine.S_ROOT, '甲电影 (2020) [tmdb-999999]', '甲电影.720p.strm')

        _patch_strategy({'match_strategy': 'tmdb_first'})
        # 本地同 TMDB 有「甲电影」和「乙电影」两个身份 -> 冲突，不配，无删除动作
        assert not [a for a in engine.build_plan() if a.kind in ('loc', 'shr')]

    def test_tmdb_first_falls_back_to_title_year_when_no_tmdb(self):
        """tmdb_first：无 TMDB 时回退「剧名+年份」，同名同年仍正常配对。"""
        _reset_libs()

        _strm(engine.L_ROOT, '回退电影 (2020)', '回退电影.2160p.strm')
        _strm(engine.S_ROOT, '回退电影 (2020)', '回退电影.720p.strm')

        _patch_strategy({'match_strategy': 'tmdb_first'})
        acts = [a for a in engine.build_plan() if a.kind in ('loc', 'shr')]
        assert len(acts) == 1
        assert acts[0].kind == 'shr'


# ═══════════════════ 画质对比规则接入治理 ═══════════════════
class TestCoverRulesInGovernance:
    def test_package_import_path_uses_cover_engine(self):
        """回归：容器以 `app.main` 方式加载，裸 `from core import` 会静默失效。
        子进程里只放项目根目录（不放 app/），确认 7 维引擎真的在跑。"""
        import subprocess
        code = (
            "import app.governance as g, app.config as c;"
            "assert g._cmp_versions('a.1080p.BluRay.mkv','a.2160p.WEB-DL.mkv')==-1;"
            "cover=c.get_cover_strategy();assert len(cover['rules'])==7;"
            "cover['rules'][1]['enabled']=True;c.get_cover_strategy=lambda:cover;"
            "assert g._cmp_versions('a.1080p.BluRay.mkv','a.2160p.WEB-DL.mkv')==1;print('ok')")
        r = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True,
                           cwd=str(Path(__file__).parents[2]),
                           env=dict(os.environ, AGENT_DATA=str(_TMP / 'data')))
        assert r.returncode == 0 and 'ok' in r.stdout, r.stderr

    def test_season_tie_follows_tie_switch(self):
        """剧集逐集 7 维全打平：默认剔除本地；开启「平局保留本地」则剔除分享"""
        for keep_local, kind in ((False, 'loc'), (True, 'shr')):
            _reset_libs()
            _patch_strategy({'multi_season_protect': 'off', 'tie_keep_local': keep_local})
            for lib in (engine.L_ROOT, engine.S_ROOT):
                _strm(lib, '季平局 (2020)/Season 01', 'E01.1080p.strm')
                _strm(lib, '季平局 (2020)/Season 01', 'E02.1080p.strm')
            hits = _acts_for_title(engine.build_plan(), '季平局')
            assert len(hits) == 1 and hits[0].kind == kind

    def _two_season(self, title, share_res):
        for sn in ('01', '02'):
            for e in ('E01', 'E02'):
                _strm(engine.L_ROOT, f'{title} (2020)/Season {sn}', f'{e}.1080p.strm')
        for sn in share_res:
            for e in ('E01', 'E02'):
                _strm(engine.S_ROOT, f'{title} (2020)/Season {sn}', f'{e}.2160p.strm')

    def test_multi_protect_compare_needs_all_seasons(self):
        """开启：分享全季达标才删本地；少一季就保本地、淘汰分享"""
        _reset_libs(); _patch_strategy({'multi_season_protect': 'compare'})
        self._two_season('多季全', ['01', '02'])
        hits = _acts_for_title(engine.build_plan(), '多季全')
        assert hits and all(a.kind == 'loc' for a in hits)
        _reset_libs(); _patch_strategy({'multi_season_protect': 'compare'})
        self._two_season('多季缺', ['01'])
        hits = _acts_for_title(engine.build_plan(), '多季缺')
        assert hits and all(a.kind == 'shr' for a in hits)

    def test_multi_protect_symmetric_share_collection_not_hollowed(self):
        """对称保护：分享 6 季合集，本地只有 S01 且画质略高 —— 不能淘汰分享 S01 掏空合集，
        反而删本地这一季；分享其余季不动。"""
        _reset_libs(); _patch_strategy({'multi_season_protect': 'compare', 'exempt_keywords': []})
        for sn in range(1, 7):
            for e in ('E01', 'E02'):
                _strm(engine.S_ROOT, f'分享合集 (2020)/Season {sn:02d}', f'{e}.1080p.strm')
        for e in ('E01', 'E02'):
            _strm(engine.L_ROOT, '分享合集 (2020)/Season 01', f'{e}.2160p.strm')  # 本地更优
        hits = _acts_for_title(engine.build_plan(), '分享合集')
        assert not any(a.kind == 'shr' for a in hits), [a.text for a in hits]
        assert len(hits) == 1 and hits[0].kind == 'loc' and hits[0].meta['season'] == 1
        assert hits[0].meta['reason'] == 'multi_protect_partial_local'

    def test_multi_protect_symmetric_share_short_season_eliminated(self):
        """分享该季集数达不到本地 → 按原规则淘汰分享（本地更全），不删本地。"""
        _reset_libs(); _patch_strategy({'multi_season_protect': 'compare', 'exempt_keywords': []})
        for sn in range(1, 4):
            for e in ('E01', 'E02', 'E03'):
                _strm(engine.S_ROOT, f'分享缺集 (2020)/Season {sn:02d}', f'{e}.1080p.strm')
        for e in ('E01', 'E02', 'E03', 'E04'):  # 本地比分享多一集
            _strm(engine.L_ROOT, '分享缺集 (2020)/Season 01', f'{e}.2160p.strm')
        hits = _acts_for_title(engine.build_plan(), '分享缺集')
        assert not any(a.kind == 'loc' for a in hits), [a.text for a in hits]
        assert len(hits) == 1 and hits[0].kind == 'shr' and hits[0].meta['season'] == 1
        assert hits[0].meta['reason'] == 'local_wins'

    def _mk(self, root, title, seasons, res):
        for sn in seasons:
            for e in ('E01', 'E02'):
                _strm(root, f'{title} (2020)/Season {sn:02d}', f'{e}.{res}.strm')

    def test_s00_joins_multi_protect_local_not_hollowed(self):
        """本地 S00+S01-S03、分享来 S00(2160p)+S01：S00 算一季参与保护——
        不能因为分享 S00 画质好就删本地 S00；保护本地，淘汰分享 S00 / S01。"""
        _reset_libs(); _patch_strategy({'multi_season_protect': 'compare', 'exempt_keywords': []})
        self._mk(engine.L_ROOT, '向往的生活', [0, 1, 2, 3], '1080p')
        self._mk(engine.S_ROOT, '向往的生活', [0, 1], '2160p')
        hits = _acts_for_title(engine.build_plan(), '向往的生活')
        assert not any(a.kind == 'loc' for a in hits), [a.text for a in hits]
        assert sorted(a.meta['season'] for a in hits if a.kind == 'shr') == [0, 1]

    def test_s00_joins_multi_protect_share_not_hollowed(self):
        """对称：分享 S00+S01-S03，本地来 S00(2160p)+S01 → 保护分享，淘汰本地 S00 / S01。"""
        _reset_libs(); _patch_strategy({'multi_season_protect': 'compare', 'exempt_keywords': []})
        self._mk(engine.S_ROOT, '特别篇对称', [0, 1, 2, 3], '1080p')
        self._mk(engine.L_ROOT, '特别篇对称', [0, 1], '2160p')
        hits = _acts_for_title(engine.build_plan(), '特别篇对称')
        assert not any(a.kind == 'shr' for a in hits), [a.text for a in hits]
        assert sorted(a.meta['season'] for a in hits if a.kind == 'loc') == [0, 1]

    def test_s00_follows_whole_show_winner_when_sets_equal(self):
        """季集合（含 S00）相同：整体比画质，S00 跟随胜负——分享全达标则本地 S00 一起删。"""
        _reset_libs(); _patch_strategy({'multi_season_protect': 'compare', 'exempt_keywords': []})
        self._mk(engine.L_ROOT, '特别篇跟随', [0, 1, 2], '1080p')
        self._mk(engine.S_ROOT, '特别篇跟随', [0, 1, 2], '2160p')
        hits = _acts_for_title(engine.build_plan(), '特别篇跟随')
        assert len(hits) == 3 and all(a.kind == 'loc' for a in hits)
        assert sorted(a.meta['season'] for a in hits) == [0, 1, 2]

    def test_s00_ignore_not_counted_in_multi_protect(self):
        """特别篇策略为「忽略」时 S00 不参与也不被动：只按数字季处理。"""
        _reset_libs(); _patch_strategy({'multi_season_protect': 'compare', 'exempt_keywords': [],
                                        'special_action': 'ignore'})
        self._mk(engine.L_ROOT, '特别篇忽略', [0, 1, 2], '1080p')
        self._mk(engine.S_ROOT, '特别篇忽略', [0, 1], '2160p')
        hits = _acts_for_title(engine.build_plan(), '特别篇忽略')
        assert [a.meta['season'] for a in hits] == [1] and hits[0].kind == 'shr'

    def test_multi_protect_equal_season_sets_compare_quality(self):
        """两边季集合相同：单库 vs 单库整体比画质留优者——分享全季达标删本地，本地更好则整体淘汰分享。"""
        for share_res, local_res, kind in (('2160p', '1080p', 'loc'), ('1080p', '2160p', 'shr')):
            _reset_libs(); _patch_strategy({'multi_season_protect': 'compare', 'exempt_keywords': []})
            for sn in ('01', '02', '03'):
                for e in ('E01', 'E02'):
                    _strm(engine.L_ROOT, f'季集合相同 (2020)/Season {sn}', f'{e}.{local_res}.strm')
                    _strm(engine.S_ROOT, f'季集合相同 (2020)/Season {sn}', f'{e}.{share_res}.strm')
            hits = _acts_for_title(engine.build_plan(), '季集合相同')
            assert len(hits) == 3 and all(a.kind == kind for a in hits), [a.text for a in hits]

    def test_multi_protect_partial_overlap_touches_neither(self):
        """双方各有对方没有的季（本地 S01-S02、分享 S02-S03）：删哪边都会让该库断层 → 同季都不动。"""
        _reset_libs(); _patch_strategy({'multi_season_protect': 'compare', 'exempt_keywords': []})
        for sn in ('01', '02'):
            for e in ('E01', 'E02'):
                _strm(engine.L_ROOT, f'部分重叠 (2020)/Season {sn}', f'{e}.1080p.strm')
        for sn in ('02', '03'):
            for e in ('E01', 'E02'):
                _strm(engine.S_ROOT, f'部分重叠 (2020)/Season {sn}', f'{e}.2160p.strm')
        assert not _acts_for_title(engine.build_plan(), '部分重叠')

    def test_multi_protect_partial_overlap_still_cleans_residual(self):
        """部分重叠时残次品仍独立处理：本地 S02 缺 E01 → 只删本地残次那季。"""
        _reset_libs(); _patch_strategy({'multi_season_protect': 'compare', 'exempt_keywords': []})
        for e in ('E01', 'E02'):
            _strm(engine.L_ROOT, '重叠残次 (2020)/Season 01', f'{e}.1080p.strm')
        _strm(engine.L_ROOT, '重叠残次 (2020)/Season 02', 'E02.1080p.strm')
        for sn in ('02', '03'):
            for e in ('E01', 'E02'):
                _strm(engine.S_ROOT, f'重叠残次 (2020)/Season {sn}', f'{e}.2160p.strm')
        hits = _acts_for_title(engine.build_plan(), '重叠残次')
        assert len(hits) == 1 and hits[0].kind == 'loc' and hits[0].meta['season'] == 2
        assert hits[0].meta['reason'] == 'incomplete_delete_local'

    def test_series_keep_local_decision(self):
        """保留本地：剧集两库都有的季一律删分享（不比画质，哪怕分享 2160p）；分享独有季不动"""
        for mp in ('off', 'compare'):
            _reset_libs(); _patch_strategy({'decision': 'keep_local', 'multi_season_protect': mp})
            self._two_season('决策留本地', ['01', '02'])
            _strm(engine.S_ROOT, '决策留本地 (2020)/Season 03', 'E01.2160p.strm')
            hits = _acts_for_title(engine.build_plan(), '决策留本地')
            assert len(hits) == 2 and all(a.kind == 'shr' for a in hits)
            assert {a.meta['season'] for a in hits} == {1, 2}
            assert all(a.meta['reason'] == 'decision_keep_local' for a in hits)

    def test_series_keep_share_decision(self):
        """保留分享：分享覆盖本地全部集的季删本地（哪怕本地画质更好）；少一季只处理有的那季"""
        for mp in ('off', 'compare'):
            _reset_libs(); _patch_strategy({'decision': 'keep_share', 'multi_season_protect': mp})
            self._two_season('决策留分享', ['01'])
            hits = _acts_for_title(engine.build_plan(), '决策留分享')
            assert len(hits) == 1 and hits[0].kind == 'loc' and hits[0].meta['season'] == 1
            assert hits[0].meta['reason'] == 'decision_keep_share'

    def test_series_keep_share_never_deletes_local_when_share_has_gaps(self):
        """保留分享：分享该季缺集（不覆盖本地全部集）时不删本地，避免丢 115 源"""
        _reset_libs(); _patch_strategy({'decision': 'keep_share', 'multi_season_protect': 'off'})
        for e in ('E01', 'E02', 'E03'):
            _strm(engine.L_ROOT, '决策缺集 (2020)/Season 01', f'{e}.1080p.strm')
        for e in ('E01', 'E02'):
            _strm(engine.S_ROOT, '决策缺集 (2020)/Season 01', f'{e}.2160p.strm')
        assert not [a for a in _acts_for_title(engine.build_plan(), '决策缺集') if a.kind == 'loc']

    def test_series_decision_beats_multi_protect(self):
        """保留本地/保留分享优先于多季保护"""
        _reset_libs(); _patch_strategy({'decision': 'keep_share', 'multi_season_protect': 'compare'})
        self._two_season('决策高于豁免', ['01', '02'])
        hits = _acts_for_title(engine.build_plan(), '决策高于豁免')
        assert hits and all(a.kind == 'loc' and a.meta['reason'] == 'decision_keep_share' for a in hits)

    def test_season_ratio_threshold_is_configurable(self):
        """剧集达标率阈值可调：10 集里 5 集分享更优、5 集打平且「平局保留本地」→ 达标率 50%"""
        def build(ratio):
            _reset_libs()
            _patch_strategy({'multi_season_protect': 'off', 'tie_keep_local': True,
                             'season_replace_ratio': ratio})
            for i in range(1, 11):
                _strm(engine.L_ROOT, '阈值剧 (2020)/Season 01', f'E{i:02d}.1080p.strm')
                _strm(engine.S_ROOT, '阈值剧 (2020)/Season 01',
                      f'E{i:02d}.{"2160p" if i <= 5 else "1080p"}.strm')
            return _acts_for_title(engine.build_plan(), '阈值剧')
        hits = build(0.5)
        assert len(hits) == 1 and hits[0].kind == 'loc'    # 50% ≥ 50% → 删本地
        hits = build(0.9)
        assert len(hits) == 1 and hits[0].kind == 'shr'    # 50% < 90% → 删分享

    def test_season_ratio_invalid_falls_back_to_default(self):
        """阈值缺失/非法/越界时回落默认 0.9，不会让治理异常或放宽删本地门槛"""
        gov = sys.modules[engine.build_plan.__module__]  # 与 engine 同一份 governance（双导入模式下不会串到另一份）
        for bad in (None, 'abc', 0.1, 1.5, float('nan')):
            _patch_strategy({'season_replace_ratio': bad})
            assert gov._replace_ratio() == engine.SEASON_REPLACE_RATIO
        _patch_strategy({'season_replace_ratio': 0.75})
        assert gov._replace_ratio() == 0.75

    def test_season_ratio_config_clamped(self):
        from app import config as cfgm
        assert cfgm.normalize_season_ratio('0.8') == 0.8
        assert cfgm.normalize_season_ratio(0.2) == 0.5
        assert cfgm.normalize_season_ratio(9) == 1.0
        assert cfgm.normalize_season_ratio('x') == 0.9

    def test_movie_act_carries_per_dimension_evidence(self):
        _reset_libs()
        _patch_strategy({})
        _strm(engine.L_ROOT, '依据电影 (2021)', '依据电影.1080p.WEB-DL.strm')
        _strm(engine.S_ROOT, '依据电影 (2021)', '依据电影.2160p.WEB-DL.strm')
        hits = _acts_for_title(engine.build_plan(), '依据电影')
        cmp = hits[0].meta['compare']
        assert cmp['decided_by'] == 'resolution' and cmp['result'] == 1
        assert {d['key'] for d in cmp['dims']} == set(engine.core.COVER_RULE_ORDER) if hasattr(engine, 'core') else True
