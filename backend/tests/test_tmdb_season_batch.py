import asyncio
import httpx
import pytest
from strimkeep.cloud115 import CloudError
from test_organize import setup


def test_eight_episodes_share_one_season_http_request(tmp_path):
    async def run():
        store, cloud, http, engine, requests = setup(tmp_path)
        episodes = [await engine.tmdb_episode(10, 1, n) for n in range(1, 9)]
        assert [e['episode_number'] for e in episodes] == list(range(1, 9))
        paths = [r.url.path for r in requests if r.url.host == 'api.themoviedb.org']
        assert paths == ['/3/tv/10/season/1'], paths
        await http.aclose(); store.close()
    asyncio.run(run())


@pytest.mark.parametrize('broken', ['duplicate', 'wrong_season', 'missing_field', 'missing_episode'])
def test_unreliable_season_falls_back_to_individual_episode(tmp_path, broken):
    async def run():
        store, cloud, http, engine, _ = setup(tmp_path)
        calls=[]
        item = {'id':101,'season_number':1,'episode_number':1,'name':'第一集', 'overview':'简介','air_date':'2024-01-01'}
        async def tmdb(path, params=None):
            calls.append(path)
            if path.endswith('/episode/1'): return item
            if broken == 'duplicate': return {'season_number':1, 'episodes':[item,item.copy()]}
            if broken == 'wrong_season': return {'season_number':2, 'episodes':[item]}
            if broken == 'missing_field': return {'season_number':1, 'episodes':[{k:v for k,v in item.items() if k!='overview'}]}
            return {'season_number':1, 'episodes':[]}
        engine.tmdb=tmdb
        assert await engine.tmdb_episode(10,1,1) == item
        assert calls == ['tv/10/season/1','tv/10/season/1/episode/1']
        await http.aclose(); store.close()
    asyncio.run(run())


def test_season_failure_falls_back_without_repeating_season_requests(tmp_path):
    async def run():
        store, cloud, http, engine, _ = setup(tmp_path)
        calls=[]
        async def tmdb(path, params=None):
            calls.append(path)
            if '/episode/' not in path: raise CloudError('season unavailable','TMDB_UNAVAILABLE')
            n=int(path.rsplit('/',1)[-1]);return {'season_number':1,'episode_number':n}
        engine.tmdb=tmdb
        assert (await engine.tmdb_episode(10,1,1))['episode_number']==1
        assert (await engine.tmdb_episode(10,1,2))['episode_number']==2
        assert calls == ['tv/10/season/1','tv/10/season/1/episode/1','tv/10/season/1/episode/2']
        await http.aclose();store.close()
    asyncio.run(run())

def test_non_tmdb_cloud_errors_are_not_hidden_by_season_fallback(tmp_path):
    async def run():
        store, cloud, http, engine, _ = setup(tmp_path)
        calls=[]
        async def fail(path, params=None):
            calls.append(path)
            raise CloudError('limited','RATE_LIMITED')
        engine.tmdb=fail
        with pytest.raises(CloudError) as exc:
            await engine.tmdb_episode(10,1,1)
        assert exc.value.code == 'RATE_LIMITED' and calls == ['tv/10/season/1']
        await http.aclose();store.close()
    asyncio.run(run())
