import asyncio
import json
import time
import sqlite3
import hashlib
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi.testclient import TestClient

from strimkeep.cloud115 import Cloud115, CloudError
from strimkeep.main import create_app
from strimkeep.media import NeedsReview, choose_match, classify, naming, parse_name
from strimkeep.organizer import Organizer, UncertainState, dumps
from strimkeep.store import SCHEMA_VERSION, Store
from cryptography.fernet import Fernet


MEDIA = {'width': 3840, 'height': 2160, 'codec': 'hevc', 'fps': 24,
         'dynamic_range': 'Dolby Vision', 'dolby_vision_profile': 5, 'bit_depth': 10,
         'duration': '3000', 'audio': [{'codec': 'eac3', 'channels': 6, 'language': 'eng'}]}
TV = {'id': 10, 'name': '测试剧集', 'original_name': 'Test Show', 'first_air_date': '2024-01-01',
      'origin_country': ['US'], 'genres': [{'id': 18, 'name': '剧情'}], 'overview': '剧情 & 内容',
      'poster_path': '/poster.jpg', 'backdrop_path': '/backdrop.jpg'}
MOVIE = {'id': 20, 'title': '测试电影', 'release_date': '2024-01-01', 'origin_country': ['CN'],
         'genres': [{'id': 18, 'name': '剧情'}], 'overview': '电影 <剧情>'}
CONFIG = {'source_cids': ['100'], 'archive_cid': '200', 'pending_cid': '300',
          'directory_labels': {}, 'retries': 1, 'strm_delay': 0, 'notify_emby': True}


class MemoryCloud:
    def __init__(self, store):
        self.store = store
        self.nodes = {cid: {'id': cid, 'name': name, 'parent_id': '0', 'is_dir': True} for cid, name in
                      [('100', '待整理'), ('200', '归档'), ('300', '待处理')]}
        self.sequence, self.writes = 400, []
        self.after_rename = self.after_move = None
        self.skim_failure = False

    def add_video(self, fid='1000', name='测试剧集 (2024) S01E03.WEB-DL.mkv', parent='100'):
        self.nodes[fid] = {'id': fid, 'name': name, 'parent_id': parent, 'is_dir': False,
                           'size': 1000, 'pickcode': 'pc' + fid, 'sha1': 'hash' + fid}
        return self.nodes[fid]

    async def close(self):
        pass

    async def status(self):
        return {'connected': True}

    async def live_file_exists(self, fid):
        return fid in self.nodes

    async def file(self, fid):
        if self.skim_failure:
            self.skim_failure = False
            raise CloudError('核查暂时不可用。')
        return self.nodes[fid].copy()

    async def directory(self, cid):
        if cid not in self.nodes or not self.nodes[cid]['is_dir']:
            # 与真实 115 一致：目录不在时报 DIRECTORY_UNAVAILABLE，清理逻辑按这个码
            # 跳过单个目录，而不是把整轮空目录清理判成失败。
            raise CloudError('目录不存在。', 'DIRECTORY_UNAVAILABLE', 409)
        chain = [cid]
        while cid != '0':
            cid = self.nodes[cid]['parent_id']
            chain.insert(0, cid)
        return chain

    async def path_info(self, cid):
        return [{'id': key, 'name': self.nodes[key]['name'] if key != '0' else ''}
                for key in await self.directory(cid)]

    async def children(self, cid):
        return [n.copy() for n in self.nodes.values() if n['parent_id'] == cid]

    async def list_files(self, cid, offset, limit):
        rows = await self.children(str(cid))
        return {'items': rows[offset:offset+limit], 'total': len(rows), 'has_more': offset+limit < len(rows)}

    async def mkdir(self, parent, name):
        self.sequence += 1
        cid = str(self.sequence)
        self.nodes[cid] = {'id': cid, 'name': name, 'parent_id': parent, 'is_dir': True}
        self.writes.append(('mkdir', cid, parent, name))
        return {'cid': cid}

    async def rename(self, fid, name):
        self.writes.append(('rename', fid, name))
        self.nodes[fid]['name'] = name
        if self.after_rename:
            callback, self.after_rename = self.after_rename, None
            callback()

    async def move(self, fid, dest):
        self.writes.append(('move', fid, dest))
        self.nodes[fid]['parent_id'] = dest
        if self.after_move:
            callback, self.after_move = self.after_move, None
            callback()

    async def url(self, pc, ua):
        return 'https://cdn.example/' + pc, {'User-Agent': ua}


def handler(request):
    path = request.url.path
    if request.url.host == 'image.tmdb.org':
        return httpx.Response(200, content=b'test-image')
    if request.url.host == 'cdn.example':
        return httpx.Response(200, content=b'1\n00:00:00,000 --> 00:00:01,000\nsubtitle\n')
    if request.url.host == 'emby':
        return httpx.Response(204)
    if path.startswith('/3/search/'):
        kind = path.rsplit('/', 1)[-1]
        data = TV if kind == 'tv' else MOVIE
        return httpx.Response(200, json={'results': [data]})
    if '/episode/' in path:
        number = int(path.rsplit('/', 1)[-1])
        return httpx.Response(200, json={'id': 100+number, 'name': f'第{number}集', 'season_number': 1,
            'episode_number': number, 'air_date': '2024-01-01', 'overview': '单集剧情 & 描述'})
    if path == '/3/tv/10/season/1':
        return httpx.Response(200, json={'season_number': 1, 'episodes': [
            {'id': 100+number, 'name': f'第{number}集', 'season_number': 1,
             'episode_number': number, 'air_date': '2024-01-01', 'overview': '单集剧情 & 描述'}
            for number in range(1, 101)]})
    if path == '/3/tv/10':
        return httpx.Response(200, json=TV)
    if path == '/3/movie/20':
        return httpx.Response(200, json=MOVIE)
    raise AssertionError('unexpected upstream path ' + path)


async def good_probe(*args, **kwargs):
    return MEDIA.copy()


def setup(tmp_path, probe_fn=good_probe):
    store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
    store.set_many({'sync_isolate_worker': 0,
                    'cookie': 'UID=1_A1; CID=test; SEID=test', 'tmdb_key': 'secret-key',
                    'playback_base': 'https://play.example', 'emby_url': 'http://emby:8096', 'emby_key': 'emby-secret'})
    cloud = MemoryCloud(store)
    requests = []
    def traced(request):
        requests.append(request)
        return handler(request)
    http = httpx.AsyncClient(transport=httpx.MockTransport(traced))
    engine = Organizer(store, cloud, http, tmp_path / 'strm', probe_fn)
    return store, cloud, http, engine, requests


def new_job(store, config=None):
    now = time.time()
    with store.db:
        cur = store.db.execute("INSERT INTO organize_jobs(state,config,created,updated) VALUES('queued',?,?,?)",
                               (dumps(config or CONFIG), now, now))
    return cur.lastrowid


def test_movie_episode_strm_subtitles_only_and_once_notification(tmp_path):
    async def scenario():
        store, cloud, http, engine, requests = setup(tmp_path)
        video = cloud.add_video()
        cloud.add_video('1001', '测试剧集 (2024) S01E03.WEB-DL.chs.forced.srt')['size'] = 50
        cloud.add_video('1002', '测试电影 (2024).WEB-DL.mp4')
        jid = new_job(store)
        await engine.run_job(jid); await engine.emby.drain_outbox(); await engine.emby.tick()
        assert engine.job(jid)['state'] == 'completed', dict(engine.job(jid))
        files = list((tmp_path / 'strm').rglob('*.strm'))
        assert len(files) == 2
        tv = next(p for p in files if 'Season 1' in p.parts)
        assert tv.parts[-5:-1] == ('剧集', '欧美剧集', '测试剧集 (2024) {tmdb-10}', 'Season 1')
        assert 'S01E03' in tv.name and '.DoVi P5.H.265.10-bit.24fps.DDP 5.1' in tv.name
        assert tv.with_suffix('.chs.forced.srt').exists()
        assert {p.suffix for p in (tmp_path/'strm').rglob('*') if p.is_file()} == {'.strm', '.srt'}
        assert not any(r.url.host == 'image.tmdb.org' for r in requests)
        metadata = json.loads(store.db.execute('SELECT metadata FROM organized_files ORDER BY file_id LIMIT 1').fetchone()[0])
        assert metadata['details']['id'] == 10 and metadata['media']['width'] == 3840
        assert cloud.nodes['1001']['parent_id'] == cloud.nodes['1000']['parent_id']
        assert cloud.nodes['1000']['name'].endswith('.mkv') and cloud.nodes['1002']['name'].endswith('.mp4')
        assert len([r for r in requests if r.url.path.endswith('/Library/Media/Updated')]) == 1
        assert store.db.execute('SELECT count(*) FROM organized_files').fetchone()[0] == 2
        contents = [p.read_text() for p in files]
        assert all(u.startswith('https://play.example/play115/') for u in contents)
        codes = sorted({u.split('/play115/', 1)[1].split('/')[0] for u in contents})
        assert codes == ['pc1000', 'pc1002'], '每个 STRM 必须带自己那份清单身份'
        plan = json.loads(store.db.execute('SELECT plan FROM organize_items LIMIT 1').fetchone()[0])
        assert store.crypto.decrypt(plan['token_cipher'].encode()).decode() == ''
        await http.aclose(); store.close()
    asyncio.run(scenario())


def test_output_repair_preserves_emby_plugin_metadata_and_artwork(tmp_path):
    async def scenario():
        store, cloud, http, engine, requests = setup(tmp_path)
        cloud.add_video()
        jid = new_job(store)
        await engine.run_job(jid)
        assert engine.job(jid)['state'] == 'completed'
        video = next((tmp_path/'strm').rglob('*.strm'))
        owned = {video.with_suffix('.nfo'): b'Emby episode metadata',
                 video.parent.parent/'tvshow.nfo': b'Emby series metadata',
                 video.parent.parent/'poster.jpg': b'plugin poster',
                 video.parent.parent/'fanart.jpg': b'plugin backdrop'}
        for path, content in owned.items():
            path.write_bytes(content)
        writes = list(cloud.writes)
        # Old serialized plans still contain TMDB artwork; repair must ignore it.
        config = json.loads(engine.job(jid)['config'])
        config['repair_only'] = True
        with store.db:
            store.db.execute("UPDATE organize_items SET state='archived' WHERE job_id=?", (jid,))
        engine.update_job(jid, state='queued', config=dumps(config))
        await engine.run_job(jid)
        assert engine.job(jid)['state'] == 'completed'
        assert all(path.read_bytes() == content for path, content in owned.items())
        assert cloud.writes == writes
        assert not any(r.url.host == 'image.tmdb.org' for r in requests)
        assert len(list((tmp_path/'strm').rglob('*.strm'))) == 1
        await http.aclose(); store.close()
    asyncio.run(scenario())


def test_probe_failure_quarantines_original_and_associated_subtitle(tmp_path):
    async def bad(*args):
        raise CloudError('FFprobe 失败。', 'PROBE_FAILED', 422)
    async def scenario():
        store, cloud, http, engine, _ = setup(tmp_path, bad)
        video = cloud.add_video()
        name = video['name']
        sub = cloud.add_video('1001', Path(name).stem + '.zh.srt')
        jid = new_job(store)
        await engine.run_job(jid)
        assert engine.job(jid)['state'] == 'partial'
        assert cloud.nodes['1000']['name'] == name
        dest = cloud.nodes['1000']['parent_id']
        assert dest == '300'
        assert cloud.nodes['1001']['parent_id'] == dest
        assert not list((tmp_path/'strm').rglob('*.strm'))
        assert not [w for w in cloud.writes if w[0] == 'rename']
        await http.aclose(); store.close()
    asyncio.run(scenario())


def test_existing_episode_is_never_replaced(tmp_path):
    async def scenario():
        store, cloud, http, engine, _ = setup(tmp_path)
        cloud.add_video()
        first = new_job(store); await engine.run_job(first)
        old = cloud.nodes['1000'].copy()
        cloud.add_video('1009')
        second = new_job(store); await engine.run_job(second)
        assert cloud.nodes['1000'] == old
        assert cloud.nodes['1009']['parent_id'] == '300'
        assert len(list((tmp_path/'strm').rglob('*.strm'))) == 1
        assert engine.job(second)['state'] == 'partial'
        await http.aclose(); store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', ['lost_response','crash','readback'])
def test_ambiguous_move_recovery_does_not_move_or_rename_twice(tmp_path, failure):
    async def scenario():
        store, cloud, http, engine, _ = setup(tmp_path)
        cloud.add_video()
        def injected():
            if failure == 'lost_response':
                raise CloudError('响应丢失。')
            if failure == 'crash':
                raise asyncio.CancelledError()
            cloud.skim_failure = True
        cloud.after_move = injected
        jid = new_job(store)
        if failure == 'crash':
            with pytest.raises(asyncio.CancelledError):
                await engine.run_job(jid)
        else:
            await engine.run_job(jid)
        if failure != 'lost_response':
            await engine.run_job(jid)
        assert engine.job(jid)['state'] == 'completed'
        assert len([w for w in cloud.writes if w[:2] == ('move','1000')]) == 1
        assert len([w for w in cloud.writes if w[:2] == ('rename','1000')]) == 1
        assert len(list((tmp_path/'strm').rglob('*.strm'))) == 1
        assert store.db.execute('SELECT count(*) FROM playbacks').fetchone()[0] == 1
        await http.aclose(); store.close()
    asyncio.run(scenario())


def test_pause_after_rename_and_resume_at_safe_boundary(tmp_path):
    async def scenario():
        store, cloud, http, engine, _ = setup(tmp_path)
        cloud.add_video()
        jid = new_job(store)
        cloud.after_rename = lambda: engine.update_job(jid, state='paused')
        await engine.run_job(jid)
        assert engine.job(jid)['state'] == 'paused'
        assert not [w for w in cloud.writes if w[0] == 'move']
        assert not list((tmp_path/'strm').rglob('*.strm'))
        engine.update_job(jid, state='queued')
        await engine.run_job(jid)
        assert engine.job(jid)['state'] == 'completed'
        assert len([w for w in cloud.writes if w[0] == 'rename']) == 1
        await http.aclose(); store.close()
    asyncio.run(scenario())


def test_cancel_does_not_publish_or_continue_after_inflight_move(tmp_path):
    async def scenario():
        store, cloud, http, engine, _ = setup(tmp_path)
        cloud.add_video()
        jid = new_job(store)
        cloud.after_move = lambda: engine.update_job(jid, state='cancelled')
        await engine.run_job(jid)
        assert engine.job(jid)['state'] == 'cancelled'
        assert cloud.nodes['1000']['parent_id'] != '100'
        assert store.db.execute('SELECT state FROM organize_items').fetchone()[0] == 'archived'
        assert not list((tmp_path/'strm').rglob('*.strm'))
        await http.aclose(); store.close()
    asyncio.run(scenario())


def test_local_export_failure_recovery_never_repeats_cloud_move(tmp_path, monkeypatch):
    import strimkeep.organizer as module
    original = module.atomic_write
    count = 0
    def fail_once(path, content, root):
        nonlocal count
        count += 1
        if count == 1:
            raise OSError('disk full')
        return original(path, content, root)
    async def scenario():
        store, cloud, http, engine, _ = setup(tmp_path)
        cloud.add_video()
        jid = new_job(store)
        monkeypatch.setattr(module, 'atomic_write', fail_once)
        await engine.run_job(jid)
        assert engine.job(jid)['state'] == 'partial'
        await engine.run_job(jid)
        job = engine.job(jid)
        items = [(i['state'], i['step']) for i in store.db.execute(
            'SELECT * FROM organize_items WHERE job_id=?', (jid,))]
        assert job['state'] == 'completed', (job['stage'], job['notify_state'], items)
        assert len([w for w in cloud.writes if w[0] == 'move']) == 1
        assert store.db.execute('SELECT count(*) FROM playbacks').fetchone()[0] == 1
        await http.aclose(); store.close()
    asyncio.run(scenario())


def test_scan_has_complete_manifest_and_deduplicates_nested_sources(tmp_path):
    async def scenario():
        store, cloud, http, engine, _ = setup(tmp_path)
        await cloud.mkdir('100', 'nested')
        cid = str(cloud.sequence)
        cloud.add_video(parent=cid)
        jid = new_job(store, {**CONFIG, 'source_cids': ['100', cid]})
        await engine.run_job(jid)
        assert store.db.execute('SELECT count(*) FROM organize_items').fetchone()[0] == 1
        assert engine.job(jid)['state'] == 'completed'
        await http.aclose(); store.close()
    asyncio.run(scenario())


def test_overlapping_archive_rejected_before_cloud_mutation(tmp_path):
    async def scenario():
        store, cloud, http, engine, _ = setup(tmp_path)
        await cloud.mkdir('100', 'invalid archive')
        cid = str(cloud.sequence)
        cloud.writes.clear()
        jid = new_job(store, {**CONFIG, 'archive_cid': cid})
        await engine.run_job(jid)
        assert engine.job(jid)['state'] == 'needs_review'
        assert cloud.writes == []
        await http.aclose(); store.close()
    asyncio.run(scenario())


def test_emby_failed_delivery_is_queued_once_without_replaying_submit(tmp_path):
    async def scenario():
        store, cloud, http, engine, _ = setup(tmp_path)
        cloud.add_video()
        calls = []
        def serve(request):
            if request.url.host == 'emby':
                calls.append(request)
                return httpx.Response(503)
            return handler(request)
        await http.aclose()
        engine.http = httpx.AsyncClient(transport=httpx.MockTransport(serve))
        jid = new_job(store)
        await engine.run_job(jid); await engine.emby.drain_outbox(); await engine.emby.tick()
        assert engine.job(jid)['state'] == 'completed' and engine.job(jid)['notify_state'] == 'retrying'
        assert engine.emby.row(jid)['attempts'] == 1
        submitted = lambda: len([r for r in calls if r.url.path.endswith('/Library/Media/Updated')])
        await engine.notify_emby(jid)
        assert submitted() == 1
        engine.update_job(jid, notify_state='unknown')
        await engine.notify_emby(jid)
        assert submitted() == 1
        await engine.http.aclose(); store.close()
    asyncio.run(scenario())


def test_identity_and_naming_do_not_guess_unknown_values():
    assert parse_name('1917 (2019).1080p.WEB-DL.mkv')['query']=='1917'
    assert parse_name('1917.2019.1080p.WEB-DL.mkv')['year']==2019
    assert parse_name('1983 (2018) S01E01.1080p.mkv')['query']=='1983'
    from strimkeep.media import require_single_episode
    hint = require_single_episode(parse_name('测试剧集 (2024) - S01E03 - 4K.mkv'))
    assert hint['episode'] == 3 and hint['end_episode'] == 3
    for name in ('测试剧集 S01E03-E04.2160p.mkv', '测试剧集 S01E03-04.mkv', '测试剧集 S01E03E04.mkv'):
        with pytest.raises(NeedsReview, match='包含多集'):
            require_single_episode(parse_name(name))
    for name in ('测试剧集 第03-04集.mkv', '测试剧集 EP03-EP04.mkv'):
        with pytest.raises(NeedsReview, match='包含多集'):
            require_single_episode(parse_name(name), name)
    hint = parse_name('测试剧集 (2024).S01E03.mkv')
    with pytest.raises(NeedsReview):
        choose_match(hint, [TV, {**TV, 'id': 11}])
    with pytest.raises(NeedsReview):
        choose_match(hint, [{**TV, 'first_air_date': '2023-01-01'}])
    assert classify({**TV, 'origin_country': ['US','CN']}, 'tv') == ('剧集', '其他')
    assert classify({**TV, 'genres': [{'id':16},{'id':10762}]}, 'tv')[0] == '儿童节目'
    media = {**MEDIA, 'dynamic_range': '未知', 'bit_depth': None, 'fps': None}
    _, filename, _, _ = naming(hint, TV, [{'name': '第3集'}], media, '测试剧集 (2024).S01E03.mkv')
    assert '.SDR.' not in filename and '.WEB' not in filename and 'bit' not in filename


def test_exact_alias_recognition_keeps_tmdb_canonical_title(tmp_path):
    async def scenario():
        store, cloud, http, engine, _ = setup(tmp_path)
        cloud.add_video(name='旧中文译名 (2024) S01E03.mkv')
        def serve(request):
            if request.url.path.endswith('/alternative_titles'):
                return httpx.Response(200, json={'results':[{'title':'旧中文译名'}]})
            return handler(request)
        await http.aclose()
        engine.http = httpx.AsyncClient(transport=httpx.MockTransport(serve))
        jid = new_job(store); await engine.run_job(jid)
        assert engine.job(jid)['state'] == 'completed'
        assert cloud.nodes['1000']['name'].startswith('测试剧集.2024.S01E03')
        await engine.http.aclose(); store.close()
    asyncio.run(scenario())


def test_login_failure_preserves_manual_pause_and_same_account_guard(tmp_path):
    async def scenario():
        store, cloud, http, engine, _ = setup(tmp_path)
        jid = new_job(store, {**CONFIG, 'account_id':'1'})
        engine.update_job(jid, state='running')
        store.set_many({'cookie':'UID=2_A1; CID=x; SEID=x'})
        with pytest.raises(UncertainState, match='账号'):
            engine.boundary(jid)
        store.set_many({'cookie':'UID=1_A1; CID=x; SEID=x'})
        engine.update_job(jid, state='paused')
        engine.halt(jid, 'waiting_login', '登录失效')
        engine.login_ready()
        assert engine.job(jid)['state'] == 'paused'
        engine.update_job(jid, state='waiting_login')
        engine.login_ready()
        assert engine.job(jid)['state'] == 'queued'
        await http.aclose(); store.close()
    asyncio.run(scenario())


def test_subtitle_download_failure_exports_video_as_partial(tmp_path):
    async def scenario():
        store, cloud, http, engine, _ = setup(tmp_path)
        video = cloud.add_video()
        cloud.add_video('1001',Path(video['name']).stem+'.chs.srt')
        def serve(request):
            if request.url.host == 'cdn.example':
                return httpx.Response(503)
            return handler(request)
        await http.aclose()
        engine.http = httpx.AsyncClient(transport=httpx.MockTransport(serve))
        jid = new_job(store); await engine.run_job(jid)
        assert engine.job(jid)['state'] == 'partial'
        assert len(list((tmp_path/'strm').rglob('*.strm'))) == 1
        assert cloud.nodes['1000']['parent_id'] == cloud.nodes['1001']['parent_id']
        assert '字幕下载失败' in store.db.execute('SELECT message FROM organize_items').fetchone()[0]
        await engine.http.aclose(); store.close()
    asyncio.run(scenario())


def test_login_expiry_during_subtitle_waits_and_resumes_without_removing_video(tmp_path):
    async def scenario():
        store, cloud, http, engine, _ = setup(tmp_path)
        video=cloud.add_video()
        cloud.add_video('1001',Path(video['name']).stem+'.chs.srt')
        real_url=cloud.url
        expired=False
        async def once(pc,ua):
            nonlocal expired
            if 'Subtitle' in ua and not expired:
                expired=True
                raise CloudError('登录已失效。','LOGIN_INVALID',401)
            return await real_url(pc,ua)
        cloud.url=once
        jid=new_job(store); await engine.run_job(jid)
        assert engine.job(jid)['state']=='waiting_login'
        assert len([w for w in cloud.writes if w[0]=='move'])==2
        engine.login_ready(); await engine.run_job(jid)
        assert engine.job(jid)['state']=='completed'
        assert len([w for w in cloud.writes if w[0]=='move'])==2
        assert len(list((tmp_path/'strm').rglob('*.strm')))==1
        await http.aclose(); store.close()
    asyncio.run(scenario())


def test_pagination_repeated_or_changed_pages_never_silently_complete(tmp_path):
    async def scenario():
        store, _, _, _, _ = setup(tmp_path)
        cloud = Cloud115(store)
        async def page(cid, offset, limit):
            return {'items':[{'id':'1000','name':'test.mkv','is_dir':False}], 'total':2, 'has_more':offset == 0}
        cloud.list_files = page
        with pytest.raises(CloudError, match='分页重复'):
            await cloud.children('100')
        async def changed(cid, offset, limit):
            return {'items':[{'id':str(1000+offset),'name':'test.mkv','is_dir':False}], 'total':2 if offset == 0 else 3, 'has_more':offset == 0}
        cloud.list_files = changed
        assert [e['id'] for e in await cloud.children('100')] == ['1000', '1001']
        await cloud.close(); store.close()
    asyncio.run(scenario())


def test_real_sdk_login_error_is_waiting_login_not_admin_logout(tmp_path):
    async def scenario():
        store = Store(tmp_path,'admin','test-password-1234')
        store.set_many({'cookie':'UID=1_A1; CID=x; SEID=x'})
        cloud = Cloud115(store); await cloud.http.aclose()
        cloud.http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200,json={'state':False,'errno':99,'error':'请重新登录'})))
        with pytest.raises(CloudError) as exc:
            await cloud.list_files(100,0,100)
        assert exc.value.code == 'LOGIN_INVALID'
        await cloud.close(); store.close()
    asyncio.run(scenario())


def test_v01_database_additive_migration_preserves_credentials_and_tokens(tmp_path):
    (tmp_path/'config').mkdir(); (tmp_path/'data').mkdir()
    crypto = Fernet(Fernet.generate_key())
    # Fernet stores the raw urlsafe key in v0.1.0; construct an actual old-schema seed.
    key = Fernet.generate_key(); crypto = Fernet(key)
    (tmp_path/'config/master.key').write_bytes(key)
    salt = b'1234567890123456'
    hashed = hashlib.scrypt(b'original-password-1234',salt=salt,n=16384,r=8,p=1).hex()
    db=sqlite3.connect(tmp_path/'data/state.db')
    db.executescript('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL); CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT NOT NULL); CREATE TABLE playbacks(id TEXT PRIMARY KEY,file_id TEXT NOT NULL,pickcode TEXT NOT NULL,name TEXT NOT NULL,relative_path TEXT NOT NULL,token_hash TEXT NOT NULL,revoked INTEGER NOT NULL DEFAULT 0,created REAL NOT NULL);')
    db.executemany('INSERT INTO meta VALUES(?,?)',[('schema_version','1'),('username','original-admin'),('salt',salt.hex()),('password_hash',hashed)])
    db.execute('INSERT INTO settings VALUES(?,?)',('cookie',crypto.encrypt(json.dumps('UID=1_A1; CID=old; SEID=old').encode()).decode()))
    db.execute('INSERT INTO playbacks VALUES(?,?,?,?,?,?,?,?)',('old-playback','1','pc','old.mkv','old.strm','original-token-hash',0,1))
    db.commit(); db.close()
    store=Store(tmp_path,'replacement-admin','new-password-1234')
    assert store.authenticate('original-admin','original-password-1234')
    assert store.get('cookie')=='UID=1_A1; CID=old; SEID=old'
    assert store.db.execute('SELECT token_hash FROM playbacks').fetchone()[0]=='original-token-hash'
    assert store.meta('schema_version')==SCHEMA_VERSION and store.meta('organize_schema_version')=='1'
    assert store.db.execute('SELECT count(*) FROM organize_jobs').fetchone()[0]==0
    store.close()


def test_missing_scan_counts_do_not_hide_actual_directory_rows(tmp_path):
    async def scenario():
        store=Store(tmp_path,'admin','test-password-1234')
        store.set_many({'cookie':'UID=1_A1; CID=x; SEID=x'})
        cloud=Cloud115(store); await cloud.http.aclose()
        cloud.http=httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={'state':True,'data':[]})))
        assert await cloud.children('100') == []
        await cloud.close(); store.close()
    asyncio.run(scenario())


def test_live_worker_api_and_regenerated_token_survive_output_repair(tmp_path, monkeypatch):
    monkeypatch.setenv('ADMIN_USERNAME','admin'); monkeypatch.setenv('ADMIN_PASSWORD','test-password-1234')
    app = create_app(tmp_path/'data',tmp_path/'strm',MemoryCloud)
    with TestClient(app) as client:
        login = client.post('/api/v1/auth/login',json={'username':'admin','password':'test-password-1234'}).json()
        client.headers['X-CSRF-Token'] = login['csrf']
        client.put('/api/v1/settings',json={'cookie':'UID=1_A1; CID=x; SEID=x','tmdb_key':'key','playback_base':'https://play.example','emby_url':'http://emby:8096','emby_key':'key'})
        app.state.cloud.add_video()
        mock_http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        app.state.organizer.http = mock_http; app.state.organizer.probe = good_probe
        assert client.put('/api/v1/organize/config',json=CONFIG).status_code == 200
        jid = client.post('/api/v1/organize/jobs').json()['id']
        def wait_finished():
            deadline = time.monotonic()+10
            while time.monotonic() < deadline:
                response = client.get(f'/api/v1/organize/jobs/{jid}')
                job = response.json()['job']
                if job['state'] in ('completed','partial','needs_review'):
                    return response
                time.sleep(.02)
            raise AssertionError('worker did not finish')
        response = wait_finished()
        assert response.json()['job']['state'] == 'completed', response.text
        assert 'token_cipher' not in response.text and 'token=' not in response.text
        pid = app.state.store.db.execute('SELECT id FROM playbacks').fetchone()[0]
        client.put('/api/v1/settings',json={'playback_base':'https://new.example'})
        new_url = client.post(f'/api/v1/playbacks/{pid}/regenerate').json()['url']
        assert new_url.startswith('https://new.example/')
        updated = json.loads(app.state.store.db.execute('SELECT plan FROM organize_items WHERE job_id=?',(jid,)).fetchone()[0])
        assert updated['local_files'][updated['relative_path']] == hashlib.sha256((new_url+'\n').encode()).hexdigest()
        writes = list(app.state.cloud.writes)
        assert client.post(f'/api/v1/organize/jobs/{jid}/retry-output').status_code == 200
        response = wait_finished()
        assert response.json()['job']['state'] == 'completed', response.text
        assert app.state.cloud.writes == writes
        # 0.7.12 起发布链路写的是 play115（身份进 STRM，取链不查库）；
        # regenerate 仍返回旧的 /play 地址（历史入口，token 不再进文件），
        # 所以这里按台账里的文件身份算期望值。
        from strimkeep.playaddr import identity_of, play115_url
        source = json.loads(app.state.store.db.execute(
            'SELECT source FROM output_registry WHERE account_id=? AND file_id=?', ('1', '1000')).fetchone()[0])
        assert next((tmp_path/'strm').rglob('*.strm')).read_text().strip() == play115_url(
            'https://new.example', *identity_of(source), source['name'])
        client.portal.call(mock_http.aclose)


def test_real_sdk_mutation_payloads_use_keep_both_and_correct_rename_fields(tmp_path):
    async def scenario():
        store = Store(tmp_path, 'admin', 'test-password-1234')
        store.set_many({'cookie': 'UID=1_A1; CID=fake; SEID=fake'})
        cloud = Cloud115(store)
        await cloud.http.aclose()
        calls = []
        def serve(request):
            calls.append((request.url.path, parse_qs(request.content.decode())))
            return httpx.Response(200, json={'state': True, 'cid': '200'})
        cloud.http = httpx.AsyncClient(transport=httpx.MockTransport(serve), trust_env=False)
        await cloud.rename('123', '新名字.mkv')
        await cloud.move('123', '200')
        await cloud.mkdir('200', 'Season 1')
        assert calls[0] == ('/files/batch_rename', {'files_new_name[123]':['新名字.mkv']})
        assert json.loads(calls[1][1]['conflict_policy'][0]) == {'123': {'action': 'keep_both'}}
        assert calls[1][1]['pid'] == ['200'] and calls[2][1]['cname'] == ['Season 1']
        await cloud.close(); store.close()
    asyncio.run(scenario())


def test_organize_api_validation_controls_and_no_token_echo(tmp_path, monkeypatch):
    monkeypatch.setenv('ADMIN_USERNAME','admin'); monkeypatch.setenv('ADMIN_PASSWORD','test-password-1234')
    app = create_app(tmp_path/'data', tmp_path/'strm', MemoryCloud)
    with TestClient(app) as client:
        login = client.post('/api/v1/auth/login',json={'username':'admin','password':'test-password-1234'}).json()
        client.headers['X-CSRF-Token'] = login['csrf']
        client.put('/api/v1/settings',json={'cookie':'UID=1_A1; CID=x; SEID=x','tmdb_key':'key','playback_base':'https://play.example'})
        assert client.put('/api/v1/organize/config',json={**CONFIG,'archive_cid':'100'}).status_code == 422
        assert client.put('/api/v1/organize/config',json=CONFIG).status_code == 200
        # Stop the loop so control transitions can be inspected deterministically.
        app.state.organizer.closing = True
        job = client.post('/api/v1/organize/jobs').json()
        jid = job['id']
        duplicate = client.post('/api/v1/organize/jobs').json()
        assert duplicate['id'] == jid and duplicate['merged'] is True
        assert client.post(f'/api/v1/organize/jobs/{jid}/pause').json()['state'] == 'paused'
        assert client.post(f'/api/v1/organize/jobs/{jid}/resume').json()['state'] == 'queued'
        response = client.get(f'/api/v1/organize/jobs/{jid}')
        assert 'account_id' not in response.text and 'token_cipher' not in response.text
        assert client.post(f'/api/v1/organize/jobs/{jid}/cancel').json()['state'] == 'cancelled'
        assert client.post(f'/api/v1/organize/jobs/{jid}/resume').status_code == 409

@pytest.mark.parametrize('batch', [False, True])
def test_tmdb_outage_stops_without_quarantining_and_can_retry(tmp_path, batch):
    async def scenario():
        store, cloud, http, engine, _ = setup(tmp_path)
        for i in range(2 if batch else 1):
            cloud.add_video(str(1001+i), f'测试剧集 (2024) S01E0{i+1}.WEB-DL.mkv')
        if batch:
            async def move_many(ids, dest):
                raise AssertionError('TMDB unavailable must not move files')
            cloud.move_many = move_many
        original_identify = engine.identify
        async def unavailable(*args, **kwargs):
            raise CloudError('TMDB 请求失败。', 'TMDB_UNAVAILABLE')
        engine.identify = unavailable
        jid = new_job(store, {**CONFIG, 'retries': 0, 'notify_emby': False, 'ffprobe_enabled': False})
        await engine.run_job(jid)
        assert engine.job(jid)['state'] == 'needs_review'
        assert 'TMDB 请求失败' in engine.job(jid)['stage']
        rows = store.db.execute('SELECT state FROM organize_items WHERE job_id=?', (jid,)).fetchall()
        assert rows and all(row['state'] != 'pending' for row in rows)
        assert not cloud.writes
        engine.identify = original_identify
        with store.db:
            store.db.execute('UPDATE organize_jobs SET updated=0 WHERE id=?', (jid,))
        assert jid in engine.auto_retry_stalled()
        # Use the ordinary per-item route for the recovery verification.
        if batch: del cloud.move_many
        await engine.run_job(jid)
        assert engine.job(jid)['state'] == 'completed', dict(engine.job(jid))
        assert store.db.execute("SELECT count(*) FROM organize_items WHERE job_id=? AND state='done'", (jid,)).fetchone()[0] == len(rows)
        await http.aclose(); store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["verified", "digest_mismatch", "already_exists"])
def test_organize_only_registers_verified_published_subtitle(tmp_path, mode):
    async def scenario():
        store, cloud, http, engine, _ = setup(tmp_path)
        cloud.add_video()
        content = b'1\n00:00:00,000 --> 00:00:01,000\nsubtitle\n'
        sub = cloud.add_video('1001', '测试剧集 (2024) S01E03.WEB-DL.chs.forced.srt')
        sub.update(size=len(content), sha1=hashlib.sha1(content if mode != 'digest_mismatch' else b'different').hexdigest())
        if mode == 'already_exists':
            original_export = engine.export
            async def with_existing(job_id, item_id, original, actual, plan):
                existing = engine.root / plan['relative_path']
                existing = existing.parent / plan['subtitles'][0]['target_name']
                existing.parent.mkdir(parents=True, exist_ok=True)
                existing.write_bytes(b'user existing subtitle')
                await original_export(job_id, item_id, original, actual, plan)
                assert existing.read_bytes() == b'user existing subtitle'
            engine.export = with_existing
        jid = new_job(store)
        await engine.run_job(jid)
        assert engine.job(jid)['state'] == 'completed'
        record = store.db.execute('SELECT source FROM output_registry WHERE file_id=?', ('1000',)).fetchone()
        subtitles = json.loads(record['source']).get('subtitles', [])
        if mode == 'verified':
            assert len(subtitles) == 1
            assert subtitles[0] == {k: cloud.nodes['1001'][k] for k in ('id','name','parent_id','size','sha1')}
        else:
            assert subtitles == []
        await http.aclose(); store.close()
    asyncio.run(scenario())
