# 账本契约（STRM identity & ledger contract）

这份文档只有一个目的：**让以后新增功能不需要"再往账本上对一次账"**。

## 为什么需要它

两次真实事故都不是"功能写错"，而是"索引和文件不同步，系统不报错"：

- **0.9.1～0.9.47**：媒体信息改成"本地探测完就收工"，Emby 那一半没人追。70 条 STRM 在 Emby
  里空了 24 小时，期间所有报表、通知、页面都说"媒体信息已保存"。
- **0.9.49～0.9.52**：`playbacks / share_strms / organized_files / output_registry /
  owned_outputs / media_info_outputs / output_stats` 七处都在回答"这个文件是不是我们的"，
  每个会动文件的路径都要把 2～4 张表改对（32 处 `UPDATE 路径` + 21 处 `DELETE 按路径`
  + 66 处 `SELECT 按路径`，横跨 9 个模块）。一次改名就要保持这些全部一致。

## 两条契约

> **契约 1：身份写在文件里。**
> 任何生成 STRM 的代码，唯一强约束是写出**带身份**的播放地址。改名、换库、重新排版
> 都不改变身份，因此**都不需要同步任何登记表**。
>
> **契约 2：数据库只存不可推导的状态。**
> 凭据、播放令牌密文、识别结果、发布意图可以入库；**路径只是缓存**，
> 由巡检/对账（`reconcile`）追平，而不是指望每个写入方都记得更新。

## 身份三种形态

现场 1078 条 STRM **全部**可解析（`backend/strimkeep/identity.py`）：

| 形态 | 例子 | 解析结果 |
|---|---|---|
| 播放令牌 | `/play/<playback_id>` | `{'kind':'playback','playback_id':...}` |
| 网盘 | `/play115/<pickcode>/<file_id>/<sha1>/<size>/<name>` | `{'kind':'netdisk','pickcode','file_id','sha1','size'}` |
| 分享 | `/play115/share/<share_id>/<name>` | `{'kind':'share','share_id'}` |

生成入口：`playaddr.play115_url()` / `playaddr.candidates()` / `share_service.share_play_url()`。
判断归属：`identity.matches(text, pid=… / file_id=… / share_id=…)`——**只比身份段，不比文件名**。

守护测试：`backend/tests/test_ledger_contract.py`（任何生成入口的产物都必须可解析）。

## 表的分类

**必须保留（不可推导）**

| 表 | 存什么 |
|---|---|
| `playbacks` | 播放身份 + `token_cipher`（令牌密文）+ 文件名/撤销位 |
| `share_strms` | 分享身份 + `share_code`/`receive_code` 凭据 + 附件/来源/探测结果 |
| `organized_files` | 整理结果与元数据（14 个模块在读，实质是业务表） |
| `output_registry` | 发布意图：`active` 表示"这个云端文件当前已发布到本地" |

**可重建的索引（允许漂移，巡检负责追平）**

| 表 | 说明 |
|---|---|
| `owned_outputs` | `output_registry.files` 的反向索引（路径 → 归属），可直接从它重建 |
| 各表的 `relative_path` / `strm_path` | 路径缓存，搬家后由 `reconcile` 改，不由每个写入方维护 |

**已退场（0.9.54）**

| 表 | 退场理由 |
|---|---|
| `output_stats` | 只是 STRM 的 size/mtime 摘要缓存；STRM 只有几百字节，缓存省不下什么，却要求每次搬文件都对账 |
| `media_info_outputs` | sidecar 自带 `version` + `playback_id`，归属可直接从内容判定 |

## 新增功能的 checklist

1. **写文件**：用现成的 `playaddr` / `share_play_url` 生成地址，别手拼 URL。
2. **要记状态**：只记"从文件推不出来的东西"（凭据、意图、识别结果）。能推导的一律不记。
3. **要搬文件**：只改文件系统 + 一条 `strm_changes` 流水即可；路径缓存交给 `reconcile`。
   不要逐个 `UPDATE` 七张表，也不要新加"必须对账"的登记表。
4. **加完自检**：`identity.parse_file()` 能解析新产物；`/metrics` 的
   `strimkeep_ledger_drift{kind=...}` 不该长期上涨。

## 巡检与指标

- `ledger_audit.audit()`：每 6 小时算出 `stale_active` / `unregistered` / `orphan_sidecar`，
  写进 `meta.ledger_audit_last`，由 `/metrics` 暴露为 `strimkeep_ledger_drift{kind=…}`。
- `ledger_audit.orphans()` + `scripts/purge_orphan_sidecars.py`：孤儿侧车清单与清理
  （默认 dry-run，只删本项目生成、且过了保护窗口的）。
- 计划中（S2b）：`reconcile()`——以文件为准修正路径缓存、把 `orphan_sidecar` 收敛到 0，
  并让 `owned_outputs` 可一键重建。**只标记不删除是它的默认动作。**

## 反模式（别再做）

- 用路径当 join 键去更新多张表（改名就要爆炸）。
- 为"对账"新增登记表——先问：这个事实能不能从文件推出来？
- 让"本地有值"顺手把上级流程一起收工（0.9.1 的错误：本地探测完连 Emby 那一半也结束了）。
- 静默：任何"跳过/保留"的分支都要留一条事件，否则下一次还是没人知道。
