"""大库相关的两处成本：TMDB 重复请求、目录核对不能额外发请求。"""
import asyncio

import httpx

from strimkeep.cloud115 import Cloud115, CloudError
from strimkeep.organizer import Organizer
from strimkeep.store import Store
from test_organize import MemoryCloud


def make_engine(tmp_path, handler):
    store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
    store.set_many({'cookie': 'UID=1; CID=t; SEID=t', 'tmdb_key': 'k',
                    'playback_base': 'http://nas:9000'})
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return store, Organizer(store, MemoryCloud(store), http, tmp_path / 'strm'), http


def test_tmdb_details_and_episodes_are_cached(tmp_path):
    async def run():
        seen = []

        def handler(request):
            seen.append(str(request.url))
            return httpx.Response(200, json={'id': 1, 'title': 'X'})

        store, engine, http = make_engine(tmp_path, handler)
        first = await engine.tmdb('movie/1')
        second = await engine.tmdb('movie/1')
        assert first == second == {'id': 1, 'title': 'X'}
        assert len(seen) == 1
        # 集信息缓存更久，但参数不同就是另一条缓存。
        await engine.tmdb('tv/1/season/1/episode/1')
        await engine.tmdb('tv/1/season/1/episode/1')
        assert len(seen) == 2
        # 语言参与缓存键：同一部片的中英文各存一份（调用方可以按需取不同语言）。
        await engine.tmdb('movie/1', {'language': 'en-US'})
        assert len(seen) == 3
        assert store.db.execute('SELECT count(*) FROM tmdb_cache').fetchone()[0] == 3
        await http.aclose()
        store.close()

    asyncio.run(run())


def test_children_verified_reads_count_from_pages_without_extra_call():
    async def run():
        cloud = object.__new__(Cloud115)
        calls = []

        async def list_files(cid, offset, limit):
            calls.append(offset)
            if offset == 0:
                return {'items': [{'id': '1', 'name': 'a', 'is_dir': False, 'size': 1,
                                   'pickcode': 'p', 'sha1': ''}],
                        'has_more': True, 'raw_count': 1}
            return {'items': [], 'has_more': False, 'raw_count': 1}

        cloud.list_files = list_files
        items = await cloud.children_verified('9')
        assert [x['id'] for x in items] == ['1']
        assert calls == [0, 1]

    asyncio.run(run())


def test_children_verified_rejects_truncated_listing():
    async def run():
        cloud = object.__new__(Cloud115)
        calls = []

        async def list_files(cid, offset, limit):
            calls.append(offset)
            if offset == 0:
                return {'items': [{'id': '1', 'name': 'a', 'is_dir': False, 'size': 1,
                                   'pickcode': 'p', 'sha1': ''}],
                        'has_more': True, 'raw_count': 5}
            return {'items': [], 'has_more': False, 'raw_count': 5}

        cloud.list_files = list_files
        try:
            await cloud.children_verified('9')
        except CloudError as exc:
            assert exc.code == 'INCOMPLETE_LIST' and '清单不完整' in str(exc)
        else:
            raise AssertionError('截断的清单必须被拒绝')
        assert calls == [0, 1, 0, 1]

    asyncio.run(run())


def test_repeated_terminal_web_page_restarts_with_app_and_checks_all_items():
    async def run():
        cloud=object.__new__(Cloud115)
        web_calls=[]; app_calls=[]
        def item(value):return {'id':str(value),'name':str(value),'is_dir':False}
        async def web(cid,offset,limit):
            web_calls.append(offset)
            pages={0:[item(1),item(2)],2:[item(3)],3:[item(1),item(2)]}
            return {'items':pages[offset], 'raw_count':3, 'has_more':True}
        async def app(cid,offset,limit):
            app_calls.append(offset)
            pages={0:[item(1),item(2)],2:[item(3)]}
            return {'items':pages[offset], 'raw_count':3, 'has_more':offset==0}
        cloud.list_files=web; cloud.list_files_android=app
        items=await cloud.children_verified('9')
        assert [x['id'] for x in items]==['1','2','3']
        assert web_calls==[0,2,3] and app_calls==[0,2]
    asyncio.run(run())
