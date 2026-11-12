"""独立本地巡检的诊断与自愈能力；普通手动和定时同步均不隐式使用它。"""
import asyncio
import json

from test_organize import new_job, setup
from test_phase5 import sync_config


def scheduled(**over):
    config = dict(sync_config())
    config.update({'scheduled': True})
    config.update(over)
    return config


async def run_local_sweep(engine, job):
    engine.update_job(job, state='running')
    config = json.loads(engine.job(job)['config'])
    return await engine.sync.sweep(job, config)


def no_cloud_enumeration(cloud):
    """只拦枚举类调用；status() 这类登录检查放行。"""
    calls = []

    async def boom(*args, **kwargs):
        calls.append(args)
        raise AssertionError('本地巡检不应枚举云端：%r' % (args,))

    cloud.children = boom
    cloud.children_verified = boom
    cloud.path_info = boom
    return calls


def test_scheduled_sync_sweeps_locally_without_cloud_enumeration(tmp_path):
    async def run():
        store, cloud, http, engine, _ = setup(tmp_path)
        video = cloud.add_video(name='Movie.2024.1080p.mkv')
        video['sha1'] = 'A' * 40
        first = new_job(store, sync_config())          # 先完全核对一次，登记输出
        await engine.run_job(first)
        assert engine.job(first)['state'] == 'completed'

        calls = no_cloud_enumeration(cloud)
        job = new_job(store, scheduled())
        await run_local_sweep(engine, job)
        row = engine.job(job)
        assert row['state'] == 'completed', dict(row)
        assert '本地巡检' in row['message'] and '未枚举云端' in row['message']
        assert calls == []
        await http.aclose()
        store.close()
    asyncio.run(run())


def test_sweep_rewrites_missing_output_locally(tmp_path):
    """主 STRM 丢了就地重写：不枚举云端，也不升级成完全核对。"""
    async def run():
        store, cloud, http, engine, _ = setup(tmp_path)
        video = cloud.add_video(name='Movie.2024.1080p.mkv')
        video['sha1'] = 'A' * 40
        first = new_job(store, sync_config())
        await engine.run_job(first)

        registered = engine.outputs.get('1', '1000')
        path = engine.root / registered['relative_path']
        original = path.read_bytes()
        path.unlink()

        calls = no_cloud_enumeration(cloud)
        job = new_job(store, scheduled())
        await run_local_sweep(engine, job)
        row = engine.job(job)
        assert row['state'] == 'completed', dict(row)
        assert path.read_bytes() == original, '应重写成当前规范地址'
        assert not store.get('sync_full_due'), '本地能修就不该升级全量'
        assert '就地自愈 1 条' in row['message']
        assert calls == []
        await http.aclose()
        store.close()
    asyncio.run(run())


def test_sweep_rewrites_tampered_strm(tmp_path):
    """被外部改过的 STRM 内容：按当前播放地址覆盖回去。"""
    async def run():
        store, cloud, http, engine, _ = setup(tmp_path)
        video = cloud.add_video(name='Movie.2024.1080p.mkv')
        video['sha1'] = 'A' * 40
        first = new_job(store, sync_config())
        await engine.run_job(first)
        registered = engine.outputs.get('1', '1000')
        path = engine.root / registered['relative_path']
        original = path.read_bytes()
        path.write_bytes(b'http://evil.example/play/deadbeef\n')

        no_cloud_enumeration(cloud)
        job = new_job(store, scheduled())
        await run_local_sweep(engine, job)
        row = engine.job(job)
        assert row['state'] == 'completed', dict(row)
        assert path.read_bytes() == original
        assert '就地自愈 1 条' in row['message']
        await http.aclose()
        store.close()
    asyncio.run(run())


def test_sweep_escalates_when_subtitle_is_missing(tmp_path):
    """字幕要回云端下载，本地造不出来：仍然升级为完全核对。"""
    async def run():
        store, cloud, http, engine, _ = setup(tmp_path)
        video = cloud.add_video(name='Movie.2024.1080p.mkv')
        video['sha1'] = 'A' * 40
        cloud.add_video('1001', 'Movie.2024.1080p.chs.forced.srt')['size'] = 50
        first = new_job(store, sync_config())
        await engine.run_job(first)
        registered = engine.outputs.get('1', '1000')
        subtitle = [p for p in json.loads(registered['files']) if not p.endswith('.strm')][0]
        (engine.root / subtitle).unlink()

        no_cloud_enumeration(cloud)
        job = new_job(store, scheduled())
        await run_local_sweep(engine, job)
        row = engine.job(job)
        assert row['state'] == 'partial'
        assert '需要修复' in row['message'] and '下一轮自动做完全核对' in row['message']
        assert store.get('sync_full_due') == 1
        await http.aclose()
        store.close()
    asyncio.run(run())



def test_escalated_run_does_a_full_scan(tmp_path):
    async def run():
        store, cloud, http, engine, _ = setup(tmp_path)
        video = cloud.add_video(name='Movie.2024.1080p.mkv')
        video['sha1'] = 'A' * 40
        first = new_job(store, sync_config())
        await engine.run_job(first)

        store.set_many({'sync_full_due': 1})
        visited = []
        original = cloud.children

        async def counted(cid):
            visited.append(cid)
            return await original(cid)

        cloud.children = counted
        job = new_job(store, scheduled())
        await engine.run_job(job)
        assert visited, '置了 sync_full_due 就该走完全核对'
        assert engine.job(job)['state'] == 'completed'
        assert not store.get('sync_full_due'), '标记要被消费掉'
        await http.aclose()
        store.close()
    asyncio.run(run())


def test_sweep_can_be_switched_off(tmp_path):
    async def run():
        store, cloud, http, engine, _ = setup(tmp_path)
        video = cloud.add_video(name='Movie.2024.1080p.mkv')
        video['sha1'] = 'A' * 40
        first = new_job(store, sync_config())
        await engine.run_job(first)

        store.set_many({'sync_sweep_enabled': 0})
        visited = []
        original = cloud.children

        async def counted(cid):
            visited.append(cid)
            return await original(cid)

        cloud.children = counted
        job = new_job(store, scheduled())
        await engine.run_job(job)
        assert visited, '关掉巡检后定时任务应回到完全核对'
        assert '本地巡检' not in engine.job(job)['message']
        await http.aclose()
        store.close()
    asyncio.run(run())

def test_same_failures_do_not_escalate_twice(tmp_path):
    """同一批问题只升级一次，避免完全核对没修好时每天白跑全量。"""
    async def run():
        store, cloud, http, engine, _ = setup(tmp_path)
        video = cloud.add_video(name='Movie.2024.1080p.mkv')
        video['sha1'] = 'A' * 40
        first = new_job(store, sync_config())
        await engine.run_job(first)
        registered = engine.outputs.get('1', '1000')
        # 归属被改到别的文件上：本地不能覆盖，只能升级完全核对。
        with store.db:
            store.db.execute("UPDATE owned_outputs SET file_id='9999' WHERE relative_path=?",
                             (registered['relative_path'],))

        no_cloud_enumeration(cloud)
        job = new_job(store, scheduled())
        await run_local_sweep(engine, job)
        assert store.get('sync_full_due') == 1
        assert '下一轮自动做完全核对' in engine.job(job)['message']

        store.set_many({'sync_full_due': 0})     # 模拟完全核对跑过了但没修好
        second = new_job(store, scheduled())
        await run_local_sweep(engine, second)
        row = engine.job(second)
        assert row['state'] == 'partial'
        assert '与上一轮相同' in row['message']
        assert not store.get('sync_full_due'), '同一批问题不该再次升级'
        await http.aclose()
        store.close()
    asyncio.run(run())


def test_sweep_cleans_revoked_outputs_and_reports_unregistered(tmp_path):
    """对齐「清无效 .strm」：已撤销的本地输出删掉，未登记的只报告不删。"""
    async def run():
        store, cloud, http, engine, _ = setup(tmp_path)
        video = cloud.add_video(name='Movie.2024.1080p.mkv')
        video['sha1'] = 'A' * 40
        first = new_job(store, sync_config())
        await engine.run_job(first)
        registered = engine.outputs.get('1', '1000')
        path = engine.root / registered['relative_path']

        with store.db:
            store.db.execute('UPDATE playbacks SET revoked=1 WHERE id=?', (registered['playback_id'],))
        stray = path.parent / 'stray.strm'
        stray.write_text('http://x/play/unknown')

        no_cloud_enumeration(cloud)
        job = new_job(store, scheduled())
        await run_local_sweep(engine, job)
        row = engine.job(job)
        assert not path.exists(), '已撤销的输出应被清掉'
        assert stray.exists(), '未登记的 STRM 不能删'
        assert '清理已撤销输出 1 条' in row['message']
        assert '未登记 STRM 未动' in row['message']
        await http.aclose()
        store.close()
    asyncio.run(run())


def test_cleanup_retires_out_of_scope_rows_whose_files_are_gone(tmp_path):
    """范围外的旧台账：文件还在就不动；文件全没了就停用，免得巡检天天报它。"""
    async def run():
        store, cloud, http, engine, _ = setup(tmp_path)
        video = cloud.add_video(name='Movie.2024.1080p.mkv')
        video['sha1'] = 'A' * 40
        first = new_job(store, sync_config())      # 同步一次，产生台账
        await engine.run_job(first)
        registered = store.db.execute('SELECT * FROM output_registry LIMIT 1').fetchone()
        assert registered, '同步后应留下台账'
        local = engine.root / registered['relative_path']

        config = {**sync_config(['999']), 'notify_emby': False}   # 与这条台账的范围不相交
        jid = new_job(store, config)
        engine.update_job(jid, state='running')
        await engine.sync.cleanup(jid, config)
        def active_now():
            return store.db.execute('SELECT active FROM output_registry WHERE relative_path=?',
                                    (registered['relative_path'],)).fetchone()['active']

        assert active_now() == 1, '文件还在，不能因为范围外就停用'

        local.unlink()
        await engine.sync.cleanup(jid, config)
        assert active_now() == 0, '范围外且本地已不存在，应停用'
        await http.aclose()
        store.close()
    asyncio.run(run())


def test_sweep_heals_stale_library_path(tmp_path):
    """库根改名留下的「旧路径 + 新路径同摘要」：巡检把旧键剪掉，主路径改指新键。"""
    async def run():
        store, cloud, http, engine, _ = setup(tmp_path)
        video = cloud.add_video(name='Movie.2024.1080p.mkv')
        video['sha1'] = 'A' * 40
        first = new_job(store, sync_config())
        await engine.run_job(first)
        row = store.db.execute('SELECT * FROM output_registry LIMIT 1').fetchone()
        files = json.loads(row['files'])
        real = list(files)[0]
        stale = '影视媒体库/' + real.split('/', 1)[1]
        files[stale] = files[real]
        with store.db:
            store.db.execute('UPDATE output_registry SET files=?,relative_path=? WHERE account_id=? AND file_id=?',
                             (json.dumps(files, ensure_ascii=False), stale, row['account_id'], row['file_id']))

        no_cloud_enumeration(cloud)
        job = new_job(store, scheduled())
        await run_local_sweep(engine, job)
        healed = store.db.execute('SELECT * FROM output_registry LIMIT 1').fetchone()
        assert healed['relative_path'] == real, '主路径应改指仍存在的那条'
        assert stale not in json.loads(healed['files']), '旧键应被剪掉'
        assert engine.job(job)['state'] == 'completed', dict(engine.job(job))
        await http.aclose()
        store.close()
    asyncio.run(run())
