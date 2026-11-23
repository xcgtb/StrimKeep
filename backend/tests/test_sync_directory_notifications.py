import asyncio,json,time
import pytest
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
        assert '电影来源' in start and '空目录' in start
        reports={x['event_key']:x['message'] for x in messages}
        assert set(reports) == {f'strm:{jid}:start',f'strm:{jid}:sync-summary'}
        summary=reports[f'strm:{jid}:sync-summary']
        assert '电影来源' in summary and '空目录' in summary
        assert '扫描目录：2 个' in summary and '扫描条目：1 个' in summary
        assert '核对视频：1 个' in summary and '执行耗时' in summary
        assert '含排队' not in summary
        assert '本次无变化' not in summary
        if engine.notice_tasks:await asyncio.gather(*engine.notice_tasks)
        await http.aclose();store.close()
    asyncio.run(run())


@pytest.mark.parametrize('mode,label',[('incremental','增量同步'),('full','全量同步')])
def test_sync_profile_notifications_show_task_and_mode(tmp_path,mode,label):
    async def run():
        store,cloud,http,engine,_=setup(tmp_path)
        cloud.add_video(name='Movie.2024.1080p.mkv')['sha1']='A'*40
        config={**sync_config(['100']),'mode':mode,'sync_profile_name':'电影库同步'}
        if mode=='full': config.update(_force_full=True,trust_scan=False)
        jid=new_job(store,config);await engine.run_job(jid)
        assert engine.job(jid)['state']=='completed'
        await engine.notifications.job(engine.job(jid))
        messages={row['event_key']:row['message'] for row in store.db.execute('SELECT event_key,message FROM notifications WHERE job_id=?',(jid,))}
        for key in (f'strm:{jid}:start',f'strm:{jid}:sync-summary'):
            assert '任务：电影库同步' in messages[key] and '方式：'+label in messages[key]
        assert '结果：完成' in messages[f'strm:{jid}:sync-summary']
        if engine.notice_tasks:await asyncio.gather(*engine.notice_tasks)
        await http.aclose();store.close()
    asyncio.run(run())


def test_completed_sync_summary_is_scheduler_completion_marker(tmp_path):
    async def run():
        store,cloud,http,engine,_=setup(tmp_path)
        config={**sync_config(['100']),'mode':'incremental'}
        jid=new_job(store,config)
        engine.update_job(jid,state='completed',stage='STRM 同步结束',notify_state='not_started')
        assert jid in [row['id'] for row in engine.pending_notice_jobs()]
        await engine.notifications.job(engine.job(jid))
        assert store.db.execute('select 1 from notifications where event_key=?',(f'strm:{jid}:sync-summary',)).fetchone()
        assert jid not in [row['id'] for row in engine.pending_notice_jobs()]
        await engine.notifications.job(engine.job(jid))
        assert store.db.execute('select count(*) from notifications where event_key=?',(f'strm:{jid}:sync-summary',)).fetchone()[0]==1
        await http.aclose();store.close()
    asyncio.run(run())


def test_sync_summary_distinguishes_execution_from_total(tmp_path):
    async def run():
        store, cloud, http, engine, _ = setup(tmp_path)
        now = time.time()
        config = {**sync_config(['100']), 'mode':'incremental',
                  'sync_execution_started_at': now - 4}
        jid = new_job(store, config)
        store.db.execute('UPDATE organize_jobs SET created=?,updated=?,state=? WHERE id=?',
                         (now - 12, now, 'completed', jid))
        store.db.commit()
        await engine.notifications.job(engine.job(jid))
        row = store.db.execute('SELECT message FROM notifications WHERE event_key=?',
                               (f'strm:{jid}:sync-summary',)).fetchone()
        assert '执行耗时：4.00 秒' in row['message']
        assert '任务总耗时：12.00 秒' in row['message']
        assert '含排队' not in row['message']
        await http.aclose(); store.close()
    asyncio.run(run())


def test_unexpected_start_delivery_error_is_left_for_retry(tmp_path):
    async def run():
        store, cloud, http, engine, _ = setup(tmp_path)
        store.set_many({'notification_config': {'telegram_enabled':True,
                         'bot_token':'test', 'chat_id':'1'}})
        async def broken(*args):
            raise RuntimeError('simulated transport failure')
        engine.notifications._deliver = broken
        jid = new_job(store, {**sync_config(['100']), 'notify_emby':False})
        await engine.run_job(jid)
        if engine.notice_tasks:
            await asyncio.gather(*engine.notice_tasks, return_exceptions=True)
        row = store.db.execute('SELECT telegram_state FROM notifications WHERE event_key=?',
                               (f'strm:{jid}:start',)).fetchone()
        assert row['telegram_state'] == 'unknown'
        assert engine.job(jid)['state'] == 'completed'
        await http.aclose(); store.close()
    asyncio.run(run())


@pytest.mark.parametrize('mode,state', [('sweep','completed'),('sweep','partial'),('full','partial')])
def test_sweep_and_partial_notice_completion(tmp_path,mode,state):
    async def run():
        store,cloud,http,engine,_=setup(tmp_path)
        config={**sync_config(['100']),'sync_mode':mode}
        jid=new_job(store,config)
        engine.update_job(jid,state=state,stage='结束',notify_state='not_started')
        assert jid in [row['id'] for row in engine.pending_notice_jobs()]
        await engine.notifications.job(engine.job(jid))
        assert jid not in [row['id'] for row in engine.pending_notice_jobs()]
        await http.aclose();store.close()
    asyncio.run(run())



def test_single_directory_summary_keeps_scan_and_sidecar_details(tmp_path):
    async def run():
        store,cloud,http,engine,_=setup(tmp_path)
        config={**sync_config(['100']), 'mode':'incremental', 'sync_effective_roots':['100'],
                'directory_labels':{'100':'115 / 自建库'},
                'sync_directory_stats':{'100':{'directories':3,'files':17,'scan_seconds':1.25,'local_path':'/strm/自建库'}},
                'sync_metadata_stats':{'100':{'downloaded':3,'existing':4,'failed':1,'types':{'nfo':2,'image':1}}}}
        jid=new_job(store,config);engine.update_job(jid,state='completed',stage='结束',notify_state='not_started')
        await engine.notifications.job(engine.job(jid))
        message=store.db.execute('select message from notifications where event_key=?',(f'strm:{jid}:sync-summary',)).fetchone()[0]
        for text in ('云盘目录：115 / 自建库','本地目录：/strm/自建库','扫描目录：3 个','扫描条目：17 个',
                     '目录读取耗时：1.25 秒','已存在 4 个','失败 1 个','NFO 2','图片 1'):
            assert text in message
        await http.aclose();store.close()
    asyncio.run(run())


def test_summary_separates_created_updated_and_unchanged(tmp_path):
    """通知要说清 STRM 新增/更新/未变化，以及写入的附属文件数量。"""
    async def run():
        store, cloud, http, engine, _ = setup(tmp_path)
        config = {**sync_config(['100']), 'mode': 'incremental', 'sync_effective_roots': ['100'],
                  'directory_labels': {'100': '115 / 自建库'},
                  'sync_directory_stats': {'100': {'directories': 2, 'files': 5,
                                                   'scan_seconds': 0.5}}}
        jid = new_job(store, config)
        plans = ({'sync_created': True}, {'sync_updated': True},
                 {'sync_unchanged': True},
                 {'output_changed': True, 'sync_attachment_created': True})
        with store.db:
            for index, plan in enumerate(plans):
                store.db.execute("INSERT INTO organize_items(job_id,file_id,original,plan,state)"
                                 " VALUES(?,?,?,?,'done')",
                                 (jid, 'f%d' % index, '{}', json.dumps(plan)))
        engine.update_job(jid, state='completed', stage='结束', notify_state='not_started')
        await engine.notifications.job(engine.job(jid))
        message = store.db.execute('select message from notifications where event_key=?',
                                   (f'strm:{jid}:sync-summary',)).fetchone()[0]
        for text in ('方式：增量同步', 'STRM：新增 1 个 · 更新 1 个 · 未变化 1 个',
                     '附属文件：写入 1 个（新增 1、更新 0）',
                     '扫描条目：5 个', '核对视频：4 个'):
            assert text in message
        await http.aclose(); store.close()
    asyncio.run(run())
