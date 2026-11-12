"""TMDB 缺集不该拦住归档。

背景（0.9.34）：TMDB 里「十三种爱情」只登记了 1 集，实际播出 8 集。
单集详情 404 时应该按文件名里的集号兜底归档（命名本来就不看 TMDB 集名），
而不是整条转人工。
"""
import asyncio
import sqlite3

import pytest

from strimkeep.cloud115 import CloudError
from strimkeep.organizer import Organizer


class Store:
    def __init__(self, conn):
        self.db = conn


def engine_on():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.executescript('CREATE TABLE tmdb_cache(key TEXT PRIMARY KEY, body TEXT, updated REAL);')
    engine = Organizer.__new__(Organizer)
    engine.store = Store(conn)
    engine._season_lookup_unavailable = {}
    engine.TMDB_EPISODE_SECONDS = 3600

    async def tmdb(path, params=None):
        raise CloudError('TMDB 中找不到该条目（HTTP 404）。', 'TMDB_NOT_FOUND', 404)

    engine.tmdb = tmdb
    return engine


def test_missing_episode_returns_none_so_the_file_can_still_be_archived():
    engine = engine_on()
    assert asyncio.run(engine.tmdb_episode(341065, 1, 2)) is None


def test_other_tmdb_errors_still_raise():
    engine = engine_on()

    async def boom(path, params=None):
        raise CloudError('TMDB 请求失败（HTTP 502）。', 'TMDB_UNAVAILABLE', 502)

    engine.tmdb = boom
    with pytest.raises(CloudError):
        asyncio.run(engine.tmdb_episode(341065, 1, 2))
