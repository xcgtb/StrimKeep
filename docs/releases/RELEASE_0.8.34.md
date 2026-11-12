# StrimKeep 0.8.34：深度删除与本地删除全线 fail-closed + 分享库归属校验

## 一、事故

`--prune-legacy` 与库根迁移之后，Emby 陆续报「条目已移除」。0.8.32 之前深度删除对
**只有 TMDB 号、没有路径**的扫描删除事件，会按 `{tmdb-<id>}` 目录前缀把**整部作品**
（每一集）全部命中并删除。一次这样的命中把分享库「嫁金钗 (2026)」S01E01–E27 共
**27 个分享 STRM 撤销并删除了本地文件**（深删批次 `1793772724.330`，`kind=share`、
`dry_run=0` 真删）。分享源文件仍存活，属误删，已用本版恢复工具还原。

## 二、根因

1. **TMDB 号代表「作品」，不代表「被删的单集或整季」**。旧 `match_targets()` 把仅有
   TMDB 号的事件扩大成整部作品的每个条目，删错范围。
2. **真删没有「归属核实 + 回收确认」**：旧 `apply()` 直接 `unlink` + `recycle`，不核对
   台账/登记/回读，本地与网盘任一步失败都可能留下「删了但没确认」的状态。
3. **分享存活检测会连坐**：旧 `check_alive` 撤销后 `purge_strm` 连侧车文件一起删，且
   不核实本地文件归属。
4. **内部删除登记是尽力而为**：旧 `internal_delete.remember()` 失败静默放行，等于护栏
   形同虚设。

## 三、改动

| 文件 | 改动 |
| --- | --- |
| `deep_delete.match_targets()` | 无路径的 TMDB-only 事件**不再匹配**；目录事件必须整条路径含 `{tmdb-<id>}` 且季名精确，通用目录名（库根、`Season 1`）不扩大范围 |
| `deep_delete.apply()` | **拒绝托管文件真删**（`dry_run=False` 且含 managed 直接抛错），真删只能走持久队列 + 回收确认 |
| `deep_delete.guarded_managed_delete()`（新增） | 托管真删唯一入口：核实 `output_registry`/`owned_outputs`/本地摘要/网盘来源身份 → 一次性提交回收（意图落盘，绝不重复提交）→ 回读回收站 + `live_file_exists` 确认 → 才删本地并清台账 |
| `deep_delete.run_row()` | 执行前重查 `deep_delete_dry_run`（切回预演则旧队列不执行）、台账路径/播放身份是否在排队后变化、`drop_internal` 再过滤；`drain` 用 `publication_gate` 串行化 |
| `main.py` webhook | 无路径事件直接 `accepted=false` 并记事件 |
| `internal_delete.py` | `remember()`/`recent()` fail-closed：`BEGIN IMMEDIATE` 事务落盘、格式损坏即抛、窗口 7 天、上限 100000 |
| `fsops.purge_strm()` | `store` 必传，`remember()` 失败即抛，不再静默放行 |
| `outputs.unlink_recorded()` / `sync.py` 孤儿清理 / `library_roots.apply_rename()`+`rollback()` | 删除前登记必须成功，否则保留文件并报错 |
| `share_service.check_alive()` | 只有 `SHARE_NOT_FOUND` 且本地文件归属可核实（`_verified_share_strm`）才撤销；只 `unlink` 自身 STRM，**不删侧车**；登记 + `recent()` 校验 |
| `share_service.generate(allow_restore=)` | 分享条目已存在时不自动重建（用户明确重新提交才恢复，且核对原始身份） |
| `share_service` 分享整理 | `item_by_share` 按 `organize_items.id` 关联，避免 `UNIQUE(job_id,file_id)` 下同一源文件出现在多条分享记录时互相覆盖 |
| `share_api.revoke` | 撤销前核实本地归属、登记内部删除，失败即 409/503 且不撤销 |
| `emby.py` | TV 分集验证改用分页 `/Shows/<id>/Episodes`（`/Items?ParentId=` 对多季剧集会漏集） |
| `cleanup.py` / `organizer.py` | 空目录清理补元数据（nfo）快照与回收、候选重试标记 |
| `recover_share_batch.py`（新增） | 预演优先、只恢复已核实事故批次 `1793772724.330`（27 条），逐条核对身份/路径/占用，`share_recovery_intents` 落盘；**已在生产执行**（27 条 `state=done`） |
| `tests/` | 新增 `test_share_check_safety`、`test_share_batch_recovery`、`test_share_explicit_regenerate`、`test_share_pipeline_recovery`、`test_internal_delete_safety`、`test_emby_show_episode_verify`；重写 `test_deep_delete` 真删相关用例 + 修正 `test_native_media_info` 的 TV 分集 mock |

## 四、验证

| 层次 | 命令 | 结果 |
| --- | --- | --- |
| 完整门禁 | `sh scripts/verify.sh strimkeep-dev:0.8.33` | **`全部通过`，631 项通过 / 1 项跳过，11 组全绿** |

## 五、已知边界

- 只允许恢复**已核实**的指定事故批次；其它历史批次如需恢复需另核实再扩展白名单。
- 深删真删路径现在**强制**走队列 + 回收确认，`apply()` 直接真删托管文件被禁止——旧脚本若依赖直接 `apply(engine, targets, dry_run=False)` 会抛错，需改走队列。
- `_verified_share_strm` 只认「路径 + 内嵌分享 ID」一致的普通 STRM；用户手工改过 STRM 内容会被判为归属不明并保留。
