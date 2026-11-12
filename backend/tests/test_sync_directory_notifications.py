import asyncio,json
from test_organize import setup,new_job
from test_phase5 import sync_config


def test_full_sync_reports_each_selected_root_even_when_empty(tmp_path):
    async def run():
        store,cloud,http,engine,_=setup(tmp_path)
        cloud.add_video(name='Movie.2024.1080p.mkv')['sha1']='A'*40
        empty=(await cloud.mkdir('0','空目录'))['cid']
        config=sync_config(['100',empty]);config['directory_labels']={'100':'电影来源',empty:'空目录'}
        jid=new_job(store,config);await engine.run_job(jid)
        assert engine.job(jid)['state']=='completed'
        await engine.notifications.job(engine.job(jid))
        saved=json.loads(engine.job(jid)['config']);stats=saved['sync_directory_stats']
        assert stats['100']['files']==1 and stats[empty]['files']==0
        assert stats['100']['directories']==1 and stats[empty]['directories']==1
        assert stats['100']['scan_seconds']>=0
        messages=store.db.execute('SELECT event_key,message FROM notifications WHERE job_id=?',(jid,)).fetchall()
        start=next(x['message'] for x in messages if x['event_key']==f'strm:{jid}:start')
        assert '2 个目录' in start and 'ID 100' in start and 'ID '+empty in start
        reports={x['event_key']:x['message'] for x in messages}
        assert '目录 ID：100' in reports[f'strm:{jid}:dir:100']
        assert '扫描文件：0 个' in reports[f'strm:{jid}:dir:{empty}']
        assert '任务总耗时' in reports[f'strm:{jid}:sync-summary']
        if engine.notice_tasks:await asyncio.gather(*engine.notice_tasks)
        await http.aclose();store.close()
    asyncio.run(run())
