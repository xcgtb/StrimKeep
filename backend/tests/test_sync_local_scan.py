"""同步的「台账」就是磁盘本身：判定看清单 + 本地文件，不看任何记录表。"""
import asyncio
import json
import os
from pathlib import Path

from test_organize import new_job, setup
from test_phase5 import sync_config


def local_strms(engine):
    return list(engine.root.rglob('*.strm'))


def test_scope_accepts_only_explicit_cloud_root_for_output_root(tmp_path):
    store, cloud, http, engine, _ = setup(tmp_path)
    config = {**sync_config(roots=['0']),
              'sync_directory_stats': {'0': {'local_path': str(engine.root)}}}
    jid = new_job(store, config)
    assert engine.sync.scan_prefixes(jid, config) == {'.'}
    other = {**config, 'source_cids': ['100'],
             'sync_directory_stats': {'100': {'local_path': str(engine.root)}}}
    assert engine.sync.scan_prefixes(jid, other) == set()
    store.close()


def test_cleanup_deletes_strm_whose_cloud_source_is_gone(tmp_path):
    async def run():
        store, cloud, http, engine, _ = setup(tmp_path)
        video = cloud.add_video(name='Movie.2024.mkv')
        video['sha1'] = 'A' * 40
        await engine.run_job(new_job(store, {**sync_config(), 'notify_emby': False}))
        target = local_strms(engine)[0]
        cloud.nodes.pop(video['id'])
        jid = new_job(store, {**sync_config(), 'cleanup_missing': True, 'notify_emby': False})
        await engine.run_job(jid)
        assert engine.job(jid)['state'] == 'completed', dict(engine.job(jid))
        assert not target.exists(), '云端已删除的文件，本地 STRM 应被清理'
        await http.aclose(); store.close()
    asyncio.run(run())


def test_stale_detection_only_marks_without_deleting(tmp_path):
    async def run():
        store, cloud, http, engine, _ = setup(tmp_path)
        video = cloud.add_video(name='Movie.2024.mkv')
        video['sha1'] = 'A' * 40
        await engine.run_job(new_job(store, {**sync_config(), 'notify_emby': False}))
        target = local_strms(engine)[0]
        cloud.nodes.pop(video['id'])
        jid = new_job(store, {**sync_config(), 'cleanup_missing': False, 'notify_emby': False})
        await engine.run_job(jid)
        await engine.sync.stale_check(jid, {**sync_config(), 'stale_detect': True})
        assert target.exists(), '只标记不删除：stale_check 不应动文件'
        stale = json.loads(store.get('stale_strms') or '[]')
        assert any(item['file_id'] == video['id'] for item in stale)
        await http.aclose(); store.close()
    asyncio.run(run())


def test_cleanup_keeps_files_it_cannot_identify(tmp_path):
    async def init(tmp_path):
        store, cloud, http, engine, _ = setup(tmp_path)
        video = cloud.add_video(name='Movie.2024.mkv')
        video['sha1'] = 'A' * 40
        await engine.run_job(new_job(store, {**sync_config(), 'notify_emby': False}))
        return store, cloud, http, engine, video

    async def run():
        store, cloud, http, engine, video = await init(tmp_path)
        target = local_strms(engine)[0]
        target.write_text('这份不是播放地址', encoding='utf-8')
        cloud.nodes.pop(video['id'])
        jid = new_job(store, {**sync_config(), 'cleanup_missing': True, 'notify_emby': False})
        await engine.run_job(jid)
        assert target.exists(), '认不出身份的本地文件不删'
        await http.aclose(); store.close()
    asyncio.run(run())


def test_second_run_keeps_files_untouched(tmp_path):
    async def run():
        store, cloud, http, engine, _ = setup(tmp_path)
        cloud.add_video(name='Movie.2024.mkv')['sha1'] = 'A' * 40
        await engine.run_job(new_job(store, {**sync_config(), 'notify_emby': False}))
        target = local_strms(engine)[0]
        stamp = os.stat(target).st_mtime_ns
        await engine.run_job(new_job(store, {**sync_config(), 'notify_emby': False}))
        assert local_strms(engine) and os.stat(target).st_mtime_ns == stamp, '内容没变就不该重写文件'
        await http.aclose(); store.close()
    asyncio.run(run())


def test_missing_strm_is_regenerated_even_with_existing_evidence(tmp_path):
    async def run():
        store, cloud, http, engine, _ = setup(tmp_path)
        cloud.add_video(name='Movie.2024.mkv')['sha1'] = 'A' * 40
        config = {**sync_config(), 'notify_emby': False}
        await engine.run_job(new_job(store, config))
        target = local_strms(engine)[0]
        assert engine.outputs.get('1', '1000')
        target.unlink()
        jid = new_job(store, {**config, 'full_scan': False})
        await engine.run_job(jid)
        assert target.is_file()
        assert engine.job(jid)['state'] == 'completed', dict(engine.job(jid))
        await http.aclose(); store.close()
    asyncio.run(run())


def test_sibling_source_does_not_authorize_cleanup_or_stale_detection(tmp_path):
    async def run():
        store,cloud,http,engine,_=setup(tmp_path)
        parent=(await cloud.mkdir('0','库'))['cid']
        a=(await cloud.mkdir(parent,'目录A'))['cid']
        b=(await cloud.mkdir(parent,'目录B'))['cid']
        cloud.add_video('1001','A.mkv',a)['sha1']='A'*40
        cloud.add_video('1002','B.mkv',b)['sha1']='B'*40
        for cid in (a,b):
            jid=new_job(store,{**sync_config([cid]),'notify_emby':False});await engine.run_job(jid)
            assert engine.job(jid)['state']=='completed',dict(engine.job(jid))
        outside=engine.root/'库'/'目录B'/'B.strm'
        assert outside.is_file()
        del cloud.nodes['1002']
        jid=new_job(store,{**sync_config([a]),'notify_emby':False,'stale_detect':True})
        await engine.run_job(jid)
        assert outside.is_file(), '同步目录 A 不得清理目录 B'
        stale=json.loads(store.get('stale_strms') or '[]')
        assert all(item.get('file_id')!='1002' for item in stale)
        await http.aclose();store.close()
    asyncio.run(run())


def test_sync_path_keeps_actual_components_even_with_library_alias(tmp_path):
    store,cloud,http,engine,_=setup(tmp_path)
    folder='片库' * 30
    source={'name':'影片.mkv','_root':'200','_path':[{'id':'0','name':'115 网盘'},
            {'id':'200','name':folder}, {'id':'300','name':'子目录'}]}
    actual=engine.sync.relative(source,{'archive_cid':'200','library_dir_name':'人为别名'})
    assert actual == str(Path(folder,'子目录','影片.strm'))
    asyncio.run(http.aclose());store.close()


def test_organize_guard_preserves_unrelated_or_unrecognized_local_strm(tmp_path):
    from strimkeep.core import UncertainState
    from strimkeep.playaddr import play115_url
    from test_organize import setup
    import asyncio
    import pytest
    store,cloud,http,engine,_=setup(tmp_path)
    target=engine.root/'目录'/'影片.strm';target.parent.mkdir(parents=True)
    target.write_text('用户自己的内容',encoding='utf-8')
    with pytest.raises(UncertainState):engine.outputs.guard('1','1001','目录/影片.strm')
    assert target.read_text(encoding='utf-8')=='用户自己的内容'
    target.write_text(play115_url('http://localhost','pc','1002','A'*40,100,'影片.mkv'),encoding='utf-8')
    with pytest.raises(UncertainState):engine.outputs.guard('1','1001','目录/影片.strm')
    target.write_text(play115_url('http://localhost','pc','1001','A'*40,100,'影片.mkv'),encoding='utf-8')
    engine.outputs.guard('1','1001','目录/影片.strm')
    asyncio.run(http.aclose());store.close()


def test_sync_recognizes_existing_legacy_playback_for_same_file(tmp_path):
    from strimkeep.store import digest
    async def run():
        store,cloud,http,engine,_=setup(tmp_path)
        video=cloud.add_video('1001','影片.mkv');video['sha1']='A'*40
        target=engine.root/'待整理'/'影片.strm';target.parent.mkdir(parents=True)
        target.write_text('http://localhost/play/legacy-token/影片.mkv',encoding='utf-8')
        with store.db:
            store.db.execute("insert into playbacks(id,file_id,pickcode,name,relative_path,token_hash,revoked,created) values(?,?,?,?,?,?,0,0)",
                             ('legacy-token','1001','pc1001','影片.mkv','待整理/影片.strm',digest('')))
        config={**sync_config(['100']),'mode':'full','notify_emby':False,'cleanup_missing':False}
        jid=new_job(store,config);await engine.run_job(jid)
        assert engine.job(jid)['state']=='completed',dict(engine.job(jid))
        assert '/play115/' in target.read_text(encoding='utf-8')
        await http.aclose();store.close()
    asyncio.run(run())


def test_malformed_playback_identity_is_neither_reported_nor_deleted(tmp_path):
    async def run():
        store,cloud,http,engine,_=setup(tmp_path)
        target=engine.root/'待整理'/'custom.strm';target.parent.mkdir(parents=True)
        target.write_text('http://play/play115/pc/123/invalid',encoding='utf-8')
        config={**sync_config(['100']),'notify_emby':False,'stale_detect':True}
        jid=new_job(store,config);await engine.run_job(jid)
        assert target.is_file()
        assert all(item.get('file_id')!='123' for item in json.loads(store.get('stale_strms') or '[]'))
        await http.aclose();store.close()
    asyncio.run(run())
