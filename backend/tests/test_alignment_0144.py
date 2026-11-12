from fastapi.testclient import TestClient
from strimkeep.inventory import project
from strimkeep.library_mapping import card, remember_status
from test_inventory_mapping import Store, item, row, missing_reply
from test_inventory_operations_api import setup_app


def test_e00_is_projected_as_a_special_episode(tmp_path):
    path=tmp_path/'cloud/Show (2020) {tmdb-12}/Season 1/Show.S01E00.strm'
    path.parent.mkdir(parents=True); path.write_text('http://example')
    result=project([('cloud','1',path.relative_to(tmp_path).as_posix())],tmp_path)
    assert result['complete'] and result['items'][0]['cloud'][0]['episodes']==[0]


def test_manual_completion_never_hides_extra_or_pending():
    store=Store(); work=item('tv',1,'show',[row('cloud',1,1)],manual=True)
    assert card(work,store)['status']=='pending'
    remember_status(store,1,missing_reply(combined_extra=2),work)
    assert card(work,store)['status']=='extra'
    remember_status(store,1,missing_reply(combined_missing=2),work)
    assert card(work,store)['status']=='complete'


def test_ongoing_with_aired_episodes_present_is_complete():
    store=Store(); work=item('tv',2,'show',[row('cloud',1,1)])
    remember_status(store,2,missing_reply(series_status='Returning Series'),work)
    assert card(work,store)['status']=='complete'
    remember_status(store,2,missing_reply(series_status='Returning Series',combined_missing=1),work)
    assert card(work,store)['status']=='ongoing'


def test_gap_switch_rejects_direct_dual_request(tmp_path,monkeypatch):
    import strimkeep.inventory_operations_api as api
    from strimkeep.inventory import registered_rows
    from strimkeep.inventory_api import inventory_config_value
    from strimkeep.inventory_revision import governance_revision
    from strimkeep.inventory_sources import source_fingerprints
    from strimkeep.quality import public_policy
    app,admin,path,db=setup_app(tmp_path,monkeypatch); store=app.state.organizer.store
    projection=project(registered_rows(store),tmp_path)
    rev=governance_revision(projection,public_policy(),inventory_config_value({}),source_fingerprints(db,registered_rows(store),'account'))
    monkeypatch.setattr(api,'governance_preview',lambda *args,**kwargs:{'scan_complete':True,'single':[],'dual':[{'kind':'tv','tmdb_id':12,'season':1,'winner':'cloud','action_kind':'shr','incomplete_sides':['share']}]})
    with TestClient(app) as client:
        app.dependency_overrides[admin]=lambda:None
        for selection in ({'kind':'tv','tmdb_id':12,'season':1,'winner':'cloud','action_kind':'shr'}, {'kind':'tv','tmdb_id':12,'season':1,'winner':'cloud'}):
            response=client.post('/api/v1/inventory/delete/plan',json={'mode':'dual','scan_revision':rev,'selections':[selection]})
            assert response.status_code==409 and '缺集季' in response.text,response.text
    assert path.exists()


def test_gap_switch_excludes_automatic_plan_until_enabled(tmp_path,monkeypatch):
    import strimkeep.inventory_api as api
    from strimkeep.inventory_api import install_inventory
    app,admin,path,db=setup_app(tmp_path,monkeypatch); store=app.state.organizer.store
    values={'organize_config':{'library_dir_name':'cloud'},'inventory_config':{'single_gaps':False}}
    store.get=lambda key,default=None:values.get(key,default)
    async def build(body,ttl=900,reuse=None): return {'plan_id':'test','actions':[]}
    app.state.inventory_build_delete_plan=build
    monkeypatch.setattr(api,'governance_preview',lambda *a,**kw:{'scan_complete':True,'single':[],'blocked':[],'dual':[{'kind':'tv','tmdb_id':12,'season':1,'winner':'cloud','action_kind':'shr','action_reason':'incomplete_delete_share','incomplete_sides':['share']}]})
    install_inventory(app,store,admin)
    with TestClient(app) as client:
        app.dependency_overrides[admin]=lambda:None
        response=client.post('/api/v1/inventory/preview',json={'tie':'cloud'})
        assert response.status_code==200,response.text
        assert response.json()['plan'] is None
        values['inventory_config']['single_gaps']=True
        response=client.post('/api/v1/inventory/preview',json={'tie':'cloud'})
        assert response.status_code==200,response.text
        assert response.json()['plan']['plan_id']=='test'
    assert path.exists()
