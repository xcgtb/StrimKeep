"""唯一命中 + 年份一致兜底：候选数、年份双重闸门，不满足就安全失败。"""
import asyncio

import httpx

from strimkeep.media import NeedsReview
from strimkeep.organizer import Organizer
from strimkeep.store import Store
from test_organize import MemoryCloud


def make(tmp_path, queries):
    def handler(request):
        url = str(request.url)
        query = request.url.params.get('query')
        if query is not None:
            return httpx.Response(200, json={'results': queries.get(query, [])})
        # 详情请求 /tv/{id} 或 /movie/{id}：返回同 id 的候选
        import re as _re
        hit = _re.search(r'/(?:tv|movie)/(\d+)$', url.split('?')[0])
        if hit:
            for rows in queries.values():
                for row in rows:
                    if str(row['id']) == hit.group(1):
                        return httpx.Response(200, json=row)
        return httpx.Response(200, json={'results': []})

    store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
    store.set_many({'cookie': 'UID=1; CID=t; SEID=t', 'tmdb_key': 'k',
                    'playback_base': 'https://play.example'})
    cloud = MemoryCloud(store)
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    engine = Organizer(store, cloud, http, tmp_path / 'strm')
    return store, http, engine


HINT = {'query': 'Where the Mask Ends', 'year': 2026, 'media_type': 'tv',
        'season': 1, 'episode': 1, 'end_episode': 1, 'tmdb_id': None}
CAND = {'id': 300146, 'name': '嫁金钗', 'original_name': '嫁金钗',
        'first_air_date': '2026-10-16'}


def test_unique_match_with_exact_year_is_accepted(tmp_path):
    async def run():
        store, http, engine = make(tmp_path, {'Where the Mask Ends': [dict(CAND)]})
        details = await engine.identify(dict(HINT))
        assert details['id'] == 300146
        await http.aclose()
        store.close()

    asyncio.run(run())


def test_year_mismatch_still_fails_safe(tmp_path):
    async def run():
        cand = dict(CAND, first_air_date='2025-11-01')     # 同名不同年 → 拒绝
        store, http, engine = make(tmp_path, {'Where the Mask Ends': [cand]})
        try:
            await engine.identify(dict(HINT))
            raise AssertionError('年份不一致应当进待处理')
        except NeedsReview:
            pass
        await http.aclose()
        store.close()

    asyncio.run(run())


def test_two_candidates_still_fails_safe(tmp_path):
    async def run():
        cand2 = dict(CAND, id=999999, name='另一部同名剧')
        store, http, engine = make(tmp_path, {'Where the Mask Ends': [dict(CAND), cand2]})
        try:
            await engine.identify(dict(HINT))
            raise AssertionError('多候选应当进待处理')
        except NeedsReview:
            pass
        await http.aclose()
        store.close()

    asyncio.run(run())


def test_normal_exact_match_is_untouched(tmp_path):
    async def run():
        exact = [{'id': 4114, 'name': '法证先锋', 'original_name': '法证先锋',
                  'first_air_date': '2006-04-03'}]
        store, http, engine = make(tmp_path, {'法证先锋': exact})
        hint = {'query': '法证先锋', 'year': 2006, 'media_type': 'tv',
                'season': 1, 'episode': 1, 'end_episode': 1, 'tmdb_id': None}
        details = await engine.identify(hint)
        assert details['id'] == 4114                     # 走原有精确路径，行为不变
        await http.aclose()
        store.close()

    asyncio.run(run())
