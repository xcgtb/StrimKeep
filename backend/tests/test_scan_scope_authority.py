"""扫描范围唯一授权：落盘只负责落盘，能否整理只看目录是否在扫描范围。"""
import asyncio

from strimkeep.organizer import Organizer
from strimkeep.store import Store


class FakeCloud:
    def __init__(self, chains):
        self.chains = chains

    async def directory(self, cid):
        if str(cid) not in self.chains:
            raise RuntimeError('目录不存在')
        return self.chains[str(cid)]


def engine_with(tmp_path, source_cids, chains):
    store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
    if source_cids is None:
        store.set_many({'organize_config': {}})
    else:
        store.set_many({'organize_config': {'source_cids': source_cids}})
    engine = Organizer.__new__(Organizer)
    engine.store = store
    engine.cloud = FakeCloud(chains)
    return store, engine


def test_scope_allows_root_and_child(tmp_path):
    """扫描根本身、以及它的子目录都算授权。"""
    async def run():
        store, engine = engine_with(tmp_path, ['100'], {
            '100': ['0', '100'],
            '120': ['0', '100', '120'],
            '200': ['0', '200'],
        })
        assert await engine.scan_scope_allows(['100']) is True    # 扫描根
        assert await engine.scan_scope_allows(['120']) is True    # 待整理/电影 这类子目录
        assert await engine.scan_scope_allows(['200']) is False   # AV资源 / 普通资源
        assert await engine.allowed_scan_cids(['100', '200']) == ['100']
        assert await engine.allowed_scan_cids(['200']) == []
        store.close()
    asyncio.run(run())


def test_scope_rejects_when_no_config(tmp_path):
    """没配置扫描目录时一律不授权（避免"没配置就整盘扫"）。"""
    async def run():
        store, engine = engine_with(tmp_path, [], {'100': ['0', '100']})
        assert await engine.scan_scope_allows(['100']) is False
        store.close()
    asyncio.run(run())


def test_scope_rejects_unreadable_directory(tmp_path):
    """读不到目录链时不确认、不授权。"""
    async def run():
        store, engine = engine_with(tmp_path, ['100'], {})
        assert await engine.scan_scope_allows(['120']) is False
        assert await engine.allowed_scan_cids(['120']) == []
        store.close()
    asyncio.run(run())


def test_scope_handles_dict_chain(tmp_path):
    """真实 cloud115 的 directory() 可能返回 dict 链，也要能识别。"""
    async def run():
        store, engine = engine_with(tmp_path, ['100'], {
            '120': [{'id': '0'}, {'id': '100'}, {'id': '120'}],
        })
        assert await engine.scan_scope_allows(['120']) is True
        store.close()
    asyncio.run(run())
