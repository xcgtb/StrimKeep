import asyncio
import hashlib
import json
import os
import time

from strimkeep.sync_hash_worker import HashWorker
from test_organize import setup, new_job
from test_phase5 import sync_config
from test_queue_priority import make


def test_hash_worker_runs_outside_server_and_recovers_after_exit(tmp_path):
    async def scenario():
        path=tmp_path/'one.strm'; path.write_bytes(b'stream link')
        worker=HashWorker()
        assert await worker.checksum(path)==hashlib.sha256(b'stream link').hexdigest()
        first=worker.process
        assert first.pid != os.getpid()
        first.kill();await first.wait()
        assert await worker.checksum(path)==hashlib.sha256(b'stream link').hexdigest()
        assert worker.process.pid != first.pid
        assert await worker.checksum(tmp_path/'missing.strm') is None
        process=worker.process
        await worker.close()
        assert process.returncode is not None
    asyncio.run(scenario())


def test_failed_hash_worker_stops_sync_without_cleanup(tmp_path):
    async def scenario():
        store,cloud,http,engine,_=setup(tmp_path)
        cloud.add_video(name='Movie.mkv')['sha1']='A'*40
        first=new_job(store,{**sync_config(),'notify_emby':False});await engine.run_job(first)
        old=engine.outputs.get('1','1000');path=engine.root/old['relative_path'];before=path.read_bytes()
        async def fail(): raise OSError('synthetic child failure')
        engine.sync.hash_worker._start=fail
        second=new_job(store,{**sync_config(),'notify_emby':False});await engine.run_job(second)
        assert engine.job(second)['state']=='needs_review'
        assert path.read_bytes()==before
        await http.aclose();store.close()
    asyncio.run(scenario())


def test_supervisor_recovers_only_its_lane_without_replaying_job(tmp_path):
    async def scenario():
        store,cloud,http,engine=make(tmp_path);now=time.time()
        with store.db:
            sync_id=store.db.execute("INSERT INTO organize_jobs(state,config,created,updated) VALUES('running',?,?,?)",(json.dumps({'job_kind':'strm_sync'}),now,now)).lastrowid
            transfer_id=store.db.execute("INSERT INTO organize_jobs(state,config,created,updated) VALUES('running',?,?,?)",(json.dumps({'job_kind':'transfer'}),now,now)).lastrowid
        restarted=asyncio.Event();count=0
        async def flaky(lane):
            nonlocal count
            count+=1
            if count==1:raise RuntimeError('synthetic failure')
            restarted.set(); await asyncio.Event().wait()
        engine.loop=flaky
        task=asyncio.create_task(engine.supervise_lane('sync'))
        try:
            await asyncio.wait_for(restarted.wait(),3)
            assert engine.job(sync_id)['state']=='needs_review'
            assert engine.job(transfer_id)['state']=='running'
            assert engine.lane_faults['sync'] is False and count==2
        finally:
            engine.closing=True;task.cancel();await asyncio.gather(task,return_exceptions=True)
            await http.aclose();store.close()
    asyncio.run(scenario())


def test_read_only_hash_request_retries_once_after_child_dies(tmp_path):
    import sys
    async def scenario():
        path=tmp_path/'stream.strm';path.write_bytes(b'payload')
        worker=HashWorker();start=worker._start;calls=0
        async def fail_once():
            nonlocal calls
            calls+=1
            if calls==1:
                worker.process=await asyncio.create_subprocess_exec(sys.executable,'-c',
                    'import sys;sys.stdin.readline();sys.exit(7)',stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE)
            else:await start()
        worker._start=fail_once
        try:
            assert await worker.checksum(path)==hashlib.sha256(b'payload').hexdigest()
            assert calls==2
        finally:await worker.close()
    asyncio.run(scenario())


def test_abbreviated_organizer_trail_reuses_full_scan_and_repairs_registry(tmp_path):
    async def scenario():
        store, cloud, http, engine, _ = setup(tmp_path)
        middle = (await cloud.mkdir('200', '剧集'))['cid']
        parent = (await cloud.mkdir(middle, 'Season 1'))['cid']
        video = cloud.add_video(name='Show.S01E01.mkv', parent=parent)
        video['sha1'] = 'A' * 40
        config = {**sync_config(roots=['200']), 'notify_emby': False}
        first = new_job(store, config)
        await engine.run_job(first)
        assert engine.job(first)['state'] == 'completed'
        old = engine.outputs.get('1', video['id'])
        target = engine.root / old['relative_path']
        contents = target.read_bytes()
        with store.db:
            store.db.execute('UPDATE output_registry SET trail=? WHERE file_id=?',
                             (json.dumps(['200', parent]), video['id']))
        calls = []
        original_file = cloud.file
        async def count_file(fid):
            calls.append(fid)
            return await original_file(fid)
        cloud.file = count_file
        job = new_job(store, config)
        await engine.run_job(job)
        assert engine.job(job)['state'] == 'completed'
        item = store.db.execute('SELECT original,step FROM organize_items WHERE job_id=?', (job,)).fetchone()
        assert item['step'] == '输出未变更，已复用核对结果'
        assert calls == []
        assert json.loads(engine.outputs.get('1', video['id'])['trail']) == [x['id'] for x in json.loads(item['original'])['_path']]
        assert target.read_bytes() == contents
        await engine.sync.hash_worker.close(); await http.aclose(); store.close()
    asyncio.run(scenario())


def test_abbreviated_trail_never_bypasses_changed_identity_or_owned_bytes(tmp_path):
    async def scenario():
        store, cloud, http, engine, _ = setup(tmp_path)
        middle = (await cloud.mkdir('200', '剧集'))['cid']
        parent = (await cloud.mkdir(middle, 'Season 1'))['cid']
        video = cloud.add_video(name='Show.S01E01.mkv', parent=parent)
        video['sha1'] = 'A' * 40
        config = {**sync_config(roots=['200']), 'notify_emby': False}
        job = new_job(store, config); await engine.run_job(job)
        row = store.db.execute('SELECT original FROM organize_items WHERE job_id=?', (job,)).fetchone()
        source = json.loads(row['original'])
        with store.db:
            store.db.execute('UPDATE output_registry SET trail=? WHERE file_id=?', (json.dumps(['200', parent]), video['id']))
        assert await engine.sync.unchanged(source, config)
        changed = {**source, 'sha1': 'B' * 40}
        assert await engine.sync.unchanged(changed, config) is None
        changed = {**source, '_scan_at': time.time() - 901}
        assert await engine.sync.unchanged(changed, config) is None
        changed = {**source, '_path': [*source['_path'][:-1], {**source['_path'][-1], 'name': 'renamed'}]}
        assert await engine.sync.unchanged(changed, config) is None
        target = engine.root / engine.outputs.get('1', video['id'])['relative_path']
        target.write_text('user modified')
        assert await engine.sync.unchanged(source, config) is None
        await engine.sync.hash_worker.close(); await http.aclose(); store.close()
    asyncio.run(scenario())
