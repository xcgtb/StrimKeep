# StrimKeep 0.8.32：程序自己删本地 STRM 时，深度删除不得再删云端

## 一、事故（2026-11-04，测试库）

`--prune-legacy` 删掉 395 个旧命名副本之后，**61 个云端源文件（约 610 GB）被删除**（都在 115 回收站）。
链路：

```
① 程序删掉「旧命名」那份本地 .strm（只动本地）
② Emby 发现这一集的文件不见了（它不知道那是重复副本）
③ Emby 发「Episode deleted」webhook
④ 深度删除按台账命中 → 去 115 删掉该集的源文件（进回收站）
        ⇒ 新命名那份 .strm 还在，但指向的文件没了 → 死链
```

根因不是「删错了谁」，而是**深度删除分不清「用户删的」和「程序删的」**：它只看到"这一集不见了"。
再加上库根迁移让 Emby 把整棵旧树当成消失，一次排了 **12007 条**待删（当时是"真删"模式），
等于把整个库排进了删除队列。

同一条链路也解释了更早（11-03 ~ 11-04 05:3x）尚公主 / 嫁金钗 / 伟大的长征 / Dark Matter 的云端文件
为何消失：那时是 `影视媒体库` 与 `媒体库` 两条路径并存，Emby 反复报"消失"。

## 二、改动

| 文件 | 改动 |
| --- | --- |
| `internal_delete.py`（新增） | 「程序内删除登记」：删本地 STRM 前把相对路径记进 `meta.internal_deletes`（30 分钟窗、上限 3000 条），`recent()` 供深度删除查询 |
| `fsops.purge_strm()` | 新增可选 `store` 参数，删除前登记（分享清理、库根清理、prune 都走它） |
| `outputs.unlink_recorded()` | `unlink()` 前登记（`retire()` 的退役输出、巡检自愈都走它） |
| `sync.sweep()` | 撤销输出的直接 `unlink()` 前登记 |
| `deep_delete.drop_internal()`（新增） | 深度删除匹配到「程序自己删的路径」时**整条跳过**（只记事件，不删云端）；`main` 的 webhook 入口在入队前调用 |
| `library_roots.prune_legacy()` | ① 深度删除是「真删」模式时**拒绝执行**并说明原因；② 删除前批量登记 |
| `library_roots.apply_rename()` | 改名前登记所有旧路径（改名同样会让 Emby 报删除） |
| `scripts/verify.sh` | 「自检与计数」组加入 `test_ops0832.py` |
| `tests/test_ops0832.py`（新增） | 5 项：登记窗口与过期、`drop_internal` 只剔程序删除、`purge_strm` 自动登记、深删真删时 prune 整体拒绝、改名登记旧路径 |

设计取舍：**没有**去猜"Emby 里的删除是不是用户想删"，而是记录"这条是我们自己删的"——
用户主动在 Emby 删条目时，本地 STRM 不是程序删的，深删照常执行。

## 三、验证

| 层次 | 命令 | 结果 |
| --- | --- | --- |
| 新增用例 | `tests/test_ops0832.py` | **5 项通过** |
| 受影响回归 | `test_deep_delete` / `test_sync_sweep` / `test_sync_stale*` / `test_ops0830/0831/0832` / `test_share_flat_generation` / `test_phase2_share_pipeline` / `test_cleanup_candidates` / `test_phase5` / `test_app` | **135 项通过** |
| 完整门禁 | `sh scripts/verify.sh strimkeep-dev:0.8.32` | **`全部通过：strimkeep-dev:0.8.32`，614 项通过 / 1 项跳过，12 组全绿** |

线上确认（部署后）：`deep_delete_dry_run = True`、深删队列 `queued 0`（12102 条已作废、332 条历史 done）。

## 四、复现建议（以后自查）

```sh
# 深删当前是演练还是真删
docker exec strimkeep python -c "import sys;sys.path.insert(0,'/app')" # 见文档
# 看最近被程序删掉的本地路径（深删会跳过它们）
# meta.internal_deletes
```

## 五、已知边界

- 登记窗口默认 30 分钟：如果 Emby 的通知延迟超过窗口，仍可能命中（正常扫描通常在秒级）。
- 只覆盖「本地 STRM 由程序删除」这一种情形；用户手工在文件系统里删 STRM 仍然会被深删处理。
- 深删本身的语义未改：用户主动删除条目时照旧删云端（需在设置里关闭演练）。
