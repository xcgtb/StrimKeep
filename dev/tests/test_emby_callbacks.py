"""Real projections under disposable roots; callbacks never mutate media."""
import copy
import datetime
import json
import time
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app import engine, config, emby_events as events, state_store, morning, stats, ingest, tmdb
from app.routers import library


@pytest.fixture
def env(tmp_path, monkeypatch):
    for attr, leaf in [('L_ROOT','local'), ('S_ROOT','share'), ('DATA_DIR','data'), ('STATE_DIR','data/state')]:
        path = tmp_path / leaf
        path.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(engine, attr, path)
    for attr, leaf in [('EMBY_LIB_CACHE_FILE','mapping.json'), ('_EMBY_OVERVIEW_CACHE_FILE','overview.json'),
                       ('INGEST_CACHE_FILE','ingest.json'), ('_STRM_COUNT_CACHE_FILE','counts.json')]:
        monkeypatch.setattr(engine, attr, engine.STATE_DIR / leaf)
    monkeypatch.setattr(engine, 'EMBY_HOST', 'http://fixture')
    monkeypatch.setattr(engine, 'EMBY_PATHS', engine.EmbyPathMap('/local','/share'))
    monkeypatch.setattr(engine, 'RUNTIME_CFG', {'tmdb_key': ''})
    monkeypatch.setattr(engine, '_emby_lib_cache', {'ts':0,'data':None})
    monkeypatch.setattr(engine, '_emby_index_cache', {'ts':0,'data':None})
    monkeypatch.setattr(engine, '_lib_stats_cache', {'ts':0,'data':None})
    monkeypatch.setattr(engine, '_strm_count_cache', {'ts':0,'local':0,'share':0})
    cfg = dict(config.DEFAULTS, ingest_mode='webhook', ingest_webhook_token='test-token', ingest_enabled='1')
    monkeypatch.setattr(config, 'load_config', lambda: dict(cfg))
    monkeypatch.setattr(events, 'kick', lambda: None)
    monkeypatch.setattr(events.library_updates, 'publish', lambda *a, **k: None)
    monkeypatch.setattr(engine, 'enqueue_ingest_notifications', lambda result: {'status':'queued'})
    from app import scheduler
    monkeypatch.setattr(scheduler, '_request_notification_delivery', lambda: None)
    morning.save_emby_lib_cache({'series':[], 'movies':[], 'stats':{}, 'emby_host':engine.EMBY_HOST})
    stats._recompute_all_stats()
    return cfg


def episode(ident='e1', sid='s1', lib='local', number=1):
    file = (engine.L_ROOT if lib == 'local' else engine.S_ROOT) / '剧集/样本/Season 1' / (ident+'.strm')
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text('https://fixture/video')
    return {'Id':ident, 'Type':'Episode', 'SeriesId':sid, 'SeriesName':'样本',
            'Path':'/'+lib+'/剧集/样本/Season 1/'+file.name, 'IndexNumber':number, 'ParentIndexNumber':1,
            'DateCreated':datetime.datetime.now(datetime.timezone.utc).isoformat()}


def record(sid='s1', lib='local'):
    return {'Id':sid,'Type':'Series','Name':'样本','ProductionYear':2026,
            'ProviderIds':{'Tmdb':'7'},'Path':'/'+lib+'/剧集/样本', 'ImageTags':{'Primary':'p'}}


def api(monkeypatch, items, series):
    calls = []
    def request(route, params, **kw):
        calls.append(dict(params))
        assert 'Ids' in params or 'ParentId' in params, 'no whole-library query'
        if 'Ids' in params:
            rows = [copy.deepcopy(items[x]) for x in params['Ids'].split(',') if x in items]
        else:
            rows = copy.deepcopy(series.get(params['ParentId'], []))
        return {'Items':rows, 'TotalRecordCount':len(rows)}
    monkeypatch.setattr(engine, 'emby_request', request)
    return calls


def job(row, event='library.new'):
    return {'id':row['Id'],'type':row['Type'],'sid':row.get('SeriesId',''),
            'path':row.get('Path',''),'event':event}


def test_new_episode_persists_mapping_explore_counts_and_recent(env, monkeypatch):
    ep = episode()
    calls = api(monkeypatch, {'e1':ep,'s1':record()}, {'s1':[ep]})
    notifications = []
    monkeypatch.setattr(engine,'enqueue_ingest_notifications', lambda data: notifications.append(data) or {'status':'queued'})
    events.process_batch([job(ep)])
    row = morning.read_emby_lib_cache()['series'][0]
    assert row['have_eps'] == 1 and row['in_local'] and not row['in_share']
    assert tmdb.emby_library_index()['tv:7']['in_local']
    assert stats._get_strm_counts() == (1,0)
    assert ingest.read_ingest_cache()['stats']['episodes'] == 1
    assert notifications[0]['series_facts'][0]['have_eps'] == 1
    assert len(calls) == 3
    assert engine.emby_path_to_container(ep['Path']).exists()


def test_merge_and_delete_one_library_preserves_other(env, monkeypatch):
    local, share = episode(), episode('e2','s2','share')
    items = {'e1':local,'s1':record(), 'e2':share,'s2':record('s2','share')}
    series = {'s1':[local],'s2':[share]}
    api(monkeypatch, items, series)
    events.process_batch([job(local)])
    events.process_batch([job(share)])
    row = morning.read_emby_lib_cache()['series'][0]
    assert set(row['series_ids']) == {'s1','s2'} and row['in_local'] and row['in_share']
    engine.emby_path_to_container(local['Path']).unlink()
    items.pop('e1'); series['s1'] = []
    events.process_batch([job(local,'library.deleted')])
    row = morning.read_emby_lib_cache()['series'][0]
    assert not row['in_local'] and row['in_share'] and row['have_eps'] == 1
    assert stats._get_strm_counts() == (0,1)
    assert engine.emby_path_to_container(share['Path']).exists()


def test_late_delete_projects_current_presence(env, monkeypatch):
    ep = episode()
    api(monkeypatch, {'e1':ep,'s1':record()}, {'s1':[ep]})
    events.process_batch([job(ep,'library.deleted')])
    assert morning.read_emby_lib_cache()['series'][0]['have_eps'] == 1
    assert ingest.read_ingest_cache()['stats']['episodes'] == 1


@pytest.mark.parametrize('failure', ['mount','truncated','identity','network'])
def test_uncertain_read_keeps_old_facts(env, monkeypatch, failure):
    ep = episode()
    api(monkeypatch, {'e1':ep,'s1':record()}, {'s1':[ep]})
    events.process_batch([job(ep)])
    before = morning.read_emby_lib_cache()
    if failure == 'mount':
        engine.emby_path_to_container(ep['Path']).unlink()
    else:
        def broken(*a, **k):
            if failure == 'network': raise RuntimeError('offline')
            if failure == 'truncated': return {'Items':[], 'TotalRecordCount':2}
            return {'Items':[dict(ep,Id='wrong')],'TotalRecordCount':1}
        monkeypatch.setattr(engine,'emby_request',broken)
    with pytest.raises(RuntimeError): events.process_batch([job(ep)])
    assert morning.read_emby_lib_cache() == before


def test_tmdb_failure_still_commits_facts_and_notification(env, monkeypatch):
    ep = episode()
    api(monkeypatch, {'e1':ep,'s1':record()}, {'s1':[ep]})
    monkeypatch.setattr(engine,'RUNTIME_CFG',{'tmdb_key':'key'})
    class Broken:
        def get(self,*a,**k):
            assert morning.read_emby_lib_cache()['series'][0]['have_eps'] == 1
            raise RuntimeError('offline')
    monkeypatch.setattr(engine,'Tmdb',Broken)
    notices = []
    monkeypatch.setattr(engine,'enqueue_ingest_notifications',lambda data: notices.append(data) or {'status':'queued'})
    with pytest.raises(RuntimeError,match='TMDB'): events.process_batch([job(ep)])
    assert notices and stats._get_strm_counts() == (1,0)


def test_deleted_last_episode_removes_title_without_file_mutation(env, monkeypatch):
    ep = episode()
    items, series = {'e1':ep,'s1':record()}, {'s1':[ep]}
    api(monkeypatch, items, series)
    events.process_batch([job(ep)])
    engine.emby_path_to_container(ep['Path']).unlink()
    items.pop('e1'); series['s1'] = []
    events.process_batch([job(ep,'library.deleted')])
    assert morning.read_emby_lib_cache()['series'] == []
    assert tmdb.emby_library_index() == {}
    assert stats._get_strm_counts() == (0,0)


def test_queue_is_durable_bounded_deduplicates_pending_but_allows_reimport(env, monkeypatch):
    payload = {'Event':'library.new','Item':{'Id':'e1','Type':'Episode','SeriesId':'s1'}}
    assert events.accept(payload)['status'] == 'accepted'
    assert events.accept(payload)['duplicate']
    data = events._load()
    assert data['jobs']['Episode:e1']['generation'] == 1
    payload['Event'] = 'library.deleted'
    events.accept(payload)
    assert events._load()['jobs']['Episode:e1']['generation'] == 2
    data = events._load(); data['jobs'] = {}; state_store.save(events._path(),data)
    payload['Event'] = 'library.new'
    assert not events.accept(payload).get('duplicate')
    monkeypatch.setattr(events,'MAX_PENDING',1)
    payload['Item']['Id'] = 'e2'
    assert events.accept(payload)['status'] == 'busy'
    assert events.status()['pending'] == 1


def test_atomic_documents_roll_back_together(env, monkeypatch):
    a, b = engine.STATE_DIR/'a.json', engine.STATE_DIR/'b.json'
    state_store.save_many([(a,{'n':1}),(b,{'n':1})])
    put = state_store._put
    def fail(con,name,value):
        if name == 'b': raise OSError('disk failure')
        put(con,name,value)
    monkeypatch.setattr(state_store,'_put',fail)
    with pytest.raises(OSError): state_store.save_many([(a,{'n':2}),(b,{'n':2})])
    assert state_store.read(a) == state_store.read(b) == {'n':1}


@pytest.mark.parametrize('encoding', ['json','form','multipart'])
def test_authenticated_route_accepts_real_encodings(env, encoding):
    app = FastAPI(); app.include_router(library.router)
    client = TestClient(app)
    payload = {'Event':'library.new','Item':{'Id':'e1','Type':'Episode','SeriesId':'s1'}}
    assert client.post('/api/emby/webhook',json=payload).status_code == 401
    url = '/api/emby/webhook?token=test-token'
    kwargs = {'json':payload} if encoding == 'json' else {'data':{'data':json.dumps(payload)}}
    if encoding == 'multipart': kwargs = {'files':{'data':(None,json.dumps(payload))}}
    assert client.post(url,**kwargs).status_code == 202
    assert events.status()['pending'] == 1
    assert client.post(url,content=b'x'*(256*1024+1)).status_code == 413
    assert client.post(url,content=b'bad').status_code == 400


def test_webhook_cache_reads_never_discover_emby(env, monkeypatch):
    monkeypatch.setattr(engine,'emby_request',lambda *a,**k: pytest.fail('remote discovery'))
    monkeypatch.setattr(engine,'_build_emby_library_index',lambda: pytest.fail('full index'))
    morning.emby_library_overview()
    tmdb.emby_library_index()
    ingest.get_ingest()
    stats.action_library_stats(None)


def test_root_file_counts_are_exact_and_idempotent(env):
    file = engine.L_ROOT/'root.strm'; file.write_text('fixture')
    assert stats.refresh_event_directories([str(engine.L_ROOT)])
    assert stats._get_strm_counts() == (1,0)
    stats.refresh_event_directories([str(engine.L_ROOT)])
    assert stats._get_strm_counts() == (1,0)
    file.unlink(); stats.refresh_event_directories([str(engine.L_ROOT)])
    assert stats._get_strm_counts() == (0,0)


def test_movie_added_merged_and_deleted_without_touching_other_library(env, monkeypatch):
    def movie(ident,lib):
        file = (engine.L_ROOT if lib == 'local' else engine.S_ROOT)/'电影/样本'/ (ident+'.strm')
        file.parent.mkdir(parents=True,exist_ok=True); file.write_text('fixture')
        return dict(record(ident,lib),Type='Movie',Path='/'+lib+'/电影/样本/'+file.name,
                    DateCreated=datetime.datetime.now(datetime.timezone.utc).isoformat())
    a,b = movie('m1','local'),movie('m2','share')
    items = {'m1':a,'m2':b}
    api(monkeypatch,items,{})
    events.process_batch([job(a)]); events.process_batch([job(b)])
    rows = morning.read_emby_lib_cache()['movies']
    assert len(rows) == 1 and set(rows[0]['ids']) == {'m1','m2'}
    assert tmdb.emby_library_index()['movie:7']['in_share']
    items.pop('m1'); engine.emby_path_to_container(a['Path']).unlink()
    events.process_batch([job(a,'library.deleted')])
    row = morning.read_emby_lib_cache()['movies'][0]
    assert row['in_share'] and not row['in_local'] and row['ids'] == ['m2']
    assert engine.emby_path_to_container(b['Path']).exists()


def test_relocated_episode_recounts_old_and_new_directories(env, monkeypatch):
    ep = episode(); items = {'e1':ep,'s1':record()}; series = {'s1':[ep]}
    api(monkeypatch,items,series); events.process_batch([job(ep)])
    old = engine.emby_path_to_container(ep['Path']); old.unlink()
    moved = dict(ep,Path='/share/剧集/样本/Season 1/e1.strm')
    target = engine.emby_path_to_container(moved['Path']); target.parent.mkdir(parents=True,exist_ok=True); target.write_text('fixture')
    items['e1'] = moved; series['s1'] = [moved]
    events.process_batch([job(moved,'library.updated')])
    assert stats._get_strm_counts() == (0,1)
    row = morning.read_emby_lib_cache()['series'][0]
    assert row['in_share'] and not row['in_local']


@pytest.mark.parametrize('replacement', [False,True])
def test_worker_retains_failure_and_does_not_ack_new_generation(env, monkeypatch, replacement):
    class Wake:
        def wait(self,*a): pass
        def clear(self): pass
    monkeypatch.setattr(events,'_wake',Wake())
    payload = {'Event':'library.new','Item':{'Id':'e1','Type':'Episode','SeriesId':'s1'}}
    events.accept(payload)
    data = events._load(); data['jobs']['Episode:e1']['due'] = 0; state_store.save(events._path(),data)
    def process(jobs):
        if replacement:
            payload['Event'] = 'library.deleted'; events.accept(payload)
        env['ingest_enabled'] = '0'
        if not replacement: raise RuntimeError('network failed')
    monkeypatch.setattr(events,'process_batch',process)
    events._run()
    row = events._load()['jobs']['Episode:e1']
    assert row['generation'] == (2 if replacement else 1)
    assert row['attempts'] == (0 if replacement else 1)


def test_scheduler_webhook_mode_does_not_discover_even_after_restart(env, monkeypatch):
    from app import scheduler, overview
    env.update(tmdb_scan_enabled='0',subscribe_enabled='0')
    monkeypatch.setattr(overview,'request_refresh',lambda **kw: None)
    monkeypatch.setattr(engine,'refresh_ingest_cache',lambda: pytest.fail('polling Emby'))
    monkeypatch.setattr(config,'get_morning_report',lambda: {'enabled':False})
    monkeypatch.setattr(scheduler,'_bg_state',dict(scheduler._bg_state,last_ingest_check=0))
    scheduler._tick(time.time())


def test_filename_category_change_with_same_count_matches_full_baseline(env):
    old = engine.L_ROOT/'国产电影.strm'; old.write_text('fixture')
    stats.refresh_event_directories([str(engine.L_ROOT)])
    before = state_store.read(engine.STATE_DIR/'library_stats_cache.json')
    assert next(r for r in before['rows'] if r['name'].endswith('华语电影'))['local'] == 1
    old.rename(engine.L_ROOT/'国产剧.strm')
    stats.refresh_event_directories([str(engine.L_ROOT)])
    incremental = state_store.read(engine.STATE_DIR/'library_stats_cache.json')
    full = stats._recompute_all_stats()
    for key in ['rows','local_total','share_total','local_other','share_other']:
        assert incremental[key] == full[key]


def test_overview_exposes_live_counts_and_imports_before_cached_dashboard_ttl(env, monkeypatch):
    from app import overview
    monkeypatch.setattr(overview,'request_refresh',lambda **kw: {})
    monkeypatch.setattr(overview,'_memory',{'path':None,'stamp':None,'data':None,'health_stamp':None})
    state_store.save(overview._path(), {'schema':1,'ts':time.time(),'emby_host':engine.EMBY_HOST,
        'library_stats':{'ts':0},'dashboard':{'localCount':'0','shareCount':'0','libraryHealth':{},'ingest':{'episodes':0}}})
    ep = episode(); api(monkeypatch,{'e1':ep,'s1':record()},{'s1':[ep]})
    events.process_batch([job(ep)])
    dashboard = overview.get_overview()['dashboard']
    assert dashboard['localCount'] == '1' and dashboard['shareCount'] == '0'
    assert dashboard['ingest']['episodes'] == 1


def test_full_tmdb_completion_cannot_restore_deleted_title(env, monkeypatch):
    ep = episode(); api(monkeypatch,{'e1':ep,'s1':record()},{'s1':[ep]})
    events.process_batch([job(ep)])
    class Client:
        key = 'key'
        def __init__(self): self.cache = {}
        def save(self): pass
    monkeypatch.setattr(engine,'Tmdb',Client)
    monkeypatch.setattr(engine,'_tmdb_scan_progress',{'started_at':time.time()})
    monkeypatch.setattr(config,'save_config',lambda cfg: None)
    def bulk(*a,**k):
        morning.save_emby_lib_cache({'series':[],'movies':[],'stats':{},'emby_host':engine.EMBY_HOST})
        return {('tv','7'):{'seasons':[{'season_number':1,'episode_count':2}]}},0,{}
    monkeypatch.setattr(morning,'_tmdb_bulk_get',bulk)
    result = morning._refresh_tmdb_scan()
    assert result['status'] == 'success'
    assert morning.read_emby_lib_cache()['series'] == []
