"""文件详情缓存：只有「洗版候选复核」走缓存，写入后的核对仍读最新事实。"""
import asyncio

from strimkeep.cloud115 import Cloud115
from strimkeep.store import Store


class FakeSDK:
    """SDK 替身：真实调用会带 async_/request/session/timeout 等关键字参数。"""

    def __init__(self):
        self.calls = []
        self.name = '剧名.S01E01.mkv'

    async def fs_file_skim(self, file_id, **kwargs):
        self.calls.append(('skim', str(file_id)))
        return {'data': [{'file_id': str(file_id), 'file_category': '1',
                          'file_name': self.name, 'pick_code': 'pc1', 'file_size': 1024}]}

    async def fs_supervision(self, pickcode, **kwargs):
        self.calls.append(('supervision', str(pickcode)))
        return {'data': {'file_id': '1001', 'file_name': self.name, 'parent_id': '9',
                         'file_size': '1024', 'file_sha1': 'ABC', 'pick_code': 'pc1'}}

    async def fs_rename(self, *args, **kwargs):
        self.calls.append(('rename',))
        return {'state': True}


def make(tmp_path):
    store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
    store.set_many({'cookie': 'UID=1_A1; CID=test; SEID=test'})
    cloud = Cloud115(store)
    sdk = FakeSDK()
    cloud.client = lambda: sdk
    cloud.interval = lambda kind: 0.0

    async def no_sleep(_seconds):
        return None

    cloud.sleep = no_sleep
    return store, cloud, sdk


def test_cached_file_reads_once_within_the_same_job(tmp_path):
    """洗版准备逐个复核库内已有版本：同一文件只该读一次（skim + supervision 各一次）。"""
    async def run():
        store, cloud, sdk = make(tmp_path)
        first = await cloud.cached_file('1001')
        second = await cloud.cached_file('1001')
        assert first == second
        assert [c[0] for c in sdk.calls] == ['skim', 'supervision'], sdk.calls
        # 返回值是副本，调用方改它不会污染缓存
        second['name'] = 'changed'
        assert (await cloud.cached_file('1001'))['name'] == '剧名.S01E01.mkv'
        await cloud.close()
        store.close()
    asyncio.run(run())


def test_plain_file_read_is_never_cached(tmp_path):
    """写入后的核对走 file()：改名后必须读到新名字，不能被缓存挡住。"""
    async def run():
        store, cloud, sdk = make(tmp_path)
        await cloud.file('1001')
        sdk.name = '剧名.S01E01-改名.mkv'
        assert (await cloud.file('1001'))['name'] == '剧名.S01E01-改名.mkv'
        await cloud.close()
        store.close()
    asyncio.run(run())


def test_rename_invalidates_the_cached_detail(tmp_path):
    async def run():
        store, cloud, sdk = make(tmp_path)
        await cloud.cached_file('1001')
        assert cloud._file_memo, '第一次读应写入缓存'
        await cloud.rename('1001', 'new.mkv')
        assert not cloud._file_memo, '改名后必须失效'
        await cloud.close()
        store.close()
    asyncio.run(run())


def test_cached_detail_expires(tmp_path):
    async def run():
        store, cloud, sdk = make(tmp_path)
        cloud.FILE_MEMO_SECONDS = 0          # 直接让缓存过期，验证 TTL 兜底外部改动
        await cloud.cached_file('1001')
        sdk.calls.clear()
        await cloud.cached_file('1001')
        assert ('skim', '1001') in sdk.calls, '超过 TTL 后必须重新读'
        await cloud.close()
        store.close()
    asyncio.run(run())
