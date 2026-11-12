import asyncio
import pytest
from strimkeep.cloud115 import Cloud115
from strimkeep.store import Store


def test_recycle_many_uses_indexed_fid_payload(tmp_path):
    """真机验证过：逗号 fid 会被 115 忽略（errno 231011），必须用 fid[i]。"""
    async def run():
        store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
        store.set_many({'cookie': 'UID=1; CID=t; SEID=t'})
        cloud = Cloud115(store)
        sent = {}

        class Stub:
            async def fs_delete(self, payload, **kwargs):
                sent.update(payload)
                return {'state': True}

            async def fs_delete_open(self, payload, **kwargs):
                sent.update(payload)
                return {'state': True}

        cloud.client = lambda: Stub()

        async def fake_call(method, *args, **kwargs):
            return await method(*args)

        cloud.call = fake_call
        await cloud.recycle_many(['111', '222'], '999')
        # 旧的 /rb/delete 不认逗号 fid（errno 231011 且不删），那条路只留给单个文件。
        assert sent.get('file_ids') == '111,222'
        assert 'fid[0]' not in sent and 'fid' not in sent
        store.close()

    asyncio.run(run())
