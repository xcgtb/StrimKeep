import asyncio,json,sqlite3
from types import SimpleNamespace
import httpx
from strimkeep.untyped_cleanup import candidates,kind,recycle_companions


def test_identifies_only_clear_subtitle_or_tmdb_page():
    assert kind(b'1\n00:00:01,000 --> 00:00:02,000\nHello')=='SRT 字幕'
    assert kind(b'WEBVTT\n\n00:00:01.000 --> 00:00:02.000')=='WebVTT 字幕'
    assert kind(b'[Script Info]\n[Events]\n')=='ASS 字幕'
    assert kind(b'<html><title>Film - The Movie Database</title><a href="https://themoviedb.org">')=='TMDB 网页'
    assert kind(b'<html><title>The Movie Database</title>themoviedb.org\xe4\xb8')=='TMDB 网页'
    assert kind(b'<html><title>Personal</title>important document')==''
    assert kind(b'ordinary text')==''
    assert kind(b'\xff\xfe\x00')==''


def test_recycles_only_confirmed_same_parent_after_success_and_retains_unknown():
    async def run():
        db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row
        db.execute('CREATE TABLE organize_items(job_id INTEGER,state TEXT,original TEXT)')
        db.execute('INSERT INTO organize_items VALUES(?,?,?)',(1,'done',json.dumps({'parent_id':'7'})))
        events=[];removed=[]
        store=SimpleNamespace(db=db,event=lambda *a:events.append(a))
        files=[{'id':'11','name':'323839','size':100,'parent_id':'7','sha1':'AA'},
               {'id':'12','name':'document','size':100,'parent_id':'7','sha1':'BB'},
               {'id':'13','name':'elsewhere','size':100,'parent_id':'8','sha1':'CC'}]
        class Cloud:
            async def file(self,fid):return {**next(x for x in files if x['id']==fid),'pickcode':fid}
            async def url(self,pick,ua):return 'https://example.test/'+pick,{}
            async def recycle(self,fid,parent):removed.append((fid,parent))
        def respond(req):
            content=(b'<html><title>The Movie Database</title>themoviedb.org' if req.url.path=='/11' else b'important document')
            return httpx.Response(206,content=content)
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            def check(original,current,name,parent):
                assert all(current[k]==original[k] for k in ('id','name','size','parent_id','sha1'))
            engine=SimpleNamespace(store=store,cloud=Cloud(),http=http,check_source=check,boundary=lambda _:None)
            report={'excluded':3,'formats':{'无扩展名':3},'reasons':{'未列入当前视频或字幕扩展名，按规则跳过':3},'examples':[]}
            count=await recycle_companions(engine,1,{'untyped_companions':candidates(files),'scan_filter':report})
            assert count==1 and removed==[('11','7')]
            assert report['excluded']==2 and report['formats']['无扩展名']==2
            db.execute("UPDATE organize_items SET state='pending'");removed.clear()
            assert await recycle_companions(engine,1,{'untyped_companions':candidates(files)})==0 and not removed
    asyncio.run(run())
