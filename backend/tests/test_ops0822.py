"""0.8.22 回归：/metrics 口径与鉴权、request id、慢请求、播放计数、退避抖动、限流状态持久化。

背景见 docs/releases/RELEASE_0.8.22.md：

1. `/metrics` 默认不存在（未配置令牌时 404），配置后必须带对令牌；
2. 每个响应都带 `X-Request-Id`，慢请求（≥1 秒）才写一条操作记录；
3. 播放取链的成功/失败/耗时进入进程内计数，供 `/metrics` 与总览使用；
4. 通知与 Emby 核对的重试间隔带抖动，避免同一秒一起重发；
5. 115 的冷却与 WAF 封锁写进 meta，重启后仍然生效（以前只在内存里）。
"""
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from strimkeep.cloud115 import Cloud115, CloudError
from strimkeep.main import create_app
from strimkeep.metrics import Metrics
from strimkeep.notifications import Notifications
from strimkeep.store import Store
from test_app import FakeCloud

PASSWORD = 'test-password-1234'


def make_app(tmp_path, monkeypatch, token=None):
    monkeypatch.setenv('ADMIN_USERNAME', 'admin')
    monkeypatch.setenv('ADMIN_PASSWORD', PASSWORD)
    if token:
        monkeypatch.setenv('STRIMKEEP_METRICS_TOKEN', token)
    else:
        monkeypatch.delenv('STRIMKEEP_METRICS_TOKEN', raising=False)
    return create_app(tmp_path / 'data', tmp_path / 'strm', FakeCloud)


# ------------------------------------------------- 1) /metrics 的鉴权与口径

def test_metrics_is_hidden_when_no_token_configured(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        assert client.get('/metrics').status_code == 404
        assert client.get('/metrics?token=guess').status_code == 404


def test_metrics_requires_the_configured_token(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch, token='s3cret-token')
    with TestClient(app) as client:
        assert client.get('/metrics').status_code == 404
        assert client.get('/metrics?token=wrong').status_code == 404
        ok = client.get('/metrics?token=s3cret-token')
        assert ok.status_code == 200
        body = ok.text
        assert 'strimkeep_up 1' in body
        assert 'strimkeep_info{' in body and 'version="' in body
        assert 'strimkeep_events_total' in body
        assert 'strimkeep_play_total{result="ok"}' in body
        # 0.9.49：媒体信息的缺口必须能被 Prometheus 看见（空库也要有 0 值）。
        assert 'strimkeep_media_info_emby{state="pending"} 0' in body
        assert 'strimkeep_media_info_emby{state="gave_up"} 0' in body
        # 头部等价写法
        assert client.get('/metrics', headers={'X-Metrics-Token': 's3cret-token'}).status_code == 200


def test_metrics_unit_counts_play_and_latency(tmp_path):
    store = Store(tmp_path / 'data', 'admin', PASSWORD)
    metrics = Metrics(store)
    for value in (10.0, 20.0, 30.0, 40.0):
        metrics.record_play('ok', value)
    metrics.record_play('error', 5.0)
    metrics.record_play('timeout', 0.0)
    metrics.record_slow_request()
    body = metrics.render(type('App', (), {'version': '0.8.22', 'state': type('S', (), {})()})())
    assert 'strimkeep_play_total{result="ok"} 4' in body
    assert 'strimkeep_play_total{result="error"} 1' in body
    assert 'strimkeep_play_total{result="timeout"} 1' in body
    # 0.8.39 起延迟是 histogram（累计桶 + sum/count），不再有客户端 p50/p95 gauge。
    assert 'strimkeep_play_latency_ms_bucket{le="50"} 4' in body
    assert 'strimkeep_play_latency_ms_bucket{le="+Inf"} 4' in body
    assert 'strimkeep_play_latency_ms_count 4' in body
    assert 'strimkeep_play_latency_ms_sum 100.0' in body
    assert 'strimkeep_slow_requests_total 1' in body
    store.close()


# ------------------------------------------------- 2) request id 与慢请求

def test_every_response_carries_a_request_id(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        echoed = client.get('/health/live', headers={'X-Request-Id': 'abc12345'})
        assert echoed.headers['x-request-id'] == 'abc12345'
        generated = client.get('/health/live').headers['x-request-id']
        assert len(generated) == 8 and generated.isalnum()


def test_fast_request_does_not_write_a_perf_event(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        client.get('/health/live')
        rows = client.app.state.store.db.execute(
            "SELECT count(*) FROM events WHERE kind='perf'").fetchone()[0]
        assert rows == 0


# ------------------------------------------------- 3) 播放取链计数

def test_play_redirect_increments_metrics(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        store = app.state.store
        with store.db:
            store.db.execute(
                "INSERT INTO playbacks(id,file_id,pickcode,name,relative_path,token_hash,revoked,created)"
                " VALUES('pid1','f1','pc1','测试.mkv','a.strm','x',0,?)", (time.time(),))
        response = client.get('/play/pid1', follow_redirects=False)
        assert response.status_code == 302
        assert response.headers['location'].startswith('https://cdn.example/')
        metrics = app.state.metrics
        assert metrics.play['ok'] == 1 and metrics.play['error'] == 0
        assert metrics.play_count == 1
        assert sum(metrics.play_hist) == 1


# ------------------------------------------------- 4) 退避抖动

def test_notification_backoff_has_bounded_jitter():
    values = [Notifications.backoff(1) for _ in range(30)]
    assert all(25.5 <= value <= 34.5 for value in values)      # 30 秒 ±15%
    assert len(set(values)) > 1                                # 确实抖动
    assert Notifications.backoff(30) <= 900                    # 封顶不变


# ------------------------------------------------- 5) 115 限流状态持久化

def test_cooldown_survives_a_restart(tmp_path):
    store = Store(tmp_path / 'data', 'admin', PASSWORD)
    cloud = Cloud115(store)
    cloud.penalize()
    assert cloud.throttle_scale > 1.0
    persisted = store.meta_json('cloud_runtime', {})
    assert persisted.get('cooldown_until', 0) > time.time()

    revived = Cloud115(store)                                  # 模拟重启
    assert revived.throttle_scale > 1.0
    with pytest.raises(CloudError) as excinfo:
        revived.check_cooldown()
    assert excinfo.value.code == 'RATE_LIMITED'
    store.close()


def test_waf_block_survives_a_restart(tmp_path):
    store = Store(tmp_path / 'data', 'admin', PASSWORD)
    Cloud115(store).block_waf(405)
    revived = Cloud115(store)
    with pytest.raises(CloudError) as excinfo:
        revived.check_waf()
    assert excinfo.value.code == 'RATE_LIMITED'
    assert '封锁' in str(excinfo.value)
    store.close()


# ------------------------------------------------- 6) 看门狗线程不泄漏

def _watchdogs():
    return sum(1 for thread in threading.enumerate() if thread.name == 'sk-stall-watchdog')


def test_stall_watchdog_thread_stops_with_the_app(tmp_path, monkeypatch):
    """0.8.21 的看门狗线程永不退出：同一进程里反复建 app 会积累线程，并发
    faulthandler 转储还会让解释器崩掉（门禁曾出现退出码 139）。"""
    baseline = _watchdogs()
    for index in range(3):
        app = make_app(tmp_path / ('run%d' % index), monkeypatch)
        with TestClient(app):
            assert _watchdogs() == baseline + 1
        for _ in range(40):                       # 线程每 250 ms 检查一次停止信号
            if _watchdogs() == baseline:
                break
            time.sleep(0.05)
        assert _watchdogs() == baseline, '看门狗线程没有随应用退出'


# ------------------------------------------------- 7) close() 不抽走活线程的连接

def test_close_keeps_connections_owned_by_live_threads(tmp_path):
    """0.8.21 的 close() 会关掉所有线程的连接：工作线程正在 prune 时被抽掉连接，
    sqlite3 会在 C 层访问已释放内存（门禁实测 SIGSEGV，栈顶 _prune）。"""
    store = Store(tmp_path / 'data', 'admin', PASSWORD)
    kept = {}

    def work():
        kept['conn'] = store.db
        return store.db.execute('SELECT count(*) FROM events').fetchone()[0]

    pool = ThreadPoolExecutor(max_workers=1)
    try:
        pool.submit(work).result()
        store.close()                                # 只关本线程：工作线程的连接不能用不了
        assert pool.submit(work).result() is not None
    finally:
        pool.shutdown(wait=True)
    store.close()                                    # 线程已退出，这次连它的一起关
    with pytest.raises(sqlite3.ProgrammingError):
        kept['conn'].execute('SELECT 1')
    assert store.db.execute('SELECT count(*) FROM events').fetchone()[0] == 0   # 主线程可重建连接
    store.close()

