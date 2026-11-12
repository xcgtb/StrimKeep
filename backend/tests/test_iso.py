"""圆盘 ISO：没加进扩展名就按格式跳过；加进去就和其他视频走同一条流程（含探测），
探测读不出来时自动降级到文件名，不为任何后缀写死例外。"""
import asyncio
import json

from test_organize import CONFIG, MEDIA, new_job, setup


def test_iso_is_organized_when_added_to_video_extensions(tmp_path):
    async def run():
        probed = []

        async def probe(url, headers):
            probed.append(url)
            return dict(MEDIA)

        store, cloud, http, engine, _ = setup(tmp_path, probe)
        cloud.add_video('2000', '测试电影 (2024).iso')
        config = {**CONFIG, 'ffprobe_enabled': True,
                  'video_extensions': ['.mkv', '.mp4', '.iso']}
        jid = new_job(store, config)

        await engine.run_job(jid)

        job = engine.job(jid)
        assert job['state'] == 'completed', dict(job)
        row = store.db.execute('SELECT plan,state FROM organize_items WHERE job_id=?', (jid,)).fetchone()
        assert row['state'] == 'done'
        plan = json.loads(row['plan'])
        assert plan['target_name'].endswith('.iso'), plan['target_name']
        assert plan['media_origin'] == 'probed'        # 加进扩展名就照常探测
        assert len(probed) == 1, probed
        await http.aclose()
        store.close()

    asyncio.run(run())


def test_iso_is_skipped_when_not_in_video_extensions(tmp_path):
    """没加进扩展名时，ISO 应该是「按格式跳过」，而不是识别失败。"""
    async def run():
        store, cloud, http, engine, _ = setup(tmp_path)
        cloud.add_video('2000', '测试电影 (2024).iso')
        jid = new_job(store, {**CONFIG, 'video_extensions': ['.mkv', '.mp4']})

        await engine.run_job(jid)

        job = engine.job(jid)
        assert job['state'] == 'completed'
        config = json.loads(job['config'])
        assert config['scan_filter']['excluded'] == 1
        assert '.iso' in config['scan_filter']['formats']
        assert store.db.execute('SELECT count(*) FROM organize_items WHERE job_id=?', (jid,)).fetchone()[0] == 0
        await http.aclose()
        store.close()

    asyncio.run(run())
