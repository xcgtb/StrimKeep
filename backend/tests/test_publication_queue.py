import asyncio
import pytest
from strimkeep.cloud115 import CloudError
from test_organize import CONFIG, setup, new_job


def test_job_waits_for_background_strm_before_completion(tmp_path):
    async def scenario():
        store, cloud, http, engine, _ = setup(tmp_path)
        cloud.add_video()
        entered=asyncio.Event();release=asyncio.Event();original=engine.export
        async def blocked(*args):
            entered.set();await release.wait();return await original(*args)
        engine.export=blocked
        jid=new_job(store,{**CONFIG,'notify_emby':False})
        task=asyncio.create_task(engine.run_job(jid))
        await asyncio.wait_for(entered.wait(),5)
        assert engine.job(jid)['state']=='running'
        row=store.db.execute('SELECT state FROM organize_items WHERE job_id=?',(jid,)).fetchone()
        assert row['state']=='archived'
        release.set();await asyncio.wait_for(task,5)
        assert engine.job(jid)['state']=='completed'
        assert len(list(engine.root.rglob('*.strm')))==1
        await http.aclose();store.close()
    asyncio.run(scenario())


def test_delayed_publication_does_not_hold_organize_gate(tmp_path):
    async def scenario():
        store,cloud,http,engine,_=setup(tmp_path)
        cloud.add_video('1001','测试剧集 (2024) S01E01.WEB-DL.mkv')
        cloud.add_video('1002','测试剧集 (2024) S01E02.WEB-DL.mkv')
        second_moved=asyncio.Event();move=cloud.move;count=0
        async def observed(fid,dest):
            nonlocal count
            await move(fid,dest);count+=1
            if count==2:second_moved.set()
        cloud.move=observed
        jid=new_job(store,{**CONFIG,'notify_emby':False,'strm_delay':2})
        task=asyncio.create_task(engine.run_job(jid))
        await asyncio.wait_for(second_moved.wait(),1.5)
        assert not list(engine.root.rglob('*.strm'))
        await asyncio.wait_for(task,5)
        assert len(list(engine.root.rglob('*.strm')))==2
        await http.aclose();store.close()
    asyncio.run(scenario())


def test_background_export_failure_makes_job_partial(tmp_path):
    async def scenario():
        store, cloud, http, engine, _=setup(tmp_path);cloud.add_video()
        async def fail(*args):raise OSError('synthetic failure')
        engine.export=fail
        jid=new_job(store,{**CONFIG,'notify_emby':False});await engine.run_job(jid)
        row=store.db.execute('SELECT state,step FROM organize_items WHERE job_id=?',(jid,)).fetchone()
        assert row['state']=='review' and '后台 STRM' in row['step']
        assert engine.job(jid)['state']=='partial'
        await http.aclose();store.close()
    asyncio.run(scenario())


def test_background_login_failure_halts_job(tmp_path):
    async def scenario():
        store,cloud,http,engine,_=setup(tmp_path);cloud.add_video()
        async def fail(*args):raise CloudError('登录失效','LOGIN_INVALID',401)
        engine.export=fail
        jid=new_job(store,{**CONFIG,'notify_emby':False});await engine.run_job(jid)
        assert engine.job(jid)['state']=='waiting_login'
        assert store.db.execute('SELECT state FROM organize_items WHERE job_id=?',(jid,)).fetchone()['state']=='archived'
        await http.aclose();store.close()
    asyncio.run(scenario())


def test_background_deleted_video_is_skipped_and_other_file_continues(tmp_path):
    async def scenario():
        store,cloud,http,engine,_=setup(tmp_path)
        cloud.add_video('1001','测试剧集 (2024) S01E01.WEB-DL.mkv')
        cloud.add_video('1002','测试剧集 (2024) S01E02.WEB-DL.mkv')
        original=engine.finish_publication
        async def finish(jid,item_id,source,actual,plan,config):
            if source['id']=='1001':
                del cloud.nodes['1001']
                raise CloudError('文件不存在','FILE_NOT_FOUND',404)
            return await original(jid,item_id,source,actual,plan,config)
        engine.finish_publication=finish
        jid=new_job(store,{**CONFIG,'notify_emby':False});await engine.run_job(jid)
        states={r['file_id']:r['state'] for r in store.db.execute('SELECT file_id,state FROM organize_items WHERE job_id=?',(jid,))}
        assert states=={'1001':'skipped','1002':'done'}
        assert engine.job(jid)['state']=='completed'
        await http.aclose();store.close()
    asyncio.run(scenario())


def test_restart_resumes_archived_publication(tmp_path):
    async def scenario():
        store,cloud,http,engine,_=setup(tmp_path);cloud.add_video()
        entered=asyncio.Event();original=engine.export
        async def blocked(*args):
            entered.set();await asyncio.Event().wait()
        engine.export=blocked
        jid=new_job(store,{**CONFIG,'notify_emby':False})
        task=asyncio.create_task(engine.run_job(jid));await asyncio.wait_for(entered.wait(),5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
        assert store.db.execute('SELECT state FROM organize_items WHERE job_id=?',(jid,)).fetchone()['state']=='archived'
        engine.export=original;engine.update_job(jid,state='running')
        await engine.run_job(jid)
        assert engine.job(jid)['state']=='completed'
        assert len(list(engine.root.rglob('*.strm')))==1
        await http.aclose();store.close()
    asyncio.run(scenario())


def test_same_season_is_enqueued_as_one_directory_increment(tmp_path):
    async def scenario():
        store, cloud, http, engine, _ = setup(tmp_path)
        for i in (1, 2, 3):
            cloud.add_video(str(1000+i), f'测试剧集 (2024) S01E0{i}.WEB-DL.mkv')
        jid = new_job(store, {**CONFIG, 'notify_emby': False})
        await engine.run_job(jid)
        assert engine.job(jid)['state'] == 'completed'
        events = [r[0] for r in store.db.execute("SELECT message FROM events WHERE kind='organize' AND message LIKE ?", (f'任务 {jid}：%后台 STRM 队列%',))]
        assert len(events) == 1 and '3 个文件' in events[0]
        scans = [r[0] for r in store.db.execute("SELECT message FROM events WHERE kind='organize' AND message LIKE ?", (f'任务 {jid}：增量同步目录%',))]
        assert len(scans) == 1 and '处理 3 个归档' in scans[0]
        assert len(list(engine.root.rglob('*.strm'))) == 3
        await http.aclose(); store.close()
    asyncio.run(scenario())
