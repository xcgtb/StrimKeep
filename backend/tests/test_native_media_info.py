import asyncio
import json
import time
import httpx
from strimkeep.store import digest
from test_organize import setup


def test_opt_in_native_probe_is_verified_and_deduplicated(tmp_path, monkeypatch):
    async def run():
        store,cloud,http,engine,_=setup(tmp_path)
        delivery=engine.emby; worker=delivery.media_info
        delivery.connection=lambda: ('http://emby', 'test-key')
        target={'relative':'movie/example.strm','pid':'p1'}
        path=engine.root/'movie/example.strm';path.parent.mkdir(parents=True,exist_ok=True);path.write_text('http://test/play/p1')
        item={'Id':'abc123','Path':'/media/movie/example.strm'}
        conn=digest('http://emby|test-key')
        worker.enqueue(4,item,target,conn)
        assert store.db.execute('SELECT count(*) FROM native_media_info_queue').fetchone()[0]==0
        store.set_many({'sync_config':{'emby_media_info':True}})
        worker.enqueue(4,item,target,conn);worker.enqueue(4,item,target,conn)
        calls=[]
        async def request(_http,method,url,**kwargs):
            calls.append(url)
            streams=[{'Type':'Video','Codec':'h264'}]
            if url.endswith('/PlaybackInfo'):
                data={'MediaSources':[{'MediaStreams':streams}]}
            else:
                data={'Items':[{**item,'MediaStreams':streams if len(calls)>1 else []}]}
            return httpx.Response(200,json=data,request=httpx.Request(method,url))
        monkeypatch.setattr('strimkeep.native_media_info.emby_request',request)
        await worker.tick()
        row=store.db.execute('SELECT state FROM native_media_info_queue').fetchone()
        assert row['state']=='done' and len(calls)==3
        assert json.loads(path.with_name('example-media-info.json').read_text())['streams'][0]['Codec']=='h264'
        await worker.tick(); assert len(calls)==3
        await http.aclose();store.close()
    asyncio.run(run())


def test_play115_strm_identity_uses_playbacks_file_id(tmp_path):
    """play115 地址没有播放 token：用 playbacks.file_id 核对，不再一律判「身份已变化」。"""
    async def run():
        store, cloud, http, engine, _ = setup(tmp_path)
        delivery = engine.emby
        worker = delivery.media_info
        with store.db:
            store.db.execute(
                'INSERT INTO playbacks(id,file_id,pickcode,name,relative_path,token_hash,revoked,created) '
                "VALUES('p1','fid1','pc1','example','movie/example.strm','h',0,0)")
        target = {'relative': 'movie/example.strm', 'pid': 'p1'}
        path = engine.root / 'movie/example.strm'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('http://test/play115/pc1/fid1/sha1/123/example.mkv')
        item = {'Id': 'abc123', 'Path': '/media/movie/example.strm',
                'MediaStreams': [{'Type': 'Video', 'Codec': 'hevc'}]}
        worker.write_sidecar(target, item)
        assert path.with_name('example-media-info.json').is_file()

        path.write_text('http://test/play115/pc1/OTHER/sha1/123/example.mkv')
        try:
            worker.write_sidecar(target, item)
            raise AssertionError('身份不符时应当拒绝')
        except ValueError as exc:
            assert '身份' in str(exc)
        await http.aclose()
        store.close()
    asyncio.run(run())


def test_sweep_missing_enqueues_managed_strm_without_sidecar(tmp_path, monkeypatch):
    """补扫：受管 STRM 缺 sidecar 且 Emby 有条目 → 入队；节流窗口内不重复跑。"""
    async def run():
        store, cloud, http, engine, _ = setup(tmp_path)
        store.set_many({'sync_config': {'emby_media_info': True}})
        delivery = engine.emby
        worker = delivery.media_info
        delivery.connection = lambda: ('http://emby', 'test-key')
        path = engine.root / 'movie/example2.strm'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('http://test/play115/pc1/fid2/sha1/123/example2.mkv')
        with store.db:
            store.db.execute(
                'INSERT INTO playbacks(id,file_id,pickcode,name,relative_path,token_hash,revoked,created) '
                "VALUES('p2','fid2','pc1','example2','movie/example2.strm','h',0,0)")

        async def request(_http, method, url, **kwargs):
            items = [{'Id': 'e2', 'Path': '/media/movie/example2.strm'}]
            return httpx.Response(200, json={'Items': items},
                                  request=httpx.Request(method, url))

        monkeypatch.setattr('strimkeep.native_media_info.emby_request', request)
        assert await worker.sweep_missing() == 1
        row = store.db.execute('SELECT item_id,state FROM native_media_info_queue').fetchone()
        assert row['item_id'] == 'e2' and row['state'] == 'queued'
        assert await worker.sweep_missing() == 0          # 5 分钟节流窗口
        worker.last_sweep = 0
        path.with_name('example2-media-info.json').write_text('{}')
        assert await worker.sweep_missing() == 0          # 已有 sidecar 不再入队
        await http.aclose()
        store.close()
    asyncio.run(run())


def test_existing_foreign_sidecar_is_not_overwritten(tmp_path):
    store,cloud,http,engine,_=setup(tmp_path)
    worker=engine.emby.media_info
    path=engine.root/'movie/example.strm';path.parent.mkdir(parents=True,exist_ok=True);path.write_text('http://test/play115/share/s1')
    sidecar=path.with_name('example-media-info.json');sidecar.write_text('user data')
    target={'relative':'movie/example.strm','pid':'share/s1'}
    with store.db:
        store.db.execute("INSERT INTO share_strms(id,share_code,file_id,name,strm_path,token_hash,created) VALUES('s1','code','fid','example','movie/example.strm','h',0)")
    item={'Path':'/media/movie/example.strm','MediaStreams':[{'Type':'Video','Codec':'h264'}]}
    import pytest
    with pytest.raises(ValueError):worker.write_sidecar(target,item)
    assert sidecar.read_text()=='user data'
    asyncio.run(http.aclose());store.close()


def test_sidecar_updates_owned_file_and_does_not_write_source_urls(tmp_path):
    store,cloud,http,engine,_=setup(tmp_path);worker=engine.emby.media_info
    path=engine.root/'movie/example.strm';path.parent.mkdir(parents=True,exist_ok=True);path.write_text('http://test/play115/share/s1')
    target={'relative':'movie/example.strm','pid':'share/s1'}
    with store.db:
        store.db.execute("INSERT INTO share_strms(id,share_code,file_id,name,strm_path,token_hash,created) VALUES('s1','code','fid','example','movie/example.strm','h',0)")
    item={'Path':'/media/movie/example.strm','RunTimeTicks':100,
          'MediaStreams':[{'Type':'Video','Codec':'h264','Path':'https://private/token','Width':640},
                          {'Type':'Subtitle','Codec':'subrip','Language':'zho'}]}
    worker.write_sidecar(target,item)
    sidecar=path.with_name('example-media-info.json');content=sidecar.read_text()
    assert 'https://private/token' not in content and 'subrip' in content
    item['MediaStreams'][0]['Width']=1920;worker.write_sidecar(target,item)
    assert json.loads(sidecar.read_text())['streams'][0]['Width']==1920
    sidecar.write_text('user edited')
    import pytest
    with pytest.raises(ValueError):worker.write_sidecar(target,item)
    assert sidecar.read_text()=='user edited'
    asyncio.run(http.aclose());store.close()


def test_sidecar_corrects_reversed_emby_dimensions_only_with_probe_evidence(tmp_path):
    store,cloud,http,engine,_=setup(tmp_path);worker=engine.emby.media_info
    path=engine.root/'movie/example.strm';path.parent.mkdir(parents=True,exist_ok=True);path.write_text('http://test/play115/share/s1')
    target={'relative':'movie/example.strm','pid':'share/s1'}
    media={'width':3840,'height':2160,'codec':'hevc','probe_revision':3,'dynamic_range':'Dolby Vision',
           'duration':'2739.072000','bit_rate':6409898}
    with store.db:
        store.db.execute('ALTER TABLE share_strms ADD COLUMN media TEXT')
        store.db.execute('ALTER TABLE share_strms ADD COLUMN size REAL')
        store.db.execute("INSERT INTO share_strms(id,share_code,file_id,name,strm_path,token_hash,created,media) VALUES('s1','code','fid','example','movie/example.strm','h',0,?)", (json.dumps(media),))
        store.db.execute("UPDATE share_strms SET size=2194646809 WHERE id='s1'")
    item={'Path':'/media/movie/example.strm','MediaStreams':[{'Type':'Video','Codec':'hevc','Width':2160,'Height':3840}]}
    worker.write_sidecar(target,item)
    result=json.loads(path.with_name('example-media-info.json').read_text())
    video=result['streams'][0]
    assert (video['Width'],video['Height'])==(3840,2160)
    assert video['ExtendedVideoType']=='DolbyVision'
    assert result['size']==2194646809 and result['duration_ticks']==27390720000 and result['bitrate']==6409898
    item['MediaStreams'][0].update(Width=1080,Height=1920)
    worker.write_sidecar(target,item)
    video=json.loads(path.with_name('example-media-info.json').read_text())['streams'][0]
    assert (video['Width'],video['Height'])==(1080,1920)
    asyncio.run(http.aclose());store.close()


def test_ffprobe_video_details_survive_sidecar_pipeline(tmp_path):
    from strimkeep.probe import summarize
    store,cloud,http,engine,_=setup(tmp_path);worker=engine.emby.media_info
    path=engine.root/'movie/example.strm';path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text('http://test/play115/share/s1',encoding='utf-8')
    video={'codec_type':'video','codec_name':'hevc','width':3840,'height':1608,
           'profile':'Main 10','level':153,'pix_fmt':'yuv420p10le',
           'display_aspect_ratio':'160:67','color_primaries':'bt2020',
           'color_space':'bt2020nc','color_transfer':'smpte2084','avg_frame_rate':'25/1',
           'field_order':'progressive','tags':{'title':'Test Video'}}
    audio={'codec_type':'audio','codec_name':'eac3','channels':6,'sample_rate':'48000','bit_rate':'640000'}
    media=summarize({'streams':[video,audio], 'format':{'duration':'2739.093','bit_rate':'7752705'}})
    with store.db:
        store.db.execute('ALTER TABLE share_strms ADD COLUMN media TEXT')
        store.db.execute('ALTER TABLE share_strms ADD COLUMN size REAL')
        store.db.execute("INSERT INTO share_strms(id,share_code,file_id,name,strm_path,token_hash,created,media,size) VALUES('s1','code','fid','example','movie/example.strm','h',0,?,?)",(json.dumps(media),2654422574))
    target={'relative':'movie/example.strm','pid':'share/s1'}
    item=worker.probe_item(target,{'Path':'/media/movie/example.strm'})
    worker.write_sidecar(target,item,source_name='ffprobe')
    result=json.loads(path.with_name('example-media-info.json').read_text())
    v,a=result['streams']
    assert (v['Width'],v['Height'],v['Profile'],v['Level'])==(3840,1608,'Main 10',153)
    assert (v['PixelFormat'],v['AspectRatio'])==('yuv420p10le','160:67')
    assert (v['ColorPrimaries'],v['ColorSpace'],v['ColorTransfer'])==('bt2020','bt2020nc','smpte2084')
    assert v['IsInterlaced'] is False and (a['SampleRate'],a['BitRate'])==(48000,640000)
    assert result['size']==2654422574 and result['duration_ticks']==27390930000
    asyncio.run(http.aclose());store.close()


def test_changed_path_is_never_probed(tmp_path, monkeypatch):
    async def run():
        store,cloud,http,engine,_=setup(tmp_path);worker=engine.emby.media_info
        engine.emby.connection=lambda: ('http://emby','test-key')
        store.set_many({'sync_config':{'emby_media_info':True}})
        worker.enqueue(5,{'Id':'abc123'}, {'relative':'right/video.strm','pid':'p1'},digest('http://emby|test-key'))
        calls=[]
        async def request(_http,method,url,**kwargs):
            calls.append(url)
            return httpx.Response(200,json={'Items':[{'Id':'abc123','Path':'/other/video.strm'}]},request=httpx.Request(method,url))
        monkeypatch.setattr('strimkeep.native_media_info.emby_request',request)
        await worker.tick()
        assert store.db.execute('SELECT state FROM native_media_info_queue').fetchone()['state']=='failed'
        assert len(calls)==1
        await http.aclose();store.close()
    asyncio.run(run())


def test_emby_verification_enqueues_only_a_unique_matching_item(tmp_path):
    async def run():
        store,cloud,http,engine,_=setup(tmp_path)
        delivery=engine.emby;delivery.connection=lambda: ('http://emby','test-key')
        store.set_many({'sync_config':{'emby_media_info':True}})
        target={'relative':'movie/example.strm','pid':'p1','kind':'movie','tmdb':123,'title':'Movie','confirmed':False}
        payload={'connection':digest('http://emby|test-key'),'targets':[target]}
        store.db.execute("INSERT INTO emby_deliveries(job_id,state,payload,next_due,deadline) VALUES(?,?,?,?,?)",(9,'checking',json.dumps(payload),0,100))
        async def items(_params,_headers,_url):
            return [{'Id':'abc123','Path':'/media/movie/example.strm'}]
        delivery.items=items
        result=await delivery.verify(9)
        assert result['confirmed']==1
        assert store.db.execute('SELECT item_id,state FROM native_media_info_queue').fetchone()['item_id']=='abc123'
        await http.aclose();store.close()
    asyncio.run(run())

def test_share_sweep_and_verified_media_sidecar(tmp_path, monkeypatch):
    async def run():
        store, cloud, http, engine, _ = setup(tmp_path)
        store.set_many({'sync_config': {'emby_media_info': True}})
        worker = engine.emby.media_info
        engine.emby.connection = lambda: ('http://emby', 'test-key')
        rel = '分享影视库/剧集/Example/Season 1/E01.strm'
        path = engine.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('http://test/play115/share/s1')
        with store.db:
            store.db.execute('INSERT INTO share_strms(id,share_code,file_id,name,strm_path,token_hash,created) VALUES(?,?,?,?,?,?,?)',
                             ('s1', 'code', 'fid', 'ep', rel, 'hash', 0))
        item = {'Id': 'e1', 'Path': '/media/' + rel,
                'MediaStreams': [{'Type': 'Video', 'Codec': 'hevc'}]}
        async def request(_http, method, url, **kwargs):
            return httpx.Response(200, json={'Items': [item]}, request=httpx.Request(method, url))
        monkeypatch.setattr('strimkeep.native_media_info.emby_request', request)
        assert await worker.sweep_missing() == 1
        row = store.db.execute('SELECT * FROM native_media_info_queue').fetchone()
        assert json.loads(row['target']) == {'relative': rel, 'pid': 'share/s1'}
        await worker.tick()
        assert store.db.execute('SELECT state FROM native_media_info_queue').fetchone()['state'] == 'done'
        assert json.loads(path.with_name('E01-media-info.json').read_text())['streams'][0]['Codec'] == 'hevc'
        with store.db: store.db.execute("UPDATE share_strms SET revoked=1 WHERE id='s1'")
        import pytest
        with pytest.raises(ValueError, match='身份'):
            worker.write_sidecar({'relative': rel, 'pid': 'share/s1'}, item)
        await http.aclose(); store.close()
    asyncio.run(run())


def test_share_sweep_skips_ambiguous_emby_paths(tmp_path, monkeypatch):
    async def run():
        store, cloud, http, engine, _ = setup(tmp_path)
        store.set_many({'sync_config': {'emby_media_info': True}})
        worker = engine.emby.media_info
        engine.emby.connection = lambda: ('http://emby', 'test-key')
        rel = '分享影视库/example.strm'
        path = engine.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('http://test/play115/share/s1')
        with store.db:
            store.db.execute('INSERT INTO share_strms(id,share_code,file_id,name,strm_path,token_hash,created) VALUES(?,?,?,?,?,?,?)',
                             ('s1', 'code', 'fid', 'example', rel, 'hash', 0))
        async def request(_http, method, url, **kwargs):
            return httpx.Response(200, json={'Items': [{'Id': 'a', 'Path': '/media/' + rel}, {'Id': 'b', 'Path': '/other/' + rel}]}, request=httpx.Request(method, url))
        monkeypatch.setattr('strimkeep.native_media_info.emby_request', request)
        assert await worker.sweep_missing() == 0
        assert store.db.execute('SELECT count(*) FROM native_media_info_queue').fetchone()[0] == 0
        await http.aclose(); store.close()
    asyncio.run(run())

def test_local_probe_finishes_locally_but_keeps_emby_pending(tmp_path, monkeypatch):
    """本地 ffprobe 只结束本项目这一半；Emby 端的媒体信息仍要 Emby 自己探（0.9.48）。"""
    async def run():
        store, cloud, http, engine, _ = setup(tmp_path)
        from strimkeep.share_service import install
        install(store)
        store.set_many({'sync_config': {'emby_media_info': True}})
        worker = engine.emby.media_info
        engine.emby.connection = lambda: ('http://emby', 'test-key')
        rel = '分享影视库/剧集/Example/Season 1/E01.strm'
        path = engine.root / rel; path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('http://test/play115/share/s1')
        media = {'probe_revision': 1, 'codec': 'hevc', 'width': 3840, 'height': 2160,
                 'audio': [{'codec': 'aac', 'channels': 2, 'language': 'zho'}]}
        with store.db:
            store.db.execute('INSERT INTO share_strms(id,share_code,file_id,name,strm_path,token_hash,created,media) VALUES(?,?,?,?,?,?,?,?)',
                             ('s1','code','fid','ep',rel,'hash',0,json.dumps(media)))
        target = {'relative': rel, 'pid': 'share/s1'}
        worker.enqueue(1, {'Id': 'e1'}, target, digest('http://emby|test-key'))
        calls = []
        emby = {'probed': False}
        async def request(_http, method, url, **kwargs):
            calls.append(url)
            streams = [{'Type': 'Video', 'Codec': 'hevc'}, {'Type': 'Audio', 'Codec': 'aac'}]
            if url.endswith('/PlaybackInfo'):
                emby['probed'] = True
                return httpx.Response(200, json={'MediaSources': [{'MediaStreams': streams}]},
                                      request=httpx.Request(method, url))
            item = {'Id': 'e1', 'Path': '/media/' + rel,
                    'MediaStreams': streams if emby['probed'] else []}
            return httpx.Response(200, json={'Items': [item]}, request=httpx.Request(method, url))
        monkeypatch.setattr('strimkeep.native_media_info.emby_request', request)
        await worker.tick()
        data = json.loads(path.with_name('E01-media-info.json').read_text())
        assert data['streams'][0]['Codec'] == 'hevc' and data['streams'][1]['Codec'] == 'aac'
        assert data['source'] == 'ffprobe'
        # 本项目这一半立刻收工：只查一次 Emby 条目就落盘，不等那 25 分钟的重试窗口。
        row = store.db.execute('SELECT state,emby_state FROM native_media_info_queue').fetchone()
        assert len(calls) == 1 and row['state'] == 'done'
        # 但 Emby 自己还没有视频流，这一半必须继续追——0.9.1 就是在这里漏掉的。
        assert row['emby_state'] == 'pending'
        assert await worker.probe_emby() == 1
        assert any(url.endswith('/PlaybackInfo') for url in calls)
        row = store.db.execute('SELECT emby_state,emby_attempts FROM native_media_info_queue').fetchone()
        assert row['emby_state'] == 'confirmed' and row['emby_attempts'] == 0
        # 确认之后用 Emby 的流覆盖 sidecar：Emby 端以 Emby 的探测值为准。
        assert json.loads(path.with_name('E01-media-info.json').read_text())['streams'][1]['Codec'] == 'aac'
        assert await worker.probe_emby() == 0
        media['estimated'] = True
        with store.db: store.db.execute('UPDATE share_strms SET media=? WHERE id=?', (json.dumps(media), 's1'))
        assert worker.probe_item(target, {'Path': '/media/'+rel}) is None
        await http.aclose(); store.close()
    asyncio.run(run())


def test_without_local_probe_it_still_falls_back_to_emby(tmp_path, monkeypatch):
    async def run():
        store, cloud, http, engine, _ = setup(tmp_path)
        from strimkeep.share_service import install
        install(store)
        store.set_many({'sync_config': {'emby_media_info': True}})
        worker = engine.emby.media_info
        engine.emby.connection = lambda: ('http://emby', 'test-key')
        rel = '分享影视库/example.strm'
        path = engine.root / rel; path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('http://test/play115/share/s1')
        # 本地没有可用的探测结果（整理时没跑 ffprobe）：只能等 Emby 自己探测。
        with store.db:
            store.db.execute('INSERT INTO share_strms(id,share_code,file_id,name,strm_path,token_hash,created,media) VALUES(?,?,?,?,?,?,?,?)',
                             ('s1','code','fid','ep',rel,'hash',0,None))
        worker.enqueue(1, {'Id': 'e1'}, {'relative': rel, 'pid': 'share/s1'}, digest('http://emby|test-key'))
        async def request(_http, method, url, **kwargs):
            if url.endswith('/PlaybackInfo'):
                return httpx.Response(200, json={'MediaSources': []}, request=httpx.Request(method,url))
            return httpx.Response(200, json={'Items':[{'Id':'e1','Path':'/media/'+rel,'MediaStreams':[]}]}, request=httpx.Request(method,url))
        monkeypatch.setattr('strimkeep.native_media_info.emby_request', request)
        await worker.tick()
        row = store.db.execute('SELECT state,attempts FROM native_media_info_queue').fetchone()
        assert row['state'] == 'queued' and row['attempts'] == 1
        assert not path.with_name('example-media-info.json').exists()
        await http.aclose(); store.close()
    asyncio.run(run())


def test_verify_deduplicates_same_episode_from_two_series_but_not_distinct_ids(tmp_path):
    async def run():
        store,cloud,http,engine,_=setup(tmp_path);delivery=engine.emby
        delivery.connection=lambda:('http://emby','test-key')
        store.set_many({'sync_config':{'emby_media_info':True}})
        target={'relative':'share/ep.strm','pid':'share/s1','kind':'tv','tmdb':123,'title':'剧集','confirmed':False}
        payload={'connection':digest('http://emby|test-key'),'targets':[target]}
        store.db.execute("INSERT INTO emby_deliveries(job_id,state,payload,next_due,deadline) VALUES(?,?,?,?,?)",(9,'checking',json.dumps(payload),0,100))
        async def items(params,*_):
            if params.get('IncludeItemTypes')=='Series':return [{'Id':'series-a'},{'Id':'series-b'}]
            return [{'Id':'same-episode','Path':'/media/share/ep.strm'}]
        delivery.items=items
        result=await delivery.verify(9)
        assert result['confirmed']==1
        assert store.db.execute('SELECT item_id FROM native_media_info_queue').fetchone()['item_id']=='same-episode'
        store.db.execute('DELETE FROM native_media_info_queue');payload['targets'][0]['confirmed']=False
        delivery.save(9,payload=json.dumps(payload))
        async def ambiguous(params,*args):
            if params.get('IncludeItemTypes')=='Series':return [{'Id':'series-a'},{'Id':'series-b'}]
            endpoint = args[2] if len(args) > 2 else '/Items'
            sid = endpoint.split('/')[2]
            return [{'Id':sid,'Path':'/media/share/ep.strm'}]
        delivery.items=ambiguous;await delivery.verify(9)
        assert store.db.execute('SELECT count(*) FROM native_media_info_queue').fetchone()[0]==0
        await http.aclose();store.close()
    asyncio.run(run())

def test_sweep_paginates_to_find_later_share_episode(tmp_path,monkeypatch):
    async def run():
        store,cloud,http,engine,_=setup(tmp_path);worker=engine.emby.media_info
        store.set_many({'sync_config':{'emby_media_info':True}});engine.emby.connection=lambda:('http://emby','test-key')
        rel='分享影视库/剧集/Example/E24.strm';path=engine.root/rel;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('http://test/play115/share/s1')
        with store.db:store.db.execute('INSERT INTO share_strms(id,share_code,file_id,name,strm_path,token_hash,created) VALUES(?,?,?,?,?,?,?)',('s1','code','fid','ep',rel,'h',0))
        starts=[]
        async def request(_http,method,url,**kwargs):
            start=kwargs['params']['StartIndex'];starts.append(start)
            items=[{'Id':f'dummy-{i}','Path':f'/unrelated/{i}'} for i in range(500)] if start==0 else [{'Id':'e24','Path':'/media/'+rel}]
            return httpx.Response(200,json={'Items':items,'TotalRecordCount':501},request=httpx.Request(method,url))
        monkeypatch.setattr('strimkeep.native_media_info.emby_request',request)
        assert await worker.sweep_missing()==1 and starts==[0,500]
        assert store.db.execute('SELECT item_id FROM native_media_info_queue').fetchone()['item_id']=='e24'
        with store.db:store.db.execute('DELETE FROM native_media_info_queue')
        worker.last_sweep=0
        async def truncated(_http,method,url,**kwargs):
            start=kwargs['params']['StartIndex']
            items=[{'Id':f'dummy-{i}','Path':f'/unrelated/{i}'} for i in range(500)] if start==0 else []
            return httpx.Response(200,json={'Items':items,'TotalRecordCount':501},request=httpx.Request(method,url))
        monkeypatch.setattr('strimkeep.native_media_info.emby_request',truncated)
        import pytest
        with pytest.raises(ValueError,match='截断'):await worker.sweep_missing()
        assert store.db.execute('SELECT count(*) FROM native_media_info_queue').fetchone()[0]==0
        await http.aclose();store.close()
    asyncio.run(run())

def test_existing_sidecar_does_not_mask_missing_emby_streams(tmp_path,monkeypatch):
    async def run():
        store,cloud,http,engine,_=setup(tmp_path);worker=engine.emby.media_info
        store.set_many({'sync_config':{'emby_media_info':True}});engine.emby.connection=lambda:('http://emby','test-key')
        rel='分享影视库/example.strm';path=engine.root/rel;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('http://test/play115/share/s1')
        sidecar=path.with_name('example-media-info.json');sidecar.write_text('user-owned')
        with store.db:
            store.db.execute('INSERT INTO share_strms(id,share_code,file_id,name,strm_path,token_hash,created) VALUES(?,?,?,?,?,?,?)',('s1','code','fid','ep',rel,'h',0))
            store.db.execute("INSERT INTO native_media_info_queue(job_id,item_id,target,connection,state) VALUES(0,'e1','{}','old','done')")
        async def request(_http,method,url,**kwargs):
            return httpx.Response(200,json={'Items':[{'Id':'e1','Path':'/media/'+rel,'MediaStreams':[]}]},request=httpx.Request(method,url))
        monkeypatch.setattr('strimkeep.native_media_info.emby_request',request)
        assert await worker.sweep_missing()==1
        row=store.db.execute("SELECT state,attempts FROM native_media_info_queue WHERE item_id='e1'").fetchone()
        assert row['state']=='queued' and row['attempts']==0 and sidecar.read_text()=='user-owned'
        await http.aclose();store.close()
    asyncio.run(run())


def test_user_owned_sidecar_does_not_block_emby_native_probe(tmp_path,monkeypatch):
    async def run():
        store,cloud,http,engine,_=setup(tmp_path);worker=engine.emby.media_info
        from strimkeep.share_service import install
        install(store)
        store.set_many({'sync_config':{'emby_media_info':True}});engine.emby.connection=lambda:('http://emby','test-key')
        rel='分享影视库/example.strm';path=engine.root/rel;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('http://test/play115/share/s1')
        sidecar=path.with_name('example-media-info.json');sidecar.write_text('user-owned')
        media={'probe_revision':1,'codec':'hevc','width':1920,'audio':[]}
        with store.db:store.db.execute('INSERT INTO share_strms(id,share_code,file_id,name,strm_path,token_hash,created,media) VALUES(?,?,?,?,?,?,?,?)',('s1','code','fid','ep',rel,'h',0,json.dumps(media)))
        target={'relative':rel,'pid':'share/s1'};worker.enqueue(1,{'Id':'e1'},target,digest('http://emby|test-key'))
        calls=[]
        async def request(_http,method,url,**kwargs):
            calls.append(url);video=[{'Type':'Video','Codec':'hevc'}]
            if url.endswith('/PlaybackInfo'):data={'MediaSources':[{'MediaStreams':video}]}
            else:data={'Items':[{'Id':'e1','Path':'/media/'+rel,'MediaStreams':video if len(calls)>1 else []}]}
            return httpx.Response(200,json=data,request=httpx.Request(method,url))
        monkeypatch.setattr('strimkeep.native_media_info.emby_request',request)
        await worker.tick()
        assert store.db.execute('SELECT state FROM native_media_info_queue').fetchone()[0]=='done'
        # 本地探测即完成；用户自己的 sidecar 保留不动，也不再反复重试。
        assert len(calls)==1 and sidecar.read_text()=='user-owned'
        await http.aclose();store.close()
    asyncio.run(run())


def test_emby_matches_named_share_playback_identity():
    from strimkeep.emby import EmbyDelivery
    target = {'relative': 'share/example.strm', 'pid': 'share/s1'}
    for path in ('http://test/play115/share/s1', 'http://test/play115/share/s1/%E7%AC%AC1%E9%9B%86.mkv'):
        assert EmbyDelivery.matches({'Path': path}, target)
    assert not EmbyDelivery.matches({'Path': 'http://test/play115/share/s2/example.mkv'}, target)


def test_sweep_enqueues_when_emby_has_duplicate_entries_for_one_strm(tmp_path,monkeypatch):
    """同一播放入口在 Emby 有两个条目时也要补扫：按路径收敛，仍重复就稳定选一个。"""
    async def run():
        store,cloud,http,engine,_=setup(tmp_path);worker=engine.emby.media_info
        store.set_many({'sync_config':{'emby_media_info':True}});engine.emby.connection=lambda:('http://emby','test-key')
        rel='分享影视库/剧集/Example/E24.strm';path=engine.root/rel;path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text('http://test/play115/share/s1')
        with store.db:
            store.db.execute('INSERT INTO share_strms(id,share_code,file_id,name,strm_path,token_hash,created) VALUES(?,?,?,?,?,?,?)',('s1','code','fid','ep',rel,'h',0))
        play='http://test/play115/share/s1/%E7%AC%AC1%E9%9B%86.mkv'
        async def request(_http,method,url,**kwargs):
            items=[{'Id':'dup-b','Path':'/media/'+rel,'MediaSources':[{'Path':play}]},
                   {'Id':'dup-a','Path':'/media/'+rel,'MediaSources':[{'Path':play}]}]
            return httpx.Response(200,json={'Items':items,'TotalRecordCount':2},request=httpx.Request(method,url))
        monkeypatch.setattr('strimkeep.native_media_info.emby_request',request)
        assert await worker.sweep_missing()==1
        assert store.db.execute('SELECT item_id FROM native_media_info_queue').fetchone()['item_id']=='dup-a'
        await http.aclose();store.close()
    asyncio.run(run())


def test_unlink_recorded_also_removes_its_media_info_sidecar(tmp_path):
    """STRM 被洗版替换/清理时，它自己的媒体信息 JSON 不能留下来当孤儿。"""
    store,cloud,http,engine,_=setup(tmp_path)
    rel='媒体库/电影/x/x.strm';path=engine.root/rel;path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text('http://x')
    sidecar=path.with_name('x-media-info.json');sidecar.write_text('{}')
    engine.outputs.unlink_recorded(1,rel,path)
    assert not path.exists() and not sidecar.exists()
    store.close()


def test_purge_orphan_sidecars_only_trusts_sidecar_contents(tmp_path):
    """0.9.54：归属只看 sidecar 内容（version+playback_id），登记表已退场。

    带自家字段、STRM 已不在 → 清掉；没字段（判不出归属）→ 保留；
    STRM 还在 → 保留；别人结构的 JSON → 保留。
    """
    store,cloud,http,engine,_=setup(tmp_path);worker=engine.emby.media_info
    keep=engine.root/'媒体库/a.strm';keep.parent.mkdir(parents=True,exist_ok=True);keep.write_text('u')
    keep_side=keep.with_name('a-media-info.json');keep_side.write_text('{}')
    opaque=engine.root/'媒体库/b-media-info.json';opaque.write_text('{}')
    unregistered=engine.root/'媒体库/c-media-info.json'
    unregistered.write_text(json.dumps({'version':1,'playback_id':'share/x'}),encoding='utf-8')
    foreign=engine.root/'媒体库/d-media-info.json'
    foreign.write_text(json.dumps([{'MediaSourceInfo':{}}]),encoding='utf-8')
    assert worker.purge_orphan_sidecars()==1
    assert keep_side.is_file() and opaque.is_file()
    assert not unregistered.exists() and foreign.is_file()
    store.close()


def test_sidecar_is_written_for_both_libraries(tmp_path):
    """网盘库和分享库各存一份媒体信息：一边有不能代替另一边（后续双库去重要用）。"""
    store,cloud,http,engine,_=setup(tmp_path);worker=engine.emby.media_info
    from strimkeep.share_service import install
    install(store)
    media={'probe_revision':3,'codec':'hevc','width':3840,'height':2160,'duration':'2739.072',
           'audio':[{'codec':'aac','channels':2}]}
    engine.index.account = lambda _: 'account'
    (engine.root/'媒体库/剧集/X').mkdir(parents=True,exist_ok=True)
    (engine.root/'媒体库/剧集/X'/'E01.strm').write_text('http://x/play115/pick/f1/sha/1/E01.mkv',encoding='utf-8')
    (engine.root/'分享影视库/剧集/X').mkdir(parents=True,exist_ok=True)
    (engine.root/'分享影视库/剧集/X'/'E01.strm').write_text('http://x/play115/share/s1/E01.mkv',encoding='utf-8')
    with store.db:
        store.db.execute("INSERT INTO playbacks(id,file_id,pickcode,name,relative_path,token_hash,revoked,created) VALUES(?,?,?,?,?,?,0,?)",('pid1','f1','pick','E01','媒体库/剧集/X/E01.strm','h',0))
        store.db.execute("INSERT INTO share_strms(id,share_code,file_id,name,strm_path,token_hash,created,media) VALUES(?,?,?,?,?,?,?,?)",('s1','code','fid','E01','分享影视库/剧集/X/E01.strm','h',0,json.dumps(media)))
        store.db.execute("INSERT OR REPLACE INTO organized_files(file_id,job_id,item_id,playback_id,relative_path,metadata) VALUES(?,?,?,?,?,?)",('f1',1,1,'pid1','媒体库/剧集/X/E01.strm',json.dumps({'media':media})))
        store.db.execute("INSERT INTO output_registry(account_id,file_id,source,trail,playback_id,token_cipher,relative_path,files,active,updated) VALUES(?,?,?,?,?,?,?,?,?,?)",
                         ('account','f1',json.dumps({'id':'f1','size':2194646809}),'[]','pid1','x','媒体库/剧集/X/E01.strm','{}',1,0))
        store.db.execute("UPDATE share_strms SET size=2194646809 WHERE id='s1'")
    for rel,pid in (('媒体库/剧集/X/E01.strm','pid1'),('分享影视库/剧集/X/E01.strm','share/s1')):
        target={'relative':rel,'pid':pid}
        probed=worker.probe_item(target,{'Path':'/media/'+rel})
        assert probed, rel
        worker.write_sidecar(target,probed,source_name='ffprobe')
        sidecar=(engine.root/rel).with_name('E01-media-info.json')
        data = json.loads(sidecar.read_text(encoding='utf-8'))
        assert sidecar.is_file() and data['source']=='ffprobe'
        assert data['size']==2194646809 and data['duration_ticks']==27390720000
    store.close()


def test_probe_emby_backs_off_and_gives_up_without_touching_local_state(tmp_path, monkeypatch):
    """Emby 迟迟解析不出视频流：10 分钟起翻倍退避，次数用完就认输，本地仍是 done。"""
    async def run():
        store, cloud, http, engine, _ = setup(tmp_path)
        from strimkeep.share_service import install
        install(store)
        store.set_many({'sync_config': {'emby_media_info': True}})
        worker = engine.emby.media_info
        engine.emby.connection = lambda: ('http://emby', 'test-key')
        rel = '分享影视库/剧集/Example/Season 1/E01.strm'
        path = engine.root / rel; path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('http://test/play115/share/s1')
        media = {'probe_revision': 1, 'codec': 'hevc', 'width': 3840}
        with store.db:
            store.db.execute('INSERT INTO share_strms(id,share_code,file_id,name,strm_path,token_hash,created,media) VALUES(?,?,?,?,?,?,?,?)',
                             ('s1','code','fid','ep',rel,'hash',0,json.dumps(media)))
        worker.enqueue(1, {'Id': 'e1'}, {'relative': rel, 'pid': 'share/s1'},
                       digest('http://emby|test-key'))
        async def request(_http, method, url, **kwargs):
            if url.endswith('/PlaybackInfo'):
                return httpx.Response(200, json={'MediaSources': []},
                                      request=httpx.Request(method, url))
            return httpx.Response(200, json={'Items': [{'Id': 'e1', 'Path': '/media/' + rel,
                                                        'MediaStreams': []}]},
                                  request=httpx.Request(method, url))
        monkeypatch.setattr('strimkeep.native_media_info.emby_request', request)
        await worker.tick()
        assert path.with_name('E01-media-info.json').is_file()
        for _ in range(worker.EMBY_MAX_ATTEMPTS):
            with store.db: store.db.execute('UPDATE native_media_info_queue SET emby_next_due=0')
            await worker.probe_emby()
        row = store.db.execute('SELECT state,emby_state,emby_attempts FROM native_media_info_queue').fetchone()
        assert row['state'] == 'done'                      # 本地媒体信息不受影响
        assert row['emby_state'] == 'gave_up'
        assert row['emby_attempts'] == worker.EMBY_MAX_ATTEMPTS
        with store.db: store.db.execute('UPDATE native_media_info_queue SET emby_next_due=0')
        assert await worker.probe_emby() == 0              # 认输之后不再骚扰 Emby
        await http.aclose(); store.close()
    asyncio.run(run())


def test_sweep_never_resets_emby_backoff_and_confirms_when_ready(tmp_path, monkeypatch):
    """补扫只管「Emby 有流就标已确认、没记账行就补一条」；退避归 probe_emby，别去重置。"""
    async def run():
        store, cloud, http, engine, _ = setup(tmp_path)
        from strimkeep.share_service import install
        install(store)
        store.set_many({'sync_config': {'emby_media_info': True}})
        worker = engine.emby.media_info
        engine.emby.connection = lambda: ('http://emby', 'test-key')
        rel = '分享影视库/剧集/Example/Season 1/E01.strm'
        path = engine.root / rel; path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('http://test/play115/share/s1')
        media = {'probe_revision': 1, 'codec': 'hevc', 'width': 3840}
        with store.db:
            store.db.execute('INSERT INTO share_strms(id,share_code,file_id,name,strm_path,token_hash,created,media) VALUES(?,?,?,?,?,?,?,?)',
                             ('s1','code','fid','ep',rel,'hash',0,json.dumps(media)))
        worker.enqueue(1, {'Id': 'e1'}, {'relative': rel, 'pid': 'share/s1'},
                       digest('http://emby|test-key'))
        streams = {'value': []}
        async def request(_http, method, url, **kwargs):
            return httpx.Response(200, json={'Items': [{'Id': 'e1', 'Path': '/media/' + rel,
                                                        'MediaStreams': streams['value']}]},
                                  request=httpx.Request(method, url))
        monkeypatch.setattr('strimkeep.native_media_info.emby_request', request)
        await worker.tick()
        assert path.with_name('E01-media-info.json').is_file()
        # probe_emby 已经推过一次、没成：退避排期在案。
        assert await worker.probe_emby() == 1
        row = store.db.execute('SELECT emby_state,emby_next_due FROM native_media_info_queue').fetchone()
        assert row['emby_state'] == 'pending' and row['emby_next_due'] > time.time()
        scheduled = row['emby_next_due']
        worker.last_sweep = 0
        assert await worker.sweep_missing() == 0                        # 不新增队列行
        assert store.db.execute('SELECT count(*) FROM native_media_info_queue').fetchone()[0] == 1
        row = store.db.execute('SELECT emby_next_due FROM native_media_info_queue').fetchone()
        assert row['emby_next_due'] == scheduled                        # 补扫不重置退避
        # Emby 后来自己探出来了（或我们踢成功了）：补扫顺手把存量行标成已确认。
        streams['value'] = [{'Type': 'Video', 'Codec': 'hevc'}]
        worker.last_sweep = 0
        assert await worker.sweep_missing() == 0
        assert store.db.execute('SELECT emby_state FROM native_media_info_queue').fetchone()['emby_state'] == 'confirmed'
        assert store.db.execute('SELECT count(*) FROM native_media_info_queue').fetchone()[0] == 1
        await http.aclose(); store.close()
    asyncio.run(run())


def test_report_announces_the_emby_gap_once_per_window(tmp_path):
    """缺口要定期说出来：0.9.48 那次真正的代价是「静默了 24 小时」。"""
    async def run():
        store, cloud, http, engine, _ = setup(tmp_path)
        from strimkeep.share_service import install
        install(store)
        store.set_many({'sync_config': {'emby_media_info': True}})
        worker = engine.emby.media_info
        rel = '分享影视库/剧集/Example/Season 1/E01.strm'
        path = engine.root / rel; path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('http://test/play115/share/s1')
        media = {'probe_revision': 1, 'codec': 'hevc', 'width': 1920}
        with store.db:
            store.db.execute('INSERT INTO share_strms(id,share_code,file_id,name,strm_path,token_hash,created,media) VALUES(?,?,?,?,?,?,?,?)',
                             ('s1','code','fid','ep',rel,'hash',0,json.dumps(media)))
        worker.enqueue(1, {'Id': 'e1'}, {'relative': rel, 'pid': 'share/s1'},
                       digest('http://emby|test-key'))
        with store.db:
            store.db.execute("UPDATE native_media_info_queue SET state='done'")
        now = time.time()
        assert worker.report(now=now) == (1, 0)
        messages = [row['message'] for row in store.db.execute(
            "SELECT message FROM events WHERE kind='emby'")]
        assert any('媒体信息核对' in m and '待补齐' in m for m in messages)
        assert worker.report(now=now + 60) is None                 # 窗口内不重复报
        assert worker.report(now=now + worker.REPORT_SECONDS + 1) == (1, 0)
        with store.db:
            store.db.execute("UPDATE native_media_info_queue SET emby_state='confirmed'")
        worker.last_report = 0
        assert worker.report(now=now) is None                       # 没有缺口就安静
        await http.aclose(); store.close()
    asyncio.run(run())

def test_emby_media_info_retry_logs_only_first_and_give_up(tmp_path):
    """8 次退避重试不该刷 8 条同样的日志：保留第一次和放弃那一次。"""
    async def run():
        store, cloud, http, engine, _ = setup(tmp_path)
        worker = engine.emby.media_info
        conn = digest('http://emby|test-key')
        with store.db:
            store.db.execute('INSERT INTO native_media_info_queue(job_id,item_id,target,connection) '
                             'VALUES(?,?,?,?)', (7, 'i1', json.dumps({'relative': 'a/a.strm'}), conn))
        for _ in range(3):
            row = store.db.execute('SELECT * FROM native_media_info_queue').fetchone()
            worker._defer_emby(dict(row), 'Emby 还没扫描到这条 STRM')
        messages = [r[0] for r in store.db.execute(
            "SELECT message FROM events WHERE message LIKE '%Emby 端媒体信息未取得%'").fetchall()]
        assert len(messages) == 1 and '第 1/' in messages[0], messages
        await http.aclose()
        store.close()
    asyncio.run(run())
