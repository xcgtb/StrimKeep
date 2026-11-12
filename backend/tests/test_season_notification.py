import asyncio

from test_organize import setup, new_job, CONFIG


def test_season_is_sent_once_after_background_publication(tmp_path):
    async def scenario():
        store, cloud, http, engine, _ = setup(tmp_path)
        for n in range(1, 4):
            cloud.add_video(str(1000+n), f'测试剧集 (2024) S01E{n:02d}.mkv')
        cloud.add_video('1010', '测试电影 (2024).mkv')
        jid = new_job(store, {**CONFIG, 'notify_emby': False})
        await engine.run_job(jid)
        if engine.notice_tasks:
            await asyncio.gather(*engine.notice_tasks)
        await engine.notifications.job(engine.job(jid))
        rows = store.db.execute("SELECT event_key,message FROM notifications WHERE job_id=? AND event_key LIKE 'show_done:%'", (jid,)).fetchall()
        assert len(rows) == 2
        assert len([r for r in rows if 'E01-E03' in r['message'] and '文件：3 个' in r['message']]) == 1
        assert not store.db.execute("SELECT 1 FROM notifications WHERE job_id=? AND event_key LIKE 'media:%'", (jid,)).fetchone()
        await http.aclose(); store.close()
    asyncio.run(scenario())


def test_failed_episode_does_not_block_season_notice(tmp_path):
    async def scenario():
        store, cloud, http, engine, _ = setup(tmp_path)
        cloud.add_video('1001', '测试剧集 (2024) S01E01.mkv')
        cloud.add_video('1002', '测试剧集 (2024) S01E02.mkv')
        original = engine.process_item
        async def fail(job, row, config):
            if row['file_id'] == '1002':
                await engine.quarantine(job, row, __import__('json').loads(row['original']), config, '测试失败')
                return
            await original(job, row, config)
        engine.process_item = fail
        jid = new_job(store, {**CONFIG, 'notify_emby': False})
        await engine.run_job(jid)
        if engine.notice_tasks:
            await asyncio.gather(*engine.notice_tasks)
        rows = store.db.execute("SELECT message FROM notifications WHERE job_id=? AND event_key LIKE 'show_done:%'", (jid,)).fetchall()
        assert len(rows) == 1
        assert 'E01' in rows[0]['message']
        # 失败属于批次级信息：卡片只放作品本身，未完成不占海报说明。
        assert '未完成或跳过' not in rows[0]['message']
        batch = engine.job(jid)['message']
        assert '待处理' in batch or '未完成' in batch, batch
        await http.aclose(); store.close()
    asyncio.run(scenario())


def test_ten_episodes_one_notice_then_new_episode_one_notice(tmp_path):
    async def scenario():
        store, cloud, http, engine, _ = setup(tmp_path)
        for n in range(1, 11):
            cloud.add_video(str(1000+n), f'测试剧集 (2024) S01E{n:02d}.mkv')
        config = {**CONFIG, 'notify_emby': False}
        first = new_job(store, config)
        await engine.run_job(first)
        if engine.notice_tasks: await asyncio.gather(*engine.notice_tasks)
        rows = store.db.execute("SELECT message FROM notifications WHERE job_id=? AND event_key LIKE 'show_done:%'", (first,)).fetchall()
        assert len(rows) == 1 and 'E01-E10' in rows[0]['message'] and '文件：10 个' in rows[0]['message']
        cloud.add_video('1011', '测试剧集 (2024) S01E11.mkv')
        second = new_job(store, config)
        await engine.run_job(second)
        if engine.notice_tasks: await asyncio.gather(*engine.notice_tasks)
        rows = store.db.execute("SELECT message FROM notifications WHERE job_id=? AND event_key LIKE 'show_done:%'", (second,)).fetchall()
        assert len(rows) == 1 and 'E11' in rows[0]['message'] and '文件：1 个' in rows[0]['message']
        await http.aclose(); store.close()
    asyncio.run(scenario())
