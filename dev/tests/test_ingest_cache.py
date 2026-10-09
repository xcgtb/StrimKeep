"""入库缓存：分页、失败时保留旧缓存、不把残缺结果当成「0 新增」。"""
import json
import os
import sys
import tempfile
from pathlib import Path

_tmp = tempfile.mkdtemp()
os.environ.setdefault('AGENT_DATA', _tmp)
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'app'))

import engine  # noqa: E402


def _use_tmp_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(engine, 'STATE_DIR', tmp_path)
    monkeypatch.setattr(engine, 'INGEST_CACHE_FILE', tmp_path / 'ingest_cache.json')


def _item(i, kind='Movie'):
    d = {'Name': f'n{i}', 'Path': f'/strm/x/{i}.strm', 'DateCreated': '2999-01-01T00:00:00.0000000Z', 'Genres': []}
    if kind == 'Episode':
        d['SeriesName'] = f's{i % 3}'
    return d


def test_paged_items_follows_total(monkeypatch):
    calls = []

    def fake(path, params=None, **kw):
        calls.append(params['StartIndex'])
        start = params['StartIndex']
        return {'Items': [{'i': k} for k in range(start, min(start + params['Limit'], 2500))], 'TotalRecordCount': 2500}

    monkeypatch.setattr(engine, 'emby_request', fake)
    items = engine._paged_items({}, page_size=1000)
    assert len(items) == 2500 and calls == [0, 1000, 2000]


def test_fetch_failure_keeps_old_cache(monkeypatch, tmp_path):
    _use_tmp_cache(monkeypatch, tmp_path)
    good = {'ts': 1.0, 'ok': True, 'stats': {'movies': 7, 'series': 2, 'episodes': 30}, 'tree': {}}
    (tmp_path / 'ingest_cache.json').write_text(json.dumps(good))

    def boom(*a, **k):
        raise TimeoutError('timed out')

    monkeypatch.setattr(engine, 'emby_request', boom)
    out = engine.refresh_ingest_cache()
    assert out['stats']['movies'] == 7
    assert 'timed out' in out['stale_error']
    on_disk = json.loads((tmp_path / 'ingest_cache.json').read_text())
    assert on_disk['stats']['movies'] == 7 and on_disk['ts'] == 1.0


def test_fetch_failure_without_old_cache_marks_not_ok(monkeypatch, tmp_path):
    _use_tmp_cache(monkeypatch, tmp_path)
    monkeypatch.setattr(engine, 'emby_request', lambda *a, **k: (_ for _ in ()).throw(TimeoutError('timed out')))
    out = engine.refresh_ingest_cache()
    assert out['ok'] is False
    # 残缺缓存不能被 get_ingest 当成「新鲜缓存」直接返回
    monkeypatch.setattr(engine, '_fetch_ingest', lambda hours=24: {'ts': 2.0, 'ok': True, 'stats': {}, 'tree': {}})
    assert engine.get_ingest()['ts'] == 2.0


def test_success_overwrites_cache(monkeypatch, tmp_path):
    _use_tmp_cache(monkeypatch, tmp_path)

    def fake(path, params=None, **kw):
        kind = params['IncludeItemTypes']
        return {'Items': [_item(i, kind) for i in range(3)], 'TotalRecordCount': 3}

    monkeypatch.setattr(engine, 'emby_request', fake)
    out = engine.refresh_ingest_cache()
    assert out['ok'] is True and out['stats']['movies'] == 3 and out['stats']['episodes'] == 3
