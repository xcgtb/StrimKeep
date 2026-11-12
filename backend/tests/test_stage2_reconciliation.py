import asyncio
import json
from test_organize import setup, new_job
from test_phase5 import sync_config


def test_recycle_and_restore_rebuilds_managed_strm(tmp_path):
    async def scenario():
        store, cloud, http, engine, _ = setup(tmp_path)
        source=cloud.add_video()
        first=new_job(store, {**sync_config(), 'notify_emby':False}); await engine.run_job(first)
        old=engine.outputs.get('1','1000');path=engine.root/old['relative_path']; original=path.read_bytes()
        saved=cloud.nodes.pop('1000')
        removed=new_job(store, {**sync_config(), 'notify_emby':False}); await engine.run_job(removed)
        assert not path.exists()
        assert not engine.outputs.get('1','1000')['active']
        assert store.db.execute('SELECT revoked FROM playbacks WHERE id=?',(old['playback_id'],)).fetchone()[0] == 0
        cloud.nodes['1000']=saved
        restored=new_job(store, {**sync_config(), 'notify_emby':False}); await engine.run_job(restored)
        assert engine.job(restored)['state']=='completed'
        assert path.read_bytes()==original
        assert engine.outputs.get('1','1000')['active']
        assert engine.outputs.get('1','1000')['playback_id']==old['playback_id']
        await http.aclose();store.close()
    asyncio.run(scenario())


def test_restore_after_user_revokes_playback_does_not_reauthorize(tmp_path):
    async def scenario():
        store, cloud, http, engine, _ = setup(tmp_path)
        cloud.add_video()
        config={**sync_config(), 'notify_emby':False}
        await engine.run_job(new_job(store,config))
        old=engine.outputs.get('1','1000');path=engine.root/old['relative_path']; saved=cloud.nodes.pop('1000')
        await engine.run_job(new_job(store,config))
        with store.db:store.db.execute('UPDATE playbacks SET revoked=1 WHERE id=?',(old['playback_id'],))
        cloud.nodes['1000']=saved
        jid=new_job(store,config);await engine.run_job(jid)
        assert path.exists()
        assert store.db.execute('SELECT revoked FROM playbacks WHERE id=?',(old['playback_id'],)).fetchone()[0]==1
        new = engine.outputs.get('1','1000')
        assert new['playback_id'] != old['playback_id']
        assert store.db.execute('SELECT state FROM organize_items WHERE job_id=?',(jid,)).fetchone()[0]=='done'
        await http.aclose();store.close()
    asyncio.run(scenario())


def test_sync_preserves_unmanaged_strm_when_no_source_evidence(tmp_path):
    async def scenario():
        store, cloud, http, engine, _ = setup(tmp_path)
        cloud.add_video()
        config={**sync_config(),'notify_emby':False}
        await engine.run_job(new_job(store,config))
        old=engine.outputs.get('1','1000'); path=engine.root/old['relative_path']
        orphan=path.with_name('unmanaged.strm'); orphan.write_text('https://external.example/test')
        cloud.nodes.pop('1000')
        await engine.run_job(new_job(store,config))
        assert not path.exists()
        assert orphan.read_text()=='https://external.example/test'
        await http.aclose();store.close()
    asyncio.run(scenario())


def test_one_individual_notice_suppresses_duplicate_media_card_not_summary(tmp_path):
    async def scenario():
        store, cloud, http, engine, _ = setup(tmp_path)
        cloud.add_video()
        jid=new_job(store)
        await engine.run_job(jid)
        if engine.notice_tasks:
            await asyncio.gather(*engine.notice_tasks)
        await engine.notifications.job(engine.job(jid))
        keys=[r[0] for r in store.db.execute('SELECT event_key FROM notifications WHERE job_id=?',(jid,))]
        assert len([key for key in keys if key.startswith('show_done:')])==1
        assert not [key for key in keys if key.startswith('media:')]
        assert any(key.startswith('strm:') for key in keys)
        await http.aclose();store.close()
    asyncio.run(scenario())


def test_legacy_tokenized_strm_is_upgraded_when_identity_is_known(tmp_path):
    """身份齐全时，存量 /play/<token> 会被改写为 /play115（0.7.29 起的契约）。"""
    import hashlib
    from pathlib import Path
    from strimkeep.playaddr import display_slug
    async def scenario():
        store, cloud, http, engine, _ = setup(tmp_path)
        cloud.add_video()['sha1']='A'*40
        config={**sync_config(),'notify_emby':False}
        await engine.run_job(new_job(store,config))
        old=engine.outputs.get('1','1000'); relative=old['relative_path']; path=engine.root/relative
        token='old-playback-token'; legacy=(config['playback_base']+'/play/'+old['playback_id']+'/'+display_slug(Path(relative).stem)+'?token='+token+'\n').encode()
        path.write_bytes(legacy); checksum=hashlib.sha256(legacy).hexdigest(); cipher=store.crypto.encrypt(token.encode()).decode()
        with store.db:
            store.db.execute('UPDATE output_registry SET token_cipher=?,files=? WHERE account_id=? AND file_id=?',(cipher,json.dumps({relative:checksum}),'1','1000'))
            store.db.execute('UPDATE owned_outputs SET digest=? WHERE relative_path=?',(checksum,relative))
        second=new_job(store,config)
        await engine.run_job(second)
        assert engine.job(second)['state']=='completed'
        body=path.read_bytes()
        assert b'/play115/' in body and token.encode() not in body, '存量令牌地址应升级为 play115'
        await http.aclose();store.close()
    asyncio.run(scenario())


def test_legacy_tokenized_strm_is_kept_when_identity_is_incomplete(tmp_path):
    """缺 sha1/pickcode 时只能写令牌地址，旧形态继续认作未变化，避免每轮重写。"""
    import hashlib
    from pathlib import Path
    from strimkeep.playaddr import display_slug
    from strimkeep.store import digest
    async def scenario():
        store, cloud, http, engine, _ = setup(tmp_path)
        node = cloud.add_video(); node['sha1']='A'*40; node['pickcode']=''  # 有指纹但缺 pickcode，身份不完整
        config={**sync_config(),'notify_emby':False}
        await engine.run_job(new_job(store,config))
        old=engine.outputs.get('1','1000'); relative=old['relative_path']; path=engine.root/relative
        token='old-playback-token'; legacy=(config['playback_base']+'/play/'+old['playback_id']+'/'+display_slug(Path(relative).stem)+'?token='+token+'\n').encode()
        path.write_bytes(legacy); checksum=hashlib.sha256(legacy).hexdigest(); cipher=store.crypto.encrypt(token.encode()).decode()
        with store.db:
            store.db.execute('UPDATE output_registry SET token_cipher=?,files=? WHERE account_id=? AND file_id=?',(cipher,json.dumps({relative:checksum}),'1','1000'))
            store.db.execute('UPDATE owned_outputs SET digest=? WHERE relative_path=?',(checksum,relative))
            store.db.execute('UPDATE playbacks SET token_hash=? WHERE id=?',(digest(token),old['playback_id']))
        second=new_job(store,config);await engine.run_job(second)
        assert engine.job(second)['state']=='completed'
        assert path.read_bytes()==legacy, '身份不全时旧形态应原样保留'
        await http.aclose();store.close()
    asyncio.run(scenario())


