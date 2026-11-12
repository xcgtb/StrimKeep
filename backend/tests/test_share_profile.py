import asyncio
import json
from pathlib import Path

from fastapi.testclient import TestClient
from strimkeep import share_service
from strimkeep.strm_options import shared_settings, share_effective_settings
from test_directory_readback import api_setup
from test_phase2_share_pipeline import share_setup


def test_profile_copy_is_independent_and_jobs_capture_snapshot(tmp_path):
    async def run():
        store,cloud,http,engine,_=share_setup(tmp_path)
        original=shared_settings(store)
        assert share_effective_settings(store)['organize']==original['organize']
        share={**original['organize'],'rename_enabled':False,'classify_enabled':False}
        store.set_many({'share_profile':{'organize':share,'upgrade_policy':original['upgrade_policy'],'notify_emby':False}})
        jid=share_service.enqueue_organize(engine)
        cfg=json.loads(engine.job(jid)['config'])
        store.set_many({'organize_config':{**store.get('organize_config',{}),'filename_language':'en'},
                        'share_profile':{'organize':{**share,'rename_enabled':True},'upgrade_policy':original['upgrade_policy'],'notify_emby':True}})
        assert cfg['organize_options']['rename_enabled'] is False
        assert cfg['organize_options']['classify_enabled'] is False
        assert cfg['notify_emby'] is False
        assert share_effective_settings(store)['organize']['filename_language']==share['filename_language']
        await http.aclose();store.close()
    asyncio.run(run())


def test_share_keeps_filename_and_work_folder_when_classification_disabled(tmp_path):
    async def run():
        store,cloud,http,engine,_=share_setup(tmp_path)
        inherited=shared_settings(store)
        cfg={**inherited['organize'],'rename_enabled':False,'classify_enabled':False}
        store.set_many({'share_profile':{'organize':cfg,'upgrade_policy':inherited['upgrade_policy'],'notify_emby':False},
                        'console_config':{'emby_strm_path':'/media'}})
        name='测试剧集 (2024) S01E03.1080p.WEB-DL.mkv'
        share_service.generate(store,engine.root,{'share_code':'codeB','receive_code':'abcd'},[{'id':'301','name':name,'size':5000,'path':''}])
        jid=share_service.enqueue_organize(engine)
        await engine.run_job(jid)
        assert engine.job(jid)['state']=='completed',dict(engine.job(jid))
        row=store.db.execute('select strm_path from share_strms where file_id=?',('301',)).fetchone()
        path=Path(row['strm_path'])
        assert path.name==Path(name).stem+'.strm'
        assert path.parts[:2]==('分享影视库','剧集') and path.parts[-2]=='Season 1'
        assert '{tmdb-10}' in path.parts[-3]
        assert engine.emby.targets(jid)[0]['emby_path']=='/media/'+row['strm_path']
        await http.aclose();store.close()
    asyncio.run(run())


def test_profile_api_keeps_main_config_and_quality_separate(tmp_path,monkeypatch):
    app,_=api_setup(tmp_path,monkeypatch)
    with TestClient(app) as client:
        login=client.post('/api/v1/auth/login',json={'username':'admin','password':'test-password-1234'}).json()
        client.headers['X-CSRF-Token']=login['csrf']
        store=app.state.store
        main_before=store.get('organize_config',{})
        data=client.get('/api/v1/share/profile').json()
        assert data['custom'] is False
        body={k:data['base'][k] for k in ('organize','upgrade_policy','notify_emby')}
        body['organize']['filename_language']='en'
        result=client.put('/api/v1/share/profile',json=body)
        assert result.status_code==200,result.text
        assert result.json()['custom'] is True
        assert result.json()['effective']['organize']['filename_language']=='en'
        assert store.get('organize_config',{})==main_before
        quality=client.get('/api/v1/share/quality').json()
        assert client.put('/api/v1/share/quality',json=quality['config']).status_code==200
        assert store.get('organize_config',{})==main_before
        assert store.get('share_profile')['organize']['filename_language']=='en'
