# StrimKeep 0.8.22：Prometheus 指标、request id 与慢请求、播放统计、退避抖动、115 限流状态持久化

0.8.21 解决的是「事件循环被堵了多久」，本版回答剩下四个运维问题：

1. **「现在整体什么状态？」** → `GET /metrics`（Prometheus 文本），默认关闭，带令牌才开；
2. **「这次请求慢在哪？」** → 每个响应带 `X-Request-Id`，超过 1 秒的请求自动写一条带 rid 的操作记录；
3. **「播放到底成功多少？」** → 播放取链的成功/失败/超时与耗时分位数进指标；
4. **「重启后限流状态还在吗？」** → 115 的冷却与 WAF 封锁写进 `meta`，重启接回；
   顺带把通知、Emby 核对、115 冷却的重试间隔加上抖动，避免同一秒一起重发。

数据无 schema 变更（只写 `meta`），回滚不需要迁移。

## 一、改动

| 文件 | 改动 | 验收点 |
| --- | --- | --- |
| `backend/strimkeep/metrics.py`（新增） | `Metrics`：进程内播放计数与耗时、慢请求计数；`render()` 输出 Prometheus 0.0.4 文本；`install_metrics()` 挂 `/metrics`，令牌来自 `STRIMKEEP_METRICS_TOKEN` 或 `meta.metrics_token`，**未配置时返回 404**（不暴露存在性），比较用 `hmac.compare_digest` | `test_metrics_*` 3 项 |
| `backend/strimkeep/main.py` | 中间件生成/透传 `X-Request-Id`；≥1 秒且非流式路径写一条 `perf` 事件（带 rid）；两个播放 handler 在 `finally` 里记录取链结果与耗时；挂载 `/metrics`；**看门狗线程随生命周期退出 + 全局转储锁**（见第四节） | `test_every_response_carries_a_request_id`、`test_play_redirect_increments_metrics`、`test_stall_watchdog_thread_stops_with_the_app` |
| `backend/strimkeep/store.py` | `close()` 只关闭本线程与已退出线程的连接，登记表改为 `(thread_id, conn)`（见第四节） | `test_close_keeps_connections_owned_by_live_threads` |
| `backend/strimkeep/cloud115.py` | 冷却时间加 ±15% 抖动；`_persist_runtime()`/`_restore_runtime()` 把 `throttle_scale`、冷却、WAF 封锁写进 `meta.cloud_runtime` 并在启动时接回（墙钟 → monotonic 换算） | `test_cooldown_survives_a_restart`、`test_waf_block_survives_a_restart` |
| `backend/strimkeep/notifications.py` | 重试退避 30s→900s 封顶不变，额外乘 ±15% 抖动 | `test_notification_backoff_has_bounded_jitter` |
| `backend/strimkeep/emby.py` | 入库核对重试间隔乘 ±20% 抖动 | 既有 Emby 用例 |
| `scripts/verify.sh` | 「自检与计数」组加入 `test_ops0822.py` | 门禁 9 组 |

## 二、`/metrics` 怎么用

```sh
# 1) 先给一个令牌（容器环境变量，或写进 meta）
STRIMKEEP_METRICS_TOKEN=某个长随机串
# 2) 采集
curl -s "http://127.0.0.1:9000/metrics?token=某个长随机串"
curl -s -H "X-Metrics-Token: 某个长随机串" http://127.0.0.1:9000/metrics
```

未配置令牌时该路径返回 **404**（和不存在一样），不会因为「忘了设令牌」把内部计数暴露到公网隧道。

导出内容：

| 指标 | 类型 | 说明 |
| --- | --- | --- |
| `strimkeep_up` / `strimkeep_info{version}` / `strimkeep_uptime_seconds` | gauge | 存活、版本、运行时长 |
| `strimkeep_jobs{state}` / `strimkeep_items{state}` | gauge | 任务与条目状态分布 |
| `strimkeep_events_total` / `strimkeep_notifications_unread` / `strimkeep_organized_files` | gauge | 日志条数、未读通知、台账条数 |
| `strimkeep_loop_lag_ms{quantile="p50/p95/max"}` | gauge | 0.8.21 的事件循环延迟（读 `meta.loop_lag`） |
| `strimkeep_play_total{result="ok/error/timeout"}` | counter | 播放取链结果（进程内） |
| `strimkeep_play_latency_ms{quantile="p50/p95"}` | gauge | 取链耗时（最近 200 次） |
| `strimkeep_slow_requests_total` | counter | 超过 1 秒的请求数 |
| `strimkeep_cloud_throttle_scale` / `_cooldown_seconds` / `_waf_seconds` | gauge | 115 当前限流状态 |
| `strimkeep_db_bytes` | gauge | SQLite 主文件大小 |

> 采集不产生任何外部请求，也不会消耗 115 配额。

## 三、行为变化（review 必须知道）

1. **每个响应多一个 `X-Request-Id`**（请求头带 `X-Request-Id` 时原样回显，否则随机 8 位）。
2. **慢请求才写日志**：≥1 秒且不属于 `/api/v1/tasks/events`、`/api/v1/logs/events`、`/play*`
   的请求会写一条 `perf` 事件；正常请求零写入（不会把日志表刷爆）。
3. **115 冷却/WAF 会落盘**：写 `meta.cloud_runtime`；重启后按墙钟剩余秒数接回。
   `burst`/`pause_ms` 那套触发逻辑不变。
4. **退避带抖动**：同一批任务不会在同一秒一起重试。上限（300 秒冷却、900 秒通知重试）不变。
5. `/metrics` 默认 404。

## 四、门禁当场抓到的两个崩溃（本版修复）

这两个都不是新写的代码引入的，而是 0.8.21 的**结构性缺陷**，只有「同一进程里反复建 app」
或「关库时恰好有后台任务在跑」才会暴露；线上是低频重启，属于「迟早踩、但极难复现」的那类。

### 1. 看门狗线程泄漏 + 并发 `faulthandler` 转储 → 退出码 139

`start_stall_watchdog()` 起的线程是 `while True: sleep(0.25)`，应用关闭时**不会退出**；测试
进程里跑十几个 app 就积累十几个看门狗。`faulthandler.dump_traceback()` 不是线程安全的，
十几个线程同时命中同一个停顿 → 并发转储 → 解释器在退出时 SIGSEGV。

修法：心跳协程被取消时（应用关闭）置 `threading.Event` 让线程退出；转储前再抢一把**全局
非阻塞锁** `_DUMP_LOCK`，同一时刻只允许一个转储，其余跳过。

### 2. `Store.close()` 抽走活线程的连接 → `_prune` 里 SIGSEGV

0.8.21 把 `Store.db` 改成「一线程一连接」，但 `close()` 仍然关闭**所有**线程的连接。事件循环
这一侧关闭（应用关闭、测试收尾）时，若某个 `to_thread` 工作线程正在 `prune_in_thread()` 里执行
语句，它的连接就被从脚下抽掉 —— sqlite3 在 C 层访问已释放内存，进程直接崩。

门禁原始现场（`Fatal Python error: Segmentation fault`）：

```
Current thread ...:
  File "/app/repo/backend/strimkeep/store.py", line 446 in _prune
  File "/app/repo/backend/strimkeep/store.py", line 411 in prune_in_thread
  File "/usr/local/lib/python3.12/concurrent/futures/thread.py", line 59 in run
```

修法：`close()` 只关闭「本线程的连接」和「所属线程已经退出、没人再能用的连接」；活着的线程
自己管自己的连接，线程结束时 `threading.local` 会在**它自己的线程里**释放，安全。登记表从
`[conn]` 改成 `[(thread_id, conn)]` 才能做这个判断。

线上影响：重启时若恰好在跑「定期维护」（prune），旧版本可能不是干净退出而是崩溃退出
（数据安全，WAL 会回滚未完成事务，但会留下 core）。本版之后不再可能。

## 五、验证

| 层次 | 命令 | 结果 |
| --- | --- | --- |
| 新增用例 | `tests/test_ops0822.py` | **11 项通过** |
| 受影响回归 | `test_ops0819/0820/0821` + `test_app` + `iteration1` + `phase5` + `transfer` + `cards062` + `emby064` + `play85` + `live_file_state` | **164 项通过** |
| 完整门禁 | `sh scripts/verify.sh strimkeep-dev:0.8.22` | **414 通过 / 1 跳过，9 组全绿**（最慢组「转存与洗版」69 项 / 133 秒） |

门禁**第一次**跑（修第四节那两个崩溃之前）在最后一组直接崩掉：`退出码 139`，栈顶是
`store.py:446 _prune`；修复后同一组 37 项通过、退出码 0。这两个 bug 就是这样被现场抓出来的
——它们只在「同一进程里反复建 app / 关库时有后台任务」时出现，平时跑测试很难碰到。

部署后现场（启动 90 秒）：

```sh
$ curl -s http://127.0.0.1:9000/health/ready
{"status":"ready","version":"0.8.22","stage":5}

$ curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:9000/metrics            # 无令牌
404
$ curl -s -o /dev/null -w '%{http_code}\n' 'http://127.0.0.1:9000/metrics?token=x'  # 错令牌
404
$ curl -s "http://127.0.0.1:9000/metrics?token=<令牌>" | head -3
strimkeep_up 1
strimkeep_info{version="0.8.22"} 1
strimkeep_uptime_seconds 83.3

$ curl -s -D - -o /dev/null http://127.0.0.1:9000/health/live | grep -i x-request-id
x-request-id: 8e568788
```

- 任务分布（线上 228 条）→ `strimkeep_jobs{state="completed"} 200` / `partial 21` / `cancelled 7`；
- `meta.loop_lag`（启动 90 秒后）：`p50 5.6ms / p95 59.7ms / max 82.2ms`（启动期偏高属预期）；
- 近 30 分钟 `error` 事件：**0**；容器 `running`，`RestartCount 0`。

## 六、升级 / 回滚

```sh
P="/path/to/StrimKeep"
docker tag strimkeep-dev:0.8.22 strimkeep:0.8.22
cd "$P" && cp docker-compose.yml "docker-compose.yml.before-0.8.22-$(date +%Y%m%d-%H%M%S)"
sed -i s/strimkeep:0.8.21/strimkeep:0.8.22/ docker-compose.yml
docker compose up -d && curl -s http://127.0.0.1:9000/health/ready

# 回滚（无 schema 变更；meta 里多出的键旧版本会忽略）
sed -i s/strimkeep:0.8.22/strimkeep:0.8.21/ docker-compose.yml && docker compose up -d
```

## 七、已知未修

- 指标是**拉取式**的，没有内置看板/告警；要告警得自己接 Prometheus + Alertmanager（或按
  `loop_lag`、`strimkeep_cloud_waf_seconds` 写一条定时查询）。
- `events` 表仍没有 `level`/`fields` 列（0.8.21 之前就记在待办里）：慢请求与播放入口日志
  仍是「格式化中文句子 + rid」，按字段聚合只能靠 `LIKE`。
- `offline_add` 仍未纳入全局节流（p115client 的 proapi 端点不接受 call() 注入参数）。
- 剩余同步 SQL 未搬：按 `loop_lag` 实测（p50 5.4ms / p95 11ms）暂不紧急。
- 看门狗在线程池里仍可能因为「转储正在别处进行」而跳过，属预期（宁可少一条日志也不崩进程）。
- `loop_lag` 快照在样本满 300 之后**每秒**写一次 meta：0.8.21 的条件是
  `len(samples) % 60 == 0`，而 deque 满 300 后这个式子恒为真。量级很小（WAL NORMAL 下
  每次几百字节），但属于无谓写入；0.8.23 改成「距上次写入 ≥ 60 秒」，顺带让启动后第一
  分钟就有数据（现在要等 60 个样本）。

