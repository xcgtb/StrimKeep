"""本地巡检是可选功能：显式打开后整轮不碰云端；默认按网盘清单对齐。

默认不巡检是因为巡检只认台账里登记过的输出，本地删掉的文件它看不见，
永远补不回来（见 test_sync_trigger_parity 的回归用例）。
"""
import asyncio

from test_organize import new_job, setup
from test_phase5 import sync_config


def scheduled(full_scan=False):
    config = dict(sync_config())
    config.update({'scheduled': True, 'full_scan': full_scan})
    return config


def watch(cloud):
    """记录巡检期间碰过的云端方法。"""
    touched = []
    for name in ('status', 'children', 'children_verified', 'path_info', 'file', 'list_files'):
        original = getattr(cloud, name, None)
        if original is None:
            continue

        async def wrapper(*args, _name=name, _original=original, **kwargs):
            touched.append(_name)
            return await _original(*args, **kwargs)

        setattr(cloud, name, wrapper)
    return touched


async def prepared(tmp_path):
    store, cloud, http, engine, _ = setup(tmp_path)
    video = cloud.add_video(name='Movie.2024.1080p.mkv')
    video['sha1'] = 'A' * 40
    await engine.run_job(new_job(store, sync_config()))
    store.set_many({'sync_sweep_enabled': 1})
    return store, cloud, http, engine


def test_scheduled_sweep_calls_no_cloud_method(tmp_path):
    async def run():
        store, cloud, http, engine = await prepared(tmp_path)
        touched = watch(cloud)
        job = new_job(store, scheduled(full_scan=False))
        await engine.run_job(job)
        row = engine.job(job)
        assert row['state'] == 'completed', dict(row)
        assert touched == [], '巡检期间不该调云端的任何方法：%r' % (touched,)
        await http.aclose()
        store.close()
    asyncio.run(run())


def test_scheduled_full_scan_enumerates_cloud_and_reports_full_check(tmp_path):
    async def run():
        store, cloud, http, engine = await prepared(tmp_path)
        touched = watch(cloud)
        job = new_job(store, scheduled(full_scan=True))
        await engine.run_job(job)
        row = engine.job(job)
        assert row['state'] in ('completed', 'partial'), dict(row)
        assert touched, 'full_scan 应当枚举云端'
        assert '完整清单已核实' in row['message'], row['message']
        await http.aclose()
        store.close()
    asyncio.run(run())
