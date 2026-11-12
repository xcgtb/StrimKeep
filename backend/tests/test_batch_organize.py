import asyncio
import pytest
from strimkeep.cloud115 import CloudError
from test_organize import CONFIG, setup, new_job


def test_prepare_measure_records_failed_calls_and_scoped_reads():
    from types import SimpleNamespace
    from strimkeep.organizer import _prepare_measure
    cloud = SimpleNamespace(ledger={'read': 3})
    metrics = {}
    with pytest.raises(ValueError):
        with _prepare_measure(metrics, '源文件身份核对', cloud):
            cloud.ledger['read'] += 2
            raise ValueError('failure')
    assert metrics['源文件身份核对']['calls'] == 1
    assert metrics['源文件身份核对']['reads'] == 2
    assert metrics['源文件身份核对']['seconds'] >= 0


def test_tmdb_diagnostics_are_isolated_between_concurrent_tasks():
    from strimkeep.organizer import _count_tmdb, _prepare_diagnostics
    async def record(count):
        stats = {}
        token = _prepare_diagnostics.set(stats)
        try:
            for _ in range(count):
                _count_tmdb('网络请求')
                await asyncio.sleep(0)
            return stats
        finally:
            _prepare_diagnostics.reset(token)
    async def scenario():
        left, right = await asyncio.gather(record(2), record(5))
        assert left['_tmdb']['网络请求'] == 2
        assert right['_tmdb']['网络请求'] == 5
        assert _prepare_diagnostics.get() is None
    asyncio.run(scenario())


@pytest.mark.parametrize('outcome', ['success', 'partial_response', 'deleted_after_move'])
def test_batch_move_reconciles_each_file(tmp_path, outcome):
    async def scenario():
        store, cloud, http, engine, _ = setup(tmp_path)
        for i in (1, 2):
            cloud.add_video(str(1000+i), f'测试剧集 (2024) S01E0{i}.WEB-DL.mkv')
        calls = []
        original_file = cloud.file
        async def file(fid):
            if fid not in cloud.nodes:
                raise CloudError('文件不存在', 'FILE_NOT_FOUND', 404)
            return await original_file(fid)
        cloud.file = file
        async def move_many(ids, dest):
            calls.append(list(ids))
            cloud.nodes[ids[0]]['parent_id'] = dest
            if outcome == 'partial_response':
                raise CloudError('响应丢失', 'CLOUD_UNAVAILABLE')
            if outcome == 'deleted_after_move':
                del cloud.nodes[ids[1]]
            else:
                cloud.nodes[ids[1]]['parent_id'] = dest
        cloud.move_many = move_many
        jid = new_job(store, {**CONFIG, 'notify_emby':False, 'ffprobe_enabled':False})
        await engine.run_job(jid)
        rows = store.db.execute('SELECT file_id,state FROM organize_items WHERE job_id=? ORDER BY file_id',(jid,)).fetchall()
        states = {r['file_id']:r['state'] for r in rows}
        assert calls == [['1001','1002']]
        assert states['1001'] == 'done'
        assert states['1002'] == ('skipped' if outcome == 'deleted_after_move' else 'done')
        assert len(list(engine.root.rglob('*.strm'))) == (1 if outcome == 'deleted_after_move' else 2)
        assert engine.job(jid)['state'] == 'completed', dict(engine.job(jid))
        await http.aclose(); store.close()
    asyncio.run(scenario())


def test_restart_after_partial_batch_does_not_replay_batch(tmp_path):
    async def scenario():
        store, cloud, http, engine, _ = setup(tmp_path)
        for i in (1, 2):
            cloud.add_video(str(1000+i), f'测试剧集 (2024) S01E0{i}.WEB-DL.mkv')
        calls=[]
        async def interrupted(ids, dest):
            calls.append(list(ids))
            cloud.nodes[ids[0]]['parent_id'] = dest
            raise asyncio.CancelledError()
        cloud.move_many=interrupted
        jid=new_job(store, {**CONFIG,'notify_emby':False,'ffprobe_enabled':False})
        with pytest.raises(asyncio.CancelledError):
            await engine.run_job(jid)
        plans=store.db.execute('SELECT state,plan FROM organize_items WHERE job_id=?',(jid,)).fetchall()
        assert len(plans)==2 and all(r['state']=='planned' for r in plans)
        engine.update_job(jid, state='running')
        await engine.run_job(jid)
        states=[r['state'] for r in store.db.execute('SELECT state FROM organize_items WHERE job_id=?',(jid,))]
        assert states == ['done','done']
        assert calls == [['1001','1002']]
        assert len(list(engine.root.rglob('*.strm')))==2
        await http.aclose();store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('rename_outcome', ['success', 'partial_response', 'partial_success', 'deleted_during_rename'])
def test_directory_batch_renames_and_reconciles_each_file(tmp_path, rename_outcome):
    async def scenario():
        store, cloud, http, engine, _ = setup(tmp_path)
        for i in range(1, 7):
            cloud.add_video(str(1000+i), f'测试剧集 (2024) S01E0{i}.WEB-DL.mkv')
        calls=[]; real_file=cloud.file
        async def file(fid):
            if fid not in cloud.nodes:raise CloudError('文件不存在','FILE_NOT_FOUND',404)
            return await real_file(fid)
        cloud.file=file
        async def rename_many(pairs):
            calls.append(('rename',len(pairs)))
            for idx,(fid,name) in enumerate(pairs):
                if idx == 1 and rename_outcome == 'partial_response':raise CloudError('响应丢失','CLOUD_UNAVAILABLE')
                if idx == 1 and rename_outcome == 'partial_success':return {'state': True}
                if idx == 1 and rename_outcome == 'deleted_during_rename':
                    del cloud.nodes[fid]
                    continue
                cloud.nodes[fid]['name']=name
        async def move_many(ids,dest):
            calls.append(('move',len(ids)))
            for fid in ids:cloud.nodes[fid]['parent_id']=dest
        cloud.rename_many=rename_many;cloud.move_many=move_many
        jid=new_job(store,{**CONFIG,'notify_emby':False,'ffprobe_enabled':False})
        await engine.run_job(jid)
        states=[r['state'] for r in store.db.execute('SELECT state FROM organize_items WHERE job_id=? ORDER BY id',(jid,))]
        assert calls[0]==('rename',6)
        assert store.db.execute("SELECT 1 FROM events WHERE message LIKE ?", (f'任务 {jid}：准备批量重命名 6 个文件（每批最多 50 个）。',)).fetchone()
        if rename_outcome == 'partial_success':
            assert not any(call[0] == 'move' for call in calls)
        assert states.count('done') == (5 if rename_outcome=='deleted_during_rename' else 6)
        assert states.count('skipped') == (1 if rename_outcome=='deleted_during_rename' else 0)
        assert engine.job(jid)['state']=='completed',dict(engine.job(jid))
        assert len(list(engine.root.rglob('*.strm'))) == states.count('done')
        await http.aclose();store.close()
    asyncio.run(scenario())

def test_mixed_movie_and_episodes_keep_separate_names_and_destinations(tmp_path):
    async def scenario():
        store, cloud, http, engine, _ = setup(tmp_path)
        cloud.add_video('1001', '测试剧集 (2024) S01E01.WEB-DL.mkv')
        cloud.add_video('1002', '测试剧集 (2024) S01E02.WEB-DL.mkv')
        cloud.add_video('2000', '测试电影 (2024).WEB-DL.mkv')
        batches = []
        async def rename_many(pairs):
            batches.append(list(pairs))
            for fid, name in pairs: cloud.nodes[fid]['name'] = name
        async def move_many(ids, dest):
            for fid in ids: cloud.nodes[fid]['parent_id'] = dest
        cloud.rename_many = rename_many
        cloud.move_many = move_many
        jid = new_job(store, {**CONFIG, 'notify_emby': False, 'ffprobe_enabled': False})
        await engine.run_job(jid)
        states = [r['state'] for r in store.db.execute('SELECT state FROM organize_items WHERE job_id=?', (jid,))]
        assert states.count('done') == 3, states
        assert cloud.nodes['1001']['parent_id'] == cloud.nodes['1002']['parent_id']
        assert cloud.nodes['1001']['parent_id'] != cloud.nodes['2000']['parent_id']
        assert 'S01E01' in cloud.nodes['1001']['name']
        assert 'S01E02' in cloud.nodes['1002']['name']
        assert '测试电影' in cloud.nodes['2000']['name']
        assert all('2000' not in [fid for fid, _ in batch] for batch in batches)
        await http.aclose(); store.close()
    asyncio.run(scenario())

def test_new_tv_batch_reuses_empty_archive_evidence_and_rechecks_before_move(tmp_path):
    async def scenario():
        store, cloud, http, engine, _ = setup(tmp_path)
        for i in range(1, 9): cloud.add_video(str(1000+i), f'测试剧集 (2024) S01E0{i}.WEB-DL.mkv')
        calls=[]; actual_refresh=engine.index.refresh_related
        async def traced(*args):
            calls.append(1)
            return await actual_refresh(*args)
        engine.index.refresh_related=traced
        async def rename_many(pairs):
            for fid,name in pairs:cloud.nodes[fid]['name']=name
        async def move_many(ids,dest):
            for fid in ids:cloud.nodes[fid]['parent_id']=dest
        cloud.rename_many=rename_many; cloud.move_many=move_many
        jid=new_job(store,{**CONFIG,'notify_emby':False,'ffprobe_enabled':False})
        await engine.run_job(jid)
        assert engine.job(jid)['state']=='completed',dict(engine.job(jid))
        assert store.db.execute("SELECT count(*) FROM organize_items WHERE job_id=? AND state='done'",(jid,)).fetchone()[0]==8
        assert len(calls)==1, calls
        assert store.db.execute("SELECT 1 FROM events WHERE message LIKE '%新作品批次复核目标目录完成（快速路径）%' ").fetchone()
        event = store.db.execute("SELECT message FROM events WHERE message LIKE '%准备明细%' ORDER BY id DESC LIMIT 1").fetchone()[0]
        assert '源文件身份核对 8次/' in event and '季集资料 8次/' in event
        assert '作品目录与资源刷新 1次/' in event and '洗版及已有集数判断 8次/' in event
        assert 'TMDB网络请求' in event and '通用缓存命中' in event
        await http.aclose();store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['new_target_item', 'renamed_target'])
def test_new_tv_batch_aborts_when_target_changes_before_move(tmp_path, change):
    async def scenario():
        store, cloud, http, engine, _ = setup(tmp_path)
        for i in range(1, 4): cloud.add_video(str(1000+i), f'测试剧集 (2024) S01E0{i}.WEB-DL.mkv')
        calls=[]; actual_recheck=engine._recheck_new_batch_target
        async def mutate_before_recheck(job,saved,moves,config):
            calls.append(1)
            season = next(n['id'] for n in cloud.nodes.values() if n.get('is_dir') and n['name']=='Season 1')
            if change == 'new_target_item':
                cloud.add_video('9999', '测试剧集 (2024) S01E01.WEB-DL.mkv', parent=season)
            else:
                cloud.nodes[season]['name'] = '外部改名'
            return await actual_recheck(job,saved,moves,config)
        engine._recheck_new_batch_target=mutate_before_recheck
        operations=[]
        async def rename_many(pairs):operations.append('rename')
        async def move_many(ids,dest):operations.append('move')
        cloud.rename_many=rename_many;cloud.move_many=move_many
        jid=new_job(store,{**CONFIG,'notify_emby':False,'ffprobe_enabled':False})
        await engine.run_job(jid)
        assert len(calls)==1 and not operations
        assert engine.job(jid)['state']=='needs_review',dict(engine.job(jid))
        assert all(cloud.nodes[str(1000+i)]['parent_id']=='100' for i in range(1,4))
        await http.aclose();store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('decision', ['replace', 'discard', 'skip'])
def test_new_batch_target_fast_path_rejects_wash_actions(tmp_path, decision):
    async def scenario():
        store,cloud,http,engine,_=setup(tmp_path)
        saved={'new_work':True,'plan':{'current_archive_ids':[], 'conflict_parent_ids':[]},'items':[1,2]}
        moves=[(1,{}, {'wash':{'decision':decision}, 'dest':'200','parts':['剧集']}),
               (2,{}, {'wash':{'decision':'archive'}, 'dest':'200','parts':['剧集']})]
        assert not await engine._recheck_new_batch_target(1,saved,moves,CONFIG)
        await http.aclose();store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('keep_existing_names', [False, True])
def test_existing_work_batch_reuses_archive_then_rechecks(tmp_path, keep_existing_names):
    async def scenario():
        store, cloud, http, engine, _ = setup(tmp_path)
        cloud.add_video('1001', '测试剧集 (2024) S01E01.WEB-DL.mkv')
        first = new_job(store, {**CONFIG, 'notify_emby': False, 'ffprobe_enabled': False})
        await engine.run_job(first)
        assert engine.job(first)['state'] == 'completed'
        for i in (2, 3, 4, 5, 6): cloud.add_video(str(1000+i), f'测试剧集 (2024) S01E0{i}.WEB-DL.mkv')
        calls=[]; real=engine.index.refresh_related
        async def traced(*args):
            calls.append(1)
            return await real(*args)
        engine.index.refresh_related=traced
        async def rename_many(pairs):
            for fid,name in pairs: cloud.nodes[fid]['name']=name
        async def move_many(ids,dest):
            for fid in ids: cloud.nodes[fid]['parent_id']=dest
        cloud.rename_many=rename_many;cloud.move_many=move_many
        jid=new_job(store,{**CONFIG,'notify_emby':False,'ffprobe_enabled':False,
                           'keep_existing_names':keep_existing_names})
        await engine.run_job(jid)
        assert engine.job(jid)['state']=='completed',dict(engine.job(jid))
        assert len(calls)==2
        assert store.db.execute('SELECT 1 FROM events WHERE message LIKE ?', (f'任务 {jid}：同一已有作品%归档核查已复用%',)).fetchone()
        assert store.db.execute("SELECT count(*) FROM organize_items WHERE job_id=? AND state='done'", (jid,)).fetchone()[0] == 5
        await http.aclose();store.close()
    asyncio.run(scenario())


def test_existing_work_reuse_keeps_custom_title_for_each_episode(tmp_path):
    async def scenario():
        store, cloud, http, engine, _ = setup(tmp_path)
        cloud.add_video('1001', '测试剧集 (2024) S01E01.WEB-DL.mkv')
        first = new_job(store, {**CONFIG, 'notify_emby':False, 'ffprobe_enabled':False})
        await engine.run_job(first)
        assert engine.job(first)['state'] == 'completed'
        cloud.nodes['1001']['name'] = '自定义剧名.2024.S01E01.WEB-DL.mkv'
        for i in (2,3,4): cloud.add_video(str(1000+i), f'测试剧集 (2024) S01E0{i}.WEB-DL.mkv')
        calls=[]; real=engine.index.refresh_related
        async def traced(*args):
            calls.append(1)
            return await real(*args)
        engine.index.refresh_related=traced
        async def rename_many(pairs):
            for fid,name in pairs: cloud.nodes[fid]['name']=name
        async def move_many(ids,dest):
            for fid in ids: cloud.nodes[fid]['parent_id']=dest
        cloud.rename_many=rename_many;cloud.move_many=move_many
        jid=new_job(store,{**CONFIG,'notify_emby':False,'ffprobe_enabled':False,'keep_existing_names':True})
        await engine.run_job(jid)
        assert engine.job(jid)['state']=='completed', dict(engine.job(jid))
        assert len(calls)==2
        assert all(cloud.nodes[str(1000+i)]['name'].startswith('自定义剧名.2024.S01E0'+str(i)) for i in (2,3,4))
        await http.aclose();store.close()
    asyncio.run(scenario())


def test_existing_work_different_title_shape_falls_back_to_fresh_check(tmp_path):
    async def scenario():
        store, cloud, http, engine, _ = setup(tmp_path)
        cloud.add_video('1001', '测试剧集 (2024) S01E01.WEB-DL.mkv')
        await engine.run_job(new_job(store, {**CONFIG, 'notify_emby':False, 'ffprobe_enabled':False}))
        for i in (2,3,4): cloud.add_video(str(1000+i), f'测试剧集 (2024) S01E0{i}.WEB-DL.mkv')
        real_plan=engine.build_plan
        async def varying(job, original, diagnostics=None):
            plan=await real_plan(job, original, diagnostics)
            if original['id']=='1003':
                plan['target_name']=plan['target_name'].replace('测试剧集', '另一名称', 1)
            return plan
        engine.build_plan=varying
        calls=[]; refresh=engine.index.refresh_related
        async def traced(*args):
            calls.append(1)
            return await refresh(*args)
        engine.index.refresh_related=traced
        async def rename_many(pairs):
            for fid,name in pairs:cloud.nodes[fid]['name']=name
        async def move_many(ids,dest):
            for fid in ids:cloud.nodes[fid]['parent_id']=dest
        cloud.rename_many=rename_many;cloud.move_many=move_many
        jid=new_job(store,{**CONFIG,'notify_emby':False,'ffprobe_enabled':False,'keep_existing_names':True})
        await engine.run_job(jid)
        assert engine.job(jid)['state']=='completed', dict(engine.job(jid))
        assert len(calls)==3  # first snapshot, differing title, final recheck
        await http.aclose();store.close()
    asyncio.run(scenario())


def test_existing_work_batch_still_distinguishes_conflicting_episode(tmp_path):
    async def scenario():
        store, cloud, http, engine, _ = setup(tmp_path)
        cloud.add_video('1001', '测试剧集 (2024) S01E01.WEB-DL.mkv')
        await engine.run_job(new_job(store, {**CONFIG, 'notify_emby':False, 'ffprobe_enabled':False}))
        old = dict(cloud.nodes['1001'])
        cloud.add_video('2001', '测试剧集 (2024) S01E01.WEB-DL.mkv')
        cloud.add_video('2002', '测试剧集 (2024) S01E02.WEB-DL.mkv')
        cloud.add_video('2003', '测试剧集 (2024) S01E03.WEB-DL.mkv')
        async def rename_many(pairs):
            for fid, name in pairs: cloud.nodes[fid]['name'] = name
        async def move_many(ids, dest):
            for fid in ids: cloud.nodes[fid]['parent_id'] = dest
        cloud.rename_many=rename_many; cloud.move_many=move_many
        jid=new_job(store,{**CONFIG,'notify_emby':False,'ffprobe_enabled':False})
        await engine.run_job(jid)
        states={r['file_id']:r['state'] for r in store.db.execute('SELECT file_id,state FROM organize_items WHERE job_id=?',(jid,))}
        assert states['2001'] != 'done' and states['2002'] == states['2003'] == 'done'
        assert cloud.nodes['1001'] == old
        await http.aclose();store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['new_episode', 'changed_existing'])
def test_existing_work_batch_aborts_before_write_if_archive_changes(tmp_path, change):
    async def scenario():
        store, cloud, http, engine, _ = setup(tmp_path)
        cloud.add_video('1001', '测试剧集 (2024) S01E01.WEB-DL.mkv')
        first = new_job(store, {**CONFIG, 'notify_emby': False, 'ffprobe_enabled': False})
        await engine.run_job(first)
        assert engine.job(first)['state'] == 'completed'
        for i in (2, 3, 4): cloud.add_video(str(1000+i), f'测试剧集 (2024) S01E0{i}.WEB-DL.mkv')
        calls=[]; real=engine.index.refresh_related
        async def mutate_before_recheck(job, plan, original=None):
            calls.append(1)
            if len(calls) == 2:
                if change == 'new_episode':
                    cloud.add_video('9999', '测试剧集 (2024) S01E02.WEB-DL.mkv', parent=cloud.nodes['1001']['parent_id'])
                else:
                    cloud.nodes['1001']['size'] += 1
            return await real(job, plan, original)
        engine.index.refresh_related=mutate_before_recheck
        operations=[]
        async def rename_many(pairs):operations.append('rename')
        async def move_many(ids,dest):operations.append('move')
        cloud.rename_many=rename_many;cloud.move_many=move_many
        jid=new_job(store,{**CONFIG,'notify_emby':False,'ffprobe_enabled':False})
        await engine.run_job(jid)
        assert engine.job(jid)['state']=='needs_review', dict(engine.job(jid))
        assert not operations
        assert all(cloud.nodes[str(1000+i)]['parent_id']=='100' for i in (2,3,4))
        await http.aclose();store.close()
    asyncio.run(scenario())

def test_new_tv_batch_stops_if_final_archive_check_unavailable(tmp_path):
    async def scenario():
        store, cloud, http, engine, _ = setup(tmp_path)
        for i in range(1, 4): cloud.add_video(str(1000+i), f'测试剧集 (2024) S01E0{i}.WEB-DL.mkv')
        calls=[]
        async def fail_recheck(*args):
            calls.append(1)
            raise CloudError('目录暂不可用', 'CLOUD_UNAVAILABLE')
        engine._recheck_new_batch_target=fail_recheck
        operations=[]
        async def rename_many(pairs):operations.append('rename')
        async def move_many(ids,dest):operations.append('move')
        cloud.rename_many=rename_many; cloud.move_many=move_many
        jid=new_job(store,{**CONFIG,'notify_emby':False,'ffprobe_enabled':False})
        await engine.run_job(jid)
        assert engine.job(jid)['state']=='needs_review' and not operations
        assert all(cloud.nodes[str(1000+i)]['parent_id']=='100' for i in range(1,4))
        await http.aclose();store.close()
    asyncio.run(scenario())
