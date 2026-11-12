# StrimKeep 0.8.33：深度删除分批 TG 汇总、面板折叠展示、附属文件统计补齐

## 一、背景

0.8.32 把深度删除的误删链路堵住后，深度删除仍需人工盯三件事：

1. **分批结果没有汇总**：一批（比如 Emby 一次报了 50 集）删完只在队列里留下 50 条明细，
   TG 里没有一条「这批成功了 X、跳过 Y、失败 Z」的收尾消息，只能在面板里翻队列。
2. **历史明细是平铺的**：一季 20 集就是 20 行，电影/分享又各是一条，看历史像看流水账。
3. **附属文件统计缺「失败」维度**：同步结果卡片只报「下载 X 个 + 耗时」，跳过和失败
   只进实时日志，不进卡片；`sync_metadata_stats` 也没记扩展名分布。

本版补这三块，不动深度删除的执行语义（预演默认、真删需显式关闭演练等四条护栏都保留）。

## 二、改动

| 文件 | 改动 |
| --- | --- |
| `deep_delete.py` | 新增 `_group_items()`：把一批明细按「作品 · 季」聚合——剧集按 `Season N` 折叠、电影按作品文件夹单片、分享单片一组；新增 `notify_batch()`：一批全部结束后发一条 TG 汇总（成功/跳过/失败 + 前 3 条样本 + 总数），**预演不发**避免刷屏；`history()` 每批返回 `groups` + `rollup`（`works`/`seasons`/`files`/`groups`），明细仍按前 100 条截断、聚合不受截断影响 |
| `sync.py` | `download_metadata()` 的 `per_root` 统计新增 `failed` 与 `types`（nfo/json/image/subtitle/other 计数）；结束事件补上失败数（`附属文件新增 X 个，保留已有或跳过 Y 个，失败 Z 个`） |
| `notifications.py` | 同步目录卡片改成一行「独立附属文件下载：N 个（跳过 M 个）（失败 K 个）」+ 耗时行；用 `.get()` 带默认值，兼容旧 `sync_metadata_stats` 只有 `downloaded`/`seconds` 的存量任务 |
| `DeepDeleteHistory.vue` | 历史面板新增 `Group`/`Rollup` 类型与 `groupKind`/`groupTitle`；每组用 `<details>` 折叠（剧集/分享/电影），组头显示合计/完成/跳过/失败，顶部汇总行显示 `X 部作品 · Y 季 · Z 个文件` |
| `scripts/verify.sh` | 「自检与计数」组加入 `test_ops0833.py` |
| `tests/test_ops0833.py`（新增） | 4 项：剧集/电影/分享分组与计数、`history` 的 `groups`/`rollup` 形状、预演不发通知、真删发一条汇总 |

## 三、验证

| 层次 | 命令 | 结果 |
| --- | --- | --- |
| 新增用例 | `tests/test_ops0833.py` | 4 项通过 |
| 完整门禁 | `sh scripts/verify.sh strimkeep-dev:0.8.33` | **`全部通过：strimkeep-dev:0.8.33`，618 项通过 / 1 项跳过，11 组全绿** |

## 四、已知边界 / 顺延

- **`--notify` 未做**：`library_roots` 的 `--prune-legacy` / `--prune-dead` 曾要求加 TG 通知，本版**明确顺延**——深度删除自身的汇总通知已覆盖「真删收尾」，本地 prune 仍走原有实时日志与面板，避免两条通知口径打架。
- `notify_batch()` 在「真删」模式（`dry_run=0`）才发；预演批次仍静默（与 0.8.32 起「预演默认」一致）。
- 历史聚合的 `items` 每组最多 200 条、每批明细最多 100 条，`rollup`/`items_total` 按全量计，不因截断失真。
