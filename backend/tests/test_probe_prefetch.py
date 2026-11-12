"""批量探测预取：整理前并发测量，主流程逐条处理时直接命中缓存。"""
import asyncio
import hashlib
import json
import time

import httpx

from strimkeep.cloud115 import CloudError
from strimkeep.organizer import Organizer, Stopped, dumps
from strimkeep.store import Store
from test_organize import MemoryCloud, handler as tmdb_handler

MEDIA = {'width': 3840, 'height': 2160, 'codec': 'hevc', 'fps': 24,
         'dynamic_range': 'Dolby Vision', 'dolby_vision_profile': 5, 'bit_depth': 10,
         'duration': '3000', 'audio': [{'codec': 'eac3', 'channels': 6, 'language': 'eng'}]}

CONFIG = {'source_cids': ['100'], 'archive_cid': '200', 'pending_cid': '300',
          'directory_labels': {}, 'retries': 1, 'retry_interval': 0, 'strm_delay': 0,
          'notify_emby': False, 'ffprobe_enabled': True, 'probe_prefetch': True,
          'probe_concurrency': 2}


def setup(tmp_path, probe_fn):
    store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
    store.set_many({'cookie': 'UID=1; CID=t; SEID=t', 'tmdb_key': 'k',
                    'playback_base': 'https://play.example'})
    cloud = MemoryCloud(store)
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(404)))
    return store, cloud, http, Organizer(store, cloud, http, tmp_path / 'strm', probe_fn)


def add_video(cloud, fid, name):
    """带 40 位 sha1 的视频，才能算出指纹、让探测结果可复用。"""
    node = cloud.add_video(fid, name)
    node['sha1'] = hashlib.sha1(fid.encode()).hexdigest()
    return node


def new_job(store, state='running', **overrides):
    now = time.time()
    with store.db:
        cur = store.db.execute("INSERT INTO organize_jobs(state,config,created,updated) VALUES(?,?,?,?)",
                               (state, dumps({**CONFIG, **overrides}), now, now))
    return cur.lastrowid


def add_item(store, jid, node, folder=None):
    original = {'id': node['id'], 'name': node['name'], 'parent_id': node['parent_id'],
                'size': node['size'], 'sha1': node['sha1'], 'pickcode': node['pickcode']}
    if folder:
        original['folder'] = folder
    with store.db:
        store.db.execute('INSERT INTO organize_items(job_id,file_id,original) VALUES(?,?,?)',
                         (jid, node['id'], dumps(original)))
    return store.db.execute('SELECT * FROM organize_items WHERE job_id=? AND file_id=?',
                            (jid, node['id'])).fetchone()


def counter_probe(state, fail_on=()):
    async def probe(url, headers):
        state.setdefault('calls', []).append(url)
        state['inflight'] = state.get('inflight', 0) + 1
        state['peak'] = max(state.get('peak', 0), state['inflight'])
        await asyncio.sleep(0.03)
        state['inflight'] -= 1
        if any(mark in url for mark in fail_on):
            raise CloudError('FFprobe 无法读取此文件。', 'PROBE_FAILED', 422)
        return dict(MEDIA)
    return probe


def test_prefetch_measures_batch_then_processing_hits_cache(tmp_path):
    async def scenario():
        state = {'calls': []}
        store, cloud, http, engine = setup(tmp_path, counter_probe(state))
        nodes = [add_video(cloud, str(1000 + i), f'测试剧集 (2024) S01E0{i + 1}.WEB-DL.mkv')
                 for i in range(4)]
        jid = new_job(store)
        rows = [add_item(store, jid, node) for node in nodes]

        await engine.prefetch_probes(jid, rows, json.loads(engine.job(jid)['config']))

        assert len(state['calls']) == 4, state['calls']
        assert state['peak'] == 2, state['peak']      # 默认并发 2，不是一把梭
        assert engine.job(jid)['stage'] == '批量探测 4/4'
        # 主流程此刻再测量，应当直接命中缓存，不再打远程直链。
        media, _, origin = await engine.measurements.measure(jid, nodes[0], nodes[0]['name'])
        assert origin == 'cached' and media['width'] == 3840
        assert len(state['calls']) == 4
        await http.aclose(); store.close()
    asyncio.run(scenario())


def test_prefetch_honours_switches_and_skips_useless_targets(tmp_path):
    async def scenario():
        state = {'calls': []}
        store, cloud, http, engine = setup(tmp_path, counter_probe(state))
        nodes = [add_video(cloud, str(2000 + i), f'测试剧集 (2024) S01E0{i + 1}.WEB-DL.mkv')
                 for i in range(3)]
        single = add_video(cloud, '2900', '测试电影 (2024).mp4')
        no_hash = cloud.add_video('2950', '测试电影 (2024).1080p.mp4')   # sha1 不是 40 位 → 无指纹
        blank = cloud.add_video('2960', '测试电影 (2024).720p.mp4')
        blank['sha1'] = hashlib.sha1(b'blank').hexdigest()

        # 开关关闭：一次都不探。
        for overrides in ({'probe_prefetch': False}, {'ffprobe_enabled': False}):
            jid = new_job(store, **overrides)
            rows = [add_item(store, jid, node) for node in nodes]
            await engine.prefetch_probes(jid, rows, json.loads(engine.job(jid)['config']))
            assert state['calls'] == [], overrides

        # 只有一个可探目标：没有并发收益，交给主流程。
        jid = new_job(store)
        rows = [add_item(store, jid, node) for node in (single, no_hash)]
        await engine.prefetch_probes(jid, rows, json.loads(engine.job(jid)['config']))
        assert state['calls'] == []

        # ISO 与已算出 media 的条目不再探测。
        iso = cloud.add_video('2970', '测试电影 (2024).iso')
        iso['sha1'] = hashlib.sha1(b'iso').hexdigest()
        jid = new_job(store)
        rows = [add_item(store, jid, node) for node in (blank, iso)]
        with store.db:
            store.db.execute("UPDATE organize_items SET plan=? WHERE file_id=?",
                             (dumps({'media': dict(MEDIA)}), blank['id']))
        rows = [store.db.execute('SELECT * FROM organize_items WHERE id=?', (row['id'],)).fetchone()
                for row in rows]
        await engine.prefetch_probes(jid, rows, json.loads(engine.job(jid)['config']))
        assert state['calls'] == []
        await http.aclose(); store.close()
    asyncio.run(scenario())


def test_one_failed_probe_does_not_block_the_others(tmp_path):
    async def scenario():
        state = {'calls': []}
        store, cloud, http, engine = setup(tmp_path, counter_probe(state, fail_on=('pc3001',)))   # 直链里带的是 pickcode，不是文件名
        nodes = [add_video(cloud, str(3000 + i), f'测试剧集 (2024) S01E0{i + 1}.WEB-DL.mkv')
                 for i in range(3)]
        jid = new_job(store)
        rows = [add_item(store, jid, node) for node in nodes]

        await engine.prefetch_probes(jid, rows, json.loads(engine.job(jid)['config']))

        assert len(state['calls']) == 3
        cached = store.db.execute('SELECT count(*) FROM media_cache').fetchone()[0]
        assert cached == 2                      # 失败的那个没写缓存，留给主流程重试
        assert engine.job(jid)['state'] == 'running'   # 预取失败不升级成任务级故障
        await http.aclose(); store.close()
    asyncio.run(scenario())


def test_paused_job_stops_prefetch_before_any_probe(tmp_path):
    async def scenario():
        state = {'calls': []}
        store, cloud, http, engine = setup(tmp_path, counter_probe(state))
        nodes = [add_video(cloud, str(4000 + i), f'测试剧集 (2024) S01E0{i + 1}.WEB-DL.mkv')
                 for i in range(3)]
        jid = new_job(store, state='paused')
        rows = [add_item(store, jid, node) for node in nodes]
        try:
            await engine.prefetch_probes(jid, rows, json.loads(engine.job(jid)['config']))
            raise AssertionError('暂停的任务不应该继续预取')
        except Stopped:
            pass
        assert state['calls'] == []
        await http.aclose(); store.close()
    asyncio.run(scenario())


def test_run_job_prefetches_before_the_first_item(tmp_path):
    """主流程接线：处理第一个文件前，整批的探测结果已经取好，且不再重复探测。"""
    async def scenario():
        state = {'calls': []}
        store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
        store.set_many({'cookie': 'UID=1; CID=t; SEID=t', 'tmdb_key': 'k',
                        'playback_base': 'https://play.example'})
        cloud = MemoryCloud(store)
        http = httpx.AsyncClient(transport=httpx.MockTransport(tmdb_handler))
        engine = Organizer(store, cloud, http, tmp_path / 'strm', counter_probe(state))
        for index, fid in enumerate(('5000', '5001')):
            node = cloud.add_video(fid, f'测试剧集 (2024) S01E0{index + 1}.WEB-DL.mkv')
            node['sha1'] = hashlib.sha1(fid.encode()).hexdigest()
        jid = new_job(store)
        marks = []
        original = engine.process_item

        async def traced(job_id, row, config):
            marks.append(len(state.get('calls', [])))
            return await original(job_id, row, config)

        engine.process_item = traced
        await engine.run_job(jid)
        assert marks and marks[0] == 2, marks      # 首条处理前两个文件都探过了
        assert len(state['calls']) == 2, state['calls']   # 处理阶段没有再探一次
        assert engine.job(jid)['state'] == 'completed', dict(engine.job(jid))
        await http.aclose(); store.close()
    asyncio.run(scenario())


def test_run_job_keeps_prefetching_later_files_in_bounded_windows(tmp_path):
    async def scenario():
        state = {'calls': []}
        store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
        store.set_many({'cookie':'UID=1; CID=t; SEID=t', 'tmdb_key':'k', 'playback_base':'https://play.example'})
        cloud = MemoryCloud(store)
        http = httpx.AsyncClient(transport=httpx.MockTransport(tmdb_handler))
        engine = Organizer(store, cloud, http, tmp_path / 'strm', counter_probe(state))
        for index in range(6):
            fid = str(5000 + index)
            node = cloud.add_video(fid, f'测试剧集 (2024) S01E0{index + 1}.WEB-DL.mkv')
            node['sha1'] = hashlib.sha1(fid.encode()).hexdigest()
        jid = new_job(store)
        marks = []
        process = engine.process_item
        async def traced(job_id, row, config):
            marks.append(len(state['calls']))
            return await process(job_id, row, config)
        engine.process_item = traced
        await engine.run_job(jid)
        assert marks == [2, 2, 4, 4, 6, 6]
        assert state['peak'] == 2 and len(state['calls']) == 6
        assert engine.job(jid)['state'] == 'completed'
        await http.aclose(); store.close()
    asyncio.run(scenario())


def test_identify_prefetch_warms_tmdb_search_cache(tmp_path):
    """开工前并发查一遍 TMDB 搜索，正式处理时直接命中缓存。"""
    async def run():
        seen = []

        def handler(request):
            seen.append(str(request.url))
            return httpx.Response(200, json={'results': []})

        store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
        store.set_many({'cookie': 'UID=1; CID=t; SEID=t', 'tmdb_key': 'k',
                        'playback_base': 'https://play.example'})
        cloud = MemoryCloud(store)
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        engine = Organizer(store, cloud, http, tmp_path / 'strm', counter_probe({'calls': []}))
        jid = new_job(store)
        rows = []
        for index, show in enumerate(('剧甲', '剧乙')):
            node = add_video(cloud, str(1000 + index), f'{index + 1}.mkv')
            rows.append(add_item(store, jid, node, folder=show))
        config = json.loads(engine.job(jid)['config'])

        await engine.prefetch_identify(jid, rows, config)
        searches = [url for url in seen if '/search/tv' in url]
        assert len(searches) == 2, searches
        assert store.db.execute('SELECT count(*) FROM tmdb_cache').fetchone()[0] == 2

        await engine.prefetch_identify(jid, rows, config)     # 再来一次应全部命中缓存
        assert len([url for url in seen if '/search/tv' in url]) == 2
        await http.aclose(); store.close()
    asyncio.run(run())
