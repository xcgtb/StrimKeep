import asyncio
import json

from strimkeep import share_service
from test_phase2_share_pipeline import share_setup


def test_more_than_200_share_files_are_generated_and_repeat_is_idempotent(tmp_path):
    async def scenario():
        store, cloud, http, engine, _ = share_setup(tmp_path)
        secret = {'share_code': 'many', 'receive_code': ''}
        files = [{'id': str(i), 'name': f'视频{i}.mkv', 'size': 5000, 'path': ''} for i in range(1, 202)]
        result = share_service.generate(store, engine.root, secret, files, origin='monitor')
        assert len(result['created']) == 201
        assert not share_service.generate(store, engine.root, secret, files, origin='monitor')['created']
        await http.aclose(); store.close()
    asyncio.run(scenario())


def test_existing_tg_pending_entry_is_processed_after_resume(tmp_path):
    async def scenario():
        store, cloud, http, engine, _ = share_setup(tmp_path)
        secret = {'share_code': 'resume', 'receive_code': ''}
        video = {'id': '501', 'name': '测试剧集 (2024) S01E01.mkv', 'size': 5000, 'path': ''}
        share_service.generate(store, engine.root, secret, [video], origin='telegram')
        cloud.share_tree = {'resume': {'0': [dict(video, is_dir=False)]}}
        jid = share_service.enqueue_organize(engine, origin='telegram', secret=secret)
        await engine.run_job(jid)
        rows = store.db.execute('SELECT state FROM organize_items WHERE job_id=?', (jid,)).fetchall()
        assert len(rows) == 1 and rows[0]['state'] == 'done'
        await http.aclose(); store.close()
    asyncio.run(scenario())


def test_same_file_id_from_two_shares_has_distinct_task_items(tmp_path):
    async def scenario():
        store, cloud, http, engine, _ = share_setup(tmp_path)
        video = {'id': '501', 'name': '测试剧集 (2024) S01E01.mkv', 'size': 5000, 'path': ''}
        for code in ('shareA', 'shareB'):
            share_service.generate(store, engine.root, {'share_code': code, 'receive_code': ''}, [video], origin='web')
        jid = share_service.enqueue_organize(engine, origin='web')
        await engine.run_job(jid)
        items = store.db.execute('SELECT original,state FROM organize_items WHERE job_id=? ORDER BY id', (jid,)).fetchall()
        assert len(items) == 2
        assert len({json.loads(item['original'])['share_record_id'] for item in items}) == 2
        assert all(item['state'] in ('done', 'skipped', 'review', 'pending') for item in items)
        assert any(item['state'] == 'done' for item in items)
        await http.aclose(); store.close()
    asyncio.run(scenario())
