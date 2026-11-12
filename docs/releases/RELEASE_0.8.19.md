# StrimKeep 0.8.19：终态收口、转存密钥生命周期、任务视图批量统计、媒体库路径自检

本版只处理「运维面」的四件事，不改整理/转存/STRM/播放的业务判定逻辑。数据无 schema 变更，回滚不需要迁移。

## 一、现象（0.8.18 现场实测）

| # | 现象 | 实测数据（2026-11-03 23:5x） |
| --- | --- | --- |
| 1 | 任务中心「未处理 / 未识别」数字与实际不符：点进去看不到可操作项 | `organize_items` 中 294 条停在 `discovered`，所属任务全部已是 `completed(192)/partial(20)/cancelled(5)` |
| 2 | 加密的分享码/提取码越积越多 | `settings` 里 21 条 `transfer-secret:<job>`，含已完成任务的 |
| 3 | 任务列表与实时流对每条任务各跑 2~3 条统计 | 30 个任务时每 2 秒约 62 次全表扫描（`organize_items` 4582 行），空闲 CPU 9.16% |
| 4 | Emby 媒体库路径漂移没有任何提示 | 0.6.5 现场：STRM 全对，Emby 库仍指旧目录，只能等 30 分钟核对超时后报「未确认」 |

## 二、根因

1. `Organizer.manifest()` 先把整批文件写成 `state='discovered'`，再逐条推进；`pause` / `cancel` / 异常退出 / 退休任务（`classification_migration`）都不回收入口，于是「任务已结束、条目还在发现态」的行永久留在库里。
2. 转存密钥写在 `settings`，只有写入没有删除：`Store.prune()` 只清 `events` / `notifications` / `sessions` / `organize_items` / `deep_delete_queue`。
3. `job_public()` 里的 `unidentified` 与 `filtered_items` 是逐条 `LIKE` 查询；`/api/v1/tasks`（40 条历史 + 进行中）与 `/api/v1/tasks/events`（每 2 秒、最多 30 条）都会放大成 N+1。
4. 库路径覆盖检查只在「有任务投递且核对不完整」时执行（`EmbyDelivery.coverage_problem`），启动与空闲时没有任何检查。

## 三、改动

| 文件 | 改动 | 验收点 |
| --- | --- | --- |
| `backend/strimkeep/store.py` | 新增常量 `TERMINAL_JOB_STATES` / `TRANSFER_SECRET` / `TRANSFER_UPDATE`；新增 `settle_terminal_job()`、`reconcile_terminal_items()`；`prune()` 增加「已结束任务的转存密钥」与「`transfer-update` 保留最近 200 条」，返回值新增 `transfer_secrets` / `stale_updates` | 终态收口幂等；运行中任务条目不动；prune 后密钥只剩在跑的 |
| `backend/strimkeep/organizer.py` | `update_job()` 在 `state ∈ (completed, partial, cancelled)` 时调用 `store.settle_terminal_job()`（4 行，mixed EOL 已保持） | 所有终态入口统一收口，不散落在各调用点 |
| `backend/strimkeep/main.py` | lifespan 增加启动自检与媒体库自检任务（60 秒延迟、退出时取消）；新增 `job_item_stats()` 批量统计并注册 `app.state.job_item_stats`；`job_public()` 支持传入批量结果（不传则保持逐条查询） | 批量与逐条结果完全一致；空列表 0 条查询 |
| `backend/strimkeep/phase5_api.py` | `/api/v1/tasks` 与 `/api/v1/tasks/events` 改用批量统计（CRLF 已保持） | 单轮查询数与任务数无关 |
| `backend/strimkeep/emby.py` | 新增 `coverage_selfcheck()` 与 `coverage_loop()`：启动 60 秒后首次、之后每 6 小时，每天最多一条结论，写 `meta.emby_coverage_last` 与操作记录 | 库根落在 STRM 目录内即算覆盖（README 推荐的子目录配置不误报）；读不到库信息时不下结论 |
| `scripts/verify.sh` | 修正过期注释（当前 9 组；`test_transfer` / `test_upgrade` 已在门禁内；说明 `sk_test` 与测试卷会被清理）；新增「自检与计数」组 | 注释与实现一致，新测试文件在门禁内 |
| `backend/tests/test_ops0819.py` | 新增 10 项用例 | 见「四、验证」 |

## 四、契约变化（review 时必须知道）

1. **任务进终态后，未开始的条目会被归一**：`discovered` / `planned` → `skipped`，`step` 与 `message` 均为「任务结束未处理」。已出结果的条目（`done` / `archived` / `review` / `pending`）不动。
   → `backend/tests/test_phase5.py::test_retired_classification_job_never_reads_or_modifies_cloud` 的断言随之更新：仍然要求「云端零改动 + 条目其余字段一字不动」，只把条目终态归一纳入期望。
2. `Store.prune()` 的返回字典新增两个键（只增不改，调用方无需调整）。
3. `job_public(row, counts_map, emby_map, stats=None)` 新增可选参数；不传时行为与 0.8.18 完全一致。

## 五、验证

| 项 | 结果 |
| --- | --- |
| 新增用例 `tests/test_ops0819.py` | 10 passed（容器内 9.19s） |
| 受影响回归：`test_ops065` + `test_iteration1` + `test_app` + `test_phase5` | 首轮 1 failed（契约变更导致）→ 按新契约更新断言 → `test_phase5` 37 passed |
| Emby 六个文件（`test_emby064/061`、`delivery_states`、`scan_fallback`、`prewarm`、`test_console060`） | 48 passed |
| 完整门禁 `sh scripts/verify.sh strimkeep-dev:0.8.19` | 9 组全部通过：387 项通过、1 skipped（组内耗时合计约 6.5 分钟，含组间 30 秒间隔约 11 分钟） |
| 线上部署 | `/health/ready` 返回 `0.8.19`；启动后出现（或按现场数据不出现）「启动自检」事件 |

## 六、升级 / 回滚

```sh
# 升级（沿用 DEVELOPMENT.md 第 7 步）
P="/path/to/StrimKeep"
docker tag strimkeep-dev:0.8.19 strimkeep:0.8.19
cd "$P" && cp docker-compose.yml "docker-compose.yml.before-0.8.19-$(date +%Y%m%d-%H%M%S)"
sed -i s/strimkeep:0.8.18/strimkeep:0.8.19/ docker-compose.yml
docker compose up -d && curl -s http://127.0.0.1:9000/health/ready

# 回滚（镜像仍在本地，数据无 schema 变更）
sed -i s/strimkeep:0.8.19/strimkeep:0.8.18/ docker-compose.yml && docker compose up -d
```

## 七、已知未修（0.8.20 计划）

- `/tasks/events` 仍是「每 2 秒全量重建」（本版只消除了 N+1，单轮约 6 次查询）；增量游标与 payload 比对优化留到 0.8.20。
- `emby_deliveries.payload` 仍是整段 JSON，未拆行式表。
- `POST /api/v1/strm/rebuild` 仍无 dry-run 与后台进度。
- 转存密钥只在「任务终态」与 prune 时清理；`transfer-update:*` 只按条数封顶，没有时间维度。
