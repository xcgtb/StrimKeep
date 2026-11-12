import json
from fastapi.testclient import TestClient
from test_console060 import console_app,login
from test_organize import new_job

def test_share_task_details_accept_original_without_parent_id(tmp_path,monkeypatch):
    app=console_app(tmp_path,monkeypatch)
    with TestClient(app) as client:
        login(client);store=app.state.store
        jid=new_job(store,{'job_kind':'share_organize'})
        with store.db:
            store.db.execute("INSERT INTO organize_items(job_id,file_id,original,state,step,plan) VALUES(?,?,?,?,?,?)",(jid,'share-id',json.dumps({'id':'share-id','name':'S01E24.mkv','size':42}),'done','已完成',json.dumps({'relative_path':'分享影视库/剧集/S01E24.strm','details':{'id':123}})))
        response=client.get(f'/api/v1/organize/jobs/{jid}')
        assert response.status_code==200,response.text
        item=response.json()['items'][0]
        assert item['original_name']=='S01E24.mkv' and item['source_cid']=='' and item['tmdb_id']==123
