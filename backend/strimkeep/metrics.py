"""Prometheus 文本指标（0.8.22）。

默认**关闭**：只有设置了 `STRIMKEEP_METRICS_TOKEN` 环境变量（或 `meta.metrics_token`）
才暴露 `/metrics`，未配置时返回 404——避免把内部计数顺着 frp 隧道暴露到公网。
查询时用 `?token=` 或 `X-Metrics-Token` 头，常量时间比较。

指标只读、全部来自本地库与进程内计数，**不发起任何外部请求**：采集它不会消耗
115 配额，也不会让事件循环多出网络等待。

为什么单独一个模块：路由注册在 `create_app`（已经 1200 行），指标口径（哪些计数算
「状态」、哪些算「事件」）需要和 `store` 的表结构放在一起看；抽出来之后，改指标不用
再翻主文件。
"""
import json
import os
import time

from fastapi import HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

TOKEN_ENV = 'STRIMKEEP_METRICS_TOKEN'
# 这些路径是长连接/流式响应，耗时天然很长，不参与「慢请求」判定。
STREAMING_PREFIXES = ('/api/v1/tasks/events', '/api/v1/logs/events', '/play')
# 播放取链延迟直方图的桶（毫秒）。用 histogram 而不是客户端分位：
# Prometheus 官方建议「需要聚合/跨重启算分位时用 histogram，summary/预计算分位无法合并」。
PLAY_BUCKETS_MS = (50, 100, 200, 500, 1000, 2000, 5000)



def metrics_token(store):
    """指标访问令牌：环境变量优先，其次 meta（给不方便改 env 的部署留口子）。

    meta 有两种写法：文档里的裸字符串（直接 `INSERT OR REPLACE INTO meta`）和
    `store.set_meta()` 的 JSON 序列化。只读裸值的话，用 set_meta 写入会拿到带引号的
    `"abc"`，令牌永远比对不上（表现为「配了令牌还是 404」），所以两种都认。
    """
    env = os.environ.get(TOKEN_ENV)
    if env:
        return env
    raw = str(store.meta('metrics_token') or '')
    if not raw:
        return ''
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return raw.strip()
    return value.strip() if isinstance(value, str) else raw.strip()


def is_streaming(path):
    return any(path.startswith(prefix) for prefix in STREAMING_PREFIXES)


class Metrics:
    """进程内计数 + 从数据库聚合的指标。

    - 进程内（重启清零）：播放取链成功/失败/超时、取链耗时、慢请求次数；
    - 数据库（持久）：任务与条目状态分布、事件条数、未读通知、库文件大小；
    - meta（持久，由别的模块写）：事件循环延迟 `loop_lag`、115 限流运行时状态。
    """

    def __init__(self, store):
        self.store = store
        self.started = time.time()
        self.play = {'ok': 0, 'error': 0, 'timeout': 0}
        self.play_hist = [0] * (len(PLAY_BUCKETS_MS) + 1)   # 末桶 = +Inf
        self.play_sum = 0.0
        self.play_count = 0
        self.slow_requests = 0
        self.http = {}

    # ---------------------------------------------------------------- 写入
    def record_play(self, result, elapsed_ms):
        """记录一次播放取链结果。result ∈ ok / error / timeout。

        只有成功取链才进延迟直方图：失败耗时没有代表性，混进去会把 p95 拉偏。
        """
        self.play[result] = self.play.get(result, 0) + 1
        if result != 'ok' or not isinstance(elapsed_ms, (int, float)):
            return
        try:
            value = float(elapsed_ms)
        except (TypeError, ValueError):
            return
        if value < 0:
            return
        self.play_sum += value
        self.play_count += 1
        for index, bound in enumerate(PLAY_BUCKETS_MS):
            if value <= bound:
                self.play_hist[index] += 1
                return
        self.play_hist[-1] += 1

    def record_slow_request(self):
        self.slow_requests += 1

    def record_request(self, method, route, status):
        """按「方法 + 路由模板 + 状态码」计数：这是错误率的分母。

        只用路由模板（如 `/api/v1/share/strm/{sid}`），不用原始 URL，
        否则 `/play115/<pickcode>/...` 会制造无限多的 label。
        """
        try:
            key = (str(method).upper(), str(route or 'unmatched'), int(status))
        except (TypeError, ValueError):
            return
        self.http[key] = self.http.get(key, 0) + 1

    # ---------------------------------------------------------------- 读取
    def _counts(self, sql):
        try:
            return {str(key): int(value or 0) for key, value in self.store.db.execute(sql)}
        except Exception:
            return {}

    def render(self, app):
        """生成 Prometheus 文本（0.0.4 格式）。"""
        lines = []

        def add(name, value, help_text='', kind='gauge', labels=None):
            if help_text and not any(line.startswith('# HELP ' + name) for line in lines):
                lines.append('# HELP %s %s' % (name, help_text))
                lines.append('# TYPE %s %s' % (name, kind))
            suffix = ''
            if labels:
                suffix = '{' + ','.join('%s="%s"' % (k, v) for k, v in labels.items()) + '}'
            lines.append('%s%s %s' % (name, suffix, value))

        add('strimkeep_up', 1, '1 if the service is running')
        add('strimkeep_info', 1, 'Build information', labels={'version': getattr(app, 'version', '')})
        add('strimkeep_uptime_seconds', round(time.time() - self.started, 1),
            'Seconds since this process started')

        for state, count in sorted(self._counts(
                'SELECT state,count(*) FROM organize_jobs GROUP BY state').items()):
            add('strimkeep_jobs', count, 'Organize jobs by state', labels={'state': state})
        for state, count in sorted(self._counts(
                'SELECT state,count(*) FROM organize_items GROUP BY state').items()):
            add('strimkeep_items', count, 'Organize items by state', labels={'state': state})

        for name, sql in (('strimkeep_events_total', 'SELECT count(*) FROM events'),
                          ('strimkeep_notifications_unread',
                           "SELECT count(*) FROM notifications WHERE read_at IS NULL AND level<>'log'"),
                          ('strimkeep_organized_files', 'SELECT count(*) FROM organized_files')):
            try:
                add(name, int(self.store.db.execute(sql).fetchone()[0]),
                    'Row count derived from the local database')
            except Exception:
                add(name, 0, 'Row count derived from the local database')

        # 0.9.49：媒体信息的缺口必须能量化。0.9.1 把「本地探测完就收工」做进 `state`
        # 之后，Emby 那一半没有任何指标可看，现场静默了 24 小时才被人肉发现。
        # pending = 还在追；gave_up = 追不动了；两者同时不涨才算健康。
        for state in ('pending', 'gave_up', 'confirmed'):
            try:
                value = int(self.store.db.execute(
                    'SELECT count(*) FROM native_media_info_queue WHERE emby_state=?',
                    (state,)).fetchone()[0])
            except Exception:
                value = 0
            add('strimkeep_media_info_emby', value,
                'Managed STRM by Emby-side media info state', labels={'state': state})

        # 0.9.50：账本体检（ledger_audit 只读算出来的三个偏差），让「索引与文件不同步」
        # 变成可告警的数字，而不是等到某天一条 STRM 静默地没人管。
        ledger = self.store.meta_json('ledger_audit_last', {}) or {}
        for kind in ('stale_active', 'unregistered', 'orphan_sidecar'):
            if isinstance(ledger.get(kind), (int, float)):
                add('strimkeep_ledger_drift', ledger[kind],
                    'Ledger rows that disagree with the filesystem', labels={'kind': kind})
        if isinstance(ledger.get('at'), (int, float)):
            add('strimkeep_ledger_audit_timestamp', ledger['at'],
                'Unix time of the last ledger audit')

        lag = self.store.meta_json('loop_lag', {}) or {}
        for key, field in (('p50', 'p50_ms'), ('p95', 'p95_ms'), ('max', 'max_ms')):
            if lag.get(field) is not None:
                add('strimkeep_loop_lag_ms', lag[field], 'Event loop lag', labels={'quantile': key})

        for result, count in sorted(self.play.items()):
            add('strimkeep_play_total', count, 'Playback link requests by result',
                'counter', labels={'result': result})

        # 播放延迟：直方图（PromQL：histogram_quantile(0.95, sum(rate(..._bucket[5m])) by (le))）。
        cumulative = 0
        for index, bound in enumerate(PLAY_BUCKETS_MS):
            cumulative += self.play_hist[index]
            add('strimkeep_play_latency_ms_bucket', cumulative,
                'Playback link latency histogram (cumulative)', 'histogram', labels={'le': bound})
        cumulative += self.play_hist[-1]
        add('strimkeep_play_latency_ms_bucket', cumulative,
            'Playback link latency histogram (cumulative)', 'histogram', labels={'le': '+Inf'})
        add('strimkeep_play_latency_ms_sum', round(self.play_sum, 1),
            'Sum of playback link latency', 'counter')
        add('strimkeep_play_latency_ms_count', self.play_count,
            'Observed playback link latency samples', 'counter')

        # 0.9.60：任务状态机的守门计数。长期为 0 才说明所有写入方都按迁移表走；
        # 一旦上涨，说明有人在写未知状态或非法迁移（当时已经抛异常拦住了）。
        jobs = getattr(getattr(self, 'store', None), 'jobs', None)
        if jobs is not None:
            add('strimkeep_job_rejected', jobs.rejected,
                'Job state writes rejected by the transition table', 'counter')

        # 0.9.59：直链缓存命中率。缓存改成「按直链自己的有效期」之后，这两个数
        # 直接回答「播放器开播还需不需要等 115 接口」——命中就不打接口。
        cloud = getattr(getattr(app, 'state', None), 'cloud', None)
        for attribute, result in (('play_hits', 'hit'), ('play_misses', 'miss')):
            value = getattr(cloud, attribute, None)
            if isinstance(value, int):
                add('strimkeep_play_cache', value,
                    'Playback link cache lookups by result', 'counter', labels={'result': result})

        for state, count in sorted(self._counts(
                'SELECT state,count(*) FROM deep_delete_queue GROUP BY state').items()):
            add('strimkeep_deep_delete_queue', count,
                'Deep delete queue length by state', labels={'state': state})

        try:
            pending = int(self.store.db.execute(
                'SELECT count(*) FROM telegram_message_refs WHERE next_try>0').fetchone()[0])
        except Exception:
            pending = 0
        add('strimkeep_tg_retry_pending', pending,
            'Telegram retries waiting for their next_try time')

        share = self.store.meta_json('share_check_last', {}) or {}
        for key in ('alive', 'dead', 'skipped'):
            if isinstance(share.get(key), (int, float)):
                add('strimkeep_share_check', share[key],
                    'Last share alive check result', labels={'result': key})
        if isinstance(share.get('at'), (int, float)):
            add('strimkeep_share_check_timestamp', share['at'],
                'Unix time of the last share alive check')
        add('strimkeep_slow_requests_total', self.slow_requests,
            'Requests slower than one second', 'counter')

        for (method, route, status), count in sorted(self.http.items()):
            add('strimkeep_http_requests_total', count,
                'HTTP requests by method, route template and status',
                'counter', labels={'method': method, 'route': route, 'status': status})

        cloud = getattr(app.state, 'cloud', None)
        clock = getattr(cloud, 'clock', None)
        # 只在真实 Cloud115 上采集：它才有 clock() 与限流状态；测试替身没有，跳过而不是抛错。
        if cloud is not None and callable(clock):
            now = clock()
            add('strimkeep_cloud_throttle_scale', round(getattr(cloud, 'throttle_scale', 1.0), 3),
                'Current 115 throttle multiplier')
            add('strimkeep_cloud_cooldown_seconds',
                round(max(0.0, getattr(cloud, 'rate_limit_until', 0.0) - now), 0),
                'Remaining 115 cooldown')
            add('strimkeep_cloud_waf_seconds',
                round(max(0.0, getattr(cloud, 'waf_until', 0.0) - now), 0),
                'Remaining 115 WAF block')

        try:
            add('strimkeep_db_bytes', int(self.store.path.stat().st_size),
                'SQLite main file size')
        except OSError:
            pass
        try:
            wal = self.store.path.with_name(self.store.path.name + '-wal')
            add('strimkeep_db_wal_bytes', int(wal.stat().st_size) if wal.exists() else 0,
                'SQLite WAL file size (checkpoint backlog indicator)')
        except OSError:
            pass
        return '\n'.join(lines) + '\n'


def install_metrics(app, store):
    """挂 `/metrics`：令牌校验用常量时间比较；未配置令牌时按 404 处理（不暴露存在性）。"""
    import hmac

    @app.get('/metrics')
    async def metrics_endpoint(request: Request, token: str = Query('', max_length=200)):
        secret = metrics_token(store)
        supplied = token or str(request.headers.get('x-metrics-token') or '')
        if not secret or not hmac.compare_digest(secret, supplied[:200]):
            raise HTTPException(404)
        return PlainTextResponse(app.state.metrics.render(app),
                                 media_type='text/plain; version=0.0.4; charset=utf-8')

