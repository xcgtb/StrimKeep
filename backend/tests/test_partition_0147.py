import time
from fastapi.testclient import TestClient
from test_inventory_operations_api import setup_app, wait
from strimkeep.inventory_partition import partition
from strimkeep.inventory import registered_rows


def add_unregistered(root, folder, filename='Show.S01E02.1080p.strm'):
    p=root/'分享影视库'/'剧集'/folder/'Season 01'/filename
    p.parent.mkdir(parents=True,exist_ok=True); p.write_text('http://example/unregistered')
    return p


def test_unrelated_unregistered_work_does_not_block_safe_delete(tmp_path,monkeypatch):
    app,admin,path,db=setup_app(tmp_path,monkeypatch)
    unrelated=add_unregistered(tmp_path,'Other {tmdb-99}')
    with TestClient(app) as client:
        app.dependency_overrides[admin]=lambda:None
        r=client.post('/api/v1/inventory/delete/plan',json={'mode':'manual','selections':[{'kind':'tv','tmdb_id':12,'side':'share'}]})
        assert r.status_code==200,r.text
        pid=r.json()['plan_id']; assert client.post(f'/api/v1/inventory/delete/{pid}/execute').status_code==200
        status=wait(client,pid)
        assert status['state']=='completed',status
        assert not path.exists() and unrelated.exists()


def test_same_title_other_tmdb_is_isolated(tmp_path,monkeypatch):
    app,admin,path,db=setup_app(tmp_path,monkeypatch)
    other=add_unregistered(tmp_path,'Show {tmdb-13}')
    with TestClient(app) as client:
        app.dependency_overrides[admin]=lambda:None
        r=client.post('/api/v1/inventory/delete/plan',json={'mode':'manual','selections':[{'kind':'tv','tmdb_id':12,'side':'share'}]})
        assert r.status_code==409 and path.exists() and other.exists()


def test_new_related_unregistered_file_after_plan_blocks_execution(tmp_path,monkeypatch):
    app,admin,path,db=setup_app(tmp_path,monkeypatch)
    with TestClient(app) as client:
        app.dependency_overrides[admin]=lambda:None
        r=client.post('/api/v1/inventory/delete/plan',json={'mode':'manual','selections':[{'kind':'tv','tmdb_id':12,'side':'share'}]})
        assert r.status_code==200,r.text
        related=add_unregistered(tmp_path,'Show {tmdb-13}')
        pid=r.json()['plan_id']; client.post(f'/api/v1/inventory/delete/{pid}/execute')
        status=wait(client,pid)
        assert status['state']=='failed' and path.exists() and related.exists()


def test_unrelated_missing_registration_is_isolated(tmp_path,monkeypatch):
    app,admin,path,db=setup_app(tmp_path,monkeypatch)
    other='分享影视库/剧集/Other {tmdb-99}/Season 01/Other.S01E01.strm'
    db.execute('INSERT INTO share_strms VALUES (?,?,?,?,?,?,?,?)',('s2','code','','f2','',other,'hash',0));db.commit()
    with TestClient(app) as client:
        app.dependency_overrides[admin]=lambda:None
        r=client.post('/api/v1/inventory/delete/plan',json={'mode':'manual','selections':[{'kind':'tv','tmdb_id':12,'side':'share'}]})
        assert r.status_code==200,r.text
    assert path.exists()


def test_ambiguous_root_file_cannot_be_ignored(tmp_path,monkeypatch):
    app,admin,path,db=setup_app(tmp_path,monkeypatch)
    (tmp_path/'分享影视库'/'unknown.strm').write_text('http://example/unknown')
    with TestClient(app) as client:
        app.dependency_overrides[admin]=lambda:None
        r=client.post('/api/v1/inventory/delete/plan',json={'mode':'manual','selections':[{'kind':'tv','tmdb_id':12,'side':'share'}]})
        assert r.status_code==409 and path.exists()

def test_missing_year_same_title_is_isolated(tmp_path,monkeypatch):
    app,admin,path,db=setup_app(tmp_path,monkeypatch)
    other=add_unregistered(tmp_path,'Show')
    with TestClient(app) as client:
        app.dependency_overrides[admin]=lambda:None
        r=client.post('/api/v1/inventory/delete/plan',json={'mode':'manual','selections':[{'kind':'tv','tmdb_id':12,'side':'share'}]})
        assert r.status_code==409 and path.exists() and other.exists()


def test_same_tmdb_different_title_is_isolated(tmp_path,monkeypatch):
    app,admin,path,db=setup_app(tmp_path,monkeypatch)
    other=add_unregistered(tmp_path,'Different {tmdb-12}')
    with TestClient(app) as client:
        app.dependency_overrides[admin]=lambda:None
        r=client.post('/api/v1/inventory/delete/plan',json={'mode':'manual','selections':[{'kind':'tv','tmdb_id':12,'side':'share'}]})
        assert r.status_code==409 and path.exists() and other.exists()


def test_unrelated_broken_source_is_isolated(tmp_path,monkeypatch):
    app,admin,path,db=setup_app(tmp_path,monkeypatch)
    other=add_unregistered(tmp_path,'Other {tmdb-99}', 'Other.S01E01.strm')
    rel=other.relative_to(tmp_path).as_posix()
    db.execute('INSERT INTO share_strms VALUES (?,?,?,?,?,?,?,?)',('s2','','','f2','',rel,'hash',0));db.commit()
    with TestClient(app) as client:
        app.dependency_overrides[admin]=lambda:None
        r=client.post('/api/v1/inventory/delete/plan',json={'mode':'manual','selections':[{'kind':'tv','tmdb_id':12,'side':'share'}]})
        assert r.status_code==200,r.text
        r=client.post('/api/v1/inventory/delete/plan',json={'mode':'manual','selections':[{'kind':'tv','tmdb_id':99,'side':'share'}]})
        assert r.status_code==409 and other.exists()


def test_scan_reports_isolation_and_allows_other_work(tmp_path,monkeypatch):
    from strimkeep.inventory_api import install_inventory
    app,admin,path,db=setup_app(tmp_path,monkeypatch)
    add_unregistered(tmp_path,'Other {tmdb-99}')
    install_inventory(app,app.state.organizer.store,admin)
    with TestClient(app) as client:
        app.dependency_overrides[admin]=lambda:None
        r=client.post('/api/v1/inventory/preview',json={'tie':'cloud'})
        assert r.status_code==200,r.text
        data=r.json()
        assert data['scan_complete'] and data['isolated_records']>=1
        assert data['audit']['clean'] is False
        assert 'scan_revision' in data


def test_unidentified_flat_category_stops_plan(tmp_path,monkeypatch):
    app,admin,path,db=setup_app(tmp_path,monkeypatch)
    folder=tmp_path/'分享影视库'/'电影';folder.mkdir(); (folder/'Film.strm').write_text('http://example')
    with TestClient(app) as client:
        app.dependency_overrides[admin]=lambda:None
        r=client.post('/api/v1/inventory/delete/plan',json={'mode':'manual','selections':[{'kind':'tv','tmdb_id':12,'side':'share'}]})
        assert r.status_code==409 and path.exists()

def test_governance_plan_works_with_unrelated_anomaly(tmp_path,monkeypatch):
    import strimkeep.inventory_api as api
    import strimkeep.inventory_operations_api as operations
    from strimkeep.inventory_api import install_inventory
    app,admin,path,db=setup_app(tmp_path,monkeypatch)
    other=add_unregistered(tmp_path,'Other {tmdb-99}')
    def preview(projection,*args,**kwargs):
        assert projection['complete'] and len(projection['items'])==1
        return {'scan_complete':True,'single':[],'blocked':[],'dual':[{'kind':'tv','tmdb_id':12,'season':1,'winner':'cloud','action_kind':'shr','action_reason':'local_wins','incomplete_sides':[]}]}
    monkeypatch.setattr(api,'governance_preview',preview)
    monkeypatch.setattr(operations,'governance_preview',preview)
    install_inventory(app,app.state.organizer.store,admin)
    with TestClient(app) as client:
        app.dependency_overrides[admin]=lambda:None
        r=client.post('/api/v1/inventory/preview',json={'tie':'cloud'})
        assert r.status_code==200,r.text
        plan=r.json()['plan']; assert plan and len(plan['actions'])==1
        pid=plan['plan_id']; client.post(f'/api/v1/inventory/delete/{pid}/execute')
        state=wait(client,pid)
        assert state['state']=='completed',state
    assert other.exists() and not path.exists()

def test_tmdb_mode_unknown_identity_fails_closed(tmp_path,monkeypatch):
    import pytest
    app,admin,path,db=setup_app(tmp_path,monkeypatch)
    add_unregistered(tmp_path,'Other')
    with pytest.raises(ValueError,match='缺少编号'):
        partition(tmp_path,{'cloud':'cloud','share':'分享影视库'},registered_rows(app.state.organizer.store),'tmdb',db,'account')
    assert path.exists()

def test_scan_reports_unattributable_path_without_exposing_it(tmp_path,monkeypatch):
    from strimkeep.inventory_api import install_inventory
    app,admin,path,db=setup_app(tmp_path,monkeypatch)
    (tmp_path/'分享影视库'/'unknown.strm').write_text('http://example/unknown')
    install_inventory(app,app.state.organizer.store,admin)
    with TestClient(app) as client:
        app.dependency_overrides[admin]=lambda:None
        r=client.post('/api/v1/inventory/preview',json={'tie':'cloud'})
        assert r.status_code==409 and '无法确定作品归属' in r.text
        assert 'unknown.strm' not in r.text and path.exists()
