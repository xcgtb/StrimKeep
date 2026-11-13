"""目录清单缓存：同一任务内重复读同一目录只发一次请求，写操作后必须失效。"""
import asyncio

import pytest

from strimkeep.cloud115 import Cloud115
from strimkeep.store import Store


def make(tmp_path):
    store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
    store.set_many({'cookie': 'UID=1_A1; CID=test; SEID=test'})
    return store, Cloud115(store)


def test_repeated_children_read_once(tmp_path):
    async def run():
        store, cloud = make(tmp_path)
        calls = []

        async def fake_children(cid, page_size=200):
            calls.append(str(cid))
            return ([{'id': '1', 'name': 'a', 'is_dir': True}], 1)

        cloud._children = fake_children
        first = await cloud.children('9')
        second = await cloud.children('9')
        assert first == second
        assert calls == ['9'], '同一目录第二次应从缓存取'
        await cloud.close()
        store.close()
    asyncio.run(run())


def test_write_clears_the_cache(tmp_path):
    async def run():
        store, cloud = make(tmp_path)
        calls = []

        async def fake_children(cid, page_size=200):
            calls.append(str(cid))
            return ([], 0)

        cloud._children = fake_children
        await cloud.children('9')
        assert len(calls) == 1

        async def fake_write(*args, **kwargs):
            return {'state': True}

        await cloud.call(fake_write, _kind='write')
        assert cloud._children_memo == {}, '写操作必须清空清单缓存'

        await cloud.children('9')
        assert len(calls) == 2, '写完之后必须重新读'
        await cloud.close()
        store.close()
    asyncio.run(run())

def test_write_without_directory_effect_keeps_the_cache(tmp_path):
    """离线提交、清空回收站这类写不改目录清单：不该把整棵缓存清空。"""
    async def run():
        store, cloud = make(tmp_path)
        calls = []

        async def fake_children(cid, page_size=200):
            calls.append(str(cid))
            return [], 0

        cloud._children = fake_children
        await cloud.children('9')

        async def fake_write(*args, **kwargs):
            return {'state': True}

        await cloud.call(fake_write, _kind='write', _parents=())
        assert cloud._children_memo, '与目录无关的写不应清缓存'
        await cloud.children('9')
        assert len(calls) == 1, '缓存仍然可用，不应重新读'
        await cloud.close()
        store.close()
    asyncio.run(run())


def test_targeted_write_forgets_only_the_affected_directory(tmp_path):
    async def run():
        store, cloud = make(tmp_path)
        async def fake_children(cid, page_size=200):
            return [], 0
        cloud._children = fake_children
        await cloud.children('9')
        await cloud.children('10')
        store_ids = {key[1] for key in cloud._children_memo}
        assert store_ids == {'9', '10'}

        async def fake_write(*args, **kwargs):
            return {'state': True}

        await cloud.call(fake_write, _kind='write', _parents=('9',))
        assert {key[1] for key in cloud._children_memo} == {'10'}, '只该丢掉受影响的目录'
        await cloud.close()
        store.close()
    asyncio.run(run())


def test_rename_and_move_narrow_the_invalidation_scope(tmp_path):
    """改名/移动只失效「文件所在目录 + 目标目录」，不再整棵清空。"""
    async def run():
        store, cloud = make(tmp_path)
        async def fake_children(cid, page_size=200):
            if str(cid) == '9':
                return ([{'id': 'f1', 'name': 'a.mkv', 'is_dir': False}], 1)
            return ([{'id': 'other', 'name': 'b', 'is_dir': True}], 1)
        cloud._children = fake_children
        await cloud.children('9')
        await cloud.children('10')

        seen = {}

        async def fake_call(method, *args, **kwargs):
            seen.update(kwargs)
            return {'state': True}

        cloud.call = fake_call
        await cloud.rename('f1', 'b.mkv')
        assert seen['_parents'] == ('9',), seen
        await cloud.move('f1', '77')
        assert set(seen['_parents']) == {'9', '77'}, seen
        await cloud.close()
        store.close()
    asyncio.run(run())

def test_unknown_file_falls_back_to_full_clear(tmp_path):
    """缓存里找不到该文件归属时必须退回整棵清空：安全优先于省请求。"""
    async def run():
        store, cloud = make(tmp_path)

        async def fake_children(cid, page_size=200):
            return [], 0

        cloud._children = fake_children
        await cloud.children('9')

        seen = {}

        async def fake_call(method, *args, **kwargs):
            seen.update(kwargs)
            return {'state': True}

        cloud.call = fake_call
        await cloud.rename('not-in-any-listing', 'x.mkv')
        assert seen['_parents'] is None, '找不到归属时按未知范围处理'
        await cloud.close()
        store.close()
    asyncio.run(run())
