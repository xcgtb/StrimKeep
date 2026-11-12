import asyncio
import copy

from strimkeep.cloud115 import Cloud115
from strimkeep.store import Store
from test_organize import CONFIG, new_job, setup


def test_new_scan_reads_external_changes_instead_of_previous_cache(tmp_path):
    async def run():
        store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
        cloud = Cloud115(store)
        items = [{'id': '1', 'name': 'old.srt', 'is_dir': False}]
        calls = []
        async def listing(cid, page_size=1000):
            calls.append(str(cid))
            return copy.deepcopy(items), len(items)
        cloud._children = listing
        cloud.use_scope('1', 'first')
        assert len(await cloud.children('100')) == 1
        items.append({'id': '2', 'name': 'new.mkv', 'is_dir': False})
        cloud.use_scope('1', 'second')
        assert len(await cloud.fresh_children('100')) == 2
        assert len(await cloud.children('100')) == 2
        assert calls == ['100', '100', '100']
        await cloud.close(); store.close()
    asyncio.run(run())


def test_manifest_uses_fresh_listing_for_root_and_subdirectory(tmp_path):
    async def run():
        store, cloud, http, engine, _ = setup(tmp_path)
        cloud.nodes['101'] = {'id': '101', 'name': '新目录', 'parent_id': '100', 'is_dir': True}
        cloud.add_video('1000', parent='101')
        actual = cloud.children
        fresh_calls = []
        async def stale(cid): return []
        async def fresh(cid):
            fresh_calls.append(str(cid))
            return await actual(cid)
        cloud.children = stale
        cloud.fresh_children = fresh
        jid = new_job(store)
        engine.update_job(jid, state='running')
        await engine.manifest(jid, dict(CONFIG))
        assert fresh_calls == ['100', '101']
        assert store.db.execute('select file_id from organize_items where job_id=?', (jid,)).fetchone()[0] == '1000'
        await http.aclose(); store.close()
    asyncio.run(run())
