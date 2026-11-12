import asyncio,json,importlib.util
from pathlib import Path
import httpx
from fastapi.testclient import TestClient
from test_console060 import console_app,login
from test_organize import setup,new_job
from test_phase5 import sync_config
from strimkeep.library_name import migrate,OLD,NAME
from strimkeep.emby_connection import probe


def test_emby_draft_test_uses_visible_url_and_does_not_overwrite_saved_connection(tmp_path,monkeypatch):
    app=console_app(tmp_path,monkeypatch)
    with TestClient(app) as c:
        login(c);app.state.store.set_many({'emby_url':'http://old:8096','emby_key':'saved-key'})
        seen=[]
        def respond(r):
            seen.append(r);assert r.url.host=='new' and r.headers['X-Emby-Token']=='saved-key'
            return httpx.Response(200,json={'ServerName':'Living Room','Version':'4.9'})
        client=httpx.AsyncClient(transport=httpx.MockTransport(respond));app.state.http=client
        result=c.post('/api/v1/connections/emby/test',json={'emby_url':'http://new:8096','emby_key':''})
        assert result.status_code==200 and result.json()['server']=='Living Room'
        assert app.state.store.get('emby_url')=='http://old:8096'
        asyncio.run(client.aclose())


def test_emby_root_api_fallback_is_read_only_and_cache_applies_to_notifications(tmp_path):
    async def run():
        store,cloud,http,engine,_=setup(tmp_path);calls=[]
        def respond(r):
            calls.append(r);assert r.method=='GET'
            return httpx.Response(404) if '/emby/' in r.url.path else httpx.Response(200,json={'ServerName':'Home'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result=await probe(client,store)
        assert result['api_base']=='http://emby:8096' and len(calls)==2
        assert engine.emby.connection()==('http://emby:8096',store.get('emby_key'))
        await http.aclose();store.close()
    asyncio.run(run())


def test_dashboard_disconnection_is_explicit_and_only_retains_known_emby_data(tmp_path,monkeypatch):
    app=console_app(tmp_path,monkeypatch)
    with TestClient(app) as c:
        login(c);app.state.store.set_many({'emby_url':'http://emby:8096','emby_key':'key'})
        down=[False]
        def respond(r):
            if down[0]:raise httpx.ConnectError('do not expose credentials',request=r)
            data={'ServerName':'Home'} if r.url.path.endswith('/System/Info') else {'MovieCount':99} if r.url.path.endswith('/Items/Counts') else [] if r.url.path.endswith('/Sessions') else {'Items':[]}
            return httpx.Response(200,json=data)
        client=httpx.AsyncClient(transport=httpx.MockTransport(respond));app.state.http=client
        first=c.get('/api/v1/dashboard').json();assert first['emby']['counts']['MovieCount']==99
        down[0]=True;second=c.get('/api/v1/dashboard').json()
        assert second['emby']['connected'] is False and second['emby']['stale'] is True
        assert second['emby']['counts']['MovieCount']==99 and '无法连接' in second['emby']['error']
        assert 'do not expose' not in second['emby']['error']
        assert '无法连接' in c.post('/api/v1/connections/emby/test').json()['error']['message']
        asyncio.run(client.aclose())


def test_library_rename_preserves_files_metadata_and_playback_token_and_notifies_old_path(tmp_path):
    async def run():
        store,cloud,http,engine,_=setup(tmp_path);cloud.add_video();await engine.run_job(new_job(store)); await engine.emby.drain_outbox(); await engine.emby.tick()
        before=engine.outputs.get('1','1000');token=store.db.execute('SELECT token_hash FROM playbacks').fetchone()[0]
        (engine.root/NAME).rename(engine.root/OLD)
        for table,columns in {'playbacks':['relative_path'],'organized_files':['relative_path'],'output_registry':['relative_path','files'],'owned_outputs':['relative_path'],'organize_items':['plan']}.items():
            for row in store.db.execute('SELECT rowid AS rid,* FROM '+table).fetchall():
                for column in columns:
                    if isinstance(row[column],str):store.db.execute('UPDATE '+table+' SET '+column+'=? WHERE rowid=?',(row[column].replace(NAME,OLD),row['rid']))
        store.db.commit();(engine.root/OLD/'poster.jpg').write_bytes(b'preserved')
        migrate(store,engine.root);migrate(store,engine.root)
        assert not (engine.root/OLD).exists() and (engine.root/NAME/'poster.jpg').read_bytes()==b'preserved'
        assert store.db.execute('SELECT token_hash FROM playbacks').fetchone()[0]==token
        assert engine.outputs.get('1','1000')['relative_path'].startswith(NAME+'/')
        # 0.8.30 起库根默认镜像 115 目录名；本用例验证的是「显式库根」的改名通知，
        # 所以显式写死 NAME，避免断言被镜像规则改变。
        jid=new_job(store,{**sync_config(['200']),'library_dir_name':NAME});await engine.run_job(jid); await engine.emby.drain_outbox(); await engine.emby.tick()
        assert engine.job(jid)['state']=='completed',dict(engine.job(jid))
        updates=json.loads(engine.emby.row(jid)['payload'])['updates']
        assert any('/'+OLD+'/' in u['Path'] and u['UpdateType']=='Deleted' for u in updates)
        assert any('/'+NAME+'/' in u['Path'] and u['UpdateType']=='Created' for u in updates)
        assert not json.loads(store.meta('library_name_pending'))
        await http.aclose();store.close()
    asyncio.run(run())


def test_library_name_collision_never_overwrites_existing_content(tmp_path):
    store,cloud,http,engine,_=setup(tmp_path)
    for folder,data in [(OLD,b'old'),(NAME,b'new')]:
        (engine.root/folder).mkdir(parents=True);(engine.root/folder/'movie.strm').write_bytes(data)
    migrate(store,engine.root)
    assert (engine.root/OLD/'movie.strm').read_bytes()==b'old' and (engine.root/NAME/'movie.strm').read_bytes()==b'new'
    asyncio.run(http.aclose());store.close()


def test_deploy_conversion_is_literal_and_preserves_paths_and_credentials():
    path=Path(__file__).resolve().parents[2]/'scripts/deploy_config.py'
    spec=importlib.util.spec_from_file_location('deploy',path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    model={'name':'existing','services':{'strimkeep':{'environment':{'ADMIN_USERNAME':'user','ADMIN_PASSWORD':"a$word'with: quotes",'SESSION_COOKIE_SECURE':'false','ALLOWED_ORIGINS':''},'volumes':[{'type':'bind','source':'/NAS/测试/data','target':'/data','bind':{'create_host_path':True}}]}}}
    text=module.convert(model)
    assert "a$$word'with: quotes" in text and '/NAS/测试/data:/data' in text
    assert 'SESSION_COOKIE_SECURE' not in text and '${' not in text
    from strimkeep import __version__ as current
    assert 'existing' in text and 'strimkeep:'+current in text
