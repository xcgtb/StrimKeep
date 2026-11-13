import asyncio
from types import SimpleNamespace

import pytest

from strimkeep.cloud115 import CloudError
from strimkeep.inventory_cloud_orphans import scan, clean_one
from strimkeep.store import Store


class FakeCloud:
    def __init__(self):
        self.nodes={
            '100':{'id':'100','parent_id':'0','name':'媒体库','is_dir':True},
            '200':{'id':'200','parent_id':'100','name':'电影','is_dir':True},
            '300':{'id':'300','parent_id':'200','name':'作品 (2024) {tmdb-1}','is_dir':True},
            '301':{'id':'301','parent_id':'300','name':'Season 1','is_dir':True},
            '400':{'id':'400','parent_id':'301','name':'episode.srt','is_dir':False,'size':10,'sha1':'ABC'},
            '401':{'id':'401','parent_id':'300','name':'poster.jpg','is_dir':False,'size':20,'sha1':'DEF'},
            '500':{'id':'500','parent_id':'200','name':'仍有视频 (2024) {tmdb-2}','is_dir':True},
            '501':{'id':'501','parent_id':'500','name':'film.mkv','is_dir':False,'size':99,'sha1':'VID'},
        }
        self.recycle_bin=[]; self.writes=[]; self.fail_after=None

    async def download_tree(self, root):
        nodes=[n.copy() for n in self.nodes.values() if n['id']!=root]
        return [n for n in nodes if n['is_dir']], [n for n in nodes if not n['is_dir']]

    async def path_info(self, cid):
        result=[]
        while cid!='0':
            n=self.nodes[cid]; result.append({'id':cid,'name':n['name']}); cid=n['parent_id']
        return [{'id':'0','name':''},*reversed(result)]

    async def children_verified(self, cid):
        return [n.copy() for n in self.nodes.values() if n['parent_id']==cid]

    async def file(self, fid): return self.nodes[fid].copy()
    async def recycle_files(self): return list(self.recycle_bin)
    async def live_file_exists(self, fid): return fid in self.nodes
    def forget_children(self, ids): pass

    async def recycle(self, fid, parent):
        n=self.nodes[fid]
        assert n['parent_id']==parent
        self.writes.append(fid); self.nodes.pop(fid)
        self.recycle_bin.append({'id':fid,'rid':'r'+fid,'name':n['name'],
                                 'size':n.get('size',0),'parent_id':parent,'is_dir':n['is_dir']})
        if self.fail_after==fid: raise CloudError('模拟网络中断')

    async def recycle_empty_directory(self, fid, parent):
        assert not await self.children_verified(fid)
        await self.recycle(fid,parent)


def setup(tmp_path):
    store=Store(tmp_path/'data','admin','test-password-1234')
    root=tmp_path/'strm'; (root/'媒体库'/'电影'/'作品 (2024) {tmdb-1}'/'Season 1').mkdir(parents=True)
    (root/'媒体库'/'电影'/'作品 (2024) {tmdb-1}'/'poster.jpg').write_bytes(b'local')
    cloud=FakeCloud()
    engine=SimpleNamespace(cloud=cloud,store=store,root=root,index=SimpleNamespace(account=lambda config:'1'))
    return store,cloud,engine


def test_scan_and_recycle_only_the_selected_metadata_work(tmp_path):
    async def run():
        store,cloud,engine=setup(tmp_path)
        result=await scan(engine,'100','1','媒体库')
        assert len(result['items'])==1 and result['skipped']==1
        item=result['items'][0]
        assert item['local_path']=='媒体库/电影/作品 (2024) {tmdb-1}'
        assert set(item['file_names'])=={'episode.srt','poster.jpg'}
        assert not cloud.writes
        cleaned=await clean_one(engine,'100','媒体库',item['id'],item['fingerprint'])
        assert cleaned['errors']==[] and cleaned['files_removed']==2 and cleaned['dirs_removed']==2
        assert set(cloud.writes)=={'300','301','400','401'}
        assert '501' in cloud.nodes and '200' in cloud.nodes and '100' in cloud.nodes
        assert (engine.root/item['local_path']/'poster.jpg').is_file()
        store.close()
    asyncio.run(run())


def test_scan_or_clean_never_authorizes_changed_video_or_local_strm(tmp_path):
    async def run():
        store,cloud,engine=setup(tmp_path)
        item=(await scan(engine,'100','1','媒体库'))['items'][0]
        cloud.nodes['402']={'id':'402','parent_id':'300','name':'new.mkv','is_dir':False,'size':30,'sha1':'123'}
        with pytest.raises(ValueError,match='视频或未知'):
            await clean_one(engine,'100','媒体库',item['id'],item['fingerprint'])
        assert not cloud.writes
        cloud.nodes.pop('402')
        (engine.root/item['local_path']/'keep.strm').write_text('link')
        result=await scan(engine,'100','1','媒体库')
        assert not result['items'] and not cloud.writes
        store.close()
    asyncio.run(run())


def test_uncertain_recycle_is_recorded_and_never_automatically_repeated(tmp_path):
    async def run():
        store,cloud,engine=setup(tmp_path)
        item=(await scan(engine,'100','1','媒体库'))['items'][0]
        cloud.fail_after='400'
        result=await clean_one(engine,'100','媒体库',item['id'],item['fingerprint'])
        assert result['errors'] and cloud.writes==['400']
        cloud.nodes['400']={'id':'400','parent_id':'301','name':'episode.srt','is_dir':False,'size':10,'sha1':'ABC'}
        cloud.fail_after=None
        newer=(await scan(engine,'100','1','媒体库'))['items'][0]
        result=await clean_one(engine,'100','媒体库',newer['id'],newer['fingerprint'])
        assert result['errors'] and cloud.writes==['400']
        store.close()
    asyncio.run(run())


def test_duplicate_cloud_paths_are_not_offered(tmp_path):
    async def run():
        store,cloud,engine=setup(tmp_path)
        cloud.nodes['600']={'id':'600','parent_id':'200','name':'作品 (2024) {tmdb-1}','is_dir':True}
        result=await scan(engine,'100','1','媒体库')
        assert not result['items'] and not cloud.writes
        store.close()
    asyncio.run(run())


def test_account_change_invalidates_scan_before_recycle(tmp_path):
    async def run():
        store,cloud,engine=setup(tmp_path)
        item=(await scan(engine,'100','1','媒体库'))['items'][0]
        engine.index=SimpleNamespace(account=lambda config:'2')
        with pytest.raises(ValueError,match='账号'):
            await clean_one(engine,'100','媒体库',item['id'],item['fingerprint'])
        assert not cloud.writes
        store.close()
    asyncio.run(run())


def test_cloud_api_checks_auth_and_local_before_any_recycle(tmp_path):
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient
    from strimkeep.inventory_api import install_inventory
    store, cloud, engine = setup(tmp_path)
    store.set_many({'organize_config':{'archive_cid':'100', 'library_dir_name':'媒体库'}})
    engine.publication_gate=asyncio.Lock(); engine.scan_receive_gate=asyncio.Lock(); engine.cleanup_gate=asyncio.Lock()
    app=FastAPI(); app.state.organizer=engine; app.state.strms=engine.root
    def admin(): raise HTTPException(401)
    install_inventory(app,store,admin)
    with TestClient(app) as client:
        assert client.post('/api/v1/inventory/cloud-orphans/scan').status_code==401
        app.dependency_overrides[admin]=lambda:None
        response=client.post('/api/v1/inventory/cloud-orphans/scan')
        assert response.status_code==200, response.text
        item=response.json()['items'][0]
        target=engine.root/item['local_path']/'restored.strm'
        target.write_text('link')
        payload={'items':[{'id':item['id'],'fingerprint':item['fingerprint']}]}
        rejected=client.post('/api/v1/inventory/cloud-orphans/clean',json=payload).json()['items'][0]
        assert rejected['errors'] and not cloud.writes
        target.unlink()
        completed=client.post('/api/v1/inventory/cloud-orphans/clean',json=payload).json()['items'][0]
        assert not completed['errors'] and completed['files_removed']==2
    store.close()
