"""0.6 workspace behavior: transfer scope, real reconciliation and quiet notifications."""
import asyncio,json,time
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
import httpx,pytest
from fastapi.testclient import TestClient
from strimkeep.main import create_app
from strimkeep.organizer import Organizer
from strimkeep.network import ServiceHTTP
from test_organize import setup,new_job,CONFIG,MemoryCloud
from test_phase5 import sync_config
from test_transfer import mock_share


def test_transfer_goes_directly_to_target_and_only_new_entries_are_organized(tmp_path):
    async def run():
        store,cloud,http,engine,_=setup(tmp_path);calls=mock_share(cloud)
        store.set_many({'organize_config':CONFIG})
        untouched=cloud.add_video('777','Existing.mkv').copy()
        jid=(await engine.transfer.enqueue('https://115.com/s/abc','100',True))[0]
        await engine.run_job(jid)
        cfg=json.loads(engine.job(jid)['config']);receipt=cfg['receipt']
        assert 'batch_cid' not in receipt and len(calls)==1 and calls[0][-1]=='100'
        assert not any(n['name'].startswith('转存任务-') for n in cloud.nodes.values())
        child=receipt['organize_job'];await engine.run_job(child)
        assert engine.job(child)['state']=='completed' and cloud.nodes['777']==untouched
        assert len(list(engine.root.rglob('*.strm')))==1
        await http.aclose();store.close()
    asyncio.run(run())


def test_no_resource_polling_only_logs_even_if_every_telegram_category_is_enabled(tmp_path):
    async def run():
        store,cloud,http,engine,_=setup(tmp_path)
        store.set_many({'notification_config':{'telegram_enabled':True,'bot_token':'1234:'+('a'*30),'chat_id':'7','categories':{'task':True,'errors':True}}})
        sent=[]
        async def send(*args):sent.append(args);return httpx.Response(200,json={'ok':True},request=httpx.Request('POST','https://api.telegram.org/test'))
        engine.notifications.send=send
        jid=new_job(store,{**CONFIG,'scheduled':True});await engine.run_job(jid)
        await engine.notifications.job(engine.job(jid));await engine.notifications.job(engine.job(jid))
        assert not sent
        assert store.db.execute("SELECT 1 FROM events WHERE message LIKE '%未发现待整理视频%'").fetchone()
        assert store.db.execute("SELECT count(*) FROM notifications WHERE level<>'log'").fetchone()[0]==0
        await http.aclose();store.close()
    asyncio.run(run())


@pytest.mark.parametrize('category',['media','transfer','strm_summary','strm_files','errors','emby'])
def test_each_notification_category_can_disable_only_itself(tmp_path,category):
    async def run():
        store,cloud,http,engine,_=setup(tmp_path);sent=[]
        store.set_many({'notification_config':{'telegram_enabled':True,'bot_token':'1234:'+('a'*30),'chat_id':'7','categories':{category:False,'general':True}}})
        async def send(*args):sent.append(args);return httpx.Response(200,json={'ok':True},request=httpx.Request('POST','https://api.telegram.org/test'))
        engine.notifications.send=send
        assert await engine.notifications.publish('chosen','Title','Message',category=category)=='disabled'
        assert await engine.notifications.publish('other','Title','Message',category='general')=='sent'
        assert len(sent)==1
        await http.aclose();store.close()
    asyncio.run(run())


def test_cron_next_run_uses_shanghai_time_and_does_not_replay_missed_runs(tmp_path):
    store,cloud,http,engine,_=setup(tmp_path)
    store.set_many({'organize_config':CONFIG,'schedule_config':{'scan_enabled':True,'scan_mode':'cron','scan_cron':'0 3 * * *','timezone':'Asia/Shanghai'}})
    zone=ZoneInfo('Asia/Shanghai');base=datetime(2026,10,25,1,37,tzinfo=zone).timestamp()
    engine.tasks.tick(base);due=store.db.execute('SELECT next_due FROM task_schedules').fetchone()[0]
    assert datetime.fromtimestamp(due,zone).isoformat()=='2026-10-25T03:00:00+08:00'
    engine.tasks.tick(due);assert store.db.execute('SELECT count(*) FROM organize_jobs').fetchone()[0]==1
    engine.tasks.tick(due+60);assert store.db.execute('SELECT count(*) FROM organize_jobs').fetchone()[0]==1
    asyncio.run(http.aclose());store.close()


def test_manual_full_sync_finds_deleted_files_and_cleans_orphan_strm_and_sidecars(tmp_path):
    # 定时任务已改为本地巡检（不枚举云端），云端删除由完全核对发现：这里用手动同步覆盖。
    async def run():
        store,cloud,http,engine,_=setup(tmp_path);cloud.add_video(name='Movie.mkv')
        jid=new_job(store,sync_config());await engine.run_job(jid)
        output=engine.outputs.get('1','1000');old=engine.root/output['relative_path']
        orphan=old.parent/'no-source.strm';orphan.write_text('http://unused')
        nfo=old.parent/'movie.nfo';nfo.write_text('metadata')
        image=old.parent/'poster.jpg';image.write_bytes(b'artwork')
        cloud.nodes.pop('1000')
        second=new_job(store,{**sync_config(),'scheduled':False});await engine.run_job(second)
        assert engine.job(second)['state']=='completed',dict(engine.job(second))
        assert not old.exists() and orphan.exists()
        assert nfo.exists() and image.exists()
        assert json.loads(engine.job(second)['config'])['sync_removed']>=1
        await http.aclose();store.close()
    asyncio.run(run())


def test_full_sync_updates_renamed_and_moved_file_and_preserves_playback_token(tmp_path):
    async def run():
        store,cloud,http,engine,_=setup(tmp_path);cloud.add_video(name='Original.mkv')
        await engine.run_job(new_job(store,sync_config()))
        first=engine.outputs.get('1','1000');old=engine.root/first['relative_path'];pid=first['playback_id']
        folder=(await cloud.mkdir('100','new folder'))['cid'];cloud.nodes['1000'].update(name='New Name.mkv',parent_id=folder)
        jid=new_job(store,sync_config());await engine.run_job(jid)
        now=engine.outputs.get('1','1000')
        assert engine.job(jid)['state']=='completed' and now['playback_id']==pid
        assert not old.exists() and (engine.root/now['relative_path']).is_file() and 'new folder' in now['relative_path']
        await http.aclose();store.close()
    asyncio.run(run())


def test_emby_receives_exact_mapped_path(tmp_path):
    async def run():
        store,cloud,http,engine,_=setup(tmp_path);cloud.add_video(name='测试电影 (2024).mkv')
        store.set_many({'console_config':{'emby_strm_path':'/custom/media'}})
        posts=[]
        async def send(url,**kwargs):posts.append((url,kwargs));return httpx.Response(204,request=httpx.Request('POST',url))
        class Outbound:
            @staticmethod
            async def get(url, **kwargs):
                # 0.7.16 起交付前有只读预检；VirtualFolders 返回非 list 即跳过核对，维持 accepted 断言。
                return httpx.Response(200, json={})
            post=staticmethod(send)
        engine.emby.http=Outbound()
        jid=new_job(store);await engine.run_job(jid); await engine.emby.drain_outbox(); await engine.emby.tick()
        target=json.loads(engine.emby.row(jid)['payload'])['targets'][0]
        assert posts[0][0].endswith('/Library/Media/Updated')
        assert posts[0][1]['json']['Updates'][0]['Path']=='/custom/media/'+target['relative']
        assert engine.job(jid)['notify_state']=='accepted'
        await http.aclose();store.close()
    asyncio.run(run())


def console_app(tmp_path,monkeypatch):
    monkeypatch.setenv('ADMIN_USERNAME','admin');monkeypatch.setenv('ADMIN_PASSWORD','test-password-1234')
    monkeypatch.setattr(Organizer,'start',lambda self:None)
    return create_app(tmp_path/'data',tmp_path/'strm',MemoryCloud)


def login(client):
    d=client.post('/api/v1/auth/login',json={'username':'admin','password':'test-password-1234'}).json();client.headers['X-CSRF-Token']=d['csrf']


def test_console_settings_persist_theme_background_mapping_and_hide_proxy_credentials(tmp_path,monkeypatch):
    app=console_app(tmp_path,monkeypatch)
    with TestClient(app) as c:
        assert c.get('/api/v1/console/config').status_code==401;login(c)
        value=c.get('/api/v1/console/config').json()['config']
        value.update(theme='dark',background_url='https://example.com/wall.jpg',emby_strm_path='/media')
        assert c.put('/api/v1/console/config',json=value).status_code==200
        assert c.get('/api/v1/console/config').json()['config']['theme']=='dark'
        r=c.put('/api/v1/network/config',json={'enabled':True,'proxy_url':'http://user:secretpass@localhost:7890'})
        assert r.status_code==200 and 'secretpass' not in r.text and 'user:' not in r.text
        assert app.state.store.get('network_config')['proxy_url'].endswith(':7890')
        value['background_url']='javascript:alert(1)';assert c.put('/api/v1/console/config',json=value).status_code==422
        assert c.put('/api/v1/system/schedules',json={'sync_mode':'cron','sync_cron':'wrong'}).status_code==422
        assert c.put('/api/v1/system/schedules',json={'sync_mode':'cron','sync_cron':'0 0 30 2 *'}).status_code==422
        assert c.put('/api/v1/network/config',json={'proxy_url':'http://localhost:invalid'}).status_code==422
        # Fractional output delay is a real saved setting, not just a UI label.
        assert c.put('/api/v1/organize/config',json={**CONFIG,'strm_delay':1.5}).status_code==200
        assert c.get('/api/v1/organize/config').json()['config']['strm_delay']==1.5
        assert c.get('/api/v1/logs').status_code==200


def test_proxy_routes_external_services_and_leaves_local_emby_direct(tmp_path):
    async def run():
        store,cloud,http,engine,_=setup(tmp_path);network=ServiceHTTP(store)
        store.set_many({'network_config':{'enabled':True,'proxy_url':'http://localhost:7890','proxy_emby':False}})
        assert network.client('https://api.telegram.org/bot/test') is not network.direct
        assert network.client('http://192.0.2.10:8096/emby') is network.direct
        store.set_many({'network_config':{'enabled':False,'proxy_url':'http://localhost:7890'}})
        assert network.client('https://api.themoviedb.org/3') is network.direct
        await network.aclose();await http.aclose();store.close()
    asyncio.run(run())


def test_logs_export_downloads_text_and_events_also_land_on_disk(tmp_path,monkeypatch):
    """日志要能下载，也要真的落一份文件——以前只有 events 表，data/logs 是空的。"""
    app=console_app(tmp_path,monkeypatch)
    with TestClient(app) as c:
        assert c.get('/api/v1/logs/export').status_code==401
        login(c)
        app.state.store.event('organize','任务 7：测试日志一行')
        files=list((tmp_path/'data'/'logs').glob('strimkeep-*.log'))
        assert files and '任务 7：测试日志一行' in files[0].read_text(encoding='utf-8')
        r=c.get('/api/v1/logs/export?kind=organize&limit=10')
        assert r.status_code==200 and r.headers['content-type'].startswith('text/plain')
        assert 'attachment; filename="strimkeep-logs-' in r.headers['content-disposition']
        assert '[organize] 任务 7：测试日志一行' in r.text


def test_strm_audit_separates_our_strms_from_foreign_and_missing(tmp_path,monkeypatch):
    """体检要分得清：本项目的、别的工具写的、登记了却没文件的。"""
    app=console_app(tmp_path,monkeypatch)
    with TestClient(app) as c:
        login(c)
        store,root=app.state.store,app.state.strms
        (root/'媒体库/电影').mkdir(parents=True,exist_ok=True)
        (root/'媒体库/电影/ours.strm').write_text('http://x/play115/abc/def/sha/1/name.mkv',encoding='utf-8')
        (root/'媒体库/电影/foreign.strm').write_text('http://x/play/share/abc/name.mkv',encoding='utf-8')
        with store.db:
            for pid,rel in (('p1','媒体库/电影/ours.strm'),('p2','媒体库/电影/gone.strm')):
                store.db.execute("INSERT INTO playbacks(id,file_id,pickcode,name,relative_path,token_hash,revoked,created) VALUES(?,?,?,?,?,?,0,?)",(pid,'f'+pid,'','x',rel,'h',0))
        data=c.get('/api/v1/strm/audit').json()
        assert data['total']==2 and data['managed']==1
        assert data['external_total']==1
        assert data['external'][0]=={'path':'媒体库/电影/foreign.strm','kind':'unknown'}
        assert data['missing_total']==1 and data['missing']==['媒体库/电影/gone.strm']


def test_stale_unconfirmed_deliveries_get_archived_after_a_week(tmp_path,monkeypatch):
    """超过 7 天还没确认的投递归档，别让任务中心一直挂红点。"""
    import asyncio,time
    app=console_app(tmp_path,monkeypatch)
    with TestClient(app) as c:
        login(c)
        store=app.state.store
        now=time.time()
        with store.db:
            store.db.execute("INSERT INTO emby_deliveries(job_id,state,payload,next_due,deadline,message) VALUES(?,?,?,?,?,?)",
                             (1,'unconfirmed','{}',0,now-2*86400,'旧的未确认'))
            store.db.execute("INSERT INTO emby_deliveries(job_id,state,payload,next_due,deadline,message) VALUES(?,?,?,?,?,?)",
                             (2,'unconfirmed','{}',0,now-60,'刚发生的'))
        asyncio.run(app.state.organizer.maintain())
        states={r['job_id']:r['state'] for r in store.db.execute('SELECT job_id,state FROM emby_deliveries')}
        assert states[1]=='archived' and states[2]=='unconfirmed'


def test_unidentified_listing_and_selected_cleanup_preserve_other_files(tmp_path,monkeypatch):
    app=console_app(tmp_path,monkeypatch)
    with TestClient(app) as c:
        assert c.get('/api/v1/strm/unidentified').status_code==401
        login(c)
        root=app.state.strms
        folder=root/'分享STRM未识别'/'作品'
        folder.mkdir(parents=True)
        (folder/'one.strm').write_text('http://x/play115/share/one',encoding='utf-8')
        (folder/'two.strm').write_text('http://x/play115/share/two',encoding='utf-8')
        (folder/'one.nfo').write_text('keep',encoding='utf-8')
        (root/'分享STRM待整理').mkdir()
        (root/'分享STRM待整理'/'pending.strm').write_text('pending',encoding='utf-8')
        listing=c.get('/api/v1/strm/unidentified').json()
        assert listing['total']==2
        audit=c.get('/api/v1/strm/audit').json()
        assert audit['external_total']==0 and audit['unidentified_total']==2 and audit['pending_total']==1
        assert c.post('/api/v1/strm/unidentified/cleanup',json={'paths':['分享STRM未识别/../分享STRM待整理/pending.strm']}).status_code==422
        result=c.post('/api/v1/strm/unidentified/cleanup',json={'paths':['分享STRM未识别/作品/one.strm']})
        assert result.status_code==200 and result.json()['removed']==1
        assert not (folder/'one.strm').exists()
        assert (folder/'two.strm').is_file() and (folder/'one.nfo').is_file()
        assert (root/'分享STRM待整理'/'pending.strm').is_file()
        assert c.post('/api/v1/strm/audit/cleanup').status_code==410


def test_unidentified_cleanup_rejects_symlinks_and_active_records(tmp_path,monkeypatch):
    app=console_app(tmp_path,monkeypatch)
    with TestClient(app) as c:
        login(c)
        root=app.state.strms
        folder=root/'分享STRM未识别'
        folder.mkdir()
        (folder/'active.strm').write_text('keep',encoding='utf-8')
        with app.state.store.db:
            app.state.store.db.execute("INSERT INTO playbacks(id,file_id,pickcode,name,relative_path,token_hash,revoked,created) VALUES(?,?,?,?,?,?,0,?)",('p','f','','x','分享STRM未识别/active.strm','h',0))
        result=c.post('/api/v1/strm/unidentified/cleanup',json={'paths':['分享STRM未识别/active.strm']}).json()
        assert result['removed']==0 and result['failed'] and (folder/'active.strm').exists()
        external=tmp_path/'outside'; external.mkdir(); (external/'outside.strm').write_text('keep')
        (folder/'link').symlink_to(external,target_is_directory=True)
        result=c.post('/api/v1/strm/unidentified/cleanup',json={'paths':['分享STRM未识别/link/outside.strm']}).json()
        assert result['removed']==0 and (external/'outside.strm').exists()
