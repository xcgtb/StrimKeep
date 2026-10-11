"""Explore hot-path regression tests: never trade freshness for speed."""
import time
from types import SimpleNamespace

from app import explore_fast_facts as fast


def setup_function():
    fast._snapshot.update(key=None, created=0, view=None)


def test_reuses_index_without_copying_full_library_again(monkeypatch):
    reads = []
    data = {'ts': time.time(), 'stale': False, 'facts_version': 'a',
            'series': [dict(tmdb_id=str(i), id='s' + str(i), have_eps=2,
                            tmdb_info={'declared_total': 8, 'match_status': 'missing'},
                            paths=['private']) for i in range(5000)]}
    engine = SimpleNamespace(cached_library_view=lambda: reads.append(1) or data)
    token = ['v1']
    monkeypatch.setattr(fast, '_revision', lambda _: token[0])
    first = fast.lookup(engine)
    for _ in range(25):
        assert fast.lookup(engine)['rows']['100'][0]['have_eps'] == 2
    assert reads == [1]
    assert 'paths' not in first['rows']['100'][0]
    assert first['facts_version'] == 'a'
    assert len(first['rows']) == 5000


def test_refreshes_counts_and_ambiguous_id_when_revision_changes(monkeypatch):
    data = {'ts': time.time(), 'stale': False, 'facts_version': 'a',
            'series': [dict(tmdb_id='15', id='s1', have_eps=3, local_eps=3, share_eps=0,
                            tmdb_info={'match_status': 'missing', 'tmdb_total': 10})]}
    engine = SimpleNamespace(cached_library_view=lambda: data)
    token = ['v1']
    monkeypatch.setattr(fast, '_revision', lambda _: token[0])
    assert fast.lookup(engine)['rows']['15'][0]['have_eps'] == 3
    data['series'][0]['have_eps'] = 9
    data['series'].append(dict(tmdb_id='15', id='s2', have_eps=4))
    data['facts_version'] = 'b'
    token[0] = 'v2'
    current = fast.lookup(engine)
    assert current['facts_version'] == 'b'
    assert len(current['rows']['15']) == 2
    assert current['rows']['15'][0]['have_eps'] == 9


def test_age_flag_is_not_frozen_at_cache_construction(monkeypatch):
    moment = [1200.]
    monkeypatch.setattr(fast.time, 'time', lambda: moment[0])
    monkeypatch.setattr(fast, '_revision', lambda _: 'stable')
    engine = SimpleNamespace(cached_library_view=lambda: {
        'ts': 1000., 'stale': False, 'series': [], 'facts_version': 'same'})
    assert fast.lookup(engine)['stale'] is False
    moment[0] = 2901.
    assert fast.lookup(engine)['stale'] is True


def test_untracked_engine_falls_back_without_stale_memo(monkeypatch):
    calls = []
    def untracked(_):
        raise AttributeError('fixture')
    monkeypatch.setattr(fast, '_revision', untracked)
    engine = SimpleNamespace(cached_library_view=lambda: calls.append(1) or {
        'ts': time.time(), 'series': [], 'facts_version': str(len(calls))})
    assert fast.lookup(engine)['facts_version'] == '1'
    assert fast.lookup(engine)['facts_version'] == '2'
    assert len(calls) == 2


def test_cold_no_data_retries_and_recovers(monkeypatch):
    moment = [1000.]
    source = [None]
    calls = []
    monkeypatch.setattr(fast.time, 'time', lambda: moment[0])
    monkeypatch.setattr(fast, '_revision', lambda _: 'stable')
    engine = SimpleNamespace(cached_library_view=lambda: calls.append(1) or source[0])
    assert not fast.lookup(engine)['rows']
    moment[0] += 2
    assert not fast.lookup(engine)['rows']
    assert len(calls) == 1
    source[0] = {'ts': 1002., 'series': [dict(tmdb_id='20', id='s2', have_eps=8)]}
    moment[0] += 5
    assert fast.lookup(engine)['rows']['20'][0]['have_eps'] == 8
    assert len(calls) == 2


def test_concurrent_status_polls_build_only_one_index(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Lock
    calls = []
    counted = Lock()
    monkeypatch.setattr(fast, '_revision', lambda _: 'single-revision')
    def view():
        with counted:
            calls.append(1)
        time.sleep(0.002)
        return {'ts': time.time(), 'series': [dict(tmdb_id='9', have_eps=7)]}
    engine = SimpleNamespace(cached_library_view=view)
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda _: fast.lookup(engine), range(40)))
    assert len(calls) == 1
    assert all(item['rows']['9'][0]['have_eps'] == 7 for item in results)


def test_rejected_racing_revision_is_not_frozen(monkeypatch):
    token = ['before']
    calls = []
    def revision(_):
        return token[0]
    def view():
        calls.append(1)
        token[0] = 'after'
        return {'ts': time.time(), 'series': [dict(tmdb_id='22', have_eps=len(calls))]}
    monkeypatch.setattr(fast, '_revision', revision)
    engine = SimpleNamespace(cached_library_view=view)
    assert fast.lookup(engine)['rows']['22'][0]['have_eps'] == 1
    assert fast.lookup(engine)['rows']['22'][0]['have_eps'] == 2
    assert len(calls) == 2
