"""巡检扫本地文件树：只删「确实播不了」的 STRM，其余只报数。"""
import asyncio

from test_organize import new_job, setup
from test_phase5 import sync_config


def test_sweep_deletes_only_unplayable_strm(tmp_path):
    async def run():
        store, cloud, http, engine, _ = setup(tmp_path)
        video = cloud.add_video(name='Movie.2024.1080p.mkv')
        video['sha1'] = 'A' * 40
        first = new_job(store, sync_config())
        await engine.run_job(first)
        registered = engine.outputs.get('1', '1000')
        live_id = registered['playback_id']

        root = engine.root / 'stray'
        root.mkdir(parents=True, exist_ok=True)
        alive = root / 'alive.strm'
        dead = root / 'dead.strm'
        foreign = root / 'foreign.strm'
        alive.write_text('https://play.example/play/%s/x\n' % live_id, encoding='utf-8')
        dead.write_text('https://play.example/play/deadbeefdeadbeef/x\n', encoding='utf-8')
        foreign.write_text('http://other.example/play/whatever/x\n', encoding='utf-8')

        job = new_job(store, sync_config())
        engine.update_job(job, state='running')
        state = await engine.sync.sweep(job, sync_config())
        row = engine.job(job)
        assert state == 'completed', (state, dict(row))
        assert not dead.exists(), '播不了的（身份不存在）应该被清掉'
        assert alive.exists(), '台账外但还能播的必须保留'
        assert foreign.exists(), '不是我们写出去的地址，一律不碰'
        assert '清理播不了的 STRM 1 条' in row['message']
        assert '2 条未登记 STRM 未动' in row['message']
        await http.aclose()
        store.close()
    asyncio.run(run())
