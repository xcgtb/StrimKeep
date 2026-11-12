"""目录读取双通道：网页接口被 405 挡住时改走 App 通道，字段归一化后行为一致。"""
import asyncio
from types import SimpleNamespace

import pytest

from strimkeep.cloud115 import Cloud115, CloudError

PARENT = '3551775428337206921'
DIR_ROW = {'fid': '3551775542304835573', 'fn': 'Season 1', 'pc': 'akucmnby2n164h9ib',
           'pid': PARENT, 'fc': '0'}
FILE_ROW = {'fid': '3551774621546055183', 'fn': '剧集.S01E01.mkv', 'fs': '2500094964',
            'sha1': 'B582548444D06EBA5F4A171818500558A63DF324', 'pc': 'akucmhvyonzx0h9ib',
            'pid': PARENT, 'fc': '1'}


def build(web_blocked=True, rows=(DIR_ROW, FILE_ROW), count=2):
    cloud = Cloud115.__new__(Cloud115)
    cloud.webapi_until = 0.0
    cloud.clock = lambda: 0.0
    api = SimpleNamespace(fs_files=lambda *a, **k: None, request=lambda *a, **k: None)
    cloud.client = lambda: api
    calls = []

    async def call(fn, *args, **kw):
        if fn is api.fs_files:
            calls.append('web')
            if web_blocked:
                # 真实实现里由 call() 的 405 分支设置这个窗口，这里同样模拟
                cloud.webapi_until = cloud.clock() + 600
                raise CloudError('115 网页接口被限流（HTTP 405）', 'WEBAPI_BLOCKED', 503)
            return {'data': [], 'count': 0, 'path': []}
        calls.append('android')
        return {'data': list(rows), 'count': count, 'path': [{'cid': '0', 'name': ''}]}

    cloud.call = call
    return cloud, calls


def run(cloud, cid=PARENT):
    return asyncio.run(cloud.list_files(cid, 0, 1000))


def test_webapi_blocked_falls_back_to_android():
    cloud, calls = build(web_blocked=True)
    result = run(cloud)
    assert calls == ['web', 'android']
    assert cloud.webapi_until > 0, '被挡后要记住窗口，别每页都先撞一次'
    items = result['items']
    assert [item['is_dir'] for item in items] == [True, False]
    assert items[1]['sha1'] == FILE_ROW['sha1']
    assert items[1]['pickcode'] == FILE_ROW['pc']
    assert items[1]['size'] == 2500094964
    assert result['has_more'] is False and result['raw_count'] == 2


def test_window_skips_webapi_entirely():
    cloud, calls = build(web_blocked=True)
    cloud.webapi_until = 100.0          # 已知道网页接口不可用
    run(cloud)
    assert calls == ['android']


def test_webapi_ok_path_unchanged():
    cloud, calls = build(web_blocked=False, rows=(), count=0)
    result = run(cloud)
    assert calls == ['web']
    assert result['items'] == []


def test_android_parent_mismatch_still_guarded():
    bad = dict(FILE_ROW, pid='999')
    cloud, _ = build(web_blocked=True, rows=(bad,), count=1)
    with pytest.raises(CloudError) as exc:
        run(cloud)
    assert exc.value.code == 'DIRECTORY_CHANGED'
