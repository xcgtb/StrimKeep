"""0.8.40：离线提交留痕（bot 也要记）、ed2k 按 hash 认领、离线目录兜底自动整理。"""
import asyncio

from test_organize import setup

ED2K = 'ed2k://|file|测试.mkv|1024|' + 'a' * 32 + '|/'


def test_ed2k_link_is_claimed_by_hash_not_by_url(tmp_path):
    """115 会改写 ed2k 链接，只能按 32 位 hash 认领；hash 不同绝不能误认。"""
    async def run():
        store, cloud, http, engine, _ = setup(tmp_path)
        post = engine.offline
        same = [{'id': 'A' * 32, 'url': 'https://normalized.example/x', 'file_id': '1',
                 'status': '完成', 'percent': 100}]
        assert post._matching_tasks([ED2K], same) == same
        other = [{'id': 'b' * 32, 'url': '', 'file_id': '2', 'status': '完成', 'percent': 100}]
        assert post._matching_tasks([ED2K], other) == []
        await http.aclose()
        store.close()
    asyncio.run(run())


def test_offline_submit_records_realtime_event_with_origin(tmp_path):
    """Telegram 机器人提交也必须进实时日志（以前只有 Web 路由记一条）。"""
    async def run():
        store, cloud, http, engine, _ = setup(tmp_path)
        calls = []

        async def children(cid):
            return []

        async def offline_add(urls, cid=None):
            calls.append((list(urls), cid))
            return {'state': True}

        cloud.children = children
        cloud.offline_add = offline_add
        sid = await engine.offline.submit([ED2K], '100', '离线目录', origin='bot')
        assert calls and calls[0][1] == '100'
        messages = [row['message'] for row in
                    store.db.execute("SELECT message FROM events WHERE kind='offline' ORDER BY id")]
        assert any(('离线提交 #%d（bot）' % sid) in text for text in messages), messages
        # 刚提交的目录要留个记号，兜底扫描下一轮先看它
        assert store.meta_json('offline_scan_next_cid') == '100'
        await http.aclose()
        store.close()
    asyncio.run(run())


def test_offline_scan_tick_is_disabled(tmp_path):
    """0.8.57 起停用离线目录兜底扫描：不访问云盘、不入队（第一阶段：不得用兜底逻辑跨目录整理）。"""
    async def run():
        store, cloud, http, engine, _ = setup(tmp_path)
        store.set_many({'organize_config': {'source_cids': ['1'], 'archive_cid': '2'},
                        'offline_config': {'target_cid': '900', 'magnet_cid': '900', 'ed2k_cid': '901'}})
        store.set_meta('offline_scan_next_cid', '901')
        scanned = []

        async def fresh(cid):
            scanned.append(cid)
            return [{'id': '500', 'name': '电影 (2024).mkv', 'is_dir': False}]

        engine.cloud = type('C', (), {'fresh_children': staticmethod(fresh)})()
        await engine.offline_scan_tick()
        assert scanned == []
        assert store.db.execute(
            "SELECT count(*) FROM organize_jobs WHERE json_extract(config,'$.origin') LIKE 'offline-scan:%'"
        ).fetchone()[0] == 0
        await http.aclose()
        store.close()
    asyncio.run(run())




def test_offline_scan_tick_is_disabled(tmp_path):
    """0.8.57 起停用离线目录兜底扫描：不访问云盘、不入队（第一阶段：不得用兜底逻辑跨目录整理）。"""
    async def run():
        store, cloud, http, engine, _ = setup(tmp_path)
        store.set_many({'organize_config': {'source_cids': ['1'], 'archive_cid': '2'},
                        'offline_config': {'target_cid': '900', 'magnet_cid': '900', 'ed2k_cid': '901'}})
        store.set_meta('offline_scan_next_cid', '901')
        scanned = []

        async def fresh(cid):
            scanned.append(cid)
            return [{'id': '500', 'name': '电影 (2024).mkv', 'is_dir': False}]

        engine.cloud = type('C', (), {'fresh_children': staticmethod(fresh)})()
        await engine.offline_scan_tick()
        assert scanned == []
        assert store.db.execute(
            "SELECT count(*) FROM organize_jobs WHERE json_extract(config,'$.origin') LIKE 'offline-scan:%'"
        ).fetchone()[0] == 0
        await http.aclose()
        store.close()
    asyncio.run(run())




def test_offline_scan_tick_is_disabled(tmp_path):
    """0.8.57 起停用离线目录兜底扫描：不访问云盘、不入队（第一阶段：不得用兜底逻辑跨目录整理）。"""
    async def run():
        store, cloud, http, engine, _ = setup(tmp_path)
        store.set_many({'organize_config': {'source_cids': ['1'], 'archive_cid': '2'},
                        'offline_config': {'target_cid': '900', 'magnet_cid': '900', 'ed2k_cid': '901'}})
        store.set_meta('offline_scan_next_cid', '901')
        scanned = []

        async def fresh(cid):
            scanned.append(cid)
            return [{'id': '500', 'name': '电影 (2024).mkv', 'is_dir': False}]

        engine.cloud = type('C', (), {'fresh_children': staticmethod(fresh)})()
        await engine.offline_scan_tick()
        assert scanned == []
        assert store.db.execute(
            "SELECT count(*) FROM organize_jobs WHERE json_extract(config,'$.origin') LIKE 'offline-scan:%'"
        ).fetchone()[0] == 0
        await http.aclose()
        store.close()
    asyncio.run(run())



def test_offline_submit_failure_also_lands_in_realtime_log(tmp_path):
    """提交响应丢失时只回「待核查」但没日志就无法对账，所以失败也要留痕。"""
    async def run():
        store, cloud, http, engine, _ = setup(tmp_path)

        async def children(cid):
            return []

        async def offline_add(urls, cid=None):
            raise RuntimeError('timeout')

        cloud.children = children
        cloud.offline_add = offline_add
        try:
            await engine.offline.submit([ED2K], '100', '离线目录', origin='bot')
        except RuntimeError:
            pass
        else:
            raise AssertionError('提交异常必须向上抛，不能吞掉')
        row = store.db.execute("SELECT state FROM offline_submissions ORDER BY id DESC LIMIT 1").fetchone()
        assert row['state'] == 'needs_review'
        messages = [r['message'] for r in
                    store.db.execute("SELECT message FROM events WHERE kind='offline' ORDER BY id")]
        assert any('响应未确认' in text and '（bot）' in text for text in messages), messages
        await http.aclose()
        store.close()
    asyncio.run(run())
