"""115 账号容量解析：只读接口的字段容错。"""
import asyncio

from strimkeep.cloud115 import Cloud115


def make(info, space):
    cloud = object.__new__(Cloud115)

    class Stub:
        async def user_info(self, **kwargs):
            return info

        async def user_space_info(self, **kwargs):
            return space

    cloud.client = lambda: Stub()

    async def call(method, *args, **kwargs):
        return await method()

    cloud.call = call
    return cloud


def test_account_reads_nickname_vip_and_bytes():
    cloud = make({'data': {'user_id': 4649098, 'user_name': 'aric9999', 'is_vip': 2,
                           'user_face': 'https://a/face.jpg'}},
                 {'data': {'all_total': {'size': 192097489180119.0},
                           'all_use': {'size': 56523729402580}}})
    result = asyncio.run(cloud.account())
    assert result['uid'] == '4649098' and result['nickname'] == 'aric9999'
    assert result['vip_level'] == 2 and result['avatar'].endswith('face.jpg')
    assert result['space_total'] == 192097489180119 and result['space_used'] == 56523729402580


def test_account_tolerates_missing_fields_without_crashing():
    cloud = make({'data': {'user_id': '1'}}, {'data': {'all_total': {'size': True}}})
    result = asyncio.run(cloud.account())
    assert result['nickname'] == '' and result['vip_level'] == 0
    # 布尔值不是容量，必须归零而不是当成 1 字节
    assert result['space_total'] == 0 and result['space_used'] == 0
