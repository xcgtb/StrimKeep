# 指标与可观测性（0.8.22 起）

这份文档回答「出问题的时候看哪里」，以及「怎么把 StrimKeep 接进 Prometheus」。

## 一、三层证据，先看哪一层

| 层次 | 位置 | 回答什么问题 | 保留策略 |
| --- | --- | --- | --- |
| 操作记录 | `events` 表 → 页面「操作记录」/ 通知中心 | 发生了什么（人话句子，带任务/条目/rid） | 90 天，最多 2 万条（`prune`） |
| 延迟画像 | `meta.loop_lag` → 总览页「事件循环延迟」 | 事件循环被同步工作堵了多久（p50/p95/max，5 分钟窗口） | 每分钟覆盖写一次 |
| 指标 | `GET /metrics` | 现在的整体状态（计数、延迟分位、限流状态） | 由采集方决定；进程内计数随重启清零 |

排查顺序建议：**指标异常 → 看 loop_lag 是否同步升高 → 在操作记录里按 rid / 时间点找那条 `perf` 慢请求 → 结合 `docker logs` 的看门狗栈**。

## 二、开启 `/metrics`

默认**不存在**（返回 404）。两种开启方式，任选其一：
两种写法都支持：裸字符串（上面的 `INSERT`）和 `store.set_meta()` 的 JSON 串
（0.8.41 起 `metrics_token()` 会先按 JSON 解、解不动就当裸串，并去掉首尾空白）。


```sh
# 方式 A：容器环境变量（推荐，重启生效）
STRIMKEEP_METRICS_TOKEN=换成你自己的长随机串

# 方式 B：写进库里的 meta（不用改 compose，写一次即可）
docker exec strimkeep python - <<'PY'
import secrets, sqlite3
con = sqlite3.connect('/data/data/state.db', timeout=10)
token = secrets.token_hex(24)
con.execute("INSERT OR REPLACE INTO meta VALUES('metrics_token',?)", (token,))
con.commit()
print('token =', token)
PY
```

采集：

```sh
curl -s "http://127.0.0.1:9000/metrics?token=<令牌>"
curl -s -H "X-Metrics-Token: <令牌>" http://127.0.0.1:9000/metrics
```

注意：

- 未配置令牌时 `404`，配置后令牌不对也是 `404`（不区分「没开」和「令牌错」，避免被探测）。
- 令牌比较用 `hmac.compare_digest`，长度不限但建议 ≥ 24 字节 hex。
- **不要把 `/metrics` 直接暴露到公网**：令牌只是第二道门，正确做法是只让采集器在内网/隧道内访问。

## 三、指标清单

| 指标 | 类型 | 来源 | 说明 |
| --- | --- | --- | --- |
| `strimkeep_up` | gauge | 常量 | 进程活着就是 1 |
| `strimkeep_info{version}` | gauge | `app.version` | 版本信息 |
| `strimkeep_uptime_seconds` | gauge | 进程启动时间 | 重启即归零 |
| `strimkeep_jobs{state}` | gauge | `organize_jobs` | 各状态任务数 |
| `strimkeep_items{state}` | gauge | `organize_items` | 各状态条目数 |
| `strimkeep_events_total` | gauge | `events` | 操作记录总条数（接近 2 万说明 `prune` 没跑） |
| `strimkeep_notifications_unread` | gauge | `notifications` | 未读通知（不含 `log` 级） |
| `strimkeep_organized_files` | gauge | `organized_files` | 台账条数 |
| `strimkeep_loop_lag_ms{quantile="p50/p95/max"}` | gauge | `meta.loop_lag` | 事件循环延迟，5 分钟窗口 |
| `strimkeep_play_total{result="ok/error/timeout"}` | counter | 进程内 | 播放取链结果 |
| `strimkeep_play_latency_ms{quantile="p50/p95"}` | gauge | 进程内（最近 200 次） | 取链耗时 |
| `strimkeep_slow_requests_total` | counter | 进程内 | 超过 1 秒的请求数 |
| `strimkeep_cloud_throttle_scale` | gauge | `Cloud115` | 115 当前间隔倍数（1.0 = 正常，8.0 = 最保守） |
| `strimkeep_cloud_cooldown_seconds` | gauge | `Cloud115` | 剩余冷却秒数 |
| `strimkeep_cloud_waf_seconds` | gauge | `Cloud115` | 剩余 WAF 封锁秒数 |
| `strimkeep_db_bytes` | gauge | 文件系统 | SQLite 主文件大小 |

采集成本：每次渲染跑 6 条聚合查询 + 读一次 meta，**不发任何外部请求**，60 秒一次足够。

## 四、建议告警（PromQL）

```yaml
# 采集目标可见但无法抓取（包括服务掉线、网络异常或令牌错误）
- alert: StrimKeepDown
  expr: up{job="strimkeep"} == 0
  for: 5m

# 整个 job 不再出现在服务发现中时，单独检测目标消失
- alert: StrimKeepTargetMissing
  expr: absent(up{job="strimkeep"})
  for: 5m

# 事件循环被同步工作堵住（0.8.21 修完 sweep 后 p95 ≈ 11ms，>200ms 说明又冒出新的同步块）
- alert: StrimKeepLoopLagHigh
  expr: strimkeep_loop_lag_ms{quantile="p95"} > 200
  for: 15m

# 115 进了风控封锁：不用等冷却结束，立刻人工看
- alert: StrimKeepCloudBlocked
  expr: strimkeep_cloud_waf_seconds > 0
  for: 1m

# 播放取链开始失败
- alert: StrimKeepPlayErrors
  expr: increase(strimkeep_play_total{result="error"}[10m]) > 5

# 慢请求变多（有人把大查询加回主线程了）
- alert: StrimKeepSlowRequests
  expr: increase(strimkeep_slow_requests_total[15m]) > 10

# 未读通知堆积（Telegram 推送可能失效了）
- alert: StrimKeepNoticeBacklog
  expr: strimkeep_notifications_unread > 20
  for: 1h
```

上例假定抓取任务名为 `strimkeep`；若 job 不同，请修改选择器。`absent` 用于整个 job 消失；多目标环境如需识别某一个被移除的目标，应结合预期目标清单单独告警。

Prometheus 抓取示例：

```yaml
scrape_configs:
  - job_name: strimkeep
    scrape_interval: 60s
    metrics_path: /metrics
    params:
      token: ["<令牌>"]
    static_configs:
      - targets: ["192.168.1.10:9000"]
```
