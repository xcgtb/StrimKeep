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


def test_broken_hash_worker_no_longer_blocks_sync(tmp_path):
    """同步不再靠哈希子进程判断「没变」：它坏了也不影响结果，本地文件原样保留。"""
    async def scenario():
        store,cloud,http,engine,_=setup(tmp_path)
        cloud.add_video(name='Movie.mkv')['sha1']='A'*40
        first=new_job(store,{**sync_config(),'notify_emby':False});await engine.run_job(first)
        path=next(engine.root.rglob('*.strm'));before=path.read_bytes()
        async def fail(): raise OSError('synthetic child failure')
        engine.sync.hash_worker._start=fail
        second=new_job(store,{**sync_config(),'notify_emby':False});await engine.run_job(second)
        assert engine.job(second)['state']=='completed'
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
