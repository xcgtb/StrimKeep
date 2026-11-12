import json
import time
import hashlib

from strimkeep import share_service
from strimkeep.quality import facts
from test_organize import MEDIA, new_job
from test_phase2_share_pipeline import share_setup
from test_upgrade import policy


def test_share_wash_defers_old_removal_and_uses_unknown_fallback(tmp_path):
    store,cloud,http,engine,_ = share_setup(tmp_path)
    directory=engine.root/'分享影视库'/'测试剧集'/'Season 1'
    directory.mkdir(parents=True)
    old=directory/'测试剧集.S01E01.1080p.strm';old.write_text('old')
    with store.db:
        store.db.execute("INSERT INTO share_strms(id,share_code,receive_code,file_id,pickcode,name,strm_path,token_hash,revoked,created,media,facts,origin,size) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ('old','share','','oldid','','测试剧集.S01E01.1080p',str(old.relative_to(engine.root)),'',0,time.time(),None,None,'web',1000))
    row={'id':'new'};hint={'media_type':'tv','season':1,'episode':1,'end_episode':1}
    new=facts('测试剧集.S01E01.2160p.mkv', MEDIA,1000)
    p=policy('resolution');retiring=[];jid=new_job(store)
    result=share_service._wash(engine,jid,row,directory,directory/'new.strm','new',hint,new,p,True,retiring)
    assert result=='publish' and len(retiring)==1
    assert old.read_text()=='old'
    assert store.db.execute("SELECT revoked FROM share_strms WHERE id='old'").fetchone()[0]==0
    # A same-resolution tie keeps the existing entry.
    same={**new,'resolution':1080};retiring=[]
    assert share_service._wash(engine,jid,row,directory,directory/'new.strm','new',hint,same,p,True,retiring)=='skip'
    assert not retiring and old.exists()
    import asyncio
    asyncio.run(http.aclose());store.close()


def test_same_path_replacement_is_allowed_only_after_comparison(tmp_path):
    store,cloud,http,engine,_ = share_setup(tmp_path)
    directory=engine.root/'分享影视库'/'测试剧集'/'Season 1';directory.mkdir(parents=True)
    dest=directory/'测试剧集.S01E01.1080p.strm';dest.write_text('http://host/play115/share/old')
    oldfacts=facts('测试剧集.S01E01.1080p.mkv',{**MEDIA,'width':1920,'height':1080},1000)
    with store.db:
        store.db.execute('INSERT INTO share_strms(id,share_code,receive_code,file_id,pickcode,name,strm_path,token_hash,revoked,created,media,facts,origin,size) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            ('old','s','','f','','测试剧集.S01E01.1080p',str(dest.relative_to(engine.root)),'',0,time.time(),None,json.dumps(oldfacts),'web',1000))
    hint={'media_type':'tv','season':1,'episode':1,'end_episode':1};p=policy('resolution');retiring=[]
    new=facts('测试剧集.S01E01.2160p.mkv',MEDIA,1000)
    assert share_service._wash(engine,new_job(store),{'id':'new'},directory,dest,'new',hint,new,p,True,retiring)=='publish'
    assert len(retiring)==1 and dest.read_text().endswith('/old')
    import asyncio
    asyncio.run(http.aclose());store.close()


def test_share_dolby_group_and_unknown_fallback(tmp_path):
    store,cloud,http,engine,_ = share_setup(tmp_path)
    directory=engine.root/'分享影视库'/'测试剧集'/'Season 1';directory.mkdir(parents=True)
    old=directory/'测试剧集.S01E01.1080p.strm';old.write_text('http://host/play115/share/old')
    dv=facts('测试剧集.S01E01.mkv',MEDIA,1000)
    hdr=facts('测试剧集.S01E01.mkv',{**MEDIA,'dynamic_range':'HDR PQ'},1000)
    with store.db:
        store.db.execute('INSERT INTO share_strms(id,share_code,receive_code,file_id,pickcode,name,strm_path,token_hash,revoked,created,media,facts,origin,size) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            ('old','s','','f','','测试剧集.S01E01',str(old.relative_to(engine.root)),'',0,time.time(),None,json.dumps(dv),'web',1000))
    hint={'media_type':'tv','season':1,'episode':1,'end_episode':1};jid=new_job(store)
    p=policy('resolution',dynamic_groups='dovi');retiring=[]
    dest=directory/'new.strm'
    assert share_service._wash(engine,jid,{'id':'new'},directory,dest,'new',hint,hdr,p,True,retiring)=='publish'
    assert retiring==[]
    unknown={**hdr,'dynamic_range':None};retiring=[]
    assert share_service._wash(engine,jid,{'id':'new'},directory,dest,'new',hint,unknown,p,True,retiring)=='skip'
    with store.db:store.db.execute("UPDATE share_strms SET facts=? WHERE id='old'",(json.dumps(unknown),))
    retiring=[]
    assert share_service._wash(engine,jid,{'id':'new'},directory,dest,'new',hint,dv,p,True,retiring)=='publish'
    assert len(retiring)==1 and old.exists()
    import asyncio
    asyncio.run(http.aclose());store.close()


def test_share_replacement_recovers_after_file_published_before_registry(tmp_path):
    store,cloud,http,engine,_ = share_setup(tmp_path)
    root=engine.root;dest=root/'分享影视库'/'片名.strm';dest.parent.mkdir(parents=True)
    pending=root/'分享STRM待整理'/'new.strm';pending.parent.mkdir(parents=True,exist_ok=True)
    old_bytes=b'http://host/play115/share/old';new_bytes=b'http://host/play115/share/new'
    dest.write_bytes(old_bytes);pending.write_bytes(new_bytes)
    with store.db:
        for ident,path in [('old',dest),('new',pending)]:
            store.db.execute('INSERT INTO share_strms(id,share_code,receive_code,file_id,pickcode,name,strm_path,token_hash,revoked,created,media,facts,origin,size) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (ident,'s','',ident,'',ident,str(path.relative_to(root)),'',0,time.time(),None,None,'web',1000))
        payload={'pending':str(pending.relative_to(root)),'dest':str(dest.relative_to(root)),
                 'digest':hashlib.sha256(new_bytes).hexdigest(),'name':'片名','media':'{}','facts':'{}','origin':'web',
                 'retiring':[{'id':'old','path':str(dest.relative_to(root)),'digest':hashlib.sha256(old_bytes).hexdigest()}]}
        store.db.execute('INSERT INTO share_wash_intents VALUES(?,?,?)',('new',1,json.dumps(payload)))
    # Before the atomic publish, the old file must not be retired.
    share_service.recover_share_washes(engine)
    assert dest.read_bytes()==old_bytes and store.db.execute("SELECT revoked FROM share_strms WHERE id='old'").fetchone()[0]==0
    import os
    os.replace(pending,dest)
    # Restart between os.replace and the registry update.
    share_service.recover_share_washes(engine)
    assert dest.read_bytes()==new_bytes
    assert store.db.execute("SELECT revoked FROM share_strms WHERE id='old'").fetchone()[0]==1
    assert store.db.execute("SELECT strm_path FROM share_strms WHERE id='new'").fetchone()[0]==str(dest.relative_to(root))
    assert not store.db.execute("SELECT 1 FROM share_wash_intents WHERE id='new'").fetchone()
    import asyncio
    asyncio.run(http.aclose());store.close()


def test_changed_old_file_is_preserved_with_recovery_intent(tmp_path):
    store,cloud,http,engine,_ = share_setup(tmp_path)
    root=engine.root; directory=root/'分享影视库';directory.mkdir(parents=True)
    old=directory/'old.strm';dest=directory/'new.strm';pending=root/'pending.strm'
    old_bytes=b'http://host/play115/share/old';new_bytes=b'http://host/play115/share/new'
    old.write_bytes(b'http://host/play115/share/someone_else');dest.write_bytes(new_bytes)
    with store.db:
        for ident,path in [('old',old),('new',pending)]:
            store.db.execute('INSERT INTO share_strms(id,share_code,receive_code,file_id,pickcode,name,strm_path,token_hash,revoked,created,media,facts,origin,size) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (ident,'s','',ident,'',ident,str(path.relative_to(root)),'',0,time.time(),None,None,'web',1000))
        payload={'pending':str(pending.relative_to(root)),'dest':str(dest.relative_to(root)),
                 'digest':hashlib.sha256(new_bytes).hexdigest(),'name':'片名','media':'{}','facts':'{}','origin':'web',
                 'retiring':[{'id':'old','path':str(old.relative_to(root)),'digest':hashlib.sha256(old_bytes).hexdigest()}]}
        store.db.execute('INSERT INTO share_wash_intents VALUES(?,?,?)',('new',1,json.dumps(payload)))
    share_service.recover_share_washes(engine)
    assert old.read_bytes()==b'http://host/play115/share/someone_else'
    assert store.db.execute("SELECT revoked FROM share_strms WHERE id='old'").fetchone()[0]==0
    assert store.db.execute("SELECT 1 FROM share_wash_intents WHERE id='new'").fetchone()
    old.write_bytes(old_bytes)
    share_service.recover_share_washes(engine)
    assert not old.exists()
    assert store.db.execute("SELECT revoked FROM share_strms WHERE id='old'").fetchone()[0]==1
    assert not store.db.execute("SELECT 1 FROM share_wash_intents WHERE id='new'").fetchone()
    import asyncio
    asyncio.run(http.aclose());store.close()


def test_same_destination_restart_finishes_published_replacement(tmp_path, monkeypatch):
    import asyncio
    import os
    from strimkeep.quality import public_policy
    async def scenario():
        store,cloud,http,engine,_=share_setup(tmp_path)
        store.set_many({'upgrade_policy':public_policy(policy('resolution'))})
        secret={'share_code':'samepath','receive_code':''}
        filename='测试剧集 (2024) S01E03.2160p.WEB-DL.mkv'
        share_service.generate(store,engine.root,secret,[{'id':'301','name':filename,'size':5000,'path':''}],origin='web')
        await engine.run_job(share_service.enqueue_organize(engine,origin='web'))
        old=store.db.execute("SELECT * FROM share_strms WHERE file_id='301'").fetchone()
        oldfacts=json.loads(old['facts']);oldfacts['resolution']=1080
        with store.db:store.db.execute('UPDATE share_strms SET facts=? WHERE id=?',(json.dumps(oldfacts),old['id']))
        share_service.generate(store,engine.root,secret,[{'id':'302','name':filename,'size':6000,'path':''}],origin='web')
        new=store.db.execute("SELECT * FROM share_strms WHERE file_id='302'").fetchone()
        pending=engine.root/new['strm_path'];target=engine.root/old['strm_path']
        real_replace=os.replace
        def fail_after_publish(src,dst):
            real_replace(src,dst)
            if str(src)==str(pending) and str(dst)==str(target):
                raise asyncio.CancelledError()
        monkeypatch.setattr(os,'replace',fail_after_publish)
        jid=share_service.enqueue_organize(engine,origin='web')
        try: await engine.run_job(jid)
        except asyncio.CancelledError: pass
        monkeypatch.setattr(os,'replace',real_replace)
        assert '/play115/share/'+new['id']+'/' in target.read_text()
        assert store.db.execute('SELECT revoked FROM share_strms WHERE id=?',(old['id'],)).fetchone()[0]==0
        share_service.recover_share_washes(engine)
        assert store.db.execute('SELECT revoked FROM share_strms WHERE id=?',(old['id'],)).fetchone()[0]==1
        assert store.db.execute('SELECT strm_path FROM share_strms WHERE id=?',(new['id'],)).fetchone()[0]==old['strm_path']
        assert '/play115/share/'+new['id']+'/' in target.read_text()
        await http.aclose();store.close()
    asyncio.run(scenario())
