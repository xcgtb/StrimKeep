#!/usr/bin/env python3
"""Isolated slow-service checks: temporary caches, fake APIs, no media changes."""
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0, str(Path.cwd() if __file__ == '<stdin>' else Path(__file__).resolve().parents[2]))
from app import state_store as _state


def main():
    with tempfile.TemporaryDirectory(prefix='strimkeep-explore-background-') as directory:
        temp = Path(directory)
        for name, leaf in [('L_ROOT','local'), ('S_ROOT','share'), ('CLOUD_L_ROOT','cloud'), ('AGENT_DATA','data')]:
            p = temp / leaf; p.mkdir(); os.environ[name] = str(p)
        os.environ['TG_BOT_TOKEN'] = ''; os.environ['TMDB_KEY'] = ''
        os.environ.setdefault('WEB_PASSWORD','temporary-verification-only')
        from app import engine, tmdb
        entered = threading.Event(); release = threading.Event()
        calls = []
        mode = {'fail': False, 'empty': False, 'block': True}
        class FakeTmdb:
            key = 'isolated-dummy-key'; calls = 0; hits = 0
            def __init__(self, cache=None): pass
            def get(self, path, **params):
                calls.append((path, dict(params)))
                entered.set()
                if mode['block']:
                    assert release.wait(5), 'test worker not released'
                if mode['fail']:
                    raise OSError('fake service failure')
                return {'results': [] if mode['empty'] else [{'id':100,'name':'临时剧','overview':'not persisted'}],
                        'total_pages':3,'total_results':1}
            def save(self): raise AssertionError('fake client has no writes')
        def forbidden(*a, **kw): raise AssertionError('real APIs or media scan forbidden')
        def finish_jobs(state):
            for _ in range(500):
                with tmdb._explore_page_lock:
                    if not state['jobs']: return
                time.sleep(.005)
            raise AssertionError('background job failed to finish')
        args = SimpleNamespace(media='tv')
        facts = {'facts_version':'first','facts_ts':100,'series':[
            {'id':'s1','tmdb_id':'100','have_eps':13,'local_eps':13,'share_eps':0,
             'tmdb_info':{'declared_total':20,'match_status':'missing'}}]}
        index = {'tv:100':{'id':'s1','in_local':True}}
        with patch.object(engine,'STATE_DIR',temp/'state'), patch.object(engine,'Tmdb',FakeTmdb), \
             patch.object(engine,'cached_library_view',lambda: facts), patch.object(engine,'emby_request',forbidden), \
             patch.object(tmdb,'_explore_page_state',None), patch.object(tmdb,'_explore_slots',threading.BoundedSemaphore(2)), \
             patch.object(tmdb,'emby_library_index',lambda: index):
            start = time.monotonic(); result = engine.action_explore(args)
            assert result['status'] == 'pending' and time.monotonic()-start < .5
            assert entered.wait(1)
            state = tmdb._explore_state()
            for _ in range(20): assert engine.action_explore(args)['status'] == 'pending'
            assert len(calls) == 1
            print('PASS cold page returns pending immediately and duplicate reads share one slow job')
            other = SimpleNamespace(media='tv',q='other')
            third = SimpleNamespace(media='tv',q='third')
            assert engine.action_explore(other)['status'] == 'pending'
            assert engine.action_explore(third)['status'] == 'pending'
            assert len(state['jobs']) == 2
            release.set(); finish_jobs(state); mode['block'] = False
            assert len(calls) == 2 and not any(p['query']=='third' for _,p in calls if 'query' in p)
            print('PASS at most two distinct refreshes run and excess requests create no waiting jobs')
            result = engine.action_explore(args)
            assert result['status'] == 'success' and result['total_pages'] == 3
            assert result['cards'][0]['eps']['have'] == 13
            facts['series'][0]['have_eps'] = 14; facts['facts_version'] = 'second'
            assert engine.action_explore(args)['cards'][0]['eps']['have'] == 14
            assert engine.action_explore(args)['facts_version'] == 'second'
            assert len(calls) == 2
            assert 'overview' not in json.dumps(state['pages']) and 'isolated-dummy-key' not in json.dumps(_state.read(state['path']))
            print('PASS cached discovery pages use current library facts and persist no API key or unused payload')
            # Failed refresh must retain the last complete single-page result.
            # Both fake queries have same cards; expire all to select the exact requested key safely.
            before = {k: json.dumps(v['data']) for k,v in state['pages'].items()}
            for row in state['pages'].values(): row['ts'] = 1
            mode['fail'] = True
            assert engine.action_explore(args)['status'] == 'success'
            finish_jobs(state)
            result = engine.action_explore(args)
            assert result['refresh_error'] and result['cards'][0]['eps']['have'] == 14
            count = len(calls)
            for _ in range(10): engine.action_explore(args)
            assert len(calls) == count
            assert all(json.dumps(state['pages'][k]['data']) == v for k,v in before.items())
            print('PASS TMDB failure preserves the last complete page and cooldown prevents retry storms')
            mode.update(fail=False,empty=True)
            empty_args = SimpleNamespace(media='tv',q='empty')
            engine.action_explore(empty_args); finish_jobs(state)
            assert engine.action_explore(empty_args)['cards'] == []
            count = len(calls); engine.action_explore(empty_args); assert len(calls) == count
            # Re-create memory state; the small persisted cache answers without a network job.
            tmdb._explore_page_state = None
            assert engine.action_explore(empty_args)['cards'] == [] and len(calls) == count
            print('PASS successful empty pages remain valid in memory and after reading the persisted cache')
        # Independent identity-index checks. No FakeTmdb or media scan runs here.
        index_entered = threading.Event(); index_release = threading.Event()
        def slow_index():
            index_entered.set(); assert index_release.wait(5); return {}
        index_cache = {'ts':0,'data':None}
        lock = threading.Lock()
        with patch.object(engine,'_emby_index_cache',index_cache), patch.object(engine,'_emby_index_refresh_lock',lock), \
             patch.object(engine,'_EMBY_INDEX_CACHE_FILE',temp/'index.json'), patch.object(engine,'STATE_DIR',temp), \
             patch.object(tmdb,'_build_emby_library_index',slow_index):
            start = time.monotonic(); assert tmdb.emby_library_index() == {}
            assert time.monotonic()-start < .5 and index_entered.wait(1)
            for _ in range(20): assert tmdb.emby_library_index() == {}
            index_release.set()
            assert lock.acquire(timeout=2); lock.release()
            assert index_cache['data'] == {} and index_cache['ts'] > 0
            index_entered.clear(); assert tmdb.emby_library_index() == {} and not index_entered.is_set()
            print('PASS first Emby index refresh is asynchronous and a confirmed empty library is cached')
        old = {'tv:100':{'id':'s1'}}; index_cache = {'ts':1,'data':old}
        requests = []
        def partial_index(path, params):
            requests.append(params['IncludeItemTypes'])
            if params['IncludeItemTypes']=='Series': raise OSError('fake Emby failure')
            return {'Items':[],'TotalRecordCount':0}
        with patch.object(engine,'_emby_index_cache',index_cache), patch.object(engine,'_emby_index_refresh_lock',threading.Lock()), \
             patch.object(engine,'_EMBY_INDEX_CACHE_FILE',temp/'missing.json'), patch.object(engine,'STATE_DIR',temp), \
             patch.object(engine,'_disk_tmdb_lookup',lambda:{}), patch.object(engine,'emby_request',partial_index):
            assert tmdb.emby_library_index() == old
            lock = engine._emby_index_refresh_lock; assert lock.acquire(timeout=2); lock.release()
            assert index_cache['data'] == old and index_cache['ts'] == 1 and index_cache['error_ts'] > 0
            count = len(requests); assert tmdb.emby_library_index() == old and len(requests) == count
            print('PASS partial Emby failure keeps the complete identity index and failure is not a known zero')
        # Socket/retry bounds are scoped to exploration; no URL is opened.
        with patch.object(engine,'RUNTIME_CFG',dict(engine.RUNTIME_CFG,tmdb_key='dummy')), \
             patch.object(engine,'STATE_DIR',temp), patch.object(tmdb.time,'sleep',lambda _:None):
            client = tmdb.Tmdb(cache={}); timeouts=[]
            def broken(request,timeout): timeouts.append(timeout); raise OSError('offline fixture')
            token = tmdb._explore_request_window.set({'deadline':time.monotonic()+20,'attempts':2,'timeout':8})
            try:
                with patch.object(tmdb.urllib.request,'urlopen',broken):
                    try: client.get('/discover/tv')
                    except tmdb.TmdbError: pass
                    else: raise AssertionError('expected failure')
                assert len(timeouts)==2 and all(0 < value <= 8 for value in timeouts)
            finally: tmdb._explore_request_window.reset(token)
            assert tmdb._explore_request_window.get() is None
            print('PASS exploration uses bounded retry/socket waits without leaking settings into other tasks')
        print('PASS 8/8; temporary caches and fake slow services only; no real media or API calls')


if __name__ == '__main__': main()
