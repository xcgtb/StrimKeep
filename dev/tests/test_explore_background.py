"""Exercise the NAS verifier in a subprocess to isolate runtime/config/thread state."""
from app import state_store as _state
import subprocess
import sys
from pathlib import Path


def test_explore_background_acceptance_scenarios():
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run([sys.executable, str(root/'dev/checks/verify_explore_background.py')],
                            cwd=root, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'PASS 8/8' in result.stdout


def test_page_cache_memory_and_disk_limit(tmp_path, monkeypatch):
    import json
    import threading
    from app import engine, tmdb
    class Client:
        key = 'fake'; calls = 0; hits = 0
        def get(self, path, **params): return {'results': [], 'total_pages': 1}
    monkeypatch.setattr(engine, 'STATE_DIR', tmp_path)
    monkeypatch.setattr(engine, 'Tmdb', Client)
    state = {'path':tmp_path/'explore_pages.json', 'pages':{}, 'errors':{}, 'jobs':set()}
    slots = threading.BoundedSemaphore(2)
    monkeypatch.setattr(tmdb,'_explore_slots',slots)
    for i in range(tmdb.EXPLORE_PAGE_LIMIT + 10):
        assert slots.acquire(blocking=False)
        state['jobs'].add(str(i))
        tmdb._refresh_explore_page(state,str(i),'/discover/tv',{'page':1},'fake')
    assert len(state['pages']) == len(_state.read(state['path'])) == 32
    assert not state['jobs'] and '0' not in state['pages']


def test_cold_page_failure_is_an_error_without_persisting_empty_success(tmp_path, monkeypatch):
    import threading
    import time
    from app import engine, tmdb
    calls = []
    class Client:
        key = 'fake'; calls = 0; hits = 0
        def __init__(self, cache=None): pass
        def get(self,*a,**kw): calls.append(1); return None
    monkeypatch.setattr(engine,'STATE_DIR',tmp_path)
    monkeypatch.setattr(engine,'Tmdb',Client)
    monkeypatch.setattr(tmdb,'_explore_page_state',None)
    monkeypatch.setattr(tmdb,'_explore_slots',threading.BoundedSemaphore(2))
    _,meta=tmdb._explore_page('/discover/tv',{'page':1},'tv')
    assert meta['status']=='pending'
    for _ in range(200):
        if not tmdb._explore_state()['jobs']: break
        time.sleep(.005)
    for _ in range(10):
        data,meta=tmdb._explore_page('/discover/tv',{'page':1},'tv')
        assert data is None and meta['status']=='error'
    assert len(calls)==1 and not (tmp_path/'explore_pages.json').exists()


def test_truncated_emby_identity_response_is_not_success(monkeypatch):
    import pytest
    from app import engine,tmdb
    monkeypatch.setattr(engine,'_disk_tmdb_lookup',lambda:{})
    monkeypatch.setattr(engine,'emby_request',lambda *a,**kw:{'Items':[],'TotalRecordCount':1})
    with pytest.raises(RuntimeError, match='身份索引拉取失败'):
        tmdb._build_emby_library_index()
