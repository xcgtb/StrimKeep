"""多级队列：转存优先取件；整理在文件边界让路并自动续跑，且不重复归档。"""
import asyncio
import json
import time

from strimkeep.organizer import Organizer
from strimkeep.store import Store
from test_organize import MemoryCloud, new_job
from test_organize import handler as tmdb_handler


def make(tmp_path):
    store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
    store.set_many({'cookie': 'UID=1; CID=t; SEID=t', 'tmdb_key': 'k',
                    'playback_base': 'https://play.example'})
    cloud = MemoryCloud(store)
    import httpx
    http = httpx.AsyncClient(transport=httpx.MockTransport(tmdb_handler))
    engine = Organizer(store, cloud, http, tmp_path / 'strm')
    return store, cloud, http, engine


def enqueue_transfer(store, note):
    now = time.time()
    with store.db:
        cur = store.db.execute("INSERT INTO organize_jobs(state,config,created,updated) VALUES('queued',?,?,?)",
                               (json.dumps({'job_kind': 'transfer', 'note': note}, ensure_ascii=False), now, now))
    return cur.lastrowid


def test_worker_prefers_transfer_over_older_organize(tmp_path):
    async def run():
        store, cloud, http, engine = make(tmp_path)
        now = time.time()
        with store.db:
            store.db.execute("INSERT INTO organize_jobs(id,state,config,created,updated) VALUES(1,'queued',?,?,?)",
                             (json.dumps({'job_kind': 'organize'}), now, now))
            store.db.execute("INSERT INTO organize_jobs(id,state,config,created,updated) VALUES(2,'queued',?,?,?)",
                             (json.dumps({'job_kind': 'transfer'}), now, now))
        assert engine.next_job()['id'] == 2              # 转存优先，即使更晚入队
        store.db.execute('DELETE FROM organize_jobs WHERE id=2')
        assert engine.next_job()['id'] == 1              # 没有转存时回到整理
        store.close()

    asyncio.run(run())


def test_organize_yields_to_transfer_then_resumes_without_duplicates(tmp_path):
    async def run():
        store, cloud, http, engine = make(tmp_path)
        store.set_many({'organize_config': {'source_cids': ['100'], 'archive_cid': '200',
                                            'pending_cid': '300', 'video_extensions': ['.mkv', '.mp4'],
                                            'subtitle_extensions': ['.srt'], 'min_video_size_mb': 0}})
        cloud.add_video('1001', '测试剧集 (2024) S01E01.mkv')
        cloud.add_video('1002', '测试剧集 (2024) S01E02.mkv')
        config = {'source_cids': ['100'], 'archive_cid': '200', 'pending_cid': '300',
                  'directory_labels': {}, 'retries': 1, 'retry_interval': 0, 'strm_delay': 0,
                  'notify_emby': False, 'ffprobe_enabled': False, 'probe_prefetch': False,
                  'identify_prefetch': False, 'classify_enabled': True, 'wash_enabled': True,
                  'children_enabled': True, 'concert_enabled': True, 'region_enabled': True,
                  'year_enabled': False, 'cleanup_empty_enabled': True,
                  'video_extensions': ['.mkv'], 'subtitle_extensions': ['.srt'],
                  'ignore_keywords': [], 'min_video_size_mb': 0,
                  'rename_enabled': True, 'folder_language': 'zh', 'filename_language': 'zh',
                  'job_kind': 'organize', 'account_id': '1',
                  'playback_base': 'https://play.example'}
        jid = new_job(store, config)

        # Transfer has its own worker; queuing it does not pause this file batch.
        tj = enqueue_transfer(store, 'first')
        await engine.run_job(jid)
        assert engine.job(jid)['state'] == 'completed'
        assert engine.job(tj)['state'] == 'queued'

        rows = store.db.execute('SELECT file_id, relative_path FROM organized_files').fetchall()
        assert len(rows) == 2
        assert len({r['relative_path'] for r in rows}) == 2      # 两个文件、两份输出、无重复
        await http.aclose()
        store.close()

    asyncio.run(run())
