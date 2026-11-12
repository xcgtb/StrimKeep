import asyncio, sqlite3
import pytest
from strimkeep.inventory_executor import execute_simulated
from strimkeep.inventory_plans import create_plan, _load, claim_plan, record_progress, PlanError

def fixture(count):
    db=sqlite3.connect(':memory:'); db.execute('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
    files=[{'side':'share','id':str(i),'path':'share/%s.strm'%i} for i in range(count)]
    sources=[{**v,'source_hash':'a'*64} for v in files]
    inv={'files':files,'sources':sources}; actions=[dict(v) for v in files]
    pid=create_plan(db,{'inventory':inv,'actions':actions})
    return db,inv,actions,pid

def test_over_thousand_executes_each_once_and_records_progress():
    db,inv,actions,pid=fixture(1105); calls=[]
    async def capture(): return inv
    async def verify(expected,action): return True
    async def delete(evidence,source): calls.append(evidence['id']); return True
    result=asyncio.run(execute_simulated(db,pid,capture,delete,verify_work=verify))
    assert result['state']=='completed' and len(calls)==1105 and len(set(calls))==1105
    assert len(_load(db,pid)['progress'])==1105
    assert db.execute('SELECT COUNT(*) FROM inventory_plan_progress').fetchone()[0]==0

def test_interrupted_progress_survives_and_duplicate_rejected():
    db,inv,actions,pid=fixture(2); plan={'inventory':inv,'actions':actions}
    claim=claim_plan(db,pid,plan); record_progress(db,pid,claim,actions[0])
    assert _load(db,pid)['progress']==[actions[0]]
    with pytest.raises(PlanError): record_progress(db,pid,claim,actions[0])
    assert _load(db,pid)['progress']==[actions[0]]

def test_progress_survives_connection_restart(tmp_path):
    path=tmp_path/'test.db'; db=sqlite3.connect(path)
    db.execute('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
    actions=[{'side':'share','id':'x','path':'share/x.strm'}]
    inv={'files':actions,'sources':[{**actions[0],'source_hash':'b'*64}]}
    plan={'inventory':inv,'actions':actions}; pid=create_plan(db,plan); claim=claim_plan(db,pid,plan)
    record_progress(db,pid,claim,actions[0]); db.close()
    db=sqlite3.connect(path)
    assert _load(db,pid)['state']=='executing' and _load(db,pid)['progress']==actions
    db.close()

def test_http_plan_accepts_more_than_thousand_files(tmp_path,monkeypatch):
    from test_inventory_operations_api import setup_app
    from fastapi.testclient import TestClient
    app,admin,path,db=setup_app(tmp_path,monkeypatch)
    for i in range(2,1003):
        season=(i-1)//500+1; episode=(i-1)%500+1
        relative=f'分享影视库/剧集/Show {{tmdb-12}}/Season {season:02d}/Show.S{season:02d}E{episode:03d}.1080p.strm'
        target=tmp_path/relative; target.parent.mkdir(parents=True,exist_ok=True); target.write_text('http://example/play115/share/s%s/a'%i)
        db.execute('INSERT INTO share_strms VALUES (?,?,?,?,?,?,?,?)',(f's{i}','code','',f'f{i}','',relative,'hash',0))
    db.commit()
    with TestClient(app) as client:
        app.dependency_overrides[admin]=lambda:None
        response=client.post('/api/v1/inventory/delete/plan',json={'mode':'manual','selections':[{'kind':'tv','tmdb_id':12,'side':'share'}]})
        assert response.status_code==200,response.text
        assert len(response.json()['actions'])==1002
    assert path.exists()
