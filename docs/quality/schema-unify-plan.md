# schema 归一方案（第二阶段第 3 项）

## 现状：12 张表散在 5 个模块里建

| 模块 | 表 | 行号 |
| --- | --- | --- |
| `share_service.py` | `share_strms` / `share_watchlist` / `share_revocations` / `share_wash_intents` | 64 / 69 / 73 / 76 |
| `channel_monitor.py` | `channel_state` / `channel_shares` / `channel_posts` / `channel_backfill` | 154 / 159 / 162 / 165 |
| `offline_api.py` | `offline_submissions` | 119 |
| `native_media_info.py` | `native_media_info_queue` / `media_info_outputs` | 27 / 32 |
| `emby.py` | `emby_outbox` | 41 |

`store.py` 自己还有 33 处 DDL（主 schema）。`output_stats` 曾经就是因为建表留在读路径上、启动没建，
接口直接 500——这一类事故的根因就是"建表散落"。

## 目标

1. 所有 DDL 收进 `store.py` 一处，启动时 `CREATE TABLE IF NOT EXISTS` 全量执行（幂等）；
2. 模块里只留业务代码，删掉自建表语句；
3. `meta.schema_version` 变成有意义的东西：本次提升到 `2`，启动时对比并按需写回。

## 步骤（低风险顺序）

1. **先把 DDL 搬齐，不删模块里的**：把 12 条 `CREATE TABLE IF NOT EXISTS` 原样加进 `store.py` 的
   schema 脚本（保持 `IF NOT EXISTS`，重复执行无害）。此时两处并存，行为不变。
2. 跑一遍全量目标集 + 一个新增用例：`Store()` 初始化后**每张表都必须存在**
   （把上面 12 个表名写成清单断言，这就是"漏建"的护栏）。
3. 删除模块里的 DDL（一次一个模块，每删一个跑一次目标集）。删完后第 2 步的用例仍然通过，
   说明确实都由 `store.py` 兜住了。
4. 加版本门：`SCHEMA_VERSION = 2`，启动时 `meta.schema_version < 2` → 执行脚本并写回 `2`。
   以后加表就只改 `store.py` + 提版本号。

## 验收

- `sh scripts/test_in_container.sh <镜像>` 全绿；
- 新用例：全新空目录初始化后，12 张表全部存在且 `schema_version = 2`；
- 老库升级路径：拿一份旧 `state.db` 副本启动→表齐、版本号写回、数据不变。

## 风险

- 低：全程 `IF NOT EXISTS`，先加后删，任何一步都能停；
- 唯一要注意的是 `emby.py` 的 `emby_outbox` 与 `strm_changes` 在交付链路里是同一条的，
  搬的时候别漏掉索引（`CREATE INDEX` 也要一起搬）。
