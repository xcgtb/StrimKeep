# StrimKeep 0.8.39：可观测性补齐（直方图 + 三个 gauge + 告警规则）与四段超时

## 一、这一版解决什么

0.8.38 补了「错误率的分母」，但仍有三处可观测性缺陷和一个超时隐患：

1. 播放延迟用**客户端算 p50/p95 gauge**：重启清零、无法跨实例聚合并发生成一个分位数
   （Prometheus 官方明确：需要聚合时应用 histogram）。
2. 深删队列、Telegram 重试积压、分享存活检测结果**完全没有指标**，出问题只能看日志。
3. `httpx` 全是 `timeout=15` 单值：DNS/握手卡住时也要等满 15 秒，且读/写/池排队无法分别调。
4. 没有成文的告警规则与排障顺序。

## 二、改动

| 文件 | 改动 |
| --- | --- |
| `metrics.py` | 播放延迟改 **histogram**：`strimkeep_play_latency_ms_bucket{le=50/100/200/500/1000/2000/5000/+Inf}` + `_sum` + `_count`（累计桶，只有成功取链入桶）；删除客户端 `_percentile()` 与 p50/p95 gauge |
| `metrics.py` | 新增 gauge：`strimkeep_deep_delete_queue{state}`、`strimkeep_tg_retry_pending`、`strimkeep_share_check{result=alive/dead/skipped}` 与 `strimkeep_share_check_timestamp`、`strimkeep_db_wal_bytes` |
| `share_service.check_alive()` | 结果落到 `meta.share_check_last`（`at/alive/dead/skipped`），否则指标永远是空 |
| `network.py` | 引入 `TIMEOUT = httpx.Timeout(connect=5, read=15, write=15, pool=5)`，4 个客户端统一使用 |
| `cloud115.py` | 播放取链 `Timeout(connect=5, read=10, write=10, pool=5)`；常规 API `Timeout(connect=5, read=15, write=15, pool=5)` |
| `docs/operations/ALERTING.md`（新增） | 指标一览 + 9 条 PromQL 告警（不可用/5xx 错误率/播放 p95/loop lag/深删积压/TG 积压/115 限流/WAL 膨胀/存活检测过期）+ 症状→指标排障顺序 |
| `tests/test_ops0839.py`（新增） | 4 项：直方图累计桶与 +Inf、失败不计入延迟；队列/TG/分享 gauge；`check_alive` 落 meta；四段超时常量 |
| `scripts/verify.sh` | 「自检与计数」组加入 `test_ops0839.py` |

### 明确不做：事件表 FTS5

实测容器内 SQLite 3.40.1 **支持 FTS5**，但 `events` 表被 `prune()` 限制在 **20000 行 / 90 天**
（`event_max=20000`），当前 `LIKE '%关键词%'` 在 2 万行上是毫秒级。给 2 万行上建 FTS
（+触发器 +回填 +迁移）属于**过度设计**，收益低于引入的不一致风险。等事件保留量提高到
几十万行、或要做分词/多字段检索时再上。

### 依据

- Prometheus《Histograms and summaries》：需要聚合/可变分位时用 histogram；
  原生 histogram 更好，classic 需选桶边界 → https://prometheus.io/docs/practices/histograms/
- Google SRE Book 第 6 章：四黄金信号、按症状告警、dashboard 覆盖次级问题
  → https://sre.google/sre-book/monitoring-distributed-systems/
- `httpx` Timeout 支持 connect/read/write/pool 四段（库自带能力，之前只用单值）

## 三、验证

| 层次 | 命令 | 结果 |
| --- | --- | --- |
| 新增用例 | `tests/test_ops0839.py`（4 项） | 通过 |
| 受影响回归 | `test_share_check_safety` / `test_share_schedule` / `test_phase1_foundation` / `test_sync_stale` / `test_offline_throttle` / `test_native_media_info` | 52 项通过 |
| 完整门禁 | `sh scripts/verify.sh strimkeep-dev:0.8.38` | **`全部通过`，646 项通过 / 1 项跳过，11 组全绿** |

## 四、已知边界

- 桶是毫秒制、上限 5000ms，超出落 `+Inf`；要改精度改 `PLAY_BUCKETS_MS` 一处即可。
- 进程内计数重启清零：跨重启/多实例的 SLO 需要 Prometheus 长期存储，不靠本进程。
- `strimkeep_share_check*` 只在真的跑过一次存活检测后才有值；从未跑过时该指标不出现（而不是假 0）。
