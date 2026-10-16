"""Real files/SQLite and bounded concurrency, never a live 115/TG request."""
import copy
import json
import threading
from pathlib import Path
from unittest.mock import Mock
import pytest
from app import tg, runtime_logs
from app.cloud import workflow, jobs, store, strm, settings, notifications, recognition
from app.cloud.errors import CloudError
from dev.tests.test_cloud_workbench import cloud
from dev.tests.test_cloud_notification_stages import enable


def plan_files(cloud, count=2):
    first = workflow.plan(cloud.id,'1','2')
    with store.db() as con:
        data = json.loads(con.execute('SELECT payload FROM cloud_plans WHERE id=?',(first['id'],)).fetchone()[0])
        template=data['items'][0]
        data['items']=[]
        for i in range(count):
            item=copy.deepcopy(template)
            item['source'].update(id=str(80+i),name='Show.S01E'+str(i+1)+'.mkv')
            item['followers']=[]
            item['media'].update(kind='tv',season=1,episode=i+1,title='Show',directory='剧集/欧美剧集/Show/Season 01',name='Show.S01E'+str(i+1)+'.mkv',season_episode_count=22)
            cloud.remote.files[item['source']['id']]=copy.deepcopy(item['source'])
            data['items'].append(item)
        con.execute('UPDATE cloud_plans SET payload=? WHERE id=?',(store.encode(data),first['id']))
    return first


def test_incremental_publisher_overlaps_next_move_but_card_waits_for_files(cloud,monkeypatch):
    calls=enable(monkeypatch)
    plan=plan_files(cloud)
    entered, moved=threading.Event(),threading.Event()
    generate, move=strm.generate,cloud.remote.move
    job_thread=threading.get_ident();threads=[]
    def publish(aid,row,*args,**kwargs):
        assert threading.get_ident()!=job_thread
        if row['id']=='80':
            entered.set()
            assert moved.wait(3),'organizer must advance while STRM publication waits'
        return generate(aid,row,*args,**kwargs)
    def relocate(fid,parent):
        threads.append(threading.get_ident())
        if fid=='81':
            assert entered.wait(3)
            move(fid,parent);moved.set()
        else:move(fid,parent)
    monkeypatch.setattr(strm,'generate',publish)
    monkeypatch.setattr(cloud.remote,'move',relocate)
    def card(text,image=''):
        assert len(list(cloud.root.rglob('*.strm')))==2
        assert '本次：2 集' in text and '共 22 集（TMDB）' in text
        calls.append(('card',text));return True
    monkeypatch.setattr(tg,'notify_telegram_card',card)
    jid=workflow.apply(plan['id'],True);jobs.execute(jid)
    assert store.job(jid)['state']=='success'
    assert set(threads)=={job_thread}
    assert [c[0] for c in calls]==['card','text','text']
    assert '同步目录：1 个' in calls[1][1] and '目录成功：1 个' in calls[1][1]
    with store.db() as con:
        steps=con.execute("SELECT state FROM cloud_steps WHERE job_id=? AND step_key LIKE 'publication:%'",(jid,)).fetchall()
    assert steps==[('verified',),('verified',)]
    logs=runtime_logs.read(source='cloud',limit=500)['logs']
    assert any(r.get('details',{}).get('event')=='publish_queue' and r.get('details',{}).get('job_id')==jid for r in logs)


def test_output_failure_does_not_emit_ready_card(cloud,monkeypatch):
    calls=enable(monkeypatch)
    plan=workflow.plan(cloud.id,'1','2')
    monkeypatch.setattr(strm,'generate',Mock(side_effect=CloudError('输出挂载不可写',409)))
    jid=workflow.apply(plan['id'],True);jobs.execute(jid)
    result=store.job(jid)
    assert result['state']=='needs_review' and not result['result']['generated']
    assert len(result['result']['files'])==1
    assert not [c for c in calls if c[0]=='card']
    assert '目录异常：1 个' in calls[0][1]
    jobs.execute(jid)
    assert strm.generate.call_count==1


def test_actual_file_is_verified_before_poster_and_source_replacement(cloud,monkeypatch):
    calls=enable(monkeypatch)
    plan=workflow.plan(cloud.id,'1','2')
    publish=strm.publish
    def disappear(path,*args,**kwargs):
        publish(path,*args,**kwargs)
        if path.suffix=='.strm':path.unlink()
    monkeypatch.setattr(strm,'publish',disappear)
    jid=workflow.apply(plan['id'],True);jobs.execute(jid)
    result=store.job(jid)
    assert result['state']=='needs_review' and not result['result']['generated']
    assert not [c for c in calls if c[0]=='card']
    assert '4' in cloud.remote.files


def test_waiting_source_change_is_refused_and_partial_card_is_accurate(cloud,monkeypatch):
    calls=enable(monkeypatch);plan=plan_files(cloud)
    entered, release=threading.Event(),threading.Event()
    generate,relocate=strm.generate,workflow.relocate
    def publish(aid,row,*args,**kwargs):
        if row['id']=='80':
            entered.set();assert release.wait(3)
        return generate(aid,row,*args,**kwargs)
    def move(provider,jid,row,*args):
        result=relocate(provider,jid,row,*args)
        if row['id']=='81':
            assert entered.wait(3)
            cloud.remote.files['81']['size']+=1
            release.set()
        return result
    monkeypatch.setattr(strm,'generate',publish);monkeypatch.setattr(workflow,'relocate',move)
    jid=workflow.apply(plan['id'],True);jobs.execute(jid)
    result=store.job(jid)
    assert result['state']=='needs_review' and len(result['result']['generated'])==1
    cards=[c[1] for c in calls if c[0]=='card']
    assert len(cards)==1 and '仅完成 1/2' in cards[0]


def test_delayed_batch_waits_for_last_child_and_does_not_send_per_file(cloud,monkeypatch):
    calls=enable(monkeypatch)
    settings.save({'strm_delay_seconds':60})
    plan=plan_files(cloud,3)
    jid=workflow.apply(plan['id'],True);jobs.execute(jid)
    assert not calls
    with store.db() as con:
        children=[r[0] for r in con.execute('SELECT job_id FROM cloud_job_due WHERE origin_job=? ORDER BY created',(jid,))]
        con.execute('UPDATE cloud_job_due SET due=0 WHERE origin_job=?',(jid,))
    for child in children[:-1]:
        jobs.execute(child);assert not calls
    jobs.execute(children[-1])
    assert [c[0] for c in calls]==['card','text','text']
    assert '文件：3 个' in calls[0][1] and 'STRM 更新：3 个' in calls[1][1]
    jobs.notify(children[-1],'publish','success','完成')
    assert len(calls)==3


def test_empty_scheduled_organizer_is_quiet_and_does_not_refresh(cloud,monkeypatch):
    calls=enable(monkeypatch)
    cloud.remote.files={k:v for k,v in cloud.remote.files.items() if v['is_dir']}
    refresh=Mock(return_value=True);monkeypatch.setattr(workflow,'refresh_library',refresh)
    jid=jobs.enqueue('workflow',{'account_id':cloud.id,'account_revision':1,'source_cid':'1','target_cid':'2'})
    jobs.execute(jid)
    assert store.job(jid)['state']=='success' and not calls
    refresh.assert_not_called()
    assert any(r.get('details',{}).get('event')=='empty_batch' for r in runtime_logs.read(source='cloud',limit=500)['logs'])


def test_details_sort_numerically_group_paths_and_escape_without_losing_files(cloud,monkeypatch):
    calls=enable(monkeypatch);plan=plan_files(cloud,12)
    jid=workflow.apply(plan['id'],True);jobs.execute(jid)
    result=store.job(jid)['result']
    rows=notifications.result_files(list(reversed(result['generated'])))
    assert [r['media']['episode'] for r in rows]==list(range(1,13))
    details=notifications.detail_batches(rows)
    assert len(details)==2, '12 STRM + 12 NFO require two batches'
    assert all(v.count('🔗')+v.count('⬇️')<=20 for v in details)
    assert '剧集/欧美剧集/Show/Season 01' in details[0]
    for row in rows:
        assert sum(row['name'] in text for text in details)==1


def test_season_total_is_not_inferred_from_current_batch(cloud):
    media=recognition.recognize('Movie.2024.1080p.mkv')
    media.update(kind='tv',season=20,episode=5,directory='动漫/Show/Season 20')
    text=notifications.caption([{'source':cloud.remote.info('4'),'media':media}])
    assert '本次：1 集' in text and '总集数暂未取得' in text
    assert '共 1 集' not in text


def test_backpressure_is_bounded_and_stop_drains_only_already_moved_files(cloud,monkeypatch):
    from app.cloud.publication import IncrementalPublisher
    calls=enable(monkeypatch);plan=plan_files(cloud,8)
    entered,release,throttled=threading.Event(),threading.Event(),threading.Event()
    generate,collect=strm.generate,IncrementalPublisher.collect
    def publish(*args,**kwargs):
        if not entered.is_set():
            entered.set();assert release.wait(5)
        return generate(*args,**kwargs)
    def drain(self,wait=False):
        if wait and not release.is_set():throttled.set()
        return collect(self,wait)
    monkeypatch.setattr(strm,'generate',publish);monkeypatch.setattr(IncrementalPublisher,'collect',drain)
    jid=workflow.apply(plan['id'],True)
    thread=threading.Thread(target=jobs.execute,args=(jid,))
    thread.start()
    try:
        assert entered.wait(3) and throttled.wait(3)
        with store.db() as con:
            steps=con.execute("SELECT state FROM cloud_steps WHERE job_id=? AND step_key LIKE 'publication:%'",(jid,)).fetchall()
        assert len(steps)==IncrementalPublisher.LIMIT
        assert {r[0] for r in steps} <= {'queued','dispatched'}
        jobs.request_stop(jid)
    finally:
        release.set();thread.join(8)
    assert not thread.is_alive()
    result=store.job(jid)
    assert result['state']=='stopped'
    assert len(result['result']['files'])==len(result['result']['generated'])==IncrementalPublisher.LIMIT+1
    assert len(result['result']['remaining'])==8-IncrementalPublisher.LIMIT-1
    assert not any(t.name.startswith('cloud-strm-publish') for t in threading.enumerate())
    cards=[c[1] for c in calls if c[0]=='card']
    assert len(cards)==1 and '仅完成 5/8' in cards[0]


def test_output_changes_while_waiting_do_not_publish_with_new_configuration(cloud,monkeypatch):
    calls=enable(monkeypatch);plan=plan_files(cloud)
    entered,release=threading.Event(),threading.Event()
    generate,relocate=strm.generate,workflow.relocate
    def publish(*args,**kwargs):
        entered.set();assert release.wait(3)
        return generate(*args,**kwargs)
    def move(provider,jid,row,*args):
        value=relocate(provider,jid,row,*args)
        if row['id']=='81':
            assert entered.wait(3)
            settings.save({'public_url':'http://changed.example:8321'})
            release.set()
        return value
    monkeypatch.setattr(strm,'generate',publish);monkeypatch.setattr(workflow,'relocate',move)
    jid=workflow.apply(plan['id'],True);jobs.execute(jid)
    result=store.job(jid)
    assert result['state']=='needs_review' and not result['result']['generated']
    assert not list(cloud.root.rglob('*.strm')) and not [c for c in calls if c[0]=='card']


@pytest.mark.parametrize('seasons,expected', [([{'season_number':20,'episode_count':22}],22),(None,None),([],None),([{'season_number':20,'episode_count':True}],None)])
def test_season_total_reuses_series_metadata_without_additional_request(cloud,monkeypatch,seasons,expected):
    calls=[]
    class TMDB:
        def get(self,path,**kwargs):
            calls.append(path)
            if '/episode/' in path:return {'id':55,'season_number':20,'episode_number':1}
            return {'id':44,'name':'Show','first_air_date':'2005-01-01','seasons':seasons}
    monkeypatch.setattr(recognition,'tmdb',lambda:TMDB())
    media=recognition.recognize('Show.S20E01.mkv',{'kind':'tv','tmdb_id':'44','season':20,'episode':1})
    assert media['season_episode_count']==expected
    assert calls==['/tv/44','/tv/44/season/20/episode/1']


def test_manual_sync_failure_reports_attempted_directory_instead_of_zero(cloud,monkeypatch):
    calls=enable(monkeypatch)
    monkeypatch.setattr(strm,'generate',Mock(side_effect=CloudError('输出冲突，保留已有文件',409)))
    jid=jobs.enqueue('sync',{'account_id':cloud.id,'account_revision':1,'source_cid':'1'})
    jobs.execute(jid)
    result=store.job(jid)
    assert result['state']=='needs_review'
    assert sum(v['failed'] for v in result['result']['publication'].values())==1
    assert len(calls)==1 and '目录异常：1 个' in calls[0][1]
