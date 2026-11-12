import asyncio, json
import pytest
from test_organize import setup, new_job
from test_phase5 import sync_config
from strimkeep.cloud115 import CloudError

# Cloud115 没有实现递归清单（download_tree / recursive_files_verified），
# sync.py 用 getattr 门控，缺了就一直走逐目录扫描。这两组用例是「将来要做」的规格，
# 现在跑必然是 AttributeError；实现后删掉这一行即可恢复。
pytestmark = pytest.mark.skip(reason="递归清单未实现：Cloud115 无 download_tree / recursive_files_verified")


def test_recursive_manifest_matches_directory_scan_and_excludes_empty_dirs(tmp_path):
    async def run():
        store, cloud, http, engine, _ = setup(tmp_path)
        folder = (await cloud.mkdir('100', '剧集'))['cid']
        await cloud.mkdir('100', '空目录')
        cloud.add_video('1001', 'Movie.2024.mkv', folder)
        cloud.add_video('1002', 'Movie.2024.srt', folder)
        config = sync_config(['100'])
        async def flat(root):
            return [v.copy() for v in cloud.nodes.values() if not v['is_dir'] and v['parent_id'] == folder]
        jid = new_job(store, config)
        engine.update_job(jid, state="running")
        await engine.sync.manifest(jid, dict(config))
        before = [json.loads(r['original']) for r in store.db.execute('select original from organize_items where job_id=?', (jid,))]
        cloud.recursive_files_verified = flat
        jid2 = new_job(store, config)
        engine.update_job(jid2, state="running")
        await engine.sync.manifest(jid2, dict(config))
        after = [json.loads(r['original']) for r in store.db.execute('select original from organize_items where job_id=?', (jid2,))]
        def strip(rows):
            for row in rows:
                row.pop('_scan_at', None)
                for sub in row.get('subtitles', []): sub.pop('_scan_at', None)
            return rows
        assert strip(before) == strip(after)
        saved = json.loads(engine.job(jid2)['config'])
        assert saved['sync_directory_stats']['100']['directories'] == 2
        assert saved['sync_directory_stats']['100']['directories_exact'] is False
        await http.aclose(); store.close()
    asyncio.run(run())


def test_bad_recursive_snapshot_falls_back_but_rate_limit_propagates(tmp_path):
    async def run():
        store, cloud, http, engine, _ = setup(tmp_path)
        cloud.add_video('1001', 'Movie.2024.mkv')
        config = sync_config(['100'])
        async def bad(root):
            raise CloudError('incomplete', 'INCOMPLETE_LIST', 409)
        cloud.recursive_files_verified = bad
        jid = new_job(store, config)
        engine.update_job(jid, state="running")
        await engine.sync.manifest(jid, dict(config))
        assert store.db.execute('select count(*) from organize_items where job_id=?', (jid,)).fetchone()[0] == 1
        async def rate(root):
            raise CloudError('limited', 'RATE_LIMITED', 503)
        cloud.recursive_files_verified = rate
        jid3 = new_job(store, config)
        engine.update_job(jid3, state="running")
        with pytest.raises(CloudError):
            await engine.sync.manifest(jid3, dict(config))
        await http.aclose(); store.close()
    asyncio.run(run())

def test_recursive_files_rejects_duplicates_and_changed_snapshot(monkeypatch):
    from strimkeep.cloud115 import Cloud115
    from types import SimpleNamespace
    import strimkeep.cloud115 as module
    def row(fid):
        return {'id': fid, 'parent_id': '100', 'name': fid+'.mkv', 'size': 5, 'is_dir': False, 'sha1': 'A', 'pickcode': 'x'}
    monkeypatch.setattr(module, 'normalize_attr', lambda raw, simple: raw)
    async def run(pages):
        cloud = object.__new__(Cloud115)
        cloud.client = lambda: SimpleNamespace(fs_files=object())
        async def call(method, params): return pages.pop(0)
        cloud.call = call
        return await cloud.recursive_files_verified('100')
    def page(rows, n): return {'data': rows, 'count': n}
    a, b = row('1'), row('2')
    assert len(asyncio.run(run([page([a,b],2),page([],2),page([a,b],2),page([],2)]))) == 2
    with pytest.raises(CloudError):
        asyncio.run(run([page([a,a],2)]))
    with pytest.raises(CloudError):
        asyncio.run(run([page([a],2),page([],2)]))
    with pytest.raises(CloudError):
        asyncio.run(run([page([a],1),page([],1),page([b],1),page([],1)]))
