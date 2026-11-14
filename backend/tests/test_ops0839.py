"""0.8.39：播放延迟直方图、队列/通知/分享存活三个 gauge、httpx 四段超时。"""
import asyncio

from strimkeep import network
from strimkeep.metrics import Metrics
from strimkeep.store import Store


class _FakeApp:
    version = '0.8.40'

    def __init__(self):
        self.state = type('S', (), {})()


def make_store(tmp_path):
    return Store(tmp_path / 'data', 'admin', 'test-password-1234')


def test_play_latency_is_histogram_with_cumulative_buckets(tmp_path):
    """成功取链进直方图（累计桶 + +Inf + sum/count）；失败不计入延迟。"""
    store = make_store(tmp_path)
    metrics = Metrics(store)
    for ms in (30, 80, 120, 900, 9000):
        metrics.record_play('ok', ms)
    metrics.record_play('error', 999)          # 失败不应污染 p95
    metrics.record_play('timeout', None)
    text = metrics.render(_FakeApp())

    assert 'strimkeep_play_latency_ms_bucket{le="50"} 1' in text
    assert 'strimkeep_play_latency_ms_bucket{le="100"} 2' in text
    assert 'strimkeep_play_latency_ms_bucket{le="200"} 3' in text
    assert 'strimkeep_play_latency_ms_bucket{le="500"} 3' in text
    assert 'strimkeep_play_latency_ms_bucket{le="1000"} 4' in text
    assert 'strimkeep_play_latency_ms_bucket{le="5000"} 4' in text
    assert 'strimkeep_play_latency_ms_bucket{le="+Inf"} 5' in text
    assert 'strimkeep_play_latency_ms_count 5' in text
    assert 'strimkeep_play_latency_ms_sum 10130.0' in text
    assert 'strimkeep_play_total{result="error"} 1' in text
    assert 'strimkeep_play_total{result="timeout"} 1' in text
    store.close()


def test_play_cache_hits_are_visible_in_metrics(tmp_path):
    """0.9.59：直链缓存命中率必须能看到——命中就意味着这次开播没等 115 接口。"""
    store = make_store(tmp_path)
    metrics = Metrics(store)
    app = _FakeApp()
    app.state.cloud = type('Cloud', (), {'play_hits': 41, 'play_misses': 3})()

    text = metrics.render(app)
    assert 'strimkeep_play_cache{result="hit"} 41' in text
    assert 'strimkeep_play_cache{result="miss"} 3' in text

    # 云客户端还没起来（或换成了别的实现）时，不能因为取不到计数就整段渲染失败。
    app.state.cloud = None
    assert 'strimkeep_play_cache' not in metrics.render(app)
    store.close()


def test_queue_notification_and_share_gauges(tmp_path):
    """深删队列 / TG 重试 / 分享存活检测都要能在 /metrics 上看出来。"""
    store = make_store(tmp_path)
    store.set_meta('share_check_last', {'at': 1234.5, 'alive': 7, 'dead': 2, 'skipped': 1})
    with store.db:
        # telegram_message_refs 由 Notifications 懒创建，这里按同样的 DDL 建一次。
        store.db.execute("CREATE TABLE IF NOT EXISTS telegram_message_refs("
                         "event_key TEXT PRIMARY KEY,chat_id TEXT NOT NULL,message_id INTEGER NOT NULL,"
                         "photo INTEGER NOT NULL,edited TEXT NOT NULL DEFAULT '',"
                         "attempts INTEGER NOT NULL DEFAULT 0,next_try REAL NOT NULL DEFAULT 0)")
        store.db.execute("INSERT INTO deep_delete_queue(batch,kind,state,created,updated)"
                         " VALUES('b1','share','queued',1,1)")
        store.db.execute("INSERT INTO deep_delete_queue(batch,kind,state,created,updated)"
                         " VALUES('b1','share','done',1,1)")
        store.db.execute("INSERT INTO telegram_message_refs(event_key,chat_id,message_id,photo,next_try)"
                         " VALUES('k1','1',1,0,99)")
    text = Metrics(store).render(_FakeApp())
    assert 'strimkeep_deep_delete_queue{state="queued"} 1' in text
    assert 'strimkeep_deep_delete_queue{state="done"} 1' in text
    assert 'strimkeep_tg_retry_pending 1' in text
    assert 'strimkeep_share_check{result="alive"} 7' in text
    assert 'strimkeep_share_check{result="dead"} 2' in text
    assert 'strimkeep_share_check_timestamp 1234.5' in text
    store.close()


def test_check_alive_records_last_run_for_metrics(tmp_path):
    """存活检测跑完要把结果落到 meta，否则 /metrics 永远是空的。"""
    from strimkeep import share_service
    from strimkeep.cloud115 import CloudError
    from test_phase2_share_pipeline import share_setup

    async def run():
        store, cloud, http, engine, _ = share_setup(tmp_path)
        share_service.generate(store, engine.root, {'share_code': 'code', 'receive_code': ''},
                               [{'id': '1', 'name': 'a.mkv', 'size': 5000, 'path': 'Work/'}])

        async def expired(*args):
            raise CloudError('失效', 'SHARE_NOT_FOUND', 404)

        cloud.share_url_download = expired
        await share_service.check_alive(engine)
        meta = store.meta_json('share_check_last')
        assert meta and meta['dead'] == 0 and meta['skipped'] == 1 and meta['alive'] == 0 and meta['at'] > 0, meta
        await http.aclose()
        store.close()

    asyncio.run(run())


def test_http_clients_use_four_part_timeouts():
    """connect/read/write/pool 分开设，连不上时 5 秒就放弃，而不是傻等 15 秒。"""
    assert network.TIMEOUT.connect == 5.0
    assert network.TIMEOUT.read == 15.0
    assert network.TIMEOUT.write == 15.0
    assert network.TIMEOUT.pool == 5.0
