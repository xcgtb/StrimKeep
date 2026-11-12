# 监控与告警

`/metrics` 默认**关闭**（避免经 frp 暴露到公网）。启用方式二选一：

```sh
# 方式一：环境变量（推荐）
STRIMKEEP_METRICS_TOKEN=<32 位以上随机串>
# 方式二：不方便改环境变量时写进 meta
#   store.set_meta('metrics_token', '<随机串>')
```

抓取：`curl -s 'http://<host>:9000/metrics?token=<随机串>'`，或 Prometheus 的
`bearer_token` / `params` 方式带上同一个值。

## 指标一览

| 指标 | 类型 | 含义 |
| --- | --- | --- |
| `strimkeep_up` / `strimkeep_info{version}` | gauge | 存活与版本 |
| `strimkeep_uptime_seconds` | gauge | 进程运行秒数 |
| `strimkeep_jobs{state}` / `strimkeep_items{state}` | gauge | 任务/条目状态分布 |
| `strimkeep_http_requests_total{method,route,status}` | counter | 请求数，**错误率的分母**；label 用路由模板 |
| `strimkeep_slow_requests_total` | counter | 超过 1 秒的请求数 |
| `strimkeep_play_total{result}` | counter | 播放取链成功/失败/超时 |
| `strimkeep_play_latency_ms_bucket{le}` / `_sum` / `_count` | histogram | 播放取链延迟（**仅成功**样本） |
| `strimkeep_loop_lag_ms{quantile}` | gauge | 事件循环延迟 p50/p95/max |
| `strimkeep_cloud_throttle_scale` / `_cooldown_seconds` / `_waf_seconds` | gauge | 115 限流状态 |
| `strimkeep_deep_delete_queue{state}` | gauge | 深删队列长度（`queued` 长期不降 = 卡住） |
| `strimkeep_tg_retry_pending` | gauge | Telegram 待重试条数 |
| `strimkeep_share_check{result}` / `_timestamp` | gauge | 最近一次分享存活检测结果与时间 |
| `strimkeep_db_bytes` / `strimkeep_db_wal_bytes` | gauge | 主库与 WAL 体积 |

## 建议的告警（PromQL）

```yaml
groups:
- name: strimkeep
  rules:
  # 1) 服务不可用（最常见、最该先配）
  - alert: StrimKeepDown
    expr: up{job="strimkeep"} == 0 or strimkeep_up == 0
    for: 3m

  # 2) HTTP 5xx 错误率 > 5%（四黄金信号里的「错误」）
  - alert: StrimKeepErrorRateHigh
    expr: |
      sum(rate(strimkeep_http_requests_total{status=~"5.."}[5m]))
        / sum(rate(strimkeep_http_requests_total[5m])) > 0.05
    for: 10m

  # 3) 播放取链 p95 > 2s（Emby 起播变慢）
  - alert: StrimKeepPlayLatencyHigh
    expr: |
      histogram_quantile(0.95,
        sum(rate(strimkeep_play_latency_ms_bucket[10m])) by (le)) > 2000
    for: 15m

  # 4) 事件循环卡顿（同步 SQL / 大 JSON 解析拖住 loop）
  - alert: StrimKeepLoopLagHigh
    expr: strimkeep_loop_lag_ms{quantile="p95"} > 250
    for: 10m

  # 5) 深删队列堆积（有 queued 不消费，通常是 dry_run 开关或登录失效）
  - alert: StrimKeepDeepDeleteBacklog
    expr: max_over_time(strimkeep_deep_delete_queue{state="queued"}[30m]) > 0
    for: 30m

  # 6) Telegram 重试积压（通知发不出去）
  - alert: StrimKeepTgRetryBacklog
    expr: strimkeep_tg_retry_pending > 20
    for: 30m

  # 7) 115 限流/封禁持续（继续打只会更糟，先降速）
  - alert: StrimKeepCloudThrottled
    expr: strimkeep_cloud_cooldown_seconds > 60 or strimkeep_cloud_waf_seconds > 60
    for: 10m

  # 8) WAL 未合并（体积持续增长说明长时间读事务/写放大）
  - alert: StrimKeepWalGrowing
    expr: strimkeep_db_wal_bytes > 268435456
    for: 1h

  # 9) 分享存活检测长期没跑（定时任务失效）
  - alert: StrimKeepShareCheckStale
    expr: time() - strimkeep_share_check_timestamp > 86400 * 2
    for: 1h
```

## 排障顺序（症状 → 先看哪个指标）

1. **页面打不开/很慢**：`strimkeep_up` → `strimkeep_slow_requests_total` → `strimkeep_loop_lag_ms`；
   再对照日志中心里带 `rid` 的慢请求事件。
2. **Emby 起播转圈**：`strimkeep_play_total{result="timeout"}` 与 `..._latency_ms` 的 p95；
   再看 `strimkeep_cloud_cooldown_seconds` / `_waf_seconds`（被限流时取链必慢）。
3. **同步/整理不动**：`strimkeep_jobs{state="running"}` 是否一直不降 + `strimkeep_loop_lag_ms`。
4. **通知不到**：`strimkeep_tg_retry_pending` 持续增长 = 令牌/网络问题；
   为 0 但仍收不到，去通知中心看 `telegram_state`。

> 原则（SRE Book 第 6 章）：**按症状告警**，不要为每个内部原因各配一条；
> 原因类指标留着排障用。上面 9 条都是「用户能感觉到」的症状或即将耗尽的资源。
