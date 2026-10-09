# -*- coding: utf-8 -*-
"""回归：TMDB 对照失败 "keys must be str, int, float, bool or None, not tuple"。

_tmdb_bulk_get 返回的 cache_rows 必须使用 Tmdb.cache 的字符串键，
否则 t.cache.update(cache_rows) 之后 t.save() 的 json.dumps 会抛 TypeError。
"""
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'app'))
import engine  # noqa: E402
import morning  # noqa: E402


class _FakeTmdb:
    def __init__(self):
        self.cache = {}

    def get(self, path, ttl=0, **params):
        ck = path + '?language=zh-CN'
        data = {'id': path.rsplit('/', 1)[-1], 'poster_path': '/p.jpg'}
        self.cache[ck] = {'ts': time.time(), 'data': data}
        return data


def test_bulk_get_cache_rows_have_string_keys_and_are_json_serializable(monkeypatch):
    monkeypatch.setattr(engine, 'Tmdb', _FakeTmdb)
    monkeypatch.setattr(engine, 'TMDB_INFO_TTL', 3600, raising=False)
    monkeypatch.setattr(engine, 'TMDB_LANG', 'zh-CN', raising=False)
    infos, errors, cache_rows = morning._tmdb_bulk_get(
        _FakeTmdb(), [('tv', 1), ('tv', '2'), ('tv', 1)], max_workers=2)
    assert errors == 0
    assert set(infos) == {('tv', '1'), ('tv', '2')}      # 返回的 infos 仍按 (kind, id) 取
    assert cache_rows and all(isinstance(k, str) for k in cache_rows)
    json.dumps(cache_rows)                                # 不得抛 TypeError
    t = _FakeTmdb()
    t.cache.update(cache_rows)
    json.dumps(t.cache)
