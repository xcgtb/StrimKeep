"""Reuse is bounded by subscription rounds and exact file/manual-state changes."""
from app import state_store as _state
import copy
import io
import json
import time
from pathlib import Path
from unittest.mock import patch
import pytest
from app import engine, morning, tmdb, subscribe
from dev.tests.test_library_fact_counts import cached, series, write_cache
from dev.tests.test_subscription_fact_status import subenv


def test_repeated_health_reads_parse_sources_and_hash_once(cached):
    write_cache([series()], ts=time.time())
    morning.save_library_snapshot(morning.build_library_health_snapshot())
    reads=[]; original=_state.read
    def read(p,*a,**kw):
        if p in (engine.LIBRARY_SNAPSHOT_FILE,engine.EMBY_LIB_CACHE_FILE): reads.append(p)
        return original(p,*a,**kw)
    with patch.object(_state,'read',read), patch.object(morning,'_health_snapshot',wraps=morning._health_snapshot) as build:
        results=[engine.cached_library_view() for _ in range(5)]
    assert len(reads)==2 and build.call_count==1
    assert len({r['facts_version'] for r in results})==1


def test_atomic_update_with_same_size_and_mtime_invalidates_health(cached):
    write_cache([series(12)], ts=100, facts_ts=200)
    old=engine.unified_health(); path=engine.EMBY_LIB_CACHE_FILE
    row=_state.read(path); row['series'][0].update(series(13)); _state.save(path,row)
    fresh=engine.unified_health()
    assert fresh['episodes']==13 and fresh['facts_version']!=old['facts_version']


def test_returned_nested_data_cannot_mutate_cached_facts(cached):
    write_cache([series(13)],ts=time.time())
    first=engine.cached_library_view(); version=first['facts_version']
    first['series'][0]['have_eps']=999; first['series'][0]['tmdb_info']['match_status']='aligned'
    first['movies'].append({'id':'not-real'})
    second=engine.cached_library_view()
    assert second['series'][0]['have_eps']==13 and second['stats']['missing']==1
    assert second['movies']==[] and second['facts_version']==version


def test_manual_done_and_unmark_change_stats_and_version_immediately(cached,monkeypatch):
    write_cache([series()],ts=time.time()); marks={}
    monkeypatch.setattr(morning,'read_manual_done',lambda:copy.deepcopy(marks))
    initial=engine.unified_health(); marks['s1']={'name':'temporary'}
    done=engine.unified_health(); marks.clear(); restored=engine.unified_health()
    assert done['stats']['aligned']==1 and done['facts_version']!=initial['facts_version']
    assert restored['facts_version']==initial['facts_version'] and restored['stats']['missing']==1


def test_staleness_is_recomputed_on_reuse(cached,monkeypatch):
    write_cache([series()],ts=100)
    clock=[110]; monkeypatch.setattr(morning.time,'time',lambda:clock[0])
    assert engine.unified_health(max_age=30)['stale'] is False
    clock[0]=140
    assert engine.unified_health(max_age=30)['stale'] is True
    assert engine.unified_health(max_age=None)['stale'] is False


def test_deleted_and_corrupt_sources_do_not_resurrect_memoized_data(cached):
    write_cache([series()]); assert engine.unified_health()['episodes']==13
    _state.remove(engine.EMBY_LIB_CACHE_FILE); assert engine.unified_health()=={}
    write_cache([],facts_ts=200); assert engine.unified_health()['episodes']==0
    _state.remove(engine.EMBY_LIB_CACHE_FILE); assert engine.unified_health()=={}


def prepare_real_tmdb(cached,monkeypatch):
    monkeypatch.setattr(engine,'STATE_DIR',cached)
    monkeypatch.setattr(engine,'RUNTIME_CFG',{'tmdb_key':'temporary'})
    monkeypatch.setattr(engine,'Tmdb',tmdb.Tmdb)
    monkeypatch.setattr(engine,'_tmdb_series_info',tmdb._tmdb_series_info)
    path=cached/'tmdb_cache.json'
    def info(n): return {'name':str(n),'seasons':[{'season_number':1,'episode_count':1}]}
    _state.save(path, {'/tv/100?language='+engine.TMDB_LANG:{'ts':time.time(),'data':info(100)}})
    requests=[]
    def network(req,timeout): requests.append(req.full_url); return io.BytesIO(json.dumps(info(200)).encode())
    monkeypatch.setattr(tmdb.urllib.request,'urlopen',network)
    return path,requests


def test_subscription_round_loads_once_saves_once_and_sources_are_per_query(subenv,cached,monkeypatch):
    path,requests=prepare_real_tmdb(cached,monkeypatch)
    monkeypatch.setattr(subscribe._cfg,'get_subscriptions',lambda:[{'id':str(n),'tmdb_id':str(n),'name':str(n)} for n in [100,200,100]])
    monkeypatch.setattr(subscribe,'_emby_series_latest_ep',lambda tid:{'episodes':{(1,1)},'source':'emby_live'})
    reads=[]; original=_state.read
    def read(p,*a,**kw):
        if p==path: reads.append(p)
        return original(p,*a,**kw)
    with patch.object(_state,'read',read),patch.object(tmdb.Tmdb,'save',autospec=True,wraps=None) as save:
        result=subscribe.check_subscriptions(False)
    assert len(reads)==1 and save.call_count==1 and len(requests)==1
    assert [r['tmdb_source'] for r in result['rows']]==['tmdb_cache','tmdb','tmdb_cache']
    assert result['failed']==0 and result['checked']==3


def test_cache_only_round_does_not_rewrite_tmdb_file_and_new_round_reloads(subenv,cached,monkeypatch):
    path,requests=prepare_real_tmdb(cached,monkeypatch)
    monkeypatch.setattr(subscribe,'_emby_series_latest_ep',lambda tid:{'episodes':{(1,1)}})
    reads=[]; original=_state.read
    def read(p,*a,**kw):
        if p==path: reads.append(p)
        return original(p,*a,**kw)
    with patch.object(_state,'read',read),patch.object(tmdb.Tmdb,'save',side_effect=AssertionError('no changes')):
        subscribe.check_subscriptions(False); subscribe.check_subscriptions(False)
    assert len(reads)==2 and not requests


def test_disabled_tmdb_or_paused_subscriptions_do_not_load_client(subenv,monkeypatch):
    monkeypatch.setattr(subscribe._cfg,'load_config',lambda:{'subscribe_check_tmdb':'0'})
    monkeypatch.setattr(subscribe,'_emby_series_latest_ep',lambda tid:{'episodes':{(1,1)}})
    with patch.object(engine,'Tmdb',side_effect=AssertionError('disabled query')):
        assert subscribe.check_subscriptions(False)['checked']==1
    monkeypatch.setattr(subscribe._cfg,'load_config',lambda:{})
    monkeypatch.setattr(subscribe._cfg,'get_subscriptions',lambda:[{'id':'a','tmdb_id':'100','enabled':False}])
    with patch.object(engine,'Tmdb',side_effect=AssertionError('paused query')):
        assert subscribe.check_subscriptions(False)['paused']==1


def test_subscription_session_releases_after_exception(cached,monkeypatch):
    path,requests=prepare_real_tmdb(cached,monkeypatch)
    with pytest.raises(RuntimeError):
        with tmdb.subscription_tmdb_session():
            assert tmdb._tmdb_series_info('200') is not None
            raise RuntimeError('stop')
    assert '/tv/200?language='+engine.TMDB_LANG in _state.read(path)
    assert tmdb._subscription_tmdb_client.get() is None


def test_parallel_health_readers_build_only_one_snapshot(cached):
    from concurrent.futures import ThreadPoolExecutor
    write_cache([series()],ts=time.time())
    with patch.object(morning,'_health_snapshot',wraps=morning._health_snapshot) as build:
        with ThreadPoolExecutor(max_workers=4) as pool: results=list(pool.map(lambda _:engine.unified_health(),range(8)))
    assert build.call_count==1 and all(r['episodes']==13 for r in results)


def test_parallel_subscription_sessions_do_not_share_clients(cached,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    path,requests=prepare_real_tmdb(cached,monkeypatch)
    barrier=threading.Barrier(2); clients=[];original=tmdb.Tmdb
    def factory():
        client=original(); clients.append(client); return client
    monkeypatch.setattr(engine,'Tmdb',factory)
    def run(tid):
        with tmdb.subscription_tmdb_session():
            barrier.wait(timeout=5)
            return tmdb._tmdb_series_info(tid)
    with ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(run,['200','300']))
    assert len(clients)==2 and clients[0] is not clients[1] and all(results)
    assert all('/tv/'+tid+'?language='+engine.TMDB_LANG in _state.read(path) for tid in ['200','300'])
    assert tmdb._subscription_tmdb_client.get() is None
