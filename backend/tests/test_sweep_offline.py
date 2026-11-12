"""定时同步的分工：默认本地巡检整轮不碰云端；显式 full_scan 才枚举云端。

护住 266950c 那类「巡检入口被重构删掉」的回归——死代码不会让别的测试变红，
只有这条会。
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
