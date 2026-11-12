"""深度删除联动：事件解析、台账匹配、预演/执行与安全边界。"""
import asyncio
import hashlib
import json
import time
from pathlib import Path
from types import SimpleNamespace

from strimkeep.deep_delete import apply, drain, enqueue, guarded_managed_delete, history, match_targets, parse_event
from strimkeep.store import Store
from strimkeep.share_service import install as install_share


class FakeCloud:
    def __init__(self):
        self.recycled = []

    async def file(self, fid):
        return {'id': fid, 'parent_id': '77'}

    async def recycle(self, fid, parent):
        self.recycled.append((fid, parent))


class FakeEngine:
    def __init__(self, store, root):
        self.store = store
        self.root = Path(root)
        self.cloud = FakeCloud()


def make_store(tmp_path):
    store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
    install_share(store)
    with store.db:
        store.db.execute("INSERT INTO playbacks(id,file_id,pickcode,name,relative_path,token_hash,created)"
                         " VALUES('p1','3001','pc','剧名','影视媒体库/剧集/国产剧集/剧名 (2024) {tmdb-10}/Season 1/"
                         "剧名.2024.S01E01.mkv','deadbeef',1)")
        store.db.execute("INSERT INTO organized_files(file_id,job_id,item_id,playback_id,relative_path,metadata)"
                         " VALUES('3001',1,1,'p1','影视媒体库/剧集/国产剧集/剧名 (2024) {tmdb-10}/Season 1/"
                         "剧名.2024.S01E01.mkv','{}')")
        store.db.execute("INSERT OR IGNORE INTO share_strms(id,share_code,receive_code,file_id,pickcode,"
                         "name,strm_path,token_hash,created) VALUES('s1','swabc','abcd','3002','pc',"
                         "'分享电影','分享影视库/电影/分享电影.strm','',1)")
    return store


MANAGED_REL = '影视媒体库/剧集/国产剧集/剧名 (2024) {tmdb-10}/Season 1/剧名.2024.S01E01.mkv'
MANAGED_CONTENT = b'http://play'


def add_managed_output(store, rel=MANAGED_REL, content=MANAGED_CONTENT):
    """补上受管输出登记（output_registry + owned_outputs），供真删核查走通。"""
    digest = hashlib.sha256(content).hexdigest()
    source = {'id': '3001', 'name': '剧名.2024.S01E01.mkv', 'parent_id': '77',
              'size': len(content), 'sha1': 'a' * 40}
    with store.db:
        store.db.execute(
            'INSERT INTO output_registry(account_id,file_id,source,trail,playback_id,'
            'token_cipher,relative_path,files,active,updated) VALUES(?,?,?,?,?,?,?,?,?,?)',
            ('1', '3001', json.dumps(source, ensure_ascii=False), '[]', 'p1', '',
             rel, json.dumps({rel: digest}, ensure_ascii=False), 1, 1))
        store.db.execute(
            'INSERT INTO owned_outputs(relative_path,account_id,file_id,digest) VALUES(?,?,?,?)',
            (rel, '1', '3001', digest))
    return source


def test_parse_event_accepts_delete_events_with_known_types():
    ok = parse_event({'Event': 'deep.delete', 'Type': 'Episode',
                      'Path': '/media/影视媒体库/剧集/国产剧集/剧名 (2024) {tmdb-10}/Season 1/剧名.2024.S01E01.mkv'})
    assert ok and ok['type'] == 'Episode' and ok['path'].endswith('S01E01.mkv')
    ok = parse_event({'event': 'library.deleted', 'ItemType': 'Movie', 'path': '/media/影视媒体库/电影/X.strm'})
    assert ok and ok['type'] == 'Movie'


def test_parse_event_rejects_unknown_types_and_non_delete():
    assert parse_event({'Event': 'library.updated', 'Type': 'Movie', 'Path': '/x.strm'}) is None
    assert parse_event({'Event': 'deep.delete', 'Type': 'Folder', 'Path': '/x'}) is None
    assert parse_event({'Event': 'deep.delete', 'Type': 'Episode'}) is None          # 没有路径
    assert parse_event('not a dict') is None


def test_file_event_matches_by_suffix(tmp_path):
    store = make_store(tmp_path)
    event = {'type': 'Episode', 'path': '/media/影视媒体库/剧集/国产剧集/剧名 (2024) {tmdb-10}/Season 1/剧名.2024.S01E01.mkv'}
    targets = match_targets(store, event)
    assert len(targets['managed']) == 1 and targets['managed'][0]['file_id'] == '3001'
    assert targets['share'] == []
    store.close()


def test_directory_event_needs_evidence(tmp_path):
    store = make_store(tmp_path)
    show = '/media/影视媒体库/剧集/国产剧集/剧名 (2024) {tmdb-10}'
    assert match_targets(store, {'type': 'Series', 'path': show})['managed']
    assert match_targets(store, {'type': 'Series', 'path': '/media/影视媒体库/剧集/国产剧集/不存在剧名'})['managed'] == []
    store.close()


def test_generic_directory_name_never_sweeps_other_shows(tmp_path):
    """`Season 1` 这种通用目录名不能横扫其它剧：目录事件必须整条路径对齐。"""
    store = make_store(tmp_path)
    assert match_targets(store, {'type': 'Season', 'path': '/media/Season 1'})['managed'] == []
    assert not match_targets(store, {'type': 'Season', 'path': '/media/影视媒体库'})['managed'], \
        '库根不是特定季，不能扩大深删范围'
    hit = match_targets(store, {'type': 'Season',
                                'path': '/media/影视媒体库/剧集/国产剧集/剧名 (2024) {tmdb-10}/Season 1'})
    assert len(hit['managed']) == 1 and hit['managed'][0]['file_id'] == '3001'
    store.close()


def test_share_strm_matches_only_local_side(tmp_path):
    store = make_store(tmp_path)
    targets = match_targets(store, {'type': 'Movie', 'path': '/media/分享影视库/电影/分享电影.strm'})
    assert targets['share'] and targets['share'][0]['id'] == 's1'
    store.close()


def test_scan_style_tmdb_without_path_never_matches(tmp_path):
    """仅 TMDB 号缺乏删除范围，必须保守不匹配。"""
    store = make_store(tmp_path)
    payload = {'Event': 'library.deleted',
               'Title': '已删除了 NAS 中的 剧名 (2024) {tmdb-10}',
               'Item': {'Name': '剧名 (2024) {tmdb-10}', 'ProviderIds': {'Tmdb': '10'}}}
    event = parse_event(payload)
    assert event and event['tmdb'] == '10' and event['path'] == '', event
    targets = match_targets(store, event)
    assert not targets['managed'] and not targets['share'], '仅有 TMDB 号无法确认单集或整部的删除范围'
    # 台账里没有的 tmdb 号：一条都不许命中
    other = match_targets(store, parse_event({'Event': 'library.deleted',
                                              'Item': {'Name': 'x', 'ProviderIds': {'Tmdb': '999999'}}}))
    assert other['managed'] == [] and other['share'] == []
    store.close()


def test_event_without_path_or_tmdb_is_rejected(tmp_path):
    """既没有路径也没有 tmdb 号：拒绝，宁可不删。"""
    assert parse_event({'Event': 'library.deleted', 'Item': {'Name': '剧名'}}) is None
    assert parse_event({'Event': 'library.deleted', 'Item': {'ProviderIds': {'Tmdb': 'abc'}}}) is None
    store = make_store(tmp_path)
    store.close()


def test_selftest_is_readonly_and_reports_matches(tmp_path):
    """面板「链路自检」：只读、用真实台账路径、通用目录名必须为 0 命中。"""
    from strimkeep.deep_delete import selftest
    store = make_store(tmp_path)
    report = selftest(store)
    assert report['cases'], '台账里有条目时必须给出用例'
    assert all(case['managed'] + case['share'] >= 1 for case in report['cases']), report['cases']
    assert report['generic_directory_matched'] == 0
    # 只读：台账一条都不能少
    assert store.db.execute('SELECT count(*) FROM organized_files').fetchone()[0] == 1
    assert store.db.execute('SELECT count(*) FROM share_strms').fetchone()[0] == 1
    store.close()


class ReadingCloud(FakeCloud):
    """带目录回读的假云盘：用来验证「删完到底还在不在」。"""

    def __init__(self, keep=False):
        super().__init__()
        self.keep = keep
        self.reads = []

    async def children(self, cid):
        self.reads.append(cid)
        return [{'id': '3001', 'name': 'still-here'}] if self.keep else []


class GuardedCloud(FakeCloud):
    """带回收站回读与存续检查的假云盘，供 guarded_managed_delete 使用。"""

    def __init__(self, source, keep=False):
        super().__init__()
        self.source = source
        self.keep = keep
        self.recycle_bin = []
        self.live = {source['id']: True}

    async def file(self, fid):
        return dict(self.source)

    async def recycle(self, fid, parent):
        self.recycled.append((fid, parent))
        if self.keep:
            self.live[fid] = True
            return
        self.live[fid] = False
        self.recycle_bin.append({
            'id': '', 'rid': 'rid-%d' % (len(self.recycle_bin) + 1),
            'name': self.source['name'], 'size': self.source['size'],
            'parent_id': self.source['parent_id'], 'is_dir': False,
            'deleted_at': time.time()})

    def recycle_snapshot(self):
        return list(self.recycle_bin)

    async def recycle_files(self):
        return list(self.recycle_bin)

    async def live_file_exists(self, fid):
        return self.live.get(fid, False)



def test_guarded_delete_confirms_recycle_and_purges_ledger(tmp_path):
    """真删托管文件必须先回读回收站确认，才删本地并清台账。"""
    async def run():
        store = make_store(tmp_path)
        source = add_managed_output(store)
        store.set_many({'sync_config': {'deep_delete_dry_run': False}})
        engine = FakeEngine(store, tmp_path / 'strm')
        engine.index = SimpleNamespace(account=lambda config: '1')
        engine.cloud = GuardedCloud(source)
        strm = engine.root / MANAGED_REL
        strm.parent.mkdir(parents=True)
        strm.write_bytes(MANAGED_CONTENT)
        state, message, _ = await guarded_managed_delete(
            engine, {'file_id': '3001', 'playback_id': 'p1', 'relative_path': MANAGED_REL})
        assert state == 'done' and '网盘文件离开活动空间' in message, (state, message)
        assert engine.cloud.recycled == [('3001', '77')]
        assert not strm.exists()
        assert store.db.execute('SELECT revoked FROM playbacks WHERE id=?', ('p1',)).fetchone()[0] == 1
        assert store.db.execute('SELECT count(*) FROM organized_files WHERE file_id=?', ('3001',)).fetchone()[0] == 0
        assert store.db.execute('SELECT count(*) FROM output_registry WHERE file_id=? AND active=1', ('3001',)).fetchone()[0] == 0
        store.close()

    asyncio.run(run())


def test_guarded_delete_keeps_local_when_recycle_unconfirmed(tmp_path):
    """回收结果未确认（文件仍在）时，必须保留本地 STRM 与台账。"""
    async def run():
        store = make_store(tmp_path)
        source = add_managed_output(store)
        store.set_many({'sync_config': {'deep_delete_dry_run': False}})
        engine = FakeEngine(store, tmp_path / 'strm')
        engine.index = SimpleNamespace(account=lambda config: '1')
        engine.cloud = GuardedCloud(source, keep=True)
        strm = engine.root / MANAGED_REL
        strm.parent.mkdir(parents=True)
        strm.write_bytes(MANAGED_CONTENT)
        state, message, _ = await guarded_managed_delete(
            engine, {'file_id': '3001', 'playback_id': 'p1', 'relative_path': MANAGED_REL})
        assert state == 'failed' and '尚未确认' in message, (state, message)
        assert strm.exists()
        assert store.db.execute('SELECT revoked FROM playbacks WHERE id=?', ('p1',)).fetchone()[0] == 0
        store.close()

    asyncio.run(run())


def test_inventory_adapter_calls_guarded_cloud_recycle(tmp_path):
    from strimkeep.inventory_delete_adapter import delete_one
    from strimkeep.inventory_snapshot import _fingerprint
    from strimkeep.inventory_sources import source_fingerprints
    async def run():
        store = make_store(tmp_path)
        source = add_managed_output(store)
        engine = FakeEngine(store, tmp_path / 'strm')
        engine.index = SimpleNamespace(account=lambda config: '1')
        engine.cloud = GuardedCloud(source)
        engine.publication_gate = asyncio.Lock()
        target = engine.root / MANAGED_REL
        target.parent.mkdir(parents=True)
        target.write_bytes(MANAGED_CONTENT)
        evidence = {'side': 'cloud', 'id': '3001', 'path': MANAGED_REL,
                    **_fingerprint(engine.root, MANAGED_REL)}
        identity = source_fingerprints(store.db, [('cloud', '3001', MANAGED_REL)], '1')[0]
        async with engine.publication_gate:
            assert await delete_one(engine, evidence, identity)
        assert not target.exists() and engine.cloud.recycled == [('3001', '77')]
        store.close()
    asyncio.run(run())


def test_inventory_adapter_preserves_local_when_cloud_recycle_unconfirmed(tmp_path):
    from strimkeep.inventory_delete_adapter import delete_one
    from strimkeep.inventory_snapshot import _fingerprint
    from strimkeep.inventory_sources import source_fingerprints
    async def run():
        store = make_store(tmp_path)
        source = add_managed_output(store)
        engine = FakeEngine(store, tmp_path / 'strm')
        engine.index = SimpleNamespace(account=lambda config: '1')
        engine.cloud = GuardedCloud(source, keep=True)
        engine.publication_gate = asyncio.Lock()
        target = engine.root / MANAGED_REL
        target.parent.mkdir(parents=True)
        target.write_bytes(MANAGED_CONTENT)
        evidence = {'side': 'cloud', 'id': '3001', 'path': MANAGED_REL,
                    **_fingerprint(engine.root, MANAGED_REL)}
        identity = source_fingerprints(store.db, [('cloud', '3001', MANAGED_REL)], '1')[0]
        async with engine.publication_gate:
            assert not await delete_one(engine, evidence, identity)
        assert target.exists()
        assert store.db.execute('SELECT count(*) FROM organized_files WHERE file_id=?', ('3001',)).fetchone()[0] == 1
        store.close()
    asyncio.run(run())


def test_history_groups_batches_with_counts(tmp_path):
    """历史列表：按批次汇总条数与结果，明细能逐条翻。"""
    async def run():
        store = make_store(tmp_path)
        source = add_managed_output(store)
        store.set_many({'sync_config': {'deep_delete_dry_run': False}})
        engine = FakeEngine(store, tmp_path / 'strm')
        engine.index = SimpleNamespace(account=lambda config: '1')
        engine.cloud = GuardedCloud(source)
        strm = engine.root / MANAGED_REL
        strm.parent.mkdir(parents=True)
        strm.write_bytes(MANAGED_CONTENT)
        targets = match_targets(store, {'type': 'Episode', 'path': str(strm)})
        enqueue(store, targets, dry_run=False, source='emby:/media/x')
        awaiting = history(store, limit=10)
        assert awaiting['batches'][0]['queued'] == 1, awaiting
        await drain(engine, limit=5)
        data = history(store, limit=10)
        assert len(data['batches']) == 1
        batch = data['batches'][0]
        assert batch['mode'] == '执行' and batch['total'] == 1 and batch['done'] == 1
        assert batch['skipped'] == 0 and batch['failed'] == 0 and batch['queued'] == 0
        assert batch['source'] == 'emby:/media/x'
        detail = batch['details'][0]
        assert detail['ok'] is True and '网盘文件离开活动空间' in detail['actions'][0], detail
        store.close()

    asyncio.run(run())


def test_webhook_enqueues_then_drains(tmp_path):
    """命中只入队（立刻返回），后台 drain 按序执行并回读确认。"""
    async def run():
        store = make_store(tmp_path)
        source = add_managed_output(store)
        store.set_many({'sync_config': {'deep_delete_dry_run': False}})
        engine = FakeEngine(store, tmp_path / 'strm')
        engine.index = SimpleNamespace(account=lambda config: '1')
        engine.cloud = GuardedCloud(source)
        strm = engine.root / MANAGED_REL
        strm.parent.mkdir(parents=True)
        strm.write_bytes(MANAGED_CONTENT)
        targets = match_targets(store, {'type': 'Episode', 'path': str(strm)})
        batch, queued = enqueue(store, targets, dry_run=False, source='test')
        assert queued == 1 and batch
        assert store.db.execute(
            "SELECT count(*) FROM deep_delete_queue WHERE state='queued'").fetchone()[0] == 1
        assert strm.exists(), '入队阶段不能删任何东西'
        assert await drain(engine, limit=5) == 1
        assert not strm.exists()
        row = store.db.execute('SELECT state, message FROM deep_delete_queue').fetchone()
        assert row['state'] == 'done' and '网盘文件离开活动空间' in row['message'], dict(row)
        meta = store.meta_json('deep_delete_last')
        assert meta['done'] == 1 and meta['failed'] == 0 and meta['details'][0]['ok'] is True, meta
        store.close()

    asyncio.run(run())


def test_drain_skips_rows_whose_ledger_entry_is_gone(tmp_path):
    """断点续跑安全：台账已无该条目就跳过，不报错、不重复删。"""
    async def run():
        store = make_store(tmp_path)
        store.set_many({'sync_config': {'deep_delete_dry_run': False}})
        engine = FakeEngine(store, tmp_path / 'strm')
        engine.cloud = ReadingCloud(keep=False)
        targets = match_targets(store, {'type': 'Episode',
                                        'path': '/media/影视媒体库/剧集/国产剧集/剧名 (2024) {tmdb-10}/Season 1/剧名.2024.S01E01.mkv'})
        enqueue(store, targets, dry_run=False, source='test')
        with store.db:
            store.db.execute('DELETE FROM organized_files')      # 模拟已经删过
        assert await drain(engine, limit=5) == 1
        row = store.db.execute('SELECT state, message FROM deep_delete_queue').fetchone()
        assert row['state'] == 'skipped' and '台账已无该文件' in row['message'], dict(row)
        assert engine.cloud.recycled == []
        store.close()

    asyncio.run(run())


def test_drain_waits_while_an_organize_job_runs(tmp_path):
    """整理/转存跑着时先让路，避免边整理边删。"""
    async def run():
        store = make_store(tmp_path)
        engine = FakeEngine(store, tmp_path / 'strm')
        engine.cloud = ReadingCloud(keep=False)
        targets = match_targets(store, {'type': 'Episode',
                                        'path': '/media/影视媒体库/剧集/国产剧集/剧名 (2024) {tmdb-10}/Season 1/剧名.2024.S01E01.mkv'})
        enqueue(store, targets, dry_run=False, source='test')
        with store.db:
            store.db.execute("INSERT INTO organize_jobs(id,state,config,created,updated)"
                             " VALUES(99,'running','{}',1,1)")
        assert await drain(engine, limit=5) == 0
        assert store.db.execute(
            "SELECT count(*) FROM deep_delete_queue WHERE state='queued'").fetchone()[0] == 1
        store.close()

    asyncio.run(run())


def test_apply_dry_run_only_previews(tmp_path):
    async def run():
        store = make_store(tmp_path)
        engine = FakeEngine(store, tmp_path / 'strm')
        strm = engine.root / '影视媒体库/剧集/国产剧集/剧名 (2024) {tmdb-10}/Season 1/剧名.2024.S01E01.mkv'
        strm.parent.mkdir(parents=True)
        strm.write_text('http://play')
        targets = match_targets(store, {'type': 'Episode', 'path': str(strm)})
        result = await apply(engine, targets, dry_run=True)
        assert result['dry_run'] is True
        assert any('预演' in a for m in result['managed'] for a in m['actions'])
        assert strm.exists() and engine.cloud.recycled == []
        store.close()

    asyncio.run(run())


def test_apply_refuses_managed_real_delete(tmp_path):
    """托管文件真删必须走持久队列 + 回收确认，apply 直接真删必须拒绝。"""
    async def run():
        store = make_store(tmp_path)
        engine = FakeEngine(store, tmp_path / 'strm')
        strm = engine.root / MANAGED_REL
        strm.parent.mkdir(parents=True)
        strm.write_text('http://play')
        targets = match_targets(store, {'type': 'Episode', 'path': str(strm)})
        try:
            await apply(engine, targets, dry_run=False)
        except RuntimeError as exc:
            assert '持久队列' in str(exc)
        else:
            raise AssertionError('apply 对托管文件真删必须拒绝')
        assert strm.exists()
        store.close()

    asyncio.run(run())


def test_apply_still_revokes_share_strm(tmp_path):
    """分享来源不涉及网盘文件，apply 仍可直接撤销分享 STRM。"""
    async def run():
        store = make_store(tmp_path)
        engine = FakeEngine(store, tmp_path / 'strm')
        share_local = engine.root / '分享影视库/电影/分享电影.strm'
        share_local.parent.mkdir(parents=True)
        share_local.write_text('http://play-share')
        targets = {'managed': [], 'share': match_targets(
            store, {'type': 'Movie', 'path': str(share_local)})['share']}
        result = await apply(engine, targets, dry_run=False)
        assert result['errors'] == []
        assert not share_local.exists()
        assert store.db.execute('SELECT revoked FROM share_strms WHERE id=?', ('s1',)).fetchone()[0] == 1
        store.close()

    asyncio.run(run())


def test_episode_with_tmdb_and_path_never_expands_to_siblings(tmp_path):
    from strimkeep.deep_delete import drop_internal
    from strimkeep.internal_delete import remember
    store = make_store(tmp_path)
    original = store.db.execute("SELECT relative_path FROM organized_files WHERE file_id='3001'").fetchone()[0]
    sibling = original.replace('S01E01', 'S01E02')
    with store.db:
        store.db.execute("INSERT INTO organized_files(file_id,job_id,item_id,playback_id,relative_path,metadata) VALUES(?,?,?,?,?,?)",
                         ('3009', 1, 2, 'p9', sibling, '{}'))
    event = {'type':'Episode', 'path':'/media/'+original, 'tmdb':'10'}
    matched = match_targets(store, event)
    assert [r['file_id'] for r in matched['managed']] == ['3001']
    remember(store, [original])
    remaining, skipped = drop_internal(store, matched)
    assert skipped == 1 and not remaining['managed']
    # Even a valid TMDB id may not rescue an unmatched episode path.
    assert not match_targets(store, {**event, 'path':'/media/no-such-file.strm'})['managed']
    store.close()


def test_pathless_episode_and_generic_directories_never_authorize_delete(tmp_path):
    store = make_store(tmp_path)
    for event in ({'type':'Episode', 'path':'', 'tmdb':'10'},
                  {'type':'Series', 'path':'/media/影视媒体库/剧集/国产剧集', 'tmdb':'10'},
                  {'type':'Season', 'path':'/media/影视媒体库/剧集/Season 1', 'tmdb':'10'}):
        targets = match_targets(store, event)
        assert not targets['managed'] and not targets['share']
    store.close()


def test_pathless_webhook_reports_unsafe_and_never_queues(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from strimkeep.main import create_app
    monkeypatch.setenv('ADMIN_USERNAME', 'admin')
    monkeypatch.setenv('ADMIN_PASSWORD', 'test-password-1234')
    app = create_app(tmp_path / 'app-data', tmp_path / 'app-strm')
    store = app.state.store
    store.set_many({'sync_config': {'webhook_token': 'testing-webhook', 'deep_delete_dry_run': False}})
    with TestClient(app) as client:
        response = client.post('/api/v1/emby/webhook', headers={'X-StrimKeep-Token':'testing-webhook'},
                               json={'Event':'library.deleted','Item':{'ProviderIds':{'Tmdb':'10'}}})
        assert response.status_code == 200
        assert response.json()['accepted'] is False
        assert '缺少' in response.json()['reason']
        assert store.db.execute('SELECT count(*) FROM deep_delete_queue').fetchone()[0] == 0


class ChainCloud(GuardedCloud):
    """带云端目录链与空目录回收的假云盘，供 prune_cloud_empty_chain 使用。"""

    def __init__(self, source, chain):
        super().__init__(source)
        self.chain = {str(k): list(v) for k, v in chain.items()}
        self.empty = {str(k) for k in chain}
        self.recycled_dirs = []

    async def children(self, cid):
        return [] if str(cid) in self.empty else [{'id': 'x', 'name': 'keep'}]

    async def directory(self, cid):
        return list(self.chain.get(str(cid), []))

    async def recycle_empty_directory(self, directory_id, parent_id):
        self.recycled_dirs.append((str(directory_id), str(parent_id)))
        self.empty.discard(str(directory_id))
        self.chain.pop(str(directory_id), None)
        return {'state': True}


def test_guarded_delete_recycles_empty_cloud_directory(tmp_path):
    """删完源文件、本地目录清空后，联动回收 115 上空出来的目录链，遇同步根即停。"""
    async def run():
        store = make_store(tmp_path)
        source = add_managed_output(store)
        store.set_many({'sync_config': {'deep_delete_dry_run': False, 'source_cids': ['100']}})
        engine = FakeEngine(store, tmp_path / 'strm')
        engine.index = SimpleNamespace(account=lambda config: '1')
        cloud = ChainCloud(source, {'77': ['0', '100', '77']})
        engine.cloud = cloud
        strm = engine.root / MANAGED_REL
        strm.parent.mkdir(parents=True)
        strm.write_bytes(MANAGED_CONTENT)
        state, message, _ = await guarded_managed_delete(
            engine, {'file_id': '3001', 'playback_id': 'p1', 'relative_path': MANAGED_REL})
        assert state == 'done', (state, message)
        assert cloud.recycled_dirs == [('77', '100')]
        assert '已回收网盘空目录 77' in message
        store.close()

    asyncio.run(run())


def test_guarded_delete_keeps_cloud_directory_when_cloud_still_has_content(tmp_path):
    """云端目录里还有别的文件时，绝不回收该目录。"""
    async def run():
        store = make_store(tmp_path)
        source = add_managed_output(store)
        store.set_many({'sync_config': {'deep_delete_dry_run': False, 'source_cids': ['100']}})
        engine = FakeEngine(store, tmp_path / 'strm')
        engine.index = SimpleNamespace(account=lambda config: '1')
        cloud = ChainCloud(source, {'77': ['0', '100', '77']})
        cloud.empty = set()          # 目录里还有其它文件
        engine.cloud = cloud
        strm = engine.root / MANAGED_REL
        strm.parent.mkdir(parents=True)
        strm.write_bytes(MANAGED_CONTENT)
        state, message, _ = await guarded_managed_delete(
            engine, {'file_id': '3001', 'playback_id': 'p1', 'relative_path': MANAGED_REL})
        assert state == 'done', (state, message)
        assert cloud.recycled_dirs == []
        assert '已回收网盘空目录' not in message
        store.close()

    asyncio.run(run())

