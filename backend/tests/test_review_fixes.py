import asyncio, sqlite3, tempfile
from pathlib import Path
from types import SimpleNamespace
import pytest
from strimkeep.inventory import project
from strimkeep.inventory_scope import verify_work
from strimkeep.inventory_snapshot import _fingerprint, _identity
from strimkeep.inventory_api import inventory_config_value
from strimkeep.quality import public_policy
from strimkeep.inventory_executor import execute_simulated
from strimkeep.inventory_plans import create_plan, _load
import strimkeep.inventory_scope as scope

def test_title_year_guard_checks_other_tmdb(monkeypatch, tmp_path):
    root=tmp_path; (root/'cloud').mkdir(); (root/'分享影视库').mkdir()
    paths=['cloud/剧集/Example (2020) {tmdb-12}/Season 1/S01E01.strm','分享影视库/剧集/Example (2020) {tmdb-13}/Season 1/S01E01.strm']
    for path in paths:
        p=root/path; p.parent.mkdir(parents=True,exist_ok=True); p.write_text('http://example/test')
    rows=[('cloud','c',paths[0]),('share','s',paths[1])]
    assert len(project(rows,root,'title_year')['items']) == 1
    store=SimpleNamespace(db=None,get={'organize_config':{'library_dir_name':'cloud'}}.get)
    engine=SimpleNamespace(root=root,store=store,index=SimpleNamespace(account=lambda _:'account'))
    roots={'cloud':'cloud','share':'分享影视库'}
    files=[{'side':side,'id':fid,'path':path,**_fingerprint(root,path)} for side,fid,path in rows]
    sources=[{'side':side,'id':fid,'path':path,'source_hash':'a'*64} for side,fid,path in rows]
    expected={'roots':roots,'identity':{'root':_identity(root),**{side:_identity(root/name) for side,name in roots.items()}},'files':files,'sources':sources,'policy':{'upgrade':public_policy(),'inventory':inventory_config_value({}),'guard_revision':4}}
    monkeypatch.setattr(scope,'registered_rows',lambda _:rows)
    monkeypatch.setattr(scope,'source_fingerprints',lambda db,selected,account:[s for s in sources if (s['side'],s['id'],s['path']) in set(selected)])
    assert verify_work(engine,expected,files[0],set(paths))
    (root/paths[1]).unlink()
    with pytest.raises(ValueError): verify_work(engine,expected,files[0],set(paths))

def test_batch_records_success_after_first_failure():
    db=sqlite3.connect(':memory:'); db.execute('create table meta(key text primary key,value text not null)')
    files=[{'side':'cloud','id':str(i),'path':'cloud/f'+str(i),'sha256':'a'} for i in (1,2)]
    sources=[{'side':x['side'],'id':x['id'],'path':x['path'],'source_hash':'a'*64} for x in files]
    inv={'files':files,'sources':sources}; actions=[{k:x[k] for k in ('side','id','path')} for x in files]
    pid=create_plan(db,{'inventory':inv,'actions':actions})
    async def capture(): return inv
    async def delete(*args): return True
    async def batch(evidence): return {('cloud/f1','1'):(False,'rejected'),('cloud/f2','2'):(True,'deleted')}
    with pytest.raises(ValueError): asyncio.run(execute_simulated(db,pid,capture,delete,delete_many=batch))
    result=_load(db,pid)
    assert result['state']=='failed' and result['progress']==[actions[1]]

def test_cloud_local_cleanup_failure_is_not_success(monkeypatch, tmp_path):
    import strimkeep.fsops as fsops
    from strimkeep.deep_delete import _finish_cloud_local
    path=tmp_path/'a.strm'; path.write_text('x')
    monkeypatch.setattr(fsops,'purge_strm',lambda *args,**kw: {'file':0,'dirs':0})
    with pytest.raises(RuntimeError,match='网盘已回收'):
        _finish_cloud_local(SimpleNamespace(store=SimpleNamespace()),{'path':path,'root':tmp_path})
    assert path.exists()

def test_tmdb_matching_plan_uses_scan_strategy(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from test_inventory_operations_api import setup_app
    from strimkeep.inventory_api import install_inventory
    import strimkeep.inventory_operations_api as api
    app,admin,path,db=setup_app(tmp_path,monkeypatch)
    store=app.state.organizer.store
    values={'organize_config':{'library_dir_name':'cloud'}, 'inventory_config':{'match_strategy':'tmdb'}}
    store.get=values.get
    other=tmp_path/'分享影视库/剧集/Show {tmdb-13}/Season 01/Show.S01E01.1080p.strm'
    other.parent.mkdir(parents=True); other.write_text('http://example/play115/share/s2/a')
    db.execute('INSERT INTO share_strms VALUES (?,?,?,?,?,?,?,?)',('s2','code','','f2','',other.relative_to(tmp_path).as_posix(),'hash',0)); db.commit()
    def preview(projection,*args,**kwargs):
        return {'scan_complete':True,'single':[],'dual':[{'kind':'tv','tmdb_id':12,'season':1,'winner':'cloud','action_kind':'shr','action_reason':'local_wins'}]}
    monkeypatch.setattr(api,'governance_preview',preview)
    install_inventory(app,store,admin)
    with TestClient(app) as client:
        app.dependency_overrides[admin]=lambda:None
        scan=client.post('/api/v1/inventory/preview',json={'tie':'cloud'})
        assert scan.status_code==200,scan.text
        request={'mode':'governance','scan_revision':scan.json()['scan_revision'],'selections':[{'kind':'tv','tmdb_id':12,'season':1,'winner':'cloud','action_kind':'shr','action_reason':'local_wins'}]}
        result=client.post('/api/v1/inventory/delete/plan',json=request)
        assert result.status_code==200,result.text
