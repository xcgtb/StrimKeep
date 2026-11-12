# StrimKeep 0.8.21：一线程一连接、任务流/日志/总览出事件循环、延迟度量、Sync 页预览与回滚

0.8.20 把维护清理放进 `asyncio.to_thread` 时撞出 SIGSEGV：`sqlite3` 的连接不能跨线程
共用。当时只是「绕过」（让 prune 自己开一条连接）。本版把这件事变成**默认行为**，
并把三个最频繁的读路径搬进工作线程；同时补上「事件循环到底被阻塞了多少」的度量，
以及 STRM 重建在页面上的预览/回滚入口。

数据无 schema 变更，回滚不需要迁移。

## 一、改动

| 文件 | 改动 | 验收点 |
| --- | --- | --- |
| `backend/strimkeep/store.py` | `db` 从属性值改成**属性**：按调用线程懒创建连接（`_connect()` 统一设 WAL + `busy_timeout=5000` + `synchronous=NORMAL`），并在关闭时统一收拢；新增 `run_in_thread()` 说明性入口；`prune_in_thread()` 简化为「用本线程连接跑 `_prune`」 | 30 个线程并发写不崩、不丢数据；工作线程能看到主线程已提交的数据 |
| `backend/strimkeep/main.py` | 新增 `loop_lag_monitor()`：每 1 秒采样一次事件循环延迟，最近 300 个样本的 p50/p95/max 每分钟写入 `meta.loop_lag`；单次 ≥500 ms 写一条**限流**（60 秒）的操作记录。新增 `start_stall_watchdog()`：循环连续 1 秒没推进就把**所有线程栈** dump 到 stderr（5 分钟限流一次）。两者随 lifespan 启停 | 总览页 `system.loop_lag` 有数；`test_loop_lag_monitor_records_snapshot` |
| `backend/strimkeep/emby.py` | 新增 `EmbyDelivery.play_ids(item)`（从条目取播放入口 ID），`matches()` 改为调用它；语义不变 | `test_emby_play_ids_match_previous_semantics` |
| `backend/strimkeep/native_media_info.py` | 媒体信息补扫改为「先建 `pid → 条目` 索引、再逐个 STRM 查表」，并把索引与文件遍历整段放进 `asyncio.to_thread`；无播放入口的条目仍按路径后缀匹配（与老实现等价） | `test_native_media_info.py` 16 项；现场 16 秒启动阻塞消失 |
| `backend/strimkeep/phase5_api.py` | `/api/v1/tasks` 的整段构建（2 条任务查询 + 3 条统计 + 每条 `job_public`）进工作线程；`/tasks/events` 的 `stamp()` 与 `build()` 也进线程 | 端点返回结构不变；`test_task_and_log_endpoints_still_work` |
| `backend/strimkeep/console_api.py` | `/api/v1/logs`（带 LIKE，`events` 现场约 1.1 万行）、`/logs/events`（SSE 增量读）、`/dashboard` 里遍历 `organized_files` 的聚合进工作线程；`system` 增加 `loop_lag` | 同上 |
| `frontend/src/Sync.vue` | 「按当前地址重写全部本地 STRM」改成**先预览再执行**（`?dry_run=1` → 确认框显示「将重写 N 条 / 缺失 M 条」→ 执行）；新增「回滚上次重写」按钮（确认后调 `/strm/rebuild/rollback`） | 页面构建通过；按钮文案与提示在产物里 |
| `scripts/verify.sh` | 「自检与计数」组加入 `test_ops0821.py` | 门禁 9 组 |

## 二、行为变化（review 必须知道）

1. **`Store.db` 现在是属性**：`store.db` 在哪个线程读，就返回那个线程的连接。因此
   任何现有同步代码丢进 `asyncio.to_thread` 都自动安全——这也是后续继续搬迁
   剩余同步 SQL 的前提。测试里给 `store.db` 赋值的做法仍然支持（`@db.setter` 覆盖）。
2. **多线程写库**：WAL + `busy_timeout=5000` 下，并发写等锁而不是立刻报
   `database is locked`；极端情况下（>5 秒拿不到锁）才会抛错。
3. `/api/v1/tasks`、`/api/v1/logs`、`/api/v1/dashboard` 的**返回结构没变**，只是查询
   在 work 线程执行（`TestClient` 下同样正常）。
4. Sync 页的「重写」从一次请求变成「预览 + 确认 + 执行」三步，**取消不会写任何文件**。

## 三、为什么这次没动 `emby_deliveries.payload` 行式表

审计建议把 `targets` 从 JSON 拆成行式表。0.8.20 已把「总数 / 已确认」抽成列，
`job_public` 与 SSE 都不再解析 payload；剩下的读取只在投递状态机内部
（`submit/verify/finish/maybe_scan/tick`，每个活跃任务每 10 秒一次）。收益是
「payload 少几 KB」，代价是动四处状态机、回归面覆盖整个 Emby 入库链路。
按「风险与收益匹配」原则，本版不做；如果 0.8.21 的 `loop_lag` 数据显示这块真的
有影响，再单独一版处理。

## 四、度量当场抓到并修掉的 16 秒启动阻塞

`loop_lag` 的第一份快照就是 `max_ms: 16408`。同版本的看门狗
（`start_stall_watchdog`，事件循环 1 秒没推进就 dump 所有线程栈到 stderr）在容器里抓到了栈：

```
Thread 0x00007fce6f7ebb80 (most recent call first):
  File "/app/strimkeep/emby.py", line 554 in matches
  File "/app/strimkeep/native_media_info.py", line 328 in sweep_missing
  File "/app/strimkeep/native_media_info.py", line 267 in loop
```

**根因**：媒体信息补扫是「每个本地 STRM × 每个 Emby 条目」的双层循环——  
`for strm in root.rglob('*.strm')` 里再 `for item in items`，每个条目都重新跑一遍
`matches()` 的 URL 解析。现场 949 个 STRM × 全部电影/单集 = 十几万次重复解析，而且整段
跑在事件循环上，于是**每次启动都阻塞约 16 秒**（`docker logs` 里能看到启动后 16 秒内
没有任何请求，连 Docker 健康检查都排在后面）。

**修法（同一版本）**：

1. `EmbyDelivery.play_ids(item)` 把「从条目里取播放入口 ID」单独抽出来，`matches()` 改为
   调用它（语义不变，既有用例覆盖）；
2. 补扫先建一次 `pid → 条目` 索引，并单独保留「**没有**播放入口的条目」列表，
   继续用路径后缀匹配——这样索引化之后与老实现完全等价（否则会丢掉纯路径条目）；
3. 索引构建 + 文件遍历整段进 `asyncio.to_thread`（一线程一连接保证线程安全）。

**结果**（同机两次重启对比）：

| | 修复前 | 修复后 |
| --- | --- | --- |
| `loop_lag.max_ms` | 16408 / 16152 / 16399 / 18199（每次重启都复现） | **39.0**（240 个样本） |
| 看门狗告警 | 每次启动一条 | 无 |
| 启动到首次健康检查 | 16~19 秒 | 2.3 秒 |

> 这条也说明 `loop_lag` + 看门狗值得常驻：没有度量时，这 16 秒只会被感知成
> 「刚重启那会儿网页有点慢」，不会有人去查。

## 五、验证

| 项 | 结果 |
| --- | --- |
| 新增 `tests/test_ops0821.py`（6 项） | 6 passed：线程独立连接、30 线程并发写、线程读已提交数据、`loop_lag` 快照、`play_ids/matches` 等价性、三个端点 |
| 受影响回归（ops0819/ops0820/phase5/console060/app/iteration1/iteration2/emby064/emby_delivery_states） | **124 passed** |
| 补扫改造回归（ops0821 + native_media_info + emby064/061/delivery_states） | **22 passed**（首轮 4 项失败是因为索引化丢了「无播放入口条目按路径匹配」的旧语义，补齐后全绿） |
| 完整门禁 `sh scripts/verify.sh strimkeep-dev:0.8.21` | **9 组全部通过：403 项通过、1 skipped**（约 9 分钟，含组间 30 秒间隔） |
| 线上部署后 | `/health/ready` → `0.8.21`；`meta.loop_lag` = `{p50 5.4, p95 11.1, max 39.0, samples 240}`；镜像产物含「回滚上次重写」与 `dry_run=1`；30 分钟内 0 条 error 事件 |

## 六、升级 / 回滚

```sh
P="/path/to/StrimKeep"
docker tag strimkeep-dev:0.8.21 strimkeep:0.8.21
cd "$P" && cp docker-compose.yml "docker-compose.yml.before-0.8.21-$(date +%Y%m%d-%H%M%S)"
sed -i s/strimkeep:0.8.20/strimkeep:0.8.21/ docker-compose.yml
docker compose up -d && curl -s http://127.0.0.1:9000/health/ready

# 回滚（无 schema 变更；0.8.20 的两列与快照表继续保留）
sed -i s/strimkeep:0.8.21/strimkeep:0.8.20/ docker-compose.yml && docker compose up -d
```

## 七、已知未修

- 剩余约 450 处同步 SQL 仍在事件循环上（本版搬了任务列表、实时任务流、日志、总览聚合）；
  下一版用 `loop_lag` 的实测数据挑剩下的热点。
- `dashboard` 的 6 条 `count(*)` 查询仍在主线程（小表索引统计，量级在微秒）。
- `emby_deliveries.payload` 仍是整段 JSON（见第三节）。
- 探索页模块级缓存没有容量上限（审计 X-1）。
