"""识别回归：标题归一化、pick 打分、TMDB 404 负缓存、「走马上任 / Let's Go」。

背景：剧集发布名是英文「Let's Go」，TMDB 英文搜索结果里同名的正确条目是
《走马上任》(275297)，同时还有同年的《Let's Go 怪奇组》(302824)。旧评分只看
相似度 + 年份，会把「共享前缀」的 302824 当成本剧；详情 404 后换候选时也可能
顺手换成它。本文件把这条链路的判定钉住。
"""
import asyncio

import httpx
import pytest

from strimkeep.cloud115 import CloudError
from strimkeep.media import (NeedsReview, normal_title, parse_name, pick_best,
                             query_variants, title_relation)
from strimkeep.organizer import Organizer
from test_organize import setup


LETS_GO_FILE = "Let's Go.S01E01.2026.2160p.MIGUTV.WEB-DL.AAC2.0.HFR.H.265.mp4"

# TMDB 真实返回（2026-11-17 实测）的精简版。
EN_CANDIDATES = [
    {'id': 275297, 'name': "Let's Go", 'original_name': '走马上任',
     'first_air_date': '2026-11-02', 'popularity': 21.84},
    {'id': 302824, 'name': "Let's Go 怪奇组", 'original_name': 'レッツゴー怪奇組',
     'first_air_date': '2026-07-05', 'popularity': 4.33},
    {'id': 81157, 'name': "Bakusou Kyoudai Let's & Go!!",
     'original_name': '爆走兄弟レッツ＆ゴー!!', 'first_air_date': '1996-01-08',
     'popularity': 21.46},
]
ZH_CANDIDATES = [
    {'id': 302824, 'name': "Let's Go 怪奇组", 'original_name': 'レッツゴー怪奇組',
     'first_air_date': '2026-07-05', 'popularity': 4.33},
    {'id': 96501, 'name': '我们走，露娜！', 'original_name': "Let's Go Luna!",
     'first_air_date': '2018-11-21', 'popularity': 6.71},
    {'id': 11932, 'name': "Let's Go", 'original_name': "Let's Go",
     'first_air_date': '', 'popularity': 0.13},
]
DETAILS = {
    275297: {'id': 275297, 'name': '走马上任', 'original_name': '走马上任',
             'first_air_date': '2026-11-02',
             'seasons': [{'season_number': 1, 'air_date': '2026-11-02'}]},
    302824: {'id': 302824, 'name': "Let's Go 怪奇组", 'first_air_date': '2026-07-05',
             'seasons': [{'season_number': 1, 'air_date': '2026-07-05'}]},
}


class FakeTMDB:
    """按 language 返回不同搜索结果；missing 里的 id 详情返回 404。"""

    def __init__(self, search=None, details=None, missing=()):
        self.search = search or {}
        self.details = details or {}
        self.missing = set(missing)
        self.calls = []

    async def __call__(self, path, params=None):
        params = dict(params or {})
        self.calls.append((path, params))
        if path.startswith('search/'):
            kind = path.split('/')[1]
            language = params.get('language') or 'zh-CN'
            return {'results': (self.search.get(kind) or {}).get(language, [])}
        if path.endswith('/alternative_titles'):
            return {'results': []}
        ident = int(path.split('/')[1])
        if ident in self.missing:
            raise CloudError('TMDB 中找不到该条目（GET %s → 404）。' % path, 'TMDB_NOT_FOUND', 404)
        return self.details.get(ident, {'id': ident})

    def languages_used(self):
        return [params.get('language') for path, params in self.calls if path.startswith('search/')]


def engine_with(fake):
    engine = Organizer.__new__(Organizer)
    engine.tmdb = fake
    return engine


def lets_go_hint():
    return parse_name(LETS_GO_FILE, 'Season 01 (2026)')


# ------------------------------------------------------------ 标题归一化
def test_title_normalisation_ignores_typography():
    assert normal_title("Let's Go") == normal_title('Let’s Go') == normal_title('Lets  Go')
    assert normal_title("Let's Go") == normal_title('Ｌｅｔ＇ｓ　Ｇｏ')
    assert normal_title("Let's & Go") == normal_title("Let's and Go")
    assert normal_title('Amélie') == normal_title('Amelie')
    # 中文、数字、全半角差异同样不参与比较
    assert normal_title('走马上任（2026）') == normal_title('走马上任(2026)')


def test_query_variants_keep_latin_phrase_whole():
    variants = query_variants("走马上任 / Let's Go")
    assert '走马上任' in variants
    assert "Let's Go" in variants
    # 拆成单词后「Go」会和另一部叫《Go》的作品精确命中
    assert 'Go' not in variants and "Let's" not in variants
    # 文件名尾部的年份不是标题
    assert '2026' not in query_variants("走马上任 / Let's Go 2026")
    # 原有的中英文混合写法仍然可用
    assert query_variants('群体.Colony')[1:] == ['群体', 'Colony']


def test_title_relation_separates_exact_title_from_subtitle():
    hint = {'query': "Let's Go"}
    assert title_relation(hint, {'name': "Let's Go"}) == 'exact'
    assert title_relation(hint, {'name': 'Let’s Go'}) == 'exact'
    assert title_relation(hint, {'name': "Let's Go 怪奇组"}) == 'extended'
    assert title_relation(hint, {'name': "Let's Go, Bananas!"}) == 'extended'
    assert title_relation(hint, {'name': "Bakusou Kyoudai Let's & Go!!"}) == 'partial'


# --------------------------------------------------------------- pick 打分
def test_exact_title_beats_same_year_subtitle_extension():
    """中文搜索只有「怪奇组」时，也不能把同年的它判成本剧。

    旧实现按「相似度×100 + 年份 65」排序，302824（0.8 分、2026 年）会压过
    精确同名但没有首播日期的 11932；现在精确同名优先，年份只是加分。
    """
    async def run():
        fake = FakeTMDB(search={'tv': {'zh-CN': ZH_CANDIDATES}})
        attempts = []
        picked = await engine_with(fake).pick('tv', lets_go_hint(), ZH_CANDIDATES, attempts)
        assert picked == 11932, attempts
        assert any(a.get('match') == 'extended' and a.get('evidence') == 'first_air'
                   for a in attempts)
    asyncio.run(run())


def test_lets_go_picks_zou_ma_shang_ren_end_to_end():
    """「走马上任 / Let's Go」整条链路：文件名 → 英文搜索 → 详情。"""
    async def run():
        hint = lets_go_hint()
        assert hint['media_type'] == 'tv' and hint['query'] == "Let's Go"
        assert hint['season'] == 1 and hint['episode'] == 1 and str(hint['year']) == '2026'
        fake = FakeTMDB(search={'tv': {'en-US': EN_CANDIDATES, 'zh-CN': ZH_CANDIDATES}},
                        details=DETAILS)
        details = await engine_with(fake).identify(hint)
        assert details['id'] == 275297
        assert details['name'] == '走马上任'
        assert fake.languages_used()[0] == 'en-US'
    asyncio.run(run())


def test_pick_best_uses_the_same_ranking():
    """电影走的 pick_best 与剧集共用一套排序：精确同名优先于「同名+副标题」。"""
    best = pick_best({'query': "Let's Go", 'year': 2026}, ZH_CANDIDATES)
    assert best['id'] == 11932


# ------------------------------------------------------------- 404 负缓存
def test_tmdb_404_is_negatively_cached(tmp_path):
    """失效 id 只打一次 TMDB：后续文件直接判缺失，不再等 404。"""
    async def run():
        store, cloud, http, engine, requests = setup(tmp_path)
        hits = []

        async def handler(request):
            hits.append(str(request.url))
            return httpx.Response(404, json={'status_code': 34}, request=request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        engine.http = client
        with pytest.raises(CloudError) as first:
            await engine.tmdb('tv/999')
        assert first.value.code == 'TMDB_NOT_FOUND' and first.value.status == 404
        assert 'tv/999' in str(first.value)
        with pytest.raises(CloudError) as second:
            await engine.tmdb('tv/999')
        assert second.value.code == 'TMDB_NOT_FOUND'
        assert len(hits) == 1, '第二次应命中 404 负缓存，不再请求 TMDB'
        assert Organizer.tmdb_cache_key('tv/999') in engine._tmdb_missing
        # 负缓存过期后恢复请求，不能被永久钉死
        engine._tmdb_missing[Organizer.tmdb_cache_key('tv/999')] = 0
        with pytest.raises(CloudError):
            await engine.tmdb('tv/999')
        assert len(hits) == 2
        await client.aclose()
        await http.aclose()
        store.close()
    asyncio.run(run())


# ------------------------------------------------- 详情 404 换候选的边界
def test_detail_404_does_not_swap_to_a_subtitle_similar_show():
    """正确条目详情 404 时，宁转人工，也不改判成《Let's Go 怪奇组》。"""
    async def run():
        fake = FakeTMDB(search={'tv': {'en-US': EN_CANDIDATES}},
                        details=DETAILS, missing=(275297,))
        hint = lets_go_hint()
        with pytest.raises(NeedsReview) as exc:
            await engine_with(fake).identify(hint)
        assert '275297' in str(exc.value) and '404' in str(exc.value)
        assert not hint.get('tmdb_fallback')
        assert not any(path == 'tv/302824' for path, _ in fake.calls)
    asyncio.run(run())


def test_detail_404_retries_another_exact_title_candidate():
    """失效 id 之外还有精确同名候选时，换 id 继续，并留下 trace。"""
    async def run():
        candidates = [
            {'id': 100, 'name': "Let's Go", 'first_air_date': '2026-11-02', 'popularity': 9},
            {'id': 101, 'name': "Let's Go", 'first_air_date': '', 'popularity': 1},
        ]
        fake = FakeTMDB(search={'tv': {'en-US': candidates}},
                        details={101: {'id': 101, 'name': "Let's Go",
                                       'first_air_date': '2026-01-01',
                                       'seasons': [{'season_number': 1, 'air_date': '2026-01-01'}]}},
                        missing=(100,))
        hint = lets_go_hint()
        details = await engine_with(fake).identify(hint)
        assert details['id'] == 101
        assert hint['tmdb_fallback'] is True and hint['tmdb_id'] == 101
    asyncio.run(run())


# ------------------------------------------------------------ 年份证据口径
def test_unverifiable_year_is_unknown_not_a_mismatch():
    """TMDB 没有任何日期时是「无从核实」，不能当成年份不符把精确同名候选扔掉。"""
    async def run():
        fake = FakeTMDB(details={7: {'id': 7, 'seasons': []}})
        ok, evidence = await engine_with(fake).year_ok(
            'tv', {'id': 7}, {'year': 2026, 'season': 1, 'episode': 1})
        assert not ok and evidence == 'unknown'
    asyncio.run(run())
