"""同步的「台账」就是磁盘本身：判定看清单 + 本地文件，不看任何记录表。"""
import asyncio
import json
import os

from test_organize import new_job, setup
from test_phase5 import sync_config


def local_strms(engine):
    return list(engine.root.rglob('*.strm'))


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
