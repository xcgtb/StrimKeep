"""0.8.10：扫描期间网盘有变动时，失效清理仍逐条 live 确认后执行。

旧行为：账号级 `sync_revision` 与本批 `manifest_revision` 不一致就直接跳过失效清理。
而本批只要写过任何输出都会 `invalidate()` 改掉 revision，所以这条判断几乎总会成立，
失效 STRM 长期不清理（现场 #92 / #8 / #3 都是这条）。
"""
import asyncio

from test_organize import new_job, setup
from test_phase5 import sync_config


def _bump_revision_after_scan(engine, store):
    """在「保存 manifest_revision」之后、失效清理之前改掉账号级 revision。

    模拟扫描期间网盘发生任何变动（转存、整理、动别的目录）——这是旧代码
    跳过失效清理的触发条件。
    """
    original = engine.sync.download_metadata

    async def wrapper(jid, config):
        await original(jid, config)
        with store.db:
            store.db.execute("INSERT OR REPLACE INTO meta VALUES('sync_revision:1', 'bumped-mid-scan')")

    engine.sync.download_metadata = wrapper


def test_cleanup_runs_when_manifest_revision_changed(tmp_path):
    async def run():
        store, cloud, http, engine, _ = setup(tmp_path)
        video = cloud.add_video(name='Movie.2024.1080p.mkv')
        video['sha1'] = 'A' * 40
        first = new_job(store, sync_config())
        await engine.run_job(first)
        assert engine.job(first)['state'] == 'completed', dict(engine.job(first))

        # 云端源文件消失 → 它对应的 STRM 变成失效输出
        del cloud.nodes[video['id']]
        _bump_revision_after_scan(engine, store)

        job = new_job(store, sync_config())
        await engine.run_job(job)
        message = engine.job(job)['message'] or ''
        assert '已跳过失效清理' not in message, message
        assert '清理失效受管文件 1 个' in message, message
        hits = store.db.execute(
            "SELECT count(*) FROM events WHERE message LIKE '%仍继续核对失效输出%'").fetchone()[0]
        assert hits == 1, hits
        await http.aclose()
        store.close()
    asyncio.run(run())


def test_revision_bump_still_leaves_a_trace(tmp_path):
    """留痕：不再阻断清理，但必须能在日志里看到「扫描期间有变化」。"""
    async def run():
        store, cloud, http, engine, _ = setup(tmp_path)
        video = cloud.add_video(name='Movie.2024.1080p.mkv')
        video['sha1'] = 'A' * 40
        first = new_job(store, sync_config())
        await engine.run_job(first)
        _bump_revision_after_scan(engine, store)
        job = new_job(store, sync_config())
        await engine.run_job(job)
        hits = store.db.execute(
            "SELECT count(*) FROM events WHERE message LIKE '%仍继续核对失效输出%'").fetchone()[0]
        assert hits == 1, hits
        await http.aclose()
        store.close()
    asyncio.run(run())


def test_output_registered_after_scan_start_is_not_cleaned(tmp_path):
    import time
    async def run():
        store,cloud,http,engine,_=setup(tmp_path)
        video=cloud.add_video(name='Movie.2024.1080p.mkv');video['sha1']='A'*40
        first=new_job(store,sync_config());await engine.run_job(first)
        old=engine.outputs.get('1',video['id']);assert old
        del cloud.nodes[video['id']]
        original=engine.sync.download_metadata
        async def concurrent_update(jid,config):
            await original(jid,config)
            with store.db:store.db.execute('UPDATE output_registry SET updated=? WHERE file_id=?',(time.time()+1,video['id']))
        engine.sync.download_metadata=concurrent_update
        job=new_job(store,sync_config());await engine.run_job(job)
        assert engine.outputs.get('1',video['id'])['active']==1
        assert '清理失效受管文件 0 个' in engine.job(job)['message']
        await http.aclose();store.close()
    asyncio.run(run())

def test_missing_scan_start_fails_safe_and_preserves_output(tmp_path):
    import json
    async def run():
        store,cloud,http,engine,_=setup(tmp_path)
        video=cloud.add_video(name='Movie.2024.1080p.mkv');video['sha1']='A'*40
        first=new_job(store,sync_config());await engine.run_job(first)
        del cloud.nodes[video['id']]
        original=engine.sync.download_metadata
        async def forget_start(jid,config):
            await original(jid,config)
            data=json.loads(engine.job(jid)['config']);data.pop('manifest_started_at',None)
            engine.update_job(jid,config=json.dumps(data))
        engine.sync.download_metadata=forget_start
        job=new_job(store,sync_config());await engine.run_job(job)
        assert engine.outputs.get('1',video['id'])['active']==1
        assert '缺少本次扫描起始时间' in engine.job(job)['message']
        await http.aclose();store.close()
    asyncio.run(run())


def test_modified_strm_preserves_entire_associated_output_group(tmp_path):
    async def run():
        store, cloud, http, engine, _ = setup(tmp_path)
        video = cloud.add_video(name='Movie.2024.1080p.mkv'); video['sha1'] = 'A' * 40
        sub = cloud.add_video('1001','Movie.2024.1080p.chs.srt'); sub['size'] = 50
        first = new_job(store, sync_config()); await engine.run_job(first)
        old = engine.outputs.get('1', video['id']); path = engine.root / old['relative_path']
        subtitle = path.with_suffix('.chs.srt'); assert subtitle.is_file()
        path.write_text('manually changed')
        del cloud.nodes[video['id']]; del cloud.nodes[sub['id']]
        job = new_job(store, sync_config()); await engine.run_job(job)
        assert path.read_text() == 'manually changed' and subtitle.exists()
        assert engine.outputs.get('1', video['id'])['active'] == 1
        assert engine.job(job)['state'] == 'partial'
        await engine.sync.hash_worker.close(); await http.aclose(); store.close()
    asyncio.run(run())
