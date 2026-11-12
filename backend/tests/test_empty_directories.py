import asyncio
import json
import time

import pytest

from strimkeep.cloud115 import CloudError
from test_organize import CONFIG, new_job
from test_upgrade import upgraded_setup, policy


async def fixture(tmp_path):
    store, cloud, http, engine, requests, media = upgraded_setup(tmp_path)
    outer = (await cloud.mkdir('100', '待整理作品'))['cid']
    inner = (await cloud.mkdir(outer, 'Season 1'))['cid']
    unrelated = (await cloud.mkdir('100', '与本任务无关的空目录'))['cid']
    cloud.add_video(parent=inner)
    cloud.before_empty_delete = None
    cloud.after_empty_delete = None
    async def recycle_empty(cid, parent):
        if cloud.before_empty_delete:
            callback, cloud.before_empty_delete = cloud.before_empty_delete, None
            callback(cid)
        assert cloud.nodes[cid]['parent_id'] == parent
        if await cloud.children(cid):
            raise CloudError('目录已出现内容，保留。', 'DIRECTORY_CHANGED', 409)
        node = cloud.nodes.pop(cid)
        cloud.writes.append(('recycle-dir',cid))
        cloud.bin[cid] = {'id':'','rid':'800'+cid,'name':node['name'],'size':0,
                          'parent_id':parent,'is_dir':True,'deleted_at':int(time.time())}
        if cloud.after_empty_delete:
            callback, cloud.after_empty_delete = cloud.after_empty_delete, None
            callback()
    cloud.recycle_empty_directory = recycle_empty
    return store,cloud,http,engine,outer,inner,unrelated


def test_nested_source_empty_directories_are_recycled_leaf_first_and_roots_preserved(tmp_path):
    async def scenario():
        store,cloud,http,engine,outer,inner,unrelated = await fixture(tmp_path)
        jid = new_job(store)
        await engine.run_job(jid)
        assert engine.job(jid)['state']=='completed', dict(engine.job(jid))
        assert [w for w in cloud.writes if w[0]=='recycle-dir']==[('recycle-dir',inner),('recycle-dir',outer)]
        assert all(cid in cloud.nodes for cid in ('100','200','300',unrelated))
        assert len(list((tmp_path/'strm').rglob('*.strm')))==1
        state=json.loads(engine.job(jid)['config'])['empty_cleanup']
        assert all(e['state']=='confirmed' for e in state['entries'])
        await http.aclose();store.close()
    asyncio.run(scenario())


def test_immediate_cleanup_missing_directory_is_not_reported_as_failed(tmp_path):
    async def scenario():
        store, cloud, http, engine, outer, inner, unrelated = await fixture(tmp_path)
        directory = cloud.directory

        async def unavailable_when_missing(cid):
            if str(cid) not in cloud.nodes:
                raise CloudError('目录不存在或暂时不可读取。', 'DIRECTORY_UNAVAILABLE', 409)
            return await directory(cid)

        async def live_file_exists(cid):
            return str(cid) in cloud.nodes

        cloud.directory = unavailable_when_missing
        cloud.live_file_exists = live_file_exists
        jid = new_job(store)
        await engine.run_job(jid)
        job = engine.job(jid)
        assert job['state'] == 'completed', dict(job)
        config = json.loads(job['config'])
        assert set(config['immediate_cleanup_ids']) == {inner, outer}
        assert config['empty_cleanup']['warning'] == ''
        assert unrelated in cloud.nodes
        await http.aclose(); store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('remaining', ['poster.jpg','tvshow.nfo','unbound.chs.srt','readme.txt'])
def test_nonvideo_files_are_content_and_prevent_directory_cleanup(tmp_path, remaining):
    async def scenario():
        store,cloud,http,engine,outer,inner,_ = await fixture(tmp_path)
        cloud.add_video('9999', remaining, inner)
        jid=new_job(store);await engine.run_job(jid)
        assert engine.job(jid)['state']=='completed',dict(engine.job(jid))
        if remaining in ('poster.jpg','tvshow.nfo','unbound.chs.srt'):
            # 扫描时确定未匹配的字幕与元数据，按用户设置回收。
            assert [w[0] for w in cloud.writes if w[0]=='recycle-dir']==['recycle-dir']*2
            assert inner not in cloud.nodes and outer not in cloud.nodes and '9999' not in cloud.nodes
        else:
            assert inner in cloud.nodes and outer in cloud.nodes and '9999' in cloud.nodes
            assert not any(w[0]=='recycle-dir' for w in cloud.writes)
        await http.aclose();store.close()
    asyncio.run(scenario())


def test_content_arriving_before_delete_is_preserved_and_no_recursive_delete(tmp_path):
    async def scenario():
        store,cloud,http,engine,outer,inner,_=await fixture(tmp_path)
        cloud.before_empty_delete=lambda cid:cloud.add_video('9999','important.txt',cid)
        jid=new_job(store);await engine.run_job(jid)
        assert engine.job(jid)['state']=='completed',dict(engine.job(jid))  # 单目录被跳过，不拖成 partial
        assert inner in cloud.nodes and outer in cloud.nodes and '9999' in cloud.nodes
        assert not any(w[0]=='recycle-dir' for w in cloud.writes)
        # Persisted intent is never blindly replayed.
        writes=cloud.writes.copy();await engine.run_job(jid)
        assert cloud.writes==writes
        await http.aclose();store.close()
    asyncio.run(scenario())


def test_pause_and_external_bin_purge_do_not_repeat_confirmed_directory_recycle(tmp_path):
    async def scenario():
        store,cloud,http,engine,outer,inner,_=await fixture(tmp_path)
        jid=new_job(store)
        cloud.after_empty_delete=lambda:engine.update_job(jid,state='paused')
        await engine.run_job(jid)
        assert engine.job(jid)['state']=='paused'
        assert inner not in cloud.nodes and outer in cloud.nodes
        cloud.bin.clear()
        await engine.run_job(jid)
        assert engine.job(jid)['state']=='completed',dict(engine.job(jid))
        assert [w for w in cloud.writes if w[0]=='recycle-dir']==[('recycle-dir',inner)]
        assert outer in cloud.nodes  # 一轮一层：外层等下一轮
        await http.aclose();store.close()
    asyncio.run(scenario())


def test_archive_old_work_and_season_directories_removed_after_file_confirmation(tmp_path):
    async def scenario():
        store,cloud,http,engine,_,_,_ = await fixture(tmp_path)
        jid=new_job(store);await engine.run_job(jid)
        old_season=cloud.nodes['1000']['parent_id']
        old_show=cloud.nodes[old_season]['parent_id']
        region=cloud.nodes[old_show]['parent_id']
        cloud.nodes[old_show]['name'] += ' 旧目录'
        cloud.add_video('2000')['size']=2000
        jid=new_job(store,{**CONFIG,'upgrade_policy':policy('size')})
        await engine.run_job(jid)
        assert engine.job(jid)['state']=='completed',dict(engine.job(jid))
        assert old_season not in cloud.nodes and old_show not in cloud.nodes
        assert region in cloud.nodes and cloud.nodes['2000']['parent_id'] in cloud.nodes
        assert next(w for w in cloud.writes if w[:2]==('recycle','1000'))
        assert cloud.writes.index(('recycle','1000')) < cloud.writes.index(('recycle-dir',old_season))
        await http.aclose();store.close()
    asyncio.run(scenario())


def test_local_cleanup_uses_empty_rmdir_and_preserves_plugin_files(tmp_path):
    async def scenario():
        store,cloud,http,engine,_,_,_=await fixture(tmp_path)
        base=engine.root/'影视媒体库/剧集/欧美剧集'
        empty=base/'旧作品/Season 1';empty.mkdir(parents=True)
        protected=base/'有海报的作品/Season 1';protected.mkdir(parents=True)
        poster=protected.parent/'poster.jpg';poster.write_bytes(b'plugin')
        rows=[{'state':'done','plan':json.dumps({'wash':{'old':[{'local_files':{
            str(empty.relative_to(engine.root)/'old.strm'):'hash',
            str(protected.relative_to(engine.root)/'old.strm'):'hash'}}]}})}]
        engine.cleanup.local(rows)
        assert not empty.parent.exists()
        assert poster.read_bytes()==b'plugin' and base.is_dir()
        await http.aclose();store.close()
    asyncio.run(scenario())


def test_new_subtitle_after_scan_is_not_deleted(tmp_path):
    async def scenario():
        store,cloud,http,engine,outer,inner,_=await fixture(tmp_path)
        before=engine.build_plan
        async def add_later(*args,**kwargs):
            if '9999' not in cloud.nodes: cloud.add_video('9999','later.chs.srt',inner)
            return await before(*args,**kwargs)
        engine.build_plan=add_later
        jid=new_job(store);await engine.run_job(jid)
        assert '9999' in cloud.nodes and inner in cloud.nodes and outer in cloud.nodes
        await http.aclose();store.close()
    asyncio.run(scenario())


def test_unmatched_subtitle_with_skipped_iso_is_not_deleted(tmp_path):
    async def scenario():
        store,cloud,http,engine,outer,inner,_=await fixture(tmp_path)
        cloud.add_video('9998','other.iso',inner)
        cloud.add_video('9999','unbound.chs.srt',inner)
        jid=new_job(store);await engine.run_job(jid)
        assert '9998' in cloud.nodes and '9999' in cloud.nodes and inner in cloud.nodes
        await http.aclose();store.close()
    asyncio.run(scenario())


def test_unmatched_subtitle_lost_response_is_verified_without_repeat(tmp_path):
    async def scenario():
        store,cloud,http,engine,outer,inner,_=await fixture(tmp_path)
        cloud.add_video('9999','orphan.chs.srt',inner)
        recycle=cloud.recycle
        async def lost(fid,parent):
            await recycle(fid,parent)
            if fid=='9999':raise CloudError('响应丢失','CLOUD_UNAVAILABLE',503)
        cloud.recycle=lost
        jid=new_job(store);await engine.run_job(jid)
        assert '9999' not in cloud.nodes and '9999' in cloud.bin
        assert cloud.writes.count(('recycle','9999'))==1
        assert inner not in cloud.nodes and outer not in cloud.nodes
        await http.aclose();store.close()
    asyncio.run(scenario())


def test_changed_subtitle_is_never_recycled(tmp_path):
    async def scenario():
        store,cloud,http,engine,outer,inner,_=await fixture(tmp_path)
        cloud.add_video('9999','orphan.chs.srt',inner)
        file=cloud.file
        async def changed(fid):
            result=await file(fid)
            if fid=='9999':result['size']+=1
            return result
        cloud.file=changed
        jid=new_job(store);await engine.run_job(jid)
        assert '9999' in cloud.nodes and inner in cloud.nodes and outer in cloud.nodes
        assert ('recycle','9999') not in cloud.writes
        await http.aclose();store.close()
    asyncio.run(scenario())


def test_new_video_during_subtitle_cleanup_keeps_subtitle(tmp_path):
    async def scenario():
        store,cloud,http,engine,outer,inner,_=await fixture(tmp_path)
        cloud.add_video('9999','orphan.chs.srt',inner)
        original=cloud.recycle_files
        async def arrival():
            result=await original()
            if cloud.nodes.get('1000',{}).get('parent_id') != inner and '9998' not in cloud.nodes:
                cloud.add_video('9998','newly-arrived.iso',inner)
            return result
        cloud.recycle_files=arrival
        jid=new_job(store);await engine.run_job(jid)
        assert '9998' in cloud.nodes and '9999' in cloud.nodes and inner in cloud.nodes
        assert ('recycle','9999') not in cloud.writes
        await http.aclose();store.close()
    asyncio.run(scenario())


def test_received_package_cleans_scanned_empty_siblings_without_touching_unrelated(tmp_path):
    async def scenario():
        store,cloud,http,engine,outer,inner,unrelated=await fixture(tmp_path)
        empty=(await cloud.mkdir(outer,'多余空目录'))['cid']
        config={**CONFIG,'source_entries':[dict(cloud.nodes[outer])]}
        jid=new_job(store,config);await engine.run_job(jid)
        assert engine.job(jid)['state']=='completed',dict(engine.job(jid))
        assert empty not in cloud.nodes and inner not in cloud.nodes and outer not in cloud.nodes
        assert unrelated in cloud.nodes and '100' in cloud.nodes
        await http.aclose();store.close()
    asyncio.run(scenario())


def test_orphan_subtitle_unconfirmed_retries_readback_not_recycle(tmp_path):
    async def scenario():
        store,cloud,http,engine,outer,inner,_=await fixture(tmp_path)
        cloud.add_video('9999','orphan.chs.srt',inner)
        cloud.after_recycle=lambda:setattr(cloud,'fail_listing',True)
        jid=new_job(store);await engine.run_job(jid)
        assert engine.job(jid)['state']=='partial'
        state=json.loads(engine.job(jid)['config'])['empty_cleanup']
        actions=[a for e in state['entries'] for a in e.get('subtitle_actions',[])]
        assert len(actions)==1 and actions[0]['state']=='intent'
        assert cloud.writes.count(('recycle','9999'))==1
        cloud.fail_listing=False;engine.update_job(jid,state='running')
        await engine.cleanup.run(jid,json.loads(engine.job(jid)['config']))
        assert cloud.writes.count(('recycle','9999'))==1
        assert inner not in cloud.nodes and outer not in cloud.nodes
        await http.aclose();store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('name', ['later.nfo', 'poster.jpg'])
def test_metadata_added_after_scan_is_preserved(tmp_path, name):
    async def scenario():
        store, cloud, http, engine, outer, inner, _ = await fixture(tmp_path)
        build = engine.build_plan
        async def add_later(*args, **kwargs):
            if '9999' not in cloud.nodes:
                cloud.add_video('9999', name, inner)
            return await build(*args, **kwargs)
        engine.build_plan = add_later
        jid = new_job(store); await engine.run_job(jid)
        assert '9999' in cloud.nodes and inner in cloud.nodes
        assert ('recycle', '9999') not in cloud.writes
        await http.aclose(); store.close()
    asyncio.run(scenario())


def test_metadata_changed_same_size_after_scan_is_preserved(tmp_path):
    async def scenario():
        store, cloud, http, engine, outer, inner, _ = await fixture(tmp_path)
        cloud.add_video('9999', 'movie.nfo', inner)
        build = engine.build_plan
        async def change_later(*args, **kwargs):
            cloud.nodes['9999']['sha1'] = 'different-content-same-size'
            return await build(*args, **kwargs)
        engine.build_plan = change_later
        jid = new_job(store); await engine.run_job(jid)
        assert '9999' in cloud.nodes and inner in cloud.nodes
        assert ('recycle', '9999') not in cloud.writes
        await http.aclose(); store.close()
    asyncio.run(scenario())


def test_metadata_lost_response_is_confirmed_and_never_repeated(tmp_path):
    async def scenario():
        store, cloud, http, engine, outer, inner, _ = await fixture(tmp_path)
        cloud.add_video('9999', 'movie.nfo', inner)
        recycle = cloud.recycle
        async def lost(fid, parent):
            await recycle(fid, parent)
            if fid == '9999': raise CloudError('响应丢失', 'CLOUD_UNAVAILABLE', 503)
        cloud.recycle = lost
        jid = new_job(store); await engine.run_job(jid)
        assert '9999' not in cloud.nodes and '9999' in cloud.bin
        assert cloud.writes.count(('recycle', '9999')) == 1
        await http.aclose(); store.close()
    asyncio.run(scenario())


def test_metadata_retry_confirms_uncertain_intent_without_repeating(tmp_path):
    async def scenario():
        store, cloud, http, engine, outer, inner, _ = await fixture(tmp_path)
        cloud.add_video('9999', 'movie.nfo', inner)
        cloud.after_recycle = lambda: setattr(cloud, 'fail_listing', True)
        jid = new_job(store); await engine.run_job(jid)
        assert engine.job(jid)['state'] == 'partial'
        state = json.loads(engine.job(jid)['config'])['empty_cleanup']
        actions = [a for e in state['entries'] for a in e.get('metadata_actions', [])]
        assert len(actions) == 1 and actions[0]['state'] == 'intent'
        cloud.fail_listing = False
        engine.update_job(jid, state='running')
        result = await engine.cleanup.run(jid, json.loads(engine.job(jid)['config']))
        assert cloud.writes.count(('recycle', '9999')) == 1
        assert result['warning'] == ''
        await http.aclose(); store.close()
    asyncio.run(scenario())


def test_unreadable_candidate_has_retriable_marker_without_entries():
    from strimkeep.cleanup import EmptyDirectoryCleanup
    job = {'state': 'partial', 'config': json.dumps({'empty_cleanup': {'entries': [], 'warning': '读取失败', 'retry_candidates': True}})}
    assert EmptyDirectoryCleanup.retryable(job)
    old = {'state': 'partial', 'config': json.dumps({'empty_cleanup': {'entries': [], 'warning': '1 个目录读取失败或删除状态未能确认，请检查任务日志及目录。'}})}
    assert EmptyDirectoryCleanup.retryable(old)



def test_candidate_enumeration_failure_is_retried_without_duplicating_intents(tmp_path):
    async def scenario():
        store, cloud, http, engine, outer, inner, _ = await fixture(tmp_path)
        jid = new_job(store)
        attempts = []
        async def transient(job_id, config, rows):
            attempts.append(1)
            if len(attempts) == 1:
                return [], '1 个目录读取失败'
            return [], ''
        engine.cleanup.candidates = transient
        first = await engine.cleanup.run(jid, json.loads(engine.job(jid)['config']))
        assert first['warning'] and len(attempts) == 1
        engine.update_job(jid, state='partial')
        assert engine.cleanup.retryable(engine.job(jid))
        engine.update_job(jid, state='running')
        second = await engine.cleanup.run(jid, json.loads(engine.job(jid)['config']))
        assert second['warning'] == '' and len(attempts) == 2
        assert not json.loads(engine.job(jid)['config'])['empty_cleanup']['retry_candidates']
        await http.aclose(); store.close()
    asyncio.run(scenario())



def test_old_job_without_metadata_snapshot_preserves_metadata(tmp_path):
    async def scenario():
        store, cloud, http, engine, outer, inner, _ = await fixture(tmp_path)
        cloud.add_video('9999', 'movie.nfo', inner)
        cleanup = engine.cleanup.run
        async def old_manifest(job_id, config):
            saved = json.loads(engine.job(job_id)['config'])
            saved.pop('cleanup_metadata_snapshot', None)
            engine.update_job(job_id, config=json.dumps(saved))
            return await cleanup(job_id, saved)
        engine.cleanup.run = old_manifest
        jid = new_job(store); await engine.run_job(jid)
        assert '9999' in cloud.nodes and inner in cloud.nodes
        assert ('recycle', '9999') not in cloud.writes
        await http.aclose(); store.close()
    asyncio.run(scenario())


def test_metadata_arriving_during_subtitle_cleanup_protects_both(tmp_path):
    async def scenario():
        store, cloud, http, engine, outer, inner, _ = await fixture(tmp_path)
        cloud.add_video('9999', 'orphan.chs.srt', inner)
        before = cloud.recycle_files
        async def arrival():
            result = await before()
            if cloud.nodes.get('1000', {}).get('parent_id') != inner and '9998' not in cloud.nodes:
                cloud.add_video('9998', 'later.nfo', inner)
            return result
        cloud.recycle_files = arrival
        jid = new_job(store); await engine.run_job(jid)
        assert '9998' in cloud.nodes and '9999' in cloud.nodes and inner in cloud.nodes
        assert ('recycle', '9999') not in cloud.writes
        await http.aclose(); store.close()
    asyncio.run(scenario())
