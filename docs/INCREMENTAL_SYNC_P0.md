# P0：Emby 增量入库与内存治理（2026-10-10）

## 适用范围与边界

基线：本次对话前一轮交付的 `StrimKeep-performance-tested-source.tar.gz`。这是一份 **P0** 源码优化，不是正式镜像，也不是飞牛系统现场验收。**未修改** `app/governance.py`、`app/domain/`、七维比较规则、STRM 文件删除路径、匹配阈值和删除确认。

- 近期入库仍显示**最近 24 小时**的电影部数、剧集部数及跨库去重集数；增量同步采用一次完整时间窗初始化，然后 20 分钟重叠窗口同步，按 Emby `Id` 去重（无 Id 使用路径兜底）。这项索引使用原有 SQLite `docs` 表，不创建独立 JSON 或新数据库。
- 每 **6 小时**重新完整核对近期 24 小时窗口。只依据 `DateCreated` 不可能实时捕捉删除、重新命名、修改后仍保留旧时间戳的条目；相关偏差可能在下次完整核对时才纠正。长时间断线后自动回退完整窗口。
- Emby `MinDateCreated` 在部分服务器可能无效，代码使用 `DateCreated` 降序和截止时间自行提前结束分页；若发现时间乱序则拒绝写入错误缓存并报告失败。不允许达到分页上限时静默写入不完整的统计。
- 总览数据仍每 5 分钟缓存更新，但**不再每轮**重拉 15.6 万条分集。全量 Emby 事实核对与 TMDB 原有独立手动扫描保留；总览自动事实核对最长约一天一次，异常时至少延后 1 小时再次尝试。首次无缓存、手动强制刷新仍需访问完整分集。
- 本地/分享 STRM 分类统计在正常情况下最长约 1 小时完整核对一次，而不是每 5 分钟 `rglob`。目前没有跨 CD2 / OpenList 挂载完全可靠的目录变更事件源，因此**不能承诺 STRM 文件新增后 5 分钟一定更新分类统计**；手动刷新、已知删除后的缓存失效路径保持准确性。
- 全量 Emby Episode 拉取改成每页流式处理并压缩保留的分集字段，不再把 15.6 万条原始对象放入 `_ep_cache`。真实 Python RSS 可能受其他缓存、任务并发和内存分配器影响。
- `/api/runtime/status` 增加 `memory.rss_mib` 与 `memory.peak_rss_mib` 用于量化内存变化，无后台高频采样。

## 与页面一致性

- 新增分集仍复用原有 `refresh_mapping_cache_after_ingest()`，按变化的 SeriesId 局部刷新双库 union 集数、TMDB 对照与 SQLite 快照。
- 普通增量轮询只传给映射更新器发生**新变化**的分集；首次完整近期对账传全部受影响分集，不受前端 500 条入库明细展示上限限制。
- 页面现有版本比较、前端轻量刷新机制保持原样；本次**未新增 SSE/WebSocket 推送**。实时推送及 NAS 目录增量跟踪属于后续 P1。
- 追更订阅/晨报路径保留，定时全量 TMDB 对照任务仍可能触发完整 Episode 拉取，这是安全校准的一部分。

## 本地测试

- `python -m pytest dev/tests/ -q`
- `node dev/tools/verify_frontend.cjs`
- `python dev/tools/static_check.py`
- `python dev/tools/release_check.py`
- `python dev/checks/verify_docs_schema_compat.py`
- `python dev/checks/verify_release_startup.py`

合成测试：对 1,600 部、155,684 集的相同数据，之前版本构建进程峰值 RSS 约 **186.5 MiB**，本轮 **159.1 MiB**，聚合快照哈希完全一致。这只覆盖单次**片库事实聚合**，不代表 NAS/CD2 的 Docker 总内存或端到端计时。

## 飞牛验收建议（先做测试实例）

1. 保留当前 Docker 镜像、SQLite `data/` 备份和 `docker-compose.yml`，不要直接覆盖生产 `data/`。
2. 部署测试镜像后，记录 `docker stats --no-stream strimkeep`，以及 `/proc/1/status` 的 VmRSS/VmHWM；分别在启动、空闲 15 分钟、两轮 5 分钟入库同步后采集。
3. 观察 `docker logs`：正常入库刷新出现“入库增量拉取”，不应每轮都出现“拉取分集 155684/155684”。首次无缓存及定时/手动全量核对仍会看到完整拉取。
4. 分别新增单集、补齐缺集、跨库补集，核验探索角标、映射明细、总览和入库记录；对删除、Emby 离线及 6 小时校准进行专项验收。
5. 用旧版与新版在**相同片库快照、相同规则**下 dry-run 对比治理计划及安全保护结果；正式治理执行仍按原文件事实核验。

### 回滚

回滚代码/镜像到上一版，恢复同一时刻保存的 SQLite 数据备份（若需要精确回到旧状态）；P0 只新增 `ingest_recent_items`、`overview_full_facts` 两份独立状态文档，不更改既有表结构。不要直接删除生产数据库文件。
