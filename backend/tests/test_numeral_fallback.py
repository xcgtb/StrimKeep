"""中文数字尾数兜底：只有原查询一无所获才触发，且必须唯一精确 + 季真实存在。"""
import asyncio
import json

import httpx

from strimkeep.media import NeedsReview, numeral_variants
from strimkeep.organizer import Organizer
from strimkeep.store import Store
from test_organize import MemoryCloud

BASE = {'id': 4114, 'name': '法证先锋', 'first_air_date': '2006-04-03',
        'original_name': '法证先锋', 'genres': [], 'seasons': [{'season_number': 1}, {'season_number': 2}]}


def make(tmp_path, queries, details_by_id=None):
    details_by_id = details_by_id or {'4114': BASE}

    def handler(request):
        url = str(request.url)
        query = request.url.params.get('query')
        if query is not None:
            return httpx.Response(200, json={'results': queries.get(query, [])})
        import re as _re
        hit = _re.search(r'/tv/(\d+)', url)
        if hit:
            node = details_by_id.get(hit.group(1))
            if node is not None:
                return httpx.Response(200, json=node)
        return httpx.Response(200, json={})

    store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
    store.set_many({'cookie': 'UID=1; CID=t; SEID=t', 'tmdb_key': 'k',
                    'playback_base': 'https://play.example'})
    cloud = MemoryCloud(store)
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    engine = Organizer(store, cloud, http, tmp_path / 'strm')
    return store, http, engine


def test_numeral_variants_shape():
    assert numeral_variants('法证先锋一') == [('法证先锋1', None), ('法证先锋', 1)]
    assert numeral_variants('法证先锋3') == []
    assert numeral_variants('普通剧名') == []


def test_trailing_numeral_falls_back_to_base_series_as_season(tmp_path):
    """「法证先锋一」搜不到 → 去尾数命中《法证先锋》→ 当第 1 季。"""
    async def run():
        queries = {'法证先锋一': [],
                   '法证先锋1': [dict(BASE)],
                   '法证先锋': [dict(BASE)]}
        store, http, engine = make(tmp_path, queries)
        hint = {'query': '法证先锋一', 'year': None, 'media_type': 'tv',
                'season': 1, 'episode': 5, 'end_episode': 5, 'tmdb_id': None}
        details = await engine.identify(hint)
        assert details['id'] == 4114
        # 0.7.37 起按标题评分取最高：「法证先锋1」这条别名已足够命中，
        # 不再必须先退回「法证先锋」再按季数处理；季数仍为 1。
        assert hint['query'] in ('法证先锋', '法证先锋1') and hint['season'] == 1
        await http.aclose()
        store.close()

    asyncio.run(run())


def test_arabic_title_hits_separate_series_first(tmp_path):
    """「法证先锋三」→「法证先锋3」是 TMDB 独立条目，直接命中，不当季数用。"""
    async def run():
        third = {'id': 38180, 'name': '法证先锋3', 'first_air_date': '2011-10-10',
                 'original_name': '法证先锋3', 'genres': [], 'seasons': [{'season_number': 1}]}
        queries = {'法证先锋三': [],
                   '法证先锋3': [dict(third)]}
        store, http, engine = make(tmp_path, queries, {'38180': third})
        hint = {'query': '法证先锋三', 'year': None, 'media_type': 'tv',
                'season': 1, 'episode': 1, 'end_episode': 30, 'tmdb_id': None}
        details = await engine.identify(hint)
        assert details['id'] == 38180
        assert hint['season'] == 1                      # 独立条目，季数保持
        await http.aclose()
        store.close()

    asyncio.run(run())


def test_missing_season_fails_safe_to_review(tmp_path):
    async def run():
        only_one = {'id': 4114, 'name': '某剧', 'first_air_date': '2006-04-03',
                    'original_name': '法证先锋', 'genres': [], 'seasons': [{'season_number': 1}]}
        queries = {'某剧三': [],
                   '某剧3': [],
                   '某剧': [dict(only_one, id=4114, name='某剧')]}
        store, http, engine = make(tmp_path, queries)
        hint = {'query': '某剧三', 'year': None, 'media_type': 'tv',
                'season': 1, 'episode': 1, 'end_episode': 1, 'tmdb_id': None}
        try:
            await engine.identify(hint)
            raise AssertionError('应当因季不存在而进待处理')
        except NeedsReview:
            pass
        await http.aclose()
        store.close()

    asyncio.run(run())


def test_exact_match_never_reaches_the_fallback(tmp_path):
    async def run():
        exact = [{'id': 4114, 'name': '法证先锋', 'first_air_date': '2006-04-03',
                  'original_name': '法证先锋'}]
        queries = {'法证先锋': exact}
        store, http, engine = make(tmp_path, queries)
        hint = {'query': '法证先锋', 'year': None, 'media_type': 'tv',
                'season': 1, 'episode': 1, 'end_episode': 1, 'tmdb_id': None}
        details = await engine.identify(hint)
        assert details['id'] == 4114
        await http.aclose()
        store.close()

    asyncio.run(run())
