import asyncio
from types import SimpleNamespace
import pytest
import strimkeep.cloud115 as module
from strimkeep.cloud115 import Cloud115,CloudError
from test_organize import setup,new_job
from test_phase5 import sync_config
import json

# Cloud115 没有实现递归清单（download_tree / recursive_files_verified），
# sync.py 用 getattr 门控，缺了就一直走逐目录扫描。这两组用例是「将来要做」的规格，
# 现在跑必然是 AttributeError；实现后删掉这一行即可恢复。


def make_fake(monkeypatch, issue=None, total=5001):
    monkeypatch.setattr(module,'to_id',lambda pc: int(pc))
    c=object.__new__(Cloud115)
    api=SimpleNamespace(fs_file=lambda *a,**k:None,download_folders_app=lambda *a,**k:None,download_files_app=lambda *a,**k:None,fs_file_skim=lambda *a,**k:None)
    c.client=lambda:api
    calls=[]
    async def call(fn,*args,**kw):
        if fn is api.fs_file:return {'data':[{'cid':'100','pc':'rootpc'}]}
        if fn is api.download_folders_app:
            return {'data':{'root':{'fid':'100'},'list':[{'fid':'200','pid':'100','fn':'电影'}],'count':1,'has_next_page':False}}
        if fn is api.download_files_app:
            page=args[0]['page'];calls.append(('page',page));start=(page-1)*5000;end=min(start+5000,total)
            rows=[{'pid':'200','pc':str(1000+i),'fs':5} for i in range(start,end)]
            if issue=='duplicate' and page==2 and rows:rows[0]['pc']='1000'
            if issue=='truncated' and page==2:rows=[]
            return {'data':{'root':{'fid':'100'},'list':rows,'count':end-start,'has_next_page':end<total}}
        if getattr(fn,'func',None) is api.fs_file_skim:
            ids=args[0];calls.append(('batch',len(ids)))
            result=[{'file_id':fid,'file_name':fid+'.mkv','pick_code':fid,'sha1':'A'*40,'file_size':'5'} for fid in ids]
            if issue=='missing_detail':result=result[:-1]
            return {'data':result}
        raise AssertionError('unexpected request')
    c.call=call
    return c,calls


def test_download_tree_pages_and_bulk_details(monkeypatch):
    c,calls=make_fake(monkeypatch)
    dirs,files=asyncio.run(c.download_tree('100'))
    assert len(dirs)==1 and len(files)==5001
    assert [x for x in calls if x[0]=='page']==[('page',1),('page',2)]
    assert [x for x in calls if x[0]=='batch']==[('batch',5000),('batch',1)]
    assert files[-1]['name']=='6000.mkv' and files[-1]['parent_id']=='200'

@pytest.mark.parametrize('issue',['duplicate','truncated','missing_detail'])
def test_download_tree_rejects_incomplete_pages_or_details(monkeypatch,issue):
    c,_=make_fake(monkeypatch,issue)
    with pytest.raises(CloudError) as exc:asyncio.run(c.download_tree('100'))
    assert exc.value.code=='INCOMPLETE_LIST'


def test_batched_manifest_matches_tree_and_binds_subtitles(tmp_path):
    async def run():
        store,cloud,http,engine,_=setup(tmp_path)
        folder=(await cloud.mkdir('100','剧集'))['cid']
        empty=(await cloud.mkdir('100','空目录'))['cid']
        cloud.add_video('1001','Movie.2024.mkv',folder)
        cloud.add_video('1002','Movie.2024.srt',folder)
        config=sync_config(['100']);old=new_job(store,config);engine.update_job(old,state='running')
        await engine.sync.manifest(old,dict(config))
        async def batched(root):
            return ([cloud.nodes[folder].copy(),cloud.nodes[empty].copy()],
                    [cloud.nodes['1001'].copy(),cloud.nodes['1002'].copy()])
        cloud.download_tree=batched
        new=new_job(store,config);engine.update_job(new,state='running')
        await engine.sync.manifest(new,dict(config))
        def records(jid):
            rows=[json.loads(r[0]) for r in store.db.execute('select original from organize_items where job_id=?',(jid,))]
            for row in rows:
                row.pop('_scan_at',None)
                for sub in row.get('subtitles',[]):sub.pop('_scan_at',None)
            return rows
        assert records(old)==records(new)
        saved=json.loads(engine.job(new)['config'])
        assert saved['sync_directory_stats']['100']['directories']==3
        await http.aclose();store.close()
    asyncio.run(run())
