"""Bound cache loads, preserve bulk semantics, and keep retry waits finite."""
from app import state_store as _state
import io
import json
import time
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import pytest
from app import engine, morning, tmdb


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, 'STATE_DIR', tmp_path)
    monkeypatch.setattr(engine, 'RUNTIME_CFG', {'tmdb_key': 'temporary-key'})
    monkeypatch.setattr(engine, 'Tmdb', tmdb.Tmdb)
    monkeypatch.setattr(engine, 'TMDB_LANG', 'zh-CN')
    monkeypatch.setattr(engine, 'TMDB_INFO_TTL', 3600)
    calls = []
    def network(request, timeout):
        calls.append((request.full_url, timeout))
        return io.BytesIO(json.dumps({'id': request.full_url.split('?')[0].rsplit('/', 1)[-1]}).encode())
    monkeypatch.setattr(tmdb.urllib.request, 'urlopen', network)
    return SimpleNamespace(path=tmp_path/'tmdb_cache.json', calls=calls)


def row(data=None, age=0):
    return {'ts': time.time()-age, 'data': data or {'id': 'temporary'}}


def test_cache_only_bulk_loads_the_file_once_and_constructs_no_workers(env):
    _state.save(env.path, {f'/tv/{i}?language=zh-CN': row({'id': i}) for i in range(1, 21)})
    reads=[]; original=_state.read
    def read(p, *a, **kw):
        if p == env.path: reads.append(p)
        return original(p, *a, **kw)
    with patch.object(_state, 'read', read):
        parent = tmdb.Tmdb()
        with patch.object(engine, 'Tmdb', side_effect=AssertionError('workers must not reload cache')):
            info, errors, rows = morning._tmdb_bulk_get(parent, [('tv', i) for i in range(1, 21)])
    assert errors == 0 and len(info) == len(rows) == 20
    assert len(reads) == 1 and not env.calls
    assert parent.hits == 20


def test_mixed_bulk_reads_once_requests_only_expired_or_missing_and_keeps_typed_ids(env):
    _state.save(env.path, {'/tv/1?language=zh-CN': row({'id': 'fresh'}),
                                   '/tv/2?language=zh-CN': row({'id': 'old'}, age=7200)})
    reads=[]; original=_state.read
    def read(p, *a, **kw):
        if p == env.path: reads.append(p)
        return original(p, *a, **kw)
    with patch.object(_state, 'read', read):
        parent=tmdb.Tmdb()
        info, errors, rows=morning._tmdb_bulk_get(parent, [('tv', 1), ('tv', '1'), ('tv', 2), ('movie', 1), ('tv', '')], max_workers=2)
    assert errors == 0 and set(info) == {('tv', '1'), ('tv', '2'), ('movie', '1')}
    assert info[('tv', '1')]['id'] == 'fresh' and len(env.calls) == 2
    assert len(reads) == 1 and parent.hits == 1 and parent.calls == 2
    assert all(isinstance(k, str) for k in rows)


def test_one_failed_query_does_not_discard_cached_or_successful_rows(env, monkeypatch):
    _state.save(env.path, {'/tv/1?language=zh-CN': row({'id': 1})})
    def network(req, timeout):
        if '/tv/2?' in req.full_url: raise urllib.error.HTTPError(req.full_url, 401, 'bad key', {}, None)
        return io.BytesIO(b'{"id": 3}')
    monkeypatch.setattr(tmdb.urllib.request, 'urlopen', network)
    info, errors, rows=morning._tmdb_bulk_get(tmdb.Tmdb(), [('tv', 1), ('tv', 2), ('tv', 3)], max_workers=2)
    assert errors == 1 and info[('tv', '2')] is None
    assert info[('tv', '1')] == {'id': 1} and info[('tv', '3')] == {'id': 3}
    assert '/tv/2?language=zh-CN' not in rows


def test_overlapping_clients_save_without_losing_newer_cache_rows(env):
    _state.save(env.path, {})
    first=tmdb.Tmdb(); second=tmdb.Tmdb()
    first.get('/tv/1'); second.get('/tv/2')
    first.save(); second.save()
    assert set(_state.read(env.path)) == {'/tv/1?language=zh-CN', '/tv/2?language=zh-CN'}


def test_save_streams_json_instead_of_building_a_whole_second_string(env):
    _state.save(env.path, {}); parent=tmdb.Tmdb(); parent.get('/tv/1')
    original=_state.json.dumps; sizes=[]
    def encode(value,*a,**kw):
        sizes.append(len(value) if isinstance(value,dict) else 0)
        return original(value,*a,**kw)
    with patch.object(_state.json, 'dumps', encode): parent.save()
    assert max(sizes) <= 2
    assert _state.read(env.path)['/tv/1?language=zh-CN']['data'] == {'id': '1'}


def test_large_retry_after_stops_without_a_long_sleep(env, monkeypatch):
    waits=[]
    monkeypatch.setattr(tmdb.time, 'sleep', lambda seconds: waits.append(seconds))
    monkeypatch.setattr(tmdb.urllib.request, 'urlopen', lambda req, timeout: (_ for _ in ()).throw(
        urllib.error.HTTPError(req.full_url, 429, 'rate limited', {'Retry-After': '3600'}, None)))
    with pytest.raises(tmdb.TmdbError, match='限流'):
        tmdb.Tmdb().get('/tv/1')
    assert waits == []


def test_total_retry_budget_is_not_restarted_per_attempt(env, monkeypatch):
    clock=[0.0]; attempts=[]; waits=[]
    monkeypatch.setattr(tmdb.time, 'monotonic', lambda: clock[0])
    def sleep(seconds): clock[0]+=seconds; waits.append(seconds)
    monkeypatch.setattr(tmdb.time, 'sleep', sleep)
    def network(req, timeout):
        attempts.append(timeout); clock[0]+=timeout
        raise urllib.error.URLError('timeout')
    monkeypatch.setattr(tmdb.urllib.request, 'urlopen', network)
    with pytest.raises(tmdb.TmdbError): tmdb.Tmdb().get('/tv/1')
    assert clock[0] <= 45 and len(attempts) <= 3 and attempts[-1] < 15


def test_retry_after_http_date_does_not_turn_into_an_early_retry(env, monkeypatch):
    from email.utils import formatdate
    waits=[]
    monkeypatch.setattr(tmdb.time, 'sleep', lambda seconds: waits.append(seconds))
    retry_date=formatdate(time.time()+3600, usegmt=True)
    monkeypatch.setattr(tmdb.urllib.request, 'urlopen', lambda req, timeout: (_ for _ in ()).throw(
        urllib.error.HTTPError(req.full_url, 429, 'rate limited', {'Retry-After': retry_date}, None)))
    with pytest.raises(tmdb.TmdbError, match='限流'):
        tmdb.Tmdb().get('/tv/1')
    assert waits == []


def test_bulk_respects_a_maximum_of_eight_network_workers(env, monkeypatch):
    import threading
    lock=threading.Lock(); active=[0]; peak=[0]
    def network(req, timeout):
        with lock:
            active[0]+=1; peak[0]=max(peak[0], active[0])
        try:
            time.sleep(0.02)
            return io.BytesIO(b'{"id": 1}')
        finally:
            with lock: active[0]-=1
    monkeypatch.setattr(tmdb.urllib.request, 'urlopen', network)
    result, errors, rows=morning._tmdb_bulk_get(tmdb.Tmdb(), [('tv', i) for i in range(1, 13)], max_workers=32)
    assert errors == 0 and len(result) == 12 and 1 < peak[0] <= 8


def test_save_preserves_external_newer_entry_and_existing_expiration_rule(env):
    _state.save(env.path, {'/tv/old?language=zh-CN': row(age=90000)})
    parent=tmdb.Tmdb(); parent.get('/tv/1')
    newer=row({'id': 'newer'}, age=-1)
    _state.save(env.path, {'/tv/1?language=zh-CN': newer})
    parent.save()
    saved=_state.read(env.path)
    assert saved['/tv/1?language=zh-CN']['data']['id'] == 'newer'
    assert '/tv/old?language=zh-CN' not in saved
