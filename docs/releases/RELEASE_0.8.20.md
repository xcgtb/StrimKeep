# StrimKeep 0.8.20：任务流增量刷新、Emby 交付摘列、STRM 重建可预览可回滚、SQLite 写并发

本版把审计里的 P0-1（同步 SQL 压事件循环）拆成可交付的三步，并顺手修掉一个
0.8.18 就存在、但一直没被触发的 500。数据侧只加两列一表，回滚不需要迁移。

## 一、为什么做这四件事（现场数据）

| # | 现象 | 实测（2026-11-03/04） |
| --- | --- | --- |
| 1 | 任务中心每 2 秒把 30 个任务整个重建一遍（6 条批量查询 + 一次序列化），数据没变也在算 | `/api/v1/tasks/events` 单标签页常驻即持续产生 DB 读 |
| 2 | `job_public` 为显示「已确认 N/M」要 `json.loads` 每条任务的 `emby_deliveries.payload` 再遍历 `targets` | 52 行投递记录，单条 payload 数 KB |
| 3 | `POST /api/v1/strm/rebuild` 同步遍历 892 行：读文件→比字节→写文件→2 次 UPDATE，全在事件循环上 | 审计 P0-1 的典型样本；期间播放取链与页面请求一起排队 |
| 4 | `prune()` 每小时可能删两万行，也在事件循环上 | 与 3 同类 |

## 二、改动

| 文件 | 改动 | 验收点 |
| --- | --- | --- |
| `backend/strimkeep/phase5_api.py` | `/tasks/events` 增加 `stamp()`：一条 SQL 的 4 个子查询（任务最大 `updated`、条目总数、已结束条目数、未读通知数）决定「这一轮要不要重建 payload」；签名不变时只发心跳 | 空闲时每 2 秒 1 条查询；任务在跑时 `update_job` 会推进 `updated`，界面照旧实时 |
| `backend/strimkeep/store.py` | 打开即 `PRAGMA busy_timeout=5000`、`synchronous=NORMAL`；`emby_deliveries` 增加 `total`/`confirmed` 两列（新库直接建，老库 `ALTER` + 用 SQLite JSON 函数一次性回填） | `PRAGMA` 断言；历史行回填后 `job_public` 不再解析 payload |
| `backend/strimkeep/emby.py` | `save()` 写 payload 时同步维护两列；`targets()` 把任务配置从循环里提到循环外（一季 24 集少 24 次查询） | 摘列与 payload 永远一致；摘列为 0 时 `job_public` 自动回退解析 |
| `backend/strimkeep/rebuild.py`（新增） | STRM 全量重建独立成模块：`items()` 只读台账与解密、`_inspect()` 在线程里做文件读写、`apply()` 一次事务写回台账、`snapshot()`/`rollback()` 支持回滚，`dry_run` 只读 | 见「四、验证」 |
| `backend/strimkeep/main.py` | `/strm/rebuild` 改为调用 `Rebuild.run()`，新增 `?dry_run=1`、`GET /strm/rebuild/status`、`POST /strm/rebuild/rollback` | 返回字段向后兼容（`scanned/rewritten/unchanged/missing/failed`），新增 `dry_run/snapshot/elapsed_seconds` |
| `backend/strimkeep/organizer.py` | 持有 `Rebuild` 实例；`maintain()` 改 async，`prune` 走 `asyncio.to_thread(self.store.prune_in_thread)` | 维护任务不再阻塞事件循环（`schedule_loop` 内 await） |
| `scripts/verify.sh` | 「自检与计数」组加入 `test_ops0820.py` | 门禁 9 组 |

## 三、顺手修掉的 0.8.18 缺陷

`POST /api/v1/strm/rebuild` 原来这样算目标内容：

```python
forms = play115_candidates(base, source, name)          # -> list[bytes]
content = (forms[0] if forms else play_url(...)).encode()   # bytes.encode() -> AttributeError
```

`play115_candidates()` 返回的已经是 `bytes`，所以**只要这条台账的身份齐全
（pickcode + sha1 + size，即绝大多数行），这个入口就直接 500**。0.8.20 改为：

```python
if forms:
    content = next((form for form in forms if form.endswith(b'\n')), forms[0])
else:
    content = (play_url(...) + '\n').encode()
```

第二个修正（换行风格）：现网 949 条台账里 **879 条 STRM 是「无结尾换行」形态**
（publication / sync 写的），只有 `write_strm()` 写的才带换行。第一版重建一律写带换行，
在真实数据上会为 1 个字节重写 879 个文件（还会打乱 `owned_outputs.digest`）。
最终规则：`url` 与 `url + '\n'` **都算最新**；需要改写时沿用该文件现有的结尾风格。
线上只读复核因此从「879 待重写」变成「**0 待重写**」——重建在现网数据上是幂等的。

### 教训：`asyncio.to_thread` + 共享 sqlite 连接 = SIGSEGV

第一版把 `prune` 直接写成 `await asyncio.to_thread(self.store.prune)`，在 NAS 的
容器内回归时**进程直接段错误**：

```
Fatal Python error: Segmentation fault
  File "/app/repo/backend/strimkeep/store.py", line 427 in prune
  File "/usr/local/lib/python3.12/concurrent/futures/thread.py", line 59 in run
```

原因：`sqlite3` 的连接不是线程安全的。`check_same_thread=False` 只是关掉了解析器层
的检查，同一个连接被事件循环线程与工作线程同时使用时，会在 C 层崩掉。
0.8.20 改成 `Store.prune_in_thread()`：**线程里自己开一条连接**（WAL +
`busy_timeout=5000`），跑完即关；主线程的共享连接一律不跨线程。
`tests/test_ops0820.py::test_prune_in_thread_uses_its_own_connection` 守住这个入口。

> 这条同样适用于后续把所有同步 SQL 搬进线程的计划：**必须是一线程一连接**，
> 或者一个专职写协程 + 一个专职读线程，不能只是把 `execute()` 包进 `to_thread`。

## 四、验证

| 项 | 结果 |
| --- | --- |
| 新增 `tests/test_ops0820.py`（9 项） | 9 passed：预览不写盘、执行后地址正确且台账同步、回滚还原旧字节、快照只留 5 份、无快照回滚 404、摘列跟随 payload、`job_public` 优先摘列并回退、PRAGMA 与新列存在、`prune_in_thread` 独立连接 |
| 受影响回归：`test_phase5` / `test_emby064` / `test_emby_delivery_states` / `test_emby061` / `test_ops065` / `test_iteration1` / `test_iteration2` / `test_app` | **102 passed**（首轮在 `prune` 线程版本上 SIGSEGV，定位并修复后重跑全绿） |
| 完整门禁 `sh scripts/verify.sh strimkeep-dev:0.8.20` | **9 组全部通过：397 项通过、1 skipped**（组内耗时合计约 3.7 分钟） |
| 线上只读复核（0.8.20 容器内，真实台账 949 行） | `total=949 rewrite=0 unchanged=879 missing=70 failed=0 unsafe=0`；`emby_deliveries` 53 行摘列与 payload 比对 **0 不一致**；`/strm/rebuild/status`、`/strm/rebuild/rollback` 未登录均 401；部署后 30 分钟内 **0 条 error 事件** |

> 复核里的 `missing=70` 是一个真实存在的历史现象：台账里有 70 条记录的本地 STRM
> 已经不存在（手工删除或当年未落地）。重建只统计、不创建——这与 0.8.18 的语义一致；
> 需要补齐时用「STRM 同步」，那里有归属与来源校验。

## 五、契约与兼容

- `/strm/rebuild` 的返回字段只增不改，页面现有提示（重写/未变/缺失/失败）继续可用。
- `dry_run`、`status`、`rollback` 三个能力**后端已就绪，页面上还没有按钮**（0.8.21 补 UI），
  现在可用 curl 触发；`Sync.vue` 现有「按当前播放地址重写」按钮的行为与以前一致。
- `emby_deliveries` 新列只在 `save()` 写 payload 时维护 + 升级时一次性回填，
  读路径对「两列都是 0」的历史行保留回退逻辑，不存在「升级后数字显示错」的窗口。
- 无 schema 版本号变化；回滚到 0.8.19 时多出的两列会被忽略（SQLite 允许）。

## 六、升级 / 回滚

```sh
# 升级
P="/path/to/StrimKeep"
docker tag strimkeep-dev:0.8.20 strimkeep:0.8.20
cd "$P" && cp docker-compose.yml "docker-compose.yml.before-0.8.20-$(date +%Y%m%d-%H%M%S)"
sed -i s/strimkeep:0.8.19/strimkeep:0.8.20/ docker-compose.yml
docker compose up -d && curl -s http://127.0.0.1:9000/health/ready

# 回滚（镜像仍在本地；多出的两列与快照表不影响旧版本）
sed -i s/strimkeep:0.8.20/strimkeep:0.8.19/ docker-compose.yml && docker compose up -d
```

## 七、已知未修（0.8.21 计划）

- **事件循环里的其余同步 SQL**：本版只把「重建 + prune」两处搬进线程，`to_thread`
  覆盖不到另外约 450 处调用点。完整迁移（`Store.read/write` + 单写队列）要连带
  调整所有 `with store.db:` 的边界，属于结构性改造，单独一版做。
- **`emby_deliveries.payload` 仍未拆行式表**：本版只把「总数/已确认」抽成列，
  `targets` 仍在 JSON 里（`verify()` 要逐条回写确认状态）。真正拆表要动
  `submit/verify/finish/maybe_scan` 四处状态机，风险高于收益，留待有实测需求时再做。
- **页面还没有 dry-run / 回滚按钮**。
- 探索页的模块级缓存没有容量上限（审计 X-1）。
