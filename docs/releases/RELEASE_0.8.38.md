# StrimKeep 0.8.38：砍掉深删第二条真删旁路、补错误率指标、备份脚本入库

## 一、为什么做这三件事

0.8.37 的全链路排查把「功能 bug」清完了，剩下的第一优先级是**工程风险**：

1. `deep_delete.apply()` 里还留着一条「托管文件直接真删」的代码路径（含 `confirm_recycled` 回读确认）。
   0.8.34 起真实入口已强制走 `guarded_managed_delete`，这段只剩预演能走到 —— 但代码还在，
   后来人完全可能照着它再写一条绕过「队列 + 回收确认」的删除。
2. `/metrics` 只有并发/任务/播放等指标，**没有 HTTP 请求计数**，算不出错误率分母
   （SRE 四黄金信号里的「错误」缺一半）。
3. 备份脚本不在仓库：`scripts/deploy.sh` 的 `SK_BACKUP` 默认指向 `/tmp/sk-backup.sh`，
   换台机器就没了。SQLite 在 WAL 模式下**不能用 `cp`** 备份。

## 二、改动

| 文件 | 改动 |
| --- | --- |
| `deep_delete.apply()` | 托管分支**只保留预演**（本地 STRM/NFO/网盘文件都只标「预演」）；删除真删体、`pending` 与 `confirm_recycled()` 调用 |
| `deep_delete.confirm_recycled()` | **删除**（唯一调用方已不存在）。托管真删从此只有一个入口：`guarded_managed_delete()` |
| `share_service.generate()` | 写库失败时的回滚删除改为 `purge_strm(..., store=store)`，与「删 STRM 必登记」全局约定一致 |
| `metrics.py` | 新增 `record_request(method, route, status)` 与 `strimkeep_http_requests_total{method,route,status}` 计数器；label 只用**路由模板**（`/api/v1/share/strm/{sid}`），不用原始 URL，避免 `/play115/<pickcode>/...` 制造无限基数 |
| `main.py` | HTTP 中间件在响应后记录一次请求计数（`request.scope['route'].path`；未匹配记 `unmatched`） |
| `scripts/backup.sh`（新增） | SQLite 一致快照：优先 `sqlite3 .backup`，无 CLI 时退回 Python `backup()`；先写 `.part` 自检再改名；一并备份 `config/master.key`（600）；按份数轮转 |
| `docs/operations/BACKUP_RESTORE.md`（新增） | 备份/恢复/演练步骤；明确「WAL 模式不要 `cp state.db`」 |
| `tests/test_ops0838.py`（新增） | 2 项：`/metrics` 按路由模板+状态码计数；备份脚本产出**可打开且含数据**的 SQLite 快照、无 `.part` 残留 |
| `scripts/verify.sh` | 「自检与计数」组加入 `test_ops0838.py` |

### 依据（公开文献）

- **错误率/四黄金信号**：Google SRE Book 第 6 章要求按「延迟、流量、错误、饱和度」观测，
  且错误必须能算比例（需要分子/分母）→ https://sre.google/sre-book/monitoring-distributed-systems/
- **指标类型**：Prometheus 官方对需要聚合的分位数推荐 histogram；本次先补计数器（分母），
  分位数 histrogram 化留到下一档 → https://prometheus.io/docs/practices/histograms/
- **SQLite 在线备份**：WAL 模式下应使用 `.backup` / `VACUUM INTO` 做一致快照
  → https://sqlite.org/wal.html

## 三、验证

| 层次 | 命令 | 结果 |
| --- | --- | --- |
| 新增用例 | `tests/test_ops0838.py`（2 项） | 通过 |
| 受影响回归 | `test_deep_delete` / `test_share_check_safety` / `test_share_explicit_regenerate` / `test_share_pipeline_recovery` / `test_phase2_share_pipeline` / `test_share_parent_hint` | 63 项通过 |
| 完整门禁 | `sh scripts/verify.sh strimkeep-dev:0.8.37` | **`全部通过`，642 项通过 / 1 项跳过，11 组全绿** |

## 四、已知边界

- `/metrics` 仍是**默认关闭**（需 `STRIMKEEP_METRICS_TOKEN` 或 `meta.metrics_token`），
  这是刻意的：避免经 frp 暴露到公网。
- 请求计数只在进程内累计，重启清零；跨重启/多实例的 SLO 需要 histrogram + 长期存储。
- 备份脚本默认保留 14 份、不含 `strm/` 媒体目录（STRM 可由同步重建，属于派生数据）。
