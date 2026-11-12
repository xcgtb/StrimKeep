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
