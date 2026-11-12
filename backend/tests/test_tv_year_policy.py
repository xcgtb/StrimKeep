"""剧集年份口径对齐 ：搜索不带年份，首播年 / 季年份 / 该集播出年任一命中即可。"""
import asyncio

import pytest

from strimkeep.media import NeedsReview, title_candidates
from strimkeep.organizer import Organizer


class FakeTMDB:
    def __init__(self, search=None, details=None, episodes=None):
        self.search = search or {}
        self.details = details or {}
        self.episodes = episodes or {}
        self.calls = []

    async def __call__(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        if path.startswith('search/'):
            return {'results': self.search.get(path.split('/')[1], [])}
        if '/episode/' in path:
            return self.episodes.get(path, {})
        if path.endswith('/alternative_titles'):
            return {'results': []}
        ident = int(path.split('/')[1])
        return self.details.get(ident, {'id': ident})


def engine_with(fake):
    engine = Organizer.__new__(Organizer)
    engine.tmdb = fake
    return engine


def tv_hint(**over):
    hint = {'query': 'Dynasty', 'year': 2024, 'media_type': 'tv', 'season': 5, 'episode': 1,
            'end_episode': 1, 'tmdb_id': None, 'identity_source': 'filename'}
    hint.update(over)
    return hint


def test_new_season_of_an_old_show_matches_by_season_year():
    """2017 年的剧、2024 年播第五季：文件名年份 2024 不该被判「年份不符」。"""
    async def run():
        fake = FakeTMDB(
            search={'tv': [
                {'id': 3769, 'name': 'Dynasty', 'first_air_date': '1981-01-12'},
                {'id': 72002, 'name': 'Dynasty', 'first_air_date': '2017-10-11'},
            ]},
            details={
                3769: {'id': 3769, 'first_air_date': '1981-01-12', 'seasons': [{'season_number': 1, 'air_date': '1981-01-12'}]},
                72002: {'id': 72002, 'first_air_date': '2017-10-11',
                        'seasons': [{'season_number': 1, 'air_date': '2017-10-11'},
                                    {'season_number': 5, 'air_date': '2024-03-01'}]},
            })
        details = await engine_with(fake).identify(tv_hint())
        assert details['id'] == 72002
        search_params = [p for path, p in fake.calls if path == 'search/tv'][0]
        assert 'first_air_date_year' not in search_params, '剧集搜索不应带年份'
        store = fake.calls
        assert any(pth.startswith('tv/72002') for pth, _ in store)
    asyncio.run(run())


def test_episode_air_year_is_evidence_too():
    async def run():
        fake = FakeTMDB(
            search={'tv': [{'id': 9, 'name': 'Some Show', 'first_air_date': '2015-04-01'}]},
            details={9: {'id': 9, 'first_air_date': '2015-04-01',
                         'seasons': [{'season_number': 1, 'air_date': '2015-04-01'}]}},
            episodes={'tv/9/season/2/episode/3': {'air_date': '2021-05-01',
                                                  'season_number': 2, 'episode_number': 3}},
        )
        details = await engine_with(fake).identify(tv_hint(query='Some Show', year=2021, season=2, episode=3))
        assert details['id'] == 9
    asyncio.run(run())


def test_tv_year_mismatch_rejects_even_if_title_matches():
    """对齐：剧集给了年份，但首播/季/集播出年都不符 → 拒绝匹配（不是按标题硬选）。"""
    async def run():
        fake = FakeTMDB(
            search={'tv': [{'id': 9, 'name': 'Some Show', 'first_air_date': '2015-04-01'}]},
            details={9: {'id': 9, 'first_air_date': '2015-04-01',
                         'seasons': [{'season_number': 1, 'air_date': '2015-04-01'}]}},
        )
        with pytest.raises(NeedsReview) as exc:
            await engine_with(fake).identify(tv_hint(query='Some Show', year=2022, season=3, episode=1))
        assert '未找到唯一匹配' in str(exc.value)
    asyncio.run(run())


def test_two_same_title_shows_pick_highest_score():
    """对齐：同名同年的多部剧不再转待处理，按 (年份档, 标题分, 中文, 热度) 取最高。"""
    async def run():
        fake = FakeTMDB(
            search={'tv': [{'id': 1, 'name': 'Twin', 'first_air_date': '2024-01-01'},
                           {'id': 2, 'name': 'Twin', 'first_air_date': '2024-06-01'}]},
            details={1: {'id': 1, 'first_air_date': '2024-01-01', 'seasons': []},
                     2: {'id': 2, 'first_air_date': '2024-06-01', 'seasons': []}},
        )
        details = await engine_with(fake).identify(tv_hint(query='Twin', year=2024, season=1, episode=1))
        assert details['id'] in (1, 2)
    asyncio.run(run())


def test_movie_search_still_passes_the_year():
    async def run():
        fake = FakeTMDB(search={'movie': [{'id': 5, 'title': 'A Film', 'release_date': '2024-02-02'}]},
                        details={5: {'id': 5, 'title': 'A Film', 'release_date': '2024-02-02'}})
        details = await engine_with(fake).identify(
            {'query': 'A Film', 'year': 2024, 'media_type': 'movie', 'tmdb_id': None})
        assert details['id'] == 5
        params = [p for path, p in fake.calls if path == 'search/movie'][0]
        assert params.get('year') == 2024
    asyncio.run(run())


def test_attempts_are_recorded_on_the_plan_evidence():
    async def run():
        fake = FakeTMDB(search={'tv': [{'id': 72002, 'name': 'Dynasty', 'first_air_date': '2017-10-11'}]},
                        details={72002: {'id': 72002, 'first_air_date': '2017-10-11',
                                         'seasons': [{'season_number': 5, 'air_date': '2024-03-01'}]}})
        hint = tv_hint()
        await engine_with(fake).identify(hint)
        assert hint['tmdb_attempt'][0]['endpoint'] == 'search/tv'
        assert hint['tmdb_attempt'][0]['candidates'] == 1
        recognition = Organizer.recognition(hint, {'id': 72002, 'name': 'Dynasty'}, {})
        assert recognition['tmdb_attempt'][0]['endpoint'] == 'search/tv'
        assert 'tmdb_attempt' not in hint, '证据链要落进 plan，不要留在 hint 里'
    asyncio.run(run())


def test_title_candidates_ignores_year():
    hint = {'query': 'Dynasty', 'year': 2024}
    results = [{'id': 1, 'name': 'Dynasty', 'first_air_date': '1981-01-12'},
               {'id': 2, 'name': 'Dynasty', 'first_air_date': '2017-10-11'}]
    assert [row['id'] for row in title_candidates(hint, results)] == [1, 2]


def test_episode_year_tmdb_outage_is_not_treated_as_year_mismatch():
    from strimkeep.cloud115 import CloudError
    async def run():
        fake = FakeTMDB(search={'tv':[{'id':9,'name':'Some Show','first_air_date':'2015-01-01'}]},
                        details={9:{'id':9,'first_air_date':'2015-01-01','seasons':[]}})
        async def outage(path, params=None):
            if '/episode/' in path:
                raise CloudError('TMDB unavailable','TMDB_UNAVAILABLE')
            return await fake(path,params)
        with pytest.raises(CloudError) as exc:
            await engine_with(outage).identify(tv_hint(query='Some Show',year=2022,season=3,episode=1))
        assert exc.value.code=='TMDB_UNAVAILABLE'
    asyncio.run(run())


def test_missing_candidate_episode_is_year_mismatch_not_service_failure():
    from strimkeep.cloud115 import CloudError
    async def run():
        async def missing(path,params=None):
            if '/episode/' in path:raise CloudError('TMDB 请求失败（HTTP 404）。','TMDB_UNAVAILABLE',404)
            return {'id':9,'first_air_date':'2019-01-01','seasons':[]}
        engine=engine_with(missing)
        ok,reason=await engine.year_ok('tv',{'id':9,'first_air_date':'2019-01-01'},tv_hint(year=2026,season=1,episode=5))
        assert not ok and reason=='episode_not_found'
    asyncio.run(run())

def test_tmdb_http_404_retains_status_for_safe_candidate_handling(tmp_path):
    import httpx
    from strimkeep.cloud115 import CloudError
    from test_organize import setup
    async def run():
        store,cloud,http,engine,_=setup(tmp_path)
        async def handler(request):return httpx.Response(404,json={'status_code':34},request=request)
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        engine.http=client
        with pytest.raises(CloudError) as exc:
            await engine.tmdb('tv/999/season/1/episode/5')
        # 404 是「这个条目 TMDB 里没有」，不是「TMDB 用不了」：前者进未识别、
        # 后者让任务挂起等重试。code 区分开，status 仍保留 404 供上层判断。
        assert exc.value.code=='TMDB_NOT_FOUND' and exc.value.status==404
        assert 'HTTP 404' in str(exc.value)
        await client.aclose();await http.aclose();store.close()
    asyncio.run(run())


def test_missing_episode_on_one_candidate_does_not_block_valid_candidate():
    from strimkeep.cloud115 import CloudError
    async def run():
        async def fake(path,params=None):
            if path=='tv/9':return {'id':9,'first_air_date':'2019-01-01','seasons':[]}
            if path=='tv/9/season/1/episode/5':raise CloudError('not found','TMDB_UNAVAILABLE',404)
            raise AssertionError(path)
        engine=engine_with(fake);attempts=[]
        hint=tv_hint(query='Love in Disguise',year=2026,season=1,episode=5)
        candidates=[{'id':9,'name':'Love in Disguise','first_air_date':'2019-01-01','popularity':8},
                    {'id':10,'name':'Love in Disguise','first_air_date':'2026-01-01','popularity':8}]
        assert await engine.pick('tv',hint,candidates,attempts)==10
        assert any(a.get('evidence')=='episode_not_found' for a in attempts)
    asyncio.run(run())
