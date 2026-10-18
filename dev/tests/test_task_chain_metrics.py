"""Task ownership, verified continuations and measured playback, using real SQLite."""
import json
import time
import pytest
from fastapi.testclient import TestClient
from dev.tests.test_cloud_workbench import cloud  # noqa:F401
from app import engine, main, playback_metrics as metrics
from app.cloud import jobs, store, workflow, recovery, task_chain, events
from app.cloud.errors import CloudError


def sample(position=0, paused=False, user='u1', item='m1', session='s1', rate=1):
    return {'Id':session,'UserId':user,'UserName':'观众甲','NowPlayingItem':{'Id':item,'Name':'真实影片','Type':'Movie'},
            'PlayState':{'PositionTicks':int(position*10000000),'IsPaused':paused,'PlaybackRate':rate}}


@pytest.fixture
def collecting(cloud,monkeypatch):
    monkeypatch.setattr(engine,'EMBY_HOST','http://emby.example')
    monkeypatch.setattr(engine,'EMBY_KEY','secret')
    metrics.configure(True)
    return metrics.scope()


def test_task_chain_nested_ownership_and_sibling_isolation(cloud):
    root = jobs.enqueue('intake',{})
    token = events._JOB.set(root)
    try:
        child = jobs.enqueue('transfer',{})
    finally:
        events._JOB.reset(token)
    leaf = jobs.enqueue('publish',{},parent_id=child)
    unrelated = jobs.enqueue('publish',{})
    events.emit('strm','同一流程已发布',leaf)
    events.emit('strm','别的资源',unrelated)
    value = task_chain.read(leaf)
    assert value['root_id']==root and {r['id'] for r in value['jobs']}=={root,child,leaf}
    assert any('同一流程' in r['message'] for r in jobs.timeline(child)['items'])
    assert not any('别的资源' in r['message'] for r in jobs.timeline(child)['items'])


def test_task_chain_reads_pre_upgrade_continue_and_due_links(cloud):
    root, child = jobs.enqueue('transfer',{}), jobs.enqueue('organize',{})
    with store.db() as con:
        con.execute('INSERT INTO cloud_steps VALUES(?,?,?,?,?,?)',(root,'continue','verified','{}',store.encode({'job_id':child}),1))
    assert task_chain.read(child)['root_id']==root


def stopped_plan(cloud):
    plan = workflow.plan(cloud.id,'1','2')
    jid = workflow.apply(plan['id'],True,False)
    with store.db() as con:
        con.execute("UPDATE cloud_jobs SET state='needs_review',message='服务重启' WHERE id=?",(jid,))
    return jid


def test_recovery_rechecks_once_and_preserves_root_receipt(cloud):
    jid = stopped_plan(cloud)
    preview = recovery.preview(jid)
    assert len(preview['items'])==1 and not cloud.remote.writes
    with pytest.raises(CloudError):
        recovery.apply(jid,preview['preview_id'],False)
    child = recovery.apply(jid,preview['preview_id'],True)['job_id']
    assert recovery.apply(jid,preview['preview_id'],True)['job_id']==child
    assert not cloud.remote.writes
    jobs.execute(child)
    assert store.job(child)['state']=='success'
    assert store.job(jid)['state']=='needs_review'
    assert task_chain.read(child)['root_id']==jid
    assert task_chain.read(jid)['state']=='success'


def test_recovery_unknown_rename_reconciles_positive_evidence(cloud):
    jid = stopped_plan(cloud)
    old = cloud.remote.info('4')
    with store.db() as con:
        plan_id=json.loads(con.execute('SELECT payload FROM cloud_jobs WHERE id=?',(jid,)).fetchone()[0])['plan_id']
        planned_name=json.loads(con.execute('SELECT payload FROM cloud_plans WHERE id=?',(plan_id,)).fetchone()[0])['items'][0]['media']['name']
    cloud.remote.rename('4',planned_name)
    with store.db() as con:
        con.execute('INSERT INTO cloud_steps VALUES(?,?,?,?,?,?)',(jid,'rename:4','dispatched',store.encode({'file_id':'4','name':planned_name,'source_name':old['name'],'parent':'1','sha1':old['sha1'],'size':100}),'{}',1))
    preview = recovery.preview(jid)
    assert not preview['blocked'] and preview['verified']
    child = recovery.apply(jid,preview['preview_id'],True)['job_id']
    jobs.execute(child)
    assert store.job(child)['state']=='success'
    assert len([r for r in cloud.remote.writes if r[0]=='rename' and r[1]=='4'])==1  # The positively verified rename is not sent again.


def test_recovery_unknown_write_cannot_be_proved_absent(cloud):
    jid = stopped_plan(cloud)
    with store.db() as con:
        con.execute('INSERT INTO cloud_steps VALUES(?,?,?,?,?,?)',(jid,'rename:4','dispatched',store.encode({'file_id':'4','name':'Different.mkv','source_name':'Movie.2024.1080p.mkv','parent':'1','sha1':'A'*40,'size':100}),'{}',1))
    result = recovery.preview(jid)
    assert result['blocked'] and 'preview_id' not in result
    assert not cloud.remote.writes


def test_recovery_detects_file_change_during_confirmation(cloud):
    jid = stopped_plan(cloud)
    preview = recovery.preview(jid)
    cloud.remote.files['4']['sha1']='B'*40
    with pytest.raises(CloudError,match='文件变化'):
        recovery.apply(jid,preview['preview_id'],True)
    assert not cloud.remote.writes


def test_metrics_real_progress_pause_seek_and_stop(collecting):
    sid = collecting
    now = time.time()
    metrics.observe(sid,[sample()],now)
    metrics.observe(sid,[sample(15)],now+15)
    metrics.observe(sid,[sample(30,True)],now+30)  # ambiguous pause boundary excluded
    metrics.observe(sid,[sample(30,True)],now+45)
    metrics.observe(sid,[sample(30)],now+60)  # resume boundary excluded
    metrics.observe(sid,[sample(45)],now+75)
    metrics.observe(sid,[sample(900)],now+90)  # seek excluded
    metrics.observe(sid,[sample(915)],now+105)
    metrics.observe(sid,[],now+120)  # no imagined final interval
    value = metrics.summary()
    assert value['seconds']==45 and value['plays']==1 and value['viewers']==1
    assert value['rankings'][0]['seconds']==45
    with store.db() as con:
        names = [r[0] for r in con.execute('SELECT event FROM playback_events')]
    assert 'pause' in names and 'resume' in names and 'seek' in names and 'stop' in names


def test_metrics_duplicates_out_of_order_gap_and_new_user(collecting):
    sid, now = collecting,time.time()
    metrics.observe(sid,[sample()],now)
    assert metrics.observe(sid,[sample(15)],now+15)
    assert not metrics.observe(sid,[sample(30)],now+15)
    assert not metrics.observe(sid,[sample(40)],now+10)
    metrics.observe(sid,[sample(120)],now+120)
    metrics.observe(sid,[sample(135)],now+135)
    metrics.observe(sid,[sample(150,user='u2')],now+150)
    metrics.observe(sid,[sample(165,user='u2')],now+165)
    value = metrics.summary()
    assert value['seconds']==45 and value['viewers']==2 and value['plays']==3 and value['gaps']==1


def test_metrics_speed_and_midnight_split(collecting):
    from datetime import datetime,timezone
    sid = collecting
    now = datetime.now(timezone.utc).replace(hour=23,minute=59,second=50,microsecond=0).timestamp()
    metrics.observe(sid,[sample(rate=2)],now)
    metrics.observe(sid,[sample(30,rate=2)],now+15)
    with store.db() as con:
        values = con.execute('SELECT seconds FROM playback_daily ORDER BY day').fetchall()
    assert values==[(10.0,),(5.0,)]


def test_metrics_malformed_snapshot_preserves_active_state_and_no_secrets(collecting):
    now = time.time()
    metrics.observe(collecting,[sample()],now)
    with pytest.raises(ValueError):
        metrics.observe(collecting,[sample(15),{'NowPlayingItem':{'Type':'Movie'}}],now+15)
    metrics.observe(collecting,[sample(30)],now+30)
    result = metrics.summary()
    assert result['seconds']==30
    assert 'secret' not in json.dumps(result)


def test_metrics_restarts_and_connection_change_are_isolated(collecting,monkeypatch):
    now = time.time()
    metrics.observe(collecting,[sample()],now)
    metrics.observe(collecting,[sample(15)],now+15)
    metrics.configure(False)
    metrics.configure(True)
    metrics.observe(collecting,[sample(600)],now+30)
    assert metrics.summary()['seconds']==15
    monkeypatch.setattr(engine,'EMBY_KEY','other-secret')
    assert metrics.summary()['seconds']==0 and not metrics.summary()['enabled']
    assert not metrics.observe(collecting,[sample(630)],now+60)


def test_metrics_poll_reads_server_and_failure_is_not_zero_history(collecting,monkeypatch):
    calls=[]
    monkeypatch.setattr(engine,'emby_request',lambda path,**kw: calls.append(path) or [sample()])
    metrics.poll()
    assert calls==['/Sessions']
    monkeypatch.setattr(engine,'emby_request',lambda *a,**k: (_ for _ in ()).throw(ValueError('url?api_key=secret')))
    metrics.poll()
    assert metrics.summary()['error'] and 'secret' not in metrics.summary()['error']


def test_new_routes_are_authenticated_and_validate_collection(cloud,collecting):
    client=TestClient(main.app,headers={'Origin':'http://testserver'})
    assert client.get('/api/overview/playback/metrics').status_code==401
    assert client.post('/api/overview/playback/collection',json={'enabled':True}).status_code==401
    jid=stopped_plan(cloud)
    assert client.get('/api/cloud/jobs/'+jid+'/chain').status_code==401
    assert client.post('/api/cloud/jobs/'+jid+'/recovery/preview').status_code==401
    assert client.post('/api/login',json={'username':'admin','password':'test-pass-123'}).status_code==200
    assert client.get('/api/overview/playback/metrics?days=7').json()['configured']
    assert client.post('/api/overview/playback/collection',json={'enabled':'yes'}).status_code==400
    assert client.get('/api/cloud/jobs/'+jid+'/chain').json()['root_id']==jid
    preview=client.post('/api/cloud/jobs/'+jid+'/recovery/preview').json()
    response=client.post('/api/cloud/jobs/'+jid+'/recovery/apply',json={'preview_id':preview['preview_id'],'confirmed':True})
    assert response.status_code==202


def test_completed_publication_reconciles_disk_and_does_not_publish_twice(cloud):
    plan = workflow.plan(cloud.id,'1','2')
    jid = workflow.apply(plan['id'],True,True)
    jobs.execute(jid)
    assert store.job(jid)['state']=='success'
    with store.db() as con:
        con.execute("UPDATE cloud_jobs SET state='needs_review',result='{}' WHERE id=?",(jid,))
        con.execute("UPDATE cloud_steps SET state='dispatched',result='{}' WHERE job_id=? AND step_key LIKE 'publication:%'",(jid,))
    writes = list(cloud.remote.writes)
    result = recovery.preview(jid)
    assert not result['items'] and 'preview_id' not in result
    assert any('已生成的播放引用' in note['message'] for note in result['verified'])
    assert cloud.remote.writes==writes


def test_recovery_output_and_account_revision_guards(cloud):
    from app.cloud import settings
    jid = stopped_plan(cloud)
    first = recovery.preview(jid)
    settings.save({'public_url':'http://other.example'})
    with pytest.raises(CloudError,match='输出配置变化'):
        recovery.apply(jid,first['preview_id'],True)
    second = recovery.preview(jid)
    with store.db() as con:
        con.execute('UPDATE cloud_accounts SET revision=revision+1 WHERE id=?',(cloud.id,))
    with pytest.raises(CloudError,match='授权已变化'):
        recovery.apply(jid,second['preview_id'],True)


def test_metrics_restart_discards_active_observation_but_keeps_durable_totals(collecting,monkeypatch):
    now=time.time()
    metrics.observe(collecting,[sample()],now)
    metrics.observe(collecting,[sample(15)],now+15)
    monkeypatch.setattr(metrics,'_loop',lambda: None)
    metrics.start()
    metrics.stop()
    metrics.observe(collecting,[sample(900)],now+30)
    assert metrics.summary()['seconds']==15
    with store.db() as con:
        assert con.execute("SELECT count(*) FROM playback_segments WHERE state='interrupted'").fetchone()[0]==1


def test_real_http_emby_sessions_authorization_and_persistence(cloud,monkeypatch):
    import threading
    from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
    from app import emby
    observations=[sample()]
    seen=[]
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.path,self.headers.get('X-Emby-Token')))
            if self.headers.get('X-Emby-Token')!='test-secret':
                self.send_response(401);self.end_headers();return
            self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers()
            self.wfile.write(json.dumps(observations).encode())
        def log_message(self,*args):
            pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    monkeypatch.setattr(engine,'EMBY_HOST',f'http://127.0.0.1:{server.server_port}')
    monkeypatch.setattr(engine,'EMBY_KEY','test-secret')
    monkeypatch.setattr(engine,'emby_request',emby.emby_request)
    try:
        metrics.configure(True)
        metrics.poll()
        assert seen==[('/Sessions','test-secret')]
        assert metrics.summary()['last_poll']
        assert not metrics.summary()['error']
        client=TestClient(main.app,headers={'Origin':'http://testserver'})
        client.post('/api/login',json={'username':'admin','password':'test-pass-123'})
        body=client.get('/api/overview/playback/metrics?days=30').json()
        assert body['enabled'] and body['days']==30 and len(body['trend'])==30
        assert 'test-secret' not in json.dumps(body)
    finally:
        server.shutdown();server.server_close();thread.join(2)


def test_recovery_preview_expiration_prevents_enqueue(cloud):
    jid=stopped_plan(cloud)
    preview=recovery.preview(jid)
    with store.db() as con:
        con.execute('UPDATE cloud_plans SET expires=0 WHERE id=?',(preview['preview_id'],))
    with pytest.raises(CloudError,match='过期'):
        recovery.apply(jid,preview['preview_id'],True)
    with store.db() as con:
        assert con.execute('SELECT count(*) FROM cloud_recovery_requests').fetchone()[0]==0


def test_recovery_reuses_unknown_poster_claim_without_resending(cloud,monkeypatch):
    from app.cloud import notifications,settings
    from app import tg
    jid=stopped_plan(cloud)
    preview=recovery.preview(jid)
    child=recovery.apply(jid,preview['preview_id'],True)['job_id']
    assert notifications.poster_owner(child)==jid
    sent=[]
    monkeypatch.setattr(tg,'notify_telegram_card',lambda *a: sent.append(a) or False)
    settings.save({'notify_enabled':True,'notify_organize':True})
    media={'kind':'movie','tmdb_id':1,'title':'影片','year':2024,'directory':'电影/影片 (2024)'}
    for identifier in (jid,child):
        posters=notifications.BatchPosters(identifier,[{'media':media}])
        posters.verified(0,{'id':'4'},media)
        posters.flush()
    assert len(sent)==1
    with store.db() as con:
        assert con.execute("SELECT state FROM cloud_steps WHERE job_id=? AND step_key LIKE 'poster:%'",(jid,)).fetchone()[0]=='unknown'


def test_cancelled_recovery_does_not_claim_original_work_completed(cloud):
    jid=stopped_plan(cloud)
    preview=recovery.preview(jid)
    child=recovery.apply(jid,preview['preview_id'],True)['job_id']
    with store.db() as con:
        con.execute("UPDATE cloud_jobs SET state='cancelled' WHERE id=?",(child,))
    result=task_chain.read(jid)
    assert result['state']=='needs_review'
    assert not next(j for j in result['jobs'] if j['id']==jid)['resolved_by']
