import asyncio,json
import pytest
from test_organize import setup,new_job,CONFIG
from strimkeep.cloud115 import CloudError


def package(cloud):
    cloud.sequence=600
    cloud.nodes['400']={'id':'400','name':'未知作品原始名称 (2024)','parent_id':'100','is_dir':True}
    cloud.nodes['401']={'id':'401','name':'Season 01','parent_id':'400','is_dir':True}
    cloud.add_video('1000','未知作品原始名称.mkv','401')
    cloud.add_video('1002','另一个未知文件.mkv','401')
    for fid,name,parent in [('1100','未知作品原始名称.zh.srt','401'),('1102','poster.jpg','400')]:
        cloud.nodes[fid]={'id':fid,'name':name,'parent_id':parent,'is_dir':False,'size':10,'pickcode':'p'+fid}


@pytest.mark.parametrize('pending',['','300'])
def test_unrecognized_package_keeps_all_names_hierarchy_and_extra_files(tmp_path,pending):
    async def run():
        store,cloud,http,engine,_=setup(tmp_path);package(cloud)
        before={k:dict(v) for k,v in cloud.nodes.items()}
        jid=new_job(store,{**CONFIG,'pending_cid':pending,'retries':0});await engine.run_job(jid)
        assert engine.job(jid)['state']=='partial',dict(engine.job(jid))
        for fid,value in before.items():
            expected={**value,'parent_id':pending} if fid=='400' and pending else value
            assert cloud.nodes[fid]==expected
        mutations=[w for w in cloud.writes if w[0] in ('rename','move')]
        assert mutations==([('move','400','300')] if pending else [])
        rows=store.db.execute('SELECT state,plan FROM organize_items WHERE job_id=?',(jid,)).fetchall()
        assert all(r['state']==('pending' if pending else 'skipped') for r in rows)
        assert not json.loads(engine.job(jid)['config'])['empty_cleanup']['entries']
        await http.aclose();store.close()
    asyncio.run(run())


def test_recognized_sibling_finishes_before_remaining_package_moves_once(tmp_path):
    async def run():
        store,cloud,http,engine,_=setup(tmp_path);package(cloud)
        cloud.add_video('1001','测试剧集 (2024) S01E04.mkv','401')
        jid=new_job(store,{**CONFIG,'retries':0});await engine.run_job(jid)
        assert engine.job(jid)['state']=='partial',dict(engine.job(jid))
        assert cloud.nodes['400']['parent_id']=='100' and cloud.nodes['401']['parent_id']=='400' and cloud.nodes['1000']['parent_id']=='300'
        row=store.db.execute("SELECT state FROM organize_items WHERE file_id='1001'").fetchone()
        assert row['state']=='done' and cloud.nodes['1001']['parent_id']!='401'
        assert ('move','400','300') not in cloud.writes and ('move','401','300') not in cloud.writes
        assert ('move','1000','300') in cloud.writes and ('move','1002','300') in cloud.writes
        assert not any(w[0]=='rename' and w[1] in ('400','401','1000','1002') for w in cloud.writes)
        assert cloud.nodes['1102']['parent_id']=='400'
        await http.aclose();store.close()
    asyncio.run(run())


def test_same_named_pending_folder_never_merges_or_flattens_package(tmp_path):
    async def run():
        store,cloud,http,engine,_=setup(tmp_path);package(cloud)
        cloud.nodes['900']={'id':'900','name':cloud.nodes['400']['name'],'parent_id':'300','is_dir':True}
        before={k:dict(v) for k,v in cloud.nodes.items()}
        jid=new_job(store,{**CONFIG,'retries':0});await engine.run_job(jid)
        assert cloud.nodes==before and not any(w[0] in ('rename','move') for w in cloud.writes)
        assert all(r[0]=='skipped' for r in store.db.execute('SELECT state FROM organize_items WHERE job_id=?',(jid,)))
        assert '同名目录' in store.db.execute('SELECT message FROM organize_items WHERE job_id=?',(jid,)).fetchone()[0]
        await http.aclose();store.close()
    asyncio.run(run())


def test_lost_move_response_is_confirmed_without_second_move(tmp_path):
    async def run():
        store,cloud,http,engine,_=setup(tmp_path);package(cloud)
        move=cloud.move
        async def lost(fid,dest):
            await move(fid,dest)
            if fid=='400':raise CloudError('response lost','CLOUD_UNAVAILABLE')
        cloud.move=lost
        jid=new_job(store,{**CONFIG,'retries':0});await engine.run_job(jid)
        assert cloud.nodes['400']['parent_id']=='300'
        assert sum(w==('move','400','300') for w in cloud.writes)==1
        assert all(r[0]=='pending' for r in store.db.execute('SELECT state FROM organize_items WHERE job_id=?',(jid,)))
        await http.aclose();store.close()
    asyncio.run(run())


def test_broad_collection_never_moves_with_one_unrecognized_movie(tmp_path):
    async def run():
        store,cloud,http,engine,_=setup(tmp_path)
        cloud.nodes['400']={'id':'400','name':'外语电影','parent_id':'100','is_dir':True}
        cloud.nodes['401']={'id':'401','name':'未知作品原始名称 (2024)','parent_id':'400','is_dir':True}
        cloud.nodes['402']={'id':'402','name':'别的作品','parent_id':'400','is_dir':True}
        cloud.add_video('1000','未知作品原始名称.mkv','401')
        cloud.add_video('1001','未纳入整理的原盘.iso','402')
        jid=new_job(store,{**CONFIG,'retries':0});await engine.run_job(jid)
        assert cloud.nodes['400']['parent_id']=='100'
        assert cloud.nodes['401']['parent_id']=='300'
        assert cloud.nodes['402']['parent_id']=='400' and cloud.nodes['1001']['parent_id']=='402'
        assert ('move','400','300') not in cloud.writes
        await http.aclose();store.close()
    asyncio.run(run())


def test_unprocessed_iso_stays_and_only_failed_videos_move(tmp_path):
    async def run():
        store,cloud,http,engine,_=setup(tmp_path);package(cloud)
        cloud.add_video('1003','other-movie.iso','401')
        jid=new_job(store,{**CONFIG,'retries':0});await engine.run_job(jid)
        assert cloud.nodes['400']['parent_id']=='100' and cloud.nodes['401']['parent_id']=='400'
        assert cloud.nodes['1003']['parent_id']=='401'
        assert cloud.nodes['1000']['parent_id']=='300' and cloud.nodes['1002']['parent_id']=='300'
        assert ('move','400','300') not in cloud.writes and ('move','401','300') not in cloud.writes
        assert all(r['state']=='pending' for r in store.db.execute('SELECT state FROM organize_items WHERE job_id=?',(jid,)))
        await http.aclose();store.close()
    asyncio.run(run())


@pytest.mark.parametrize('folder', ['外语电影', '外语电影 (2024)', '4K资源 (2024)', 'BluRay REMUX'])
def test_category_directory_with_single_failed_file_is_not_moved(tmp_path, folder):
    async def run():
        store,cloud,http,engine,_=setup(tmp_path)
        cloud.nodes['400']={'id':'400','name':folder,'parent_id':'100','is_dir':True}
        cloud.add_video('1000','未知作品原始名称.mkv','400')
        jid=new_job(store,{**CONFIG,'retries':0});await engine.run_job(jid)
        assert cloud.nodes['400']['parent_id']=='100'
        assert cloud.nodes['1000']['parent_id']=='300'
        assert ('move','400','300') not in cloud.writes
        await http.aclose();store.close()
    asyncio.run(run())


def test_unrelated_empty_directory_never_moves_with_failed_file(tmp_path):
    async def run():
        store,cloud,http,engine,_=setup(tmp_path);package(cloud)
        cloud.nodes['402']={'id':'402','name':'已处理旧电影','parent_id':'400','is_dir':True}
        jid=new_job(store,{**CONFIG,'retries':0});await engine.run_job(jid)
        assert cloud.nodes['400']['parent_id']=='100' and cloud.nodes['402']['parent_id']=='400'
        assert cloud.nodes['1000']['parent_id']=='300' and cloud.nodes['1002']['parent_id']=='300'
        assert ('move','400','300') not in cloud.writes
        await http.aclose();store.close()
    asyncio.run(run())


def test_ambiguous_unqualified_folder_moves_only_file(tmp_path):
    async def run():
        store,cloud,http,engine,_=setup(tmp_path)
        cloud.nodes['400']={'id':'400','name':'各种未整理目录','parent_id':'100','is_dir':True}
        cloud.add_video('1000','未知作品原始名称.mkv','400')
        jid=new_job(store,{**CONFIG,'retries':0});await engine.run_job(jid)
        assert cloud.nodes['400']['parent_id']=='100' and cloud.nodes['1000']['parent_id']=='300'
        assert ('move','400','300') not in cloud.writes
        await http.aclose();store.close()
    asyncio.run(run())


def test_all_seasons_unrecognized_preserves_complete_work_directory(tmp_path):
    async def run():
        store,cloud,http,engine,_=setup(tmp_path);package(cloud)
        cloud.nodes['405']={'id':'405','name':'Season 02','parent_id':'400','is_dir':True}
        cloud.add_video('1005','另一份未知名称.mkv','405')
        jid=new_job(store,{**CONFIG,'retries':0});await engine.run_job(jid)
        assert cloud.nodes['400']['parent_id']=='300'
        assert cloud.nodes['401']['parent_id']=='400' and cloud.nodes['405']['parent_id']=='400'
        assert cloud.nodes['1000']['parent_id']=='401' and cloud.nodes['1005']['parent_id']=='405'
        assert sum(w==('move','400','300') for w in cloud.writes)==1
        await http.aclose();store.close()
    asyncio.run(run())


def test_empty_season_not_in_failed_batch_does_not_move(tmp_path):
    async def run():
        store,cloud,http,engine,_=setup(tmp_path);package(cloud)
        cloud.nodes['405']={'id':'405','name':'Season 02','parent_id':'400','is_dir':True}
        jid=new_job(store,{**CONFIG,'retries':0});await engine.run_job(jid)
        assert cloud.nodes['400']['parent_id']=='100' and cloud.nodes['405']['parent_id']=='400'
        assert cloud.nodes['1000']['parent_id']=='300' and cloud.nodes['1002']['parent_id']=='300'
        assert ('move','400','300') not in cloud.writes
        await http.aclose();store.close()
    asyncio.run(run())
