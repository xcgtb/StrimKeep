"""第二阶段「统一业务链路」专项测试：U02/U03/U05/U06 的行为契约。"""
import asyncio
import json
import time
from pathlib import Path

import pytest

from strimkeep import share_service
from strimkeep.offline_api import OfflinePostProcess
from test_organize import MemoryCloud, setup, CONFIG, new_job


class ShareCloud(MemoryCloud):
    """在 MemoryCloud 上补分享读取、离线提交接口，供分享管线测试使用。"""

    def __init__(self, store):
        super().__init__(store)
        self.share_tree = {}
        self.offline_rows = []

    async def share_items(self, secret, cid=0):
        return [e.copy() for e in self.share_tree.get(secret['share_code'], {}).get(cid, [])]

    async def share_url_download(self, file_id, share_code, receive_code, ua):
        return 'https://cdn.example/share-' + file_id, {'User-Agent': ua}

    async def offline_add(self, urls, cid=None):
        self.writes.append(('offline_add', tuple(urls), cid))
        return {'state': True}

    async def offline_tasks(self, page=1, size=20):
        return self.offline_rows, 0


def share_setup(tmp_path):
    store, cloud, http, engine, requests = setup(tmp_path)
    # 在 MemoryCloud 实例上补分享读取、离线提交接口，供分享管线测试使用。
    cloud.__class__ = ShareCloud
    cloud.share_tree = {}
    cloud.offline_rows = []
    store.set_many({'organize_config': CONFIG})
    share_service.install(store)
    return store, cloud, http, engine, requests


def add_share_dir(cloud, code, cid, entries):
    cloud.share_tree.setdefault(code, {})[cid] = entries


def test_generate_mirrors_share_dirs_and_never_collides_on_same_name(tmp_path):
    """U02：同分享不同目录的同名文件各自有独立 STRM，互不覆盖；重复提交判重。"""
    async def scenario():
        store, cloud, http, engine, _ = share_setup(tmp_path)
        secret = {'share_code': 'codeA', 'receive_code': 'abcd'}
        videos = [
            {'id': '1', 'name': '第一集.mkv', 'size': 10, 'path': 'Season 01/'},
            {'id': '2', 'name': '第一集.mkv', 'size': 20, 'path': 'Season 02/'},
            {'id': '1', 'name': '第一集.mkv', 'size': 10, 'path': 'Season 01/'},
        ]
        result = share_service.generate(store, engine.root, secret, videos, origin='web')
        assert len(result['created']) == 2 and len(result['skipped']) == 1
        rels = sorted(r['strm_path'] for r in result['created'])
        assert rels == ['分享STRM待整理/Season 01/第一集.mkv.strm',
                        '分享STRM待整理/Season 02/第一集.mkv.strm']
        for rel in rels:
            assert (engine.root / rel).is_file()
        # 再次提交：全部判重。
        again = share_service.generate(store, engine.root, secret, videos[:2], origin='web')
        assert not again['created'] and len(again['skipped']) == 2
        await http.aclose(); store.close()
    asyncio.run(scenario())


def test_share_organize_job_publishes_and_records_evidence(tmp_path):
    """U03/U04：整理在持久化任务里完成；登记表留下 facts/media 证据；海报卡片数据可用。"""
    async def scenario():
        store, cloud, http, engine, requests = share_setup(tmp_path)
        secret = {'share_code': 'codeB', 'receive_code': 'abcd'}
        result = share_service.generate(store, engine.root, secret, [
            {'id': '301', 'name': '测试剧集 (2024) S01E03.1080p.WEB-DL.mkv', 'size': 5000, 'path': ''},
        ], origin='telegram')
        assert len(result['created']) == 1
        store.set_many({'sync_config':{'emby_media_info':True,'generate_nfo':True}})
        jid = share_service.enqueue_organize(engine, origin='telegram')
        await engine.run_job(jid)
        job = engine.job(jid)
        assert job['state'] == 'completed', dict(job)
        row = store.db.execute('SELECT * FROM share_strms WHERE file_id=?', ('301',)).fetchone()
        assert row and row['revoked'] == 0
        assert row['strm_path'].startswith('分享影视库/剧集/欧美剧集/测试剧集 (2024) {tmdb-10}/Season 1/')
        assert row['facts'] and json.loads(row['facts'])['resolution'] == 2160
        assert row['media'] and json.loads(row['media'])['width'] == 3840
        item = store.db.execute('SELECT * FROM organize_items WHERE job_id=?', (jid,)).fetchone()
        assert item and item['state'] == 'done'
        plan = json.loads(item['plan'])
        assert plan['relative_path'] == row['strm_path']
        assert plan['playback_id'] == 'share/' + row['id']
        assert store.db.execute('SELECT 1 FROM emby_outbox WHERE job_id=?',(jid,)).fetchone()
        assert plan['details']['id'] == 10
        assert any(name.endswith('.nfo') for name in plan['metadata_files'])
        assert all((engine.root / name).is_file() for name in plan['metadata_files'])
        if engine.notice_tasks: await asyncio.gather(*engine.notice_tasks)
        poster=store.db.execute("SELECT title,message FROM notifications WHERE job_id=? AND event_key LIKE 'show_done:%'",(jid,)).fetchone()
        assert poster and '新片入库' in poster['title'] and '115网盘分享' in poster['message'] and '入库待核实' in poster['message']
        detail=store.db.execute("SELECT message FROM notifications WHERE job_id=? AND event_key LIKE 'strm:%:group:%:files:created:%'",(jid,)).fetchone()
        assert detail and Path(row['strm_path']).name in detail['message']
        await http.aclose(); store.close()
    asyncio.run(scenario())


def test_share_season_report_is_emitted_before_next_movie(tmp_path):
    async def scenario():
        store,cloud,http,engine,_=share_setup(tmp_path)
        secret={'share_code':'group-share','receive_code':''}
        share_service.generate(store,engine.root,secret,[
            {'id':'301','name':'测试剧集 (2024) S01E01.mkv','size':2*1024**3,'path':''},
            {'id':'302','name':'测试剧集 (2024) S01E02.mkv','size':2*1024**3,'path':''},
            {'id':'303','name':'测试电影 (2024).mkv','size':1024**3,'path':''}])
        identify=engine.identify
        async def checked(hint):
            if hint['media_type']=='movie':
                notices=store.db.execute('SELECT title,message FROM notifications').fetchall()
                assert any(('新片入库' in n['title'] or '整理完成' in n['title']) and '体积：4.00G' in n['message'] for n in notices)
                assert any('STRM 批量结果' in n['title'] and '新增 STRM：2 个' in n['message'] for n in notices)
            return await identify(hint)
        engine.identify=checked
        jid=share_service.enqueue_organize(engine,origin='web')
        await engine.run_job(jid)
        if engine.notice_tasks:await asyncio.gather(*engine.notice_tasks)
        items=store.db.execute('SELECT state FROM organize_items WHERE job_id=?',(jid,)).fetchall()
        assert len(items)==3 and all(r['state']=='done' for r in items)
        await http.aclose();store.close()
    asyncio.run(scenario())


def test_share_wash_requires_evidence_and_same_episode(tmp_path):
    """U05：与转存整理同一规则——证据缺失共存；同集且新版本严格更优才替换。"""
    async def scenario():
        from strimkeep.quality import KEYS, UpgradePolicy, public_policy
        store, cloud, http, engine, _ = share_setup(tmp_path)
        policy = UpgradePolicy.model_validate({'mode': 'upgrade', 'rules': [
            {'key': k, 'enabled': k == 'resolution'} if k == 'resolution' else
            ({'key': k, 'enabled': True, 'order': ['dovi-p7', 'dovi', 'hdr10plus', 'hdr10', 'hlg', 'sdr']}
             if k == 'dynamic_range' else {'key': k, 'enabled': False})
            for k in KEYS]}).model_dump()
        store.set_many({'upgrade_policy': public_policy(policy)})
        secret = {'share_code': 'codeC', 'receive_code': 'abcd'}
        lib_dir = engine.root / '分享影视库' / '剧集' / '欧美剧集' / '测试剧集 (2024) {tmdb-10}' / 'Season 1'
        lib_dir.mkdir(parents=True)
        # 场景一：旧条目有 1080p 事实，新条目探测出 2160p DoVi → 严格更优，替换并撤销旧入口。
        old_stem = '测试剧集.2024.S01E03.1080p.WEB-DL'
        (lib_dir / (old_stem + '.strm')).write_text('http://old/play115/share/old1/测试剧集.2024.S01E03.1080p.WEB-DL.mkv', encoding='utf-8')
        old_facts = {'resolution': 1080, 'codec': 'h264', 'dynamic_range': 'SDR', 'fps': 24,
                     'bit_depth': 8, 'size': 1000, 'audio': [], 'group': 'grp', 'source': 'web-dl',
                     'platform': None, 'platform_ambiguous': False, 'dv_profile': None, 'revision': None}
        with store.db:
            store.db.execute(
                'INSERT OR REPLACE INTO share_strms(id,share_code,receive_code,file_id,pickcode,name,strm_path,token_hash,revoked,created,media,facts,origin,size) '
                "VALUES('old1','codeC','abcd','999','','%s','%s','t',0,?,NULL,?,'monitor',0)"
                % (old_stem, str((lib_dir / (old_stem + '.strm')).relative_to(engine.root)).replace('\\', '/')),
                (time.time(), json.dumps(old_facts)))
        share_service.generate(store, engine.root, secret, [
            {'id': '302', 'name': '测试剧集 (2024) S01E03.2160p.DV.WEB-DL.mkv', 'size': 9000, 'path': ''}], origin='web')
        jid = share_service.enqueue_organize(engine, origin='web')
        await engine.run_job(jid)
        old_row = store.db.execute("SELECT * FROM share_strms WHERE id='old1'").fetchone()
        assert old_row['revoked'] == 1, dict(old_row)
        assert not (engine.root / old_row['strm_path']).exists()
        new_row = store.db.execute("SELECT * FROM share_strms WHERE file_id='302'").fetchone()
        assert new_row and new_row['revoked'] == 0 and new_row['strm_path'].startswith('分享影视库/')
        changes = {(r['action'],r['path']) for r in store.db.execute('SELECT action,path FROM strm_changes WHERE job_id=?', (jid,))}
        assert ('removed', old_row['strm_path']) in changes and ('updated', new_row['strm_path']) in changes
        await engine.notifications.media_report(engine.job(jid))
        notices = [r['message'] for r in store.db.execute("SELECT message FROM notifications WHERE job_id=? AND event_key LIKE ?", (jid, 'strm:%:files:removed:%'))]
        assert any(old_stem in notice for notice in notices)
        # 场景二：规范名完全相同的条目（同集同规格）→ 保留已有，放弃新条目。
        share_service.generate(store, engine.root, secret, [
            {'id': '303', 'name': '测试剧集 (2024) S01E03.2160p.DV.WEB-DL.mkv', 'size': 9000, 'path': ''}], origin='web')
        jid2 = share_service.enqueue_organize(engine, origin='web')
        await engine.run_job(jid2)
        assert store.db.execute("SELECT revoked FROM share_strms WHERE file_id='303'").fetchone() is None
        assert store.db.execute("SELECT revoked FROM share_strms WHERE file_id='302'").fetchone()[0] == 0
        # 场景三：旧条目无探测记录，但名称明确 1080p；新 2160p 按分辨率胜出。
        (lib_dir / '测试剧集.2024.S01E04.1080p.WEB-DL.strm').write_text('http://old/play115/share/old2/测试剧集.2024.S01E04.1080p.WEB-DL.mkv', encoding='utf-8')
        with store.db:
            store.db.execute(
                "INSERT OR REPLACE INTO share_strms(id,share_code,receive_code,file_id,pickcode,name,strm_path,token_hash,revoked,created,media,facts,origin,size) "
                "VALUES('old2','codeC','abcd','998','','测试剧集.2024.S01E04.1080p.WEB-DL','分享影视库/剧集/欧美剧集/测试剧集 (2024) {tmdb-10}/Season 1/测试剧集.2024.S01E04.1080p.WEB-DL.strm','t',0,?,NULL,NULL,'monitor',0)",
                (time.time(),))
        share_service.generate(store, engine.root, secret, [
            {'id': '304', 'name': '测试剧集 (2024) S01E04.2160p.DV.WEB-DL.mkv', 'size': 9000, 'path': ''}], origin='web')
        jid3 = share_service.enqueue_organize(engine, origin='web')
        await engine.run_job(jid3)
        old2 = store.db.execute("SELECT * FROM share_strms WHERE id='old2'").fetchone()
        assert old2['revoked'] == 1 and not (engine.root / old2['strm_path']).exists()
        assert store.db.execute("SELECT revoked FROM share_strms WHERE file_id='304'").fetchone()[0] == 0
        await http.aclose(); store.close()
    asyncio.run(scenario())


def test_offline_submission_processes_only_its_own_batch(tmp_path):
    """U06：提交登记 → 完成核查 → 只整理本批产物；响应丢失不自动重复提交。"""
    async def scenario():
        store, cloud, http, engine, _ = share_setup(tmp_path)
        cloud.add_video()  # 目录里已有的旧资源
        post = engine.offline
        magnet = 'magnet:?xt=urn:btih:' + 'a' * 40
        sid = await post.submit([magnet], '100', '待整理')
        assert cloud.writes[-1][0] == 'offline_add'
        # 响应丢失场景：不重新提交，转 needs_review。
        async def fail_add(urls, cid=None):
            raise Exception('timeout')
        original_add = cloud.offline_add
        cloud.offline_add = fail_add
        try:
            # 换一个链接：同一链接会被「已在离线任务中」直接拒绝（0.9.33 起的口径）。
            await post.submit(['magnet:?xt=urn:btih:' + 'c' * 40], '100', '待整理')
        except Exception:
            pass
        rows = store.db.execute('SELECT * FROM offline_submissions ORDER BY id').fetchall()
        assert rows[-1]['state'] == 'needs_review'
        cloud.offline_add = original_add
        # 下载完成：任务列表显示 100%，目录出现新文件 → 只整理本批。
        cloud.offline_rows = {'data': [{'info_hash': 'a' * 40, 'file_id': '100', 'delete_file_id': '1001', 'name': 'batch', 'percentDone': 100,
                                        'status_text': '已完成', 'url': ''}]}
        cloud.add_video('1001', '测试剧集 (2024) S01E05.WEB-DL.mkv')
        await post.tick()
        row = store.db.execute('SELECT * FROM offline_submissions WHERE id=?', (sid,)).fetchone()
        assert row['state'] == 'completed', dict(row)
        jid = row['organize_job_id']
        config = json.loads(engine.job(jid)['config'])
        assert config['only_file_ids'] == ['1001'] and config['cleanup_disabled'] is True
        await engine.run_job(jid)
        managed = store.db.execute('SELECT file_id FROM organized_files').fetchall()
        assert [r[0] for r in managed] == ['1001']
        await http.aclose(); store.close()
    asyncio.run(scenario())


def test_channel_job_and_claim_commit_together(tmp_path):
    from strimkeep.channel_monitor import ChannelMonitor
    store, cloud, http, engine, _ = share_setup(tmp_path)
    ChannelMonitor(engine)
    secret = {'share_code': 'atomic', 'receive_code': ''}
    with store.db:
        store.db.execute('INSERT INTO channel_shares(share_code,name,message_id,created) VALUES(?,?,?,?)',
                         ('atomic', 'testchan', 101, 1))
    jid = share_service.enqueue_organize(engine, origin='channel', secret=secret, channel_claim=('testchan', 101))
    assert store.db.execute('SELECT job_id FROM channel_shares WHERE share_code=?', ('atomic',)).fetchone()[0] == jid
    before = store.db.execute('SELECT COUNT(*) FROM organize_jobs').fetchone()[0]
    with pytest.raises(ValueError):
        share_service.enqueue_organize(engine, origin='channel', secret=secret, channel_claim=('testchan', 101))
    assert store.db.execute('SELECT COUNT(*) FROM organize_jobs').fetchone()[0] == before
    asyncio.run(http.aclose())
    store.close()


def test_unidentified_collision_keeps_both_and_scheduled_retry_is_deferred(tmp_path):
    from strimkeep.media import NeedsReview
    async def scenario():
        store, cloud, http, engine, _ = share_setup(tmp_path)
        share_service.generate(store, engine.root, {'share_code':'hold','receive_code':''}, [
            {'id':'801','name':'测试剧集 (2024) S01E01.mkv','size':5000,'path':''}])
        row = store.db.execute('SELECT * FROM share_strms WHERE file_id=?',('801',)).fetchone()
        pending = engine.root / row['strm_path']
        _, _, unident = share_service.directories(engine.root)
        target, alternate = share_service._unidentified_targets(unident, row)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('existing'); alternate.write_text('other existing')
        contents=pending.read_text();calls=[]
        async def failed(hint):
            calls.append(hint['query'])
            raise NeedsReview('TMDB 未找到唯一匹配')
        engine.identify=failed
        first=share_service.enqueue_organize(engine,origin='web');await engine.run_job(first)
        assert pending.read_text()==contents and target.read_text()=='existing'
        assert alternate.read_text()=='other existing'
        assert store.db.execute('SELECT strm_path FROM share_strms WHERE file_id=?',('801',)).fetchone()[0]==row['strm_path']
        second=share_service.enqueue_organize(engine,origin='schedule');await engine.run_job(second)
        assert len(calls)==1
        # Once the natural name becomes free, the alternate hold must not
        # suppress the next attempt, and the other file remains unchanged.
        target.unlink()
        third=share_service.enqueue_organize(engine,origin='schedule');await engine.run_job(third)
        assert len(calls)==2 and target.read_text()==contents and not pending.exists()
        assert alternate.read_text()=='other existing'
        await http.aclose();store.close()
    asyncio.run(scenario())


def test_share_tmdb_outage_preserves_pending_for_retry(tmp_path):
    from strimkeep.cloud115 import CloudError
    async def scenario():
        store, cloud, http, engine, _ = share_setup(tmp_path)
        share_service.generate(store, engine.root, {'share_code':'network','receive_code':''}, [
            {'id':'802','name':'测试剧集 (2024) S01E01.mkv','size':5000,'path':''}])
        row=store.db.execute('SELECT * FROM share_strms WHERE file_id=?',('802',)).fetchone()
        async def unavailable(hint):raise CloudError('TMDB unavailable','TMDB_UNAVAILABLE')
        engine.identify=unavailable
        jid=share_service.enqueue_organize(engine,origin='web');await engine.run_job(jid)
        assert engine.job(jid)['state']=='needs_review'
        assert (engine.root / row['strm_path']).is_file()
        assert store.db.execute('SELECT strm_path FROM share_strms WHERE file_id=?',('802',)).fetchone()[0]==row['strm_path']
        await http.aclose();store.close()
    asyncio.run(scenario())


def test_share_season_reuses_unique_series_identity_but_checks_each_episode(tmp_path):
    async def scenario():
        store,cloud,http,engine,_=share_setup(tmp_path)
        share_service.generate(store,engine.root,{'share_code':'identity','receive_code':''},[
            {'id':'901','name':'测试剧集 (2024) S01E01.mkv','size':5000,'path':''},
            {'id':'902','name':'测试剧集 (2024) S01E02.mkv','size':5000,'path':''}])
        original=engine.identify; calls=[]
        async def counted(hint):
            calls.append(hint['episode']);return await original(hint)
        engine.identify=counted
        jid=share_service.enqueue_organize(engine,origin='web');await engine.run_job(jid)
        states=[r['state'] for r in store.db.execute('SELECT state FROM organize_items WHERE job_id=?',(jid,))]
        assert states==['done','done'] and calls==[1]
        await http.aclose();store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('case', ['verified_match', 'different_sha', 'changed_after_probe'])
def test_share_media_cache_requires_live_sha_and_size(tmp_path, case):
    from test_organize import MEDIA
    async def scenario():
        store,cloud,http,engine,_=share_setup(tmp_path)
        secret={'share_code':'cross','receive_code':''};name='测试剧集 (2024) S01E01.mkv'
        video={'id':'951','name':name,'size':5000,'path':'','sha1':'A'*40}
        actual_sha='B'*40 if case=='different_sha' else 'A'*40
        cloud.share_tree={'cross':{'0':[{'id':'951','name':name,'size':5000,'is_dir':False,'sha1':actual_sha}]}}
        if case!='changed_after_probe':
            engine.measurements.put('1',{'id':'old','sha1':'A'*40,'size':5000},MEDIA,name)
        if case=='changed_after_probe':
            read=cloud.share_items;calls=[]
            async def changed(secret,cid=0):
                calls.append(1);rows=await read(secret,cid)
                if len(calls)>1:
                    for r in rows:r['sha1']='B'*40
                return rows
            cloud.share_items=changed
        probes=[]
        async def probe(*args):probes.append(1);return dict(MEDIA)
        engine.probe=probe
        share_service.generate(store,engine.root,secret,[video])
        jid=share_service.enqueue_organize(engine,origin='web');await engine.run_job(jid)
        assert engine.job(jid)['state']=='completed',dict(engine.job(jid))
        assert len(probes)==(0 if case=='verified_match' else 1)
        if case=='different_sha':
            assert store.db.execute('SELECT 1 FROM media_by_sha WHERE account_id=? AND sha1=? AND size=?',('1','B'*40,5000)).fetchone()
        if case=='changed_after_probe':
            assert not store.db.execute('SELECT 1 FROM media_by_sha WHERE account_id=? AND sha1=?',('1','A'*40)).fetchone()
        await http.aclose();store.close()
    asyncio.run(scenario())


def test_share_cache_lookup_rate_limit_halts_without_guessing(tmp_path):
    from strimkeep.cloud115 import CloudError
    async def scenario():
        store,cloud,http,engine,_=share_setup(tmp_path)
        secret={'share_code':'rate','receive_code':''};name='测试剧集 (2024) S01E01.mkv'
        share_service.generate(store,engine.root,secret,[{'id':'961','name':name,'size':5000,'path':''}])
        row=store.db.execute('SELECT strm_path FROM share_strms WHERE file_id=?',('961',)).fetchone()
        async def limited(*args):raise CloudError('rate limited','RATE_LIMITED')
        cloud.share_items=limited
        jid=share_service.enqueue_organize(engine,origin='web');await engine.run_job(jid)
        assert engine.job(jid)['state']=='needs_review'
        assert (engine.root / row['strm_path']).is_file()
        assert store.db.execute('SELECT strm_path FROM share_strms WHERE file_id=?',('961',)).fetchone()[0]==row['strm_path']
        await http.aclose();store.close()
    asyncio.run(scenario())


def test_offline_folder_output_keeps_descendants_in_scoped_manifest(tmp_path):
    async def scenario():
        store, cloud, http, engine, _ = share_setup(tmp_path)
        post = engine.offline
        magnet = 'magnet:?xt=urn:btih:' + 'b' * 40
        sid = await post.submit([magnet], '100', '待整理')
        cloud.nodes['1500'] = {'id':'1500','name':'batch','parent_id':'100','is_dir':True}
        cloud.add_video('1501', '测试剧集 (2024) S01E05.WEB-DL.mkv', parent='1500')
        cloud.offline_rows = {'data': [{'info_hash':'b'*40, 'file_id':'100', 'delete_file_id':'1500', 'name':'batch',
                                        'percentDone':100, 'status_text':'已完成'}]}
        await post.tick()
        row = store.db.execute('SELECT * FROM offline_submissions WHERE id=?', (sid,)).fetchone()
        assert row['state'] == 'completed'
        jid = row['organize_job_id']
        config = json.loads(engine.job(jid)['config'])
        assert [e['id'] for e in config['source_entries']] == ['1500']
        assert not config.get('only_file_ids')
        await engine.run_job(jid)
        assert store.db.execute("SELECT 1 FROM organized_files WHERE file_id='1501'").fetchone()
        await http.aclose(); store.close()
    asyncio.run(scenario())

def test_matching_queued_offline_batches_coalesce_only_when_scope_matches(tmp_path):
    async def scenario():
        store, cloud, http, engine, _ = share_setup(tmp_path)
        post = engine.offline
        with store.db:
            ids = [store.db.execute("INSERT INTO offline_submissions(urls,target_cid,target_label,before_ids,submitted_at,state,message) VALUES('[]','100','待整理','[]',0,'submitted','')").lastrowid for _ in range(3)]
        rows = [store.db.execute('SELECT * FROM offline_submissions WHERE id=?', (sid,)).fetchone() for sid in ids]
        first = {'id':'1001', 'name':'a.mkv', 'is_dir':False}
        second = {'id':'1002', 'name':'b.mkv', 'is_dir':False}
        await post._enqueue_organize(rows[0], [first])
        await post._enqueue_organize(rows[1], [first])
        await post._enqueue_organize(rows[2], [second])
        jobs = [store.db.execute('SELECT organize_job_id FROM offline_submissions WHERE id=?', (sid,)).fetchone()[0] for sid in ids]
        assert jobs[0] == jobs[1] and jobs[2] != jobs[0]
        assert store.db.execute('SELECT count(*) FROM organize_jobs').fetchone()[0] == 2
        await http.aclose(); store.close()
    asyncio.run(scenario())


def test_share_poster_explains_skipped_episode_in_same_batch(tmp_path,monkeypatch):
    async def scenario():
        store,cloud,http,engine,_=share_setup(tmp_path)
        secret={'share_code':'batch-outcomes','receive_code':''}
        share_service.generate(store,engine.root,secret,[
            {'id':'201','name':'测试剧集 (2024) S01E01.mkv','size':1000,'path':''},
            {'id':'202','name':'测试剧集 (2024) S01E02.mkv','size':1000,'path':''},
            {'id':'203','name':'测试剧集 (2024) S01E03.mkv','size':1000,'path':''}])
        def wash(_engine,_job,row,*args):return 'skip' if 'S01E02' in row['name'] else 'publish'
        monkeypatch.setattr(share_service,'_wash',wash)
        jid=share_service.enqueue_organize(engine,origin='web');await engine.run_job(jid)
        if engine.notice_tasks:await asyncio.gather(*engine.notice_tasks)
        card=store.db.execute("SELECT message FROM notifications WHERE job_id=? AND event_key LIKE 'show_done:%'",(jid,)).fetchone()
        assert card and '本批：S01 E01、E03' in card['message']
        assert '保留已有：E02' in card['message'] and '缺集' not in card['message']
        await http.aclose();store.close()
    asyncio.run(scenario())
