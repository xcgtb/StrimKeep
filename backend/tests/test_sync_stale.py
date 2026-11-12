"""失效 STRM 检测与清理：全量扫描后做差集，只标记；确认后清本地与台账。"""
import asyncio
import hashlib
import json
import time

from strimkeep.store import Store
from strimkeep.sync import Sync


class FakeIndex:
    def account(self, config):
        return '1'


class FakeCloud:
    def __init__(self, live):
        self.live = set(live)

    async def live_file_exists(self, fid):
        return fid in self.live

    async def path_info(self, cid):
        return [{'id': cid, 'name': f'目录{cid}'}]


class FakeOutputs:
    def __init__(self, root):
        self.root = root

    def path(self, rel):
        return self.root / rel


class FakeSyncEngine:
    def __init__(self, store, root, cloud):
        self.store = store
        self.cloud = cloud
        self.index = FakeIndex()
        self.outputs = FakeOutputs(root)


def make_store(tmp_path, rows):
    store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
    with store.db:
        store.db.execute("CREATE TABLE IF NOT EXISTS output_registry("
                         "account_id TEXT, file_id TEXT, files TEXT, trail TEXT, active INTEGER DEFAULT 1)")
        for fid, files in rows.items():
            store.db.execute("INSERT INTO output_registry(account_id,file_id,source,trail,playback_id,"
                             "token_cipher,relative_path,files,active,updated) "
                             "VALUES('1',?,'测试','[\"100\"]','p-'+?,'cipher',?,?,'1',?)",
                             (fid, fid, '影视媒体库/剧集/国产剧集/剧名/Season 1/剧名.S01E03.strm',
                              json.dumps(['影视媒体库/剧集/国产剧集/剧名/Season 1/剧名.S01E03.strm']), time.time()))
        store.db.execute("INSERT OR IGNORE INTO playbacks(id,file_id,pickcode,name,relative_path,token_hash,created)"
                         " VALUES('p-1001','1001','pc','剧','影视媒体库/剧集/国产剧集/剧名/Season 1/剧名.S01E03.strm','h',1)")
        store.db.execute("INSERT OR REPLACE INTO organized_files(file_id,job_id,item_id,playback_id,relative_path,metadata)"
                         " VALUES('1001',1,1,'p-1001','影视媒体库/剧集/国产剧集/剧名/Season 1/剧名.S01E03.strm','{}')")
    return store


def test_stale_check_marks_only_missing_sources(tmp_path):
    async def run():
        store = make_store(tmp_path, {'1001': ['影视媒体库/剧集/国产剧集/剧名/Season 1/剧名.S01E03.strm'],
                                      '1002': ['影视媒体库/剧集/国产剧集/剧名/Season 1/剧名.S01E04.strm']})
        engine = FakeSyncEngine(store, tmp_path / 'strm', FakeCloud(live={'1002'}))
        sync = Sync(engine)
        config = {'source_cids': ['100'], 'stale_detect': True}

        await sync.stale_check(jid := 1, config) if False else await sync.stale_check(1, config)

        stale = json.loads(store.get('stale_strms') or '[]')
        assert [s['file_id'] for s in stale] == ['1001']
        store.close()

    asyncio.run(run())


def test_stale_detect_off_writes_nothing(tmp_path):
    async def run():
        store = make_store(tmp_path, {'1001': ['影视媒体库/x.strm']})
        engine = FakeSyncEngine(store, tmp_path / 'strm', FakeCloud(live=set()))
        sync = Sync(engine)

        await sync.stale_check(1, {'source_cids': ['100'], 'stale_detect': False})

        assert store.get('stale_strms') in (None, '', '[]')
        store.close()

    asyncio.run(run())


def test_stale_cleanup_removes_local_and_ledger_only(tmp_path):
    async def run():
        root = tmp_path / 'strm'
        strm = root / '影视媒体库/剧集/国产剧集/剧名/Season 1/剧名.S01E03.strm'
        strm.parent.mkdir(parents=True)
        strm.write_text('http://play')
        store = make_store(tmp_path, {})
        relative = '影视媒体库/剧集/国产剧集/剧名/Season 1/剧名.S01E03.strm'
        digest = hashlib.sha256(strm.read_bytes()).hexdigest()
        with store.db:
            store.db.execute("INSERT INTO output_registry(account_id,file_id,source,trail,playback_id,token_cipher,relative_path,files,active,updated) VALUES('1','1001','{}','[\"100\"]','p-1001','cipher',?,?,1,?)", (relative,json.dumps({relative:digest}),time.time()))
            store.db.execute('INSERT INTO owned_outputs VALUES(?,?,?,?)', (relative,'1','1001',digest))
        store.set_many({'stale_strms': json.dumps(
            [{'file_id': '1001', 'relative_path': '影视媒体库/剧集/国产剧集/剧名/Season 1/剧名.S01E03.strm'}],
            ensure_ascii=False)})
        engine = FakeSyncEngine(store, root, FakeCloud(live=set()))
        sync = Sync(engine)

        cleaned, rest = await sync.stale_cleanup()

        assert cleaned == ['影视媒体库/剧集/国产剧集/剧名/Season 1/剧名.S01E03.strm']
        assert not strm.exists()
        assert store.get('stale_strms') == '[]'
        assert store.db.execute('SELECT revoked FROM playbacks WHERE id=?', ('p-1001',)).fetchone()[0] == 1
        assert store.db.execute('SELECT count(*) FROM organized_files WHERE file_id=?', ('1001',)).fetchone()[0] == 0
        store.close()

    asyncio.run(run())


def test_manual_stale_cleanup_retains_unowned_or_modified_output(tmp_path):
    async def run():
        root = tmp_path / 'strm'; root.mkdir(); path = root / 'x.strm'; path.write_text('custom')
        store = make_store(tmp_path, {})
        store.set_many({'stale_strms': json.dumps([{'file_id':'1001','relative_path':'x.strm'}])})
        engine = FakeSyncEngine(store, root, FakeCloud(live=set()))
        cleaned, rest = await Sync(engine).stale_cleanup()
        assert not cleaned and len(rest) == 1 and path.read_text() == 'custom'
        store.close()
    asyncio.run(run())


def test_manual_cleanup_preserves_file_edited_during_cloud_check(tmp_path):
    async def run():
        root=tmp_path/'strm';root.mkdir();path=root/'x.strm';path.write_text('known')
        store=make_store(tmp_path, {});digest=hashlib.sha256(path.read_bytes()).hexdigest()
        with store.db:
            store.db.execute("INSERT INTO output_registry(account_id,file_id,source,trail,playback_id,token_cipher,relative_path,files,active,updated) VALUES('1','1001','{}','[\"100\"]','p-1001','cipher','x.strm',?,1,?)",(json.dumps({'x.strm':digest}),time.time()))
            store.db.execute('INSERT INTO owned_outputs VALUES(?,?,?,?)',('x.strm','1','1001',digest))
        store.set_many({'stale_strms':json.dumps([{'file_id':'1001','relative_path':'x.strm'}])})
        class ChangedCloud(FakeCloud):
            async def live_file_exists(self, fid):
                path.write_text('changed while waiting')
                return False
        cleaned,rest=await Sync(FakeSyncEngine(store,root,ChangedCloud(set()))).stale_cleanup()
        assert not cleaned and len(rest)==1 and path.read_text()=='changed while waiting'
        store.close()
    asyncio.run(run())
