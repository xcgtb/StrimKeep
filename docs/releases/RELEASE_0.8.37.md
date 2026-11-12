# StrimKeep 0.8.37：本地巡检/退役/孤儿清理也联动清侧车；离线单次上限 20 → 100

## 一、问题（全局排查发现）

「删 STRM 但留 nfo/海报/mediainfo，且不删空目录」的缺口不止深度删除一处，还有三处，
外加一段死代码和一处计数不实：

| # | 位置 | 问题 |
| --- | --- | --- |
| 1 | `sync.sweep()` 撤销输出清理 | 只 `unlink` STRM+字幕，不删 nfo/海报/mediainfo，不删空目录 |
| 2 | `sync.sweep_orphans()` | 只删孤儿 STRM，不删侧车、不删空目录 |
| 3 | `outputs.retire()` | 只删 `local_files`，nfo/海报 还在 → 随后的 `rmdir` 必然失败 |
| 4 | `sync.cleanup_orphans()` | **死代码**（定义了从未被调用），且其 `expected` 只含本次任务条目却扫整个同步根，另有两种“先删除再核证据”的顺序问题 |
| 5 | `sync.sweep()` | `unlink` 失败也照样删 `owned_outputs` 台账并计「已清理」 |
| 6 | 离线下载 | 单次上限写死 20，且后端/机器人/前端三处 20 各写一遍 |

## 二、改动

| 文件 | 改动 |
| --- | --- |
| `outputs.unlink_recorded(..., purge=False)` | 新增 `purge` 开关：`purge=True` 时走 `purge_strm`（删 STRM + 同名 nfo/`-media-info.json`/字幕，目录无视频时再删空目录）；**删不掉就抛错**，调用方据此保留台账，绝不假装完成 |
| `sync.sweep()` 撤销清理 | 改走 `purge_strm`（STRM 与同名侧车一起清）；只把**真的不在了**的路径记为已清理并停用台账；一个都没删掉的整行保留，下一轮再试 |
| `sync.sweep_orphans()` | 改走 `unlink_recorded(..., purge=True)`；删不掉计入 `kept`，不虚报清理数 |
| `outputs.retire()` | `.strm` 条目用 `purge` 模式删除，nfo/海报 不再挡住退役与空目录回收 |
| `sync.cleanup_orphans()` | **删除死代码**（其意图已由 `sweep_orphans` + `purge_strm` 覆盖，保留只会误导后人接线） |
| `offline_api.py` | 新增常量 `OFFLINE_MAX_LINKS = 100` 与 `ensure_link_count()`；提交接口改为调用它（超限 422，不静默截断） |
| `telegram_bot.py` | 截断与提示改用 `OFFLINE_MAX_LINKS`（不再各写死一个数字） |
| `frontend/src/Offline.vue` | 提示文案改为「单次最多 100 个链接」 |
| `tests/test_ops0837.py`（新增） | 5 项：撤销输出清同名侧车、孤儿 STRM 清侧车、`retire` 清侧车、`purge` 删不掉时必须抛错、离线上限 100（100 允许 / 101 报 422） |
| `scripts/verify.sh` | 「自检与计数」组加入 `test_ops0837.py` |

## 三、验证

| 层次 | 命令 | 结果 |
| --- | --- | --- |
| 新增用例 | `tests/test_ops0837.py`（5 项） | 通过 |
| 受影响回归 | `test_sync_sweep` / `test_sync_orphans` / `test_sync_stale(_extra)` / `test_ops0830` / `test_offline_throttle` / `test_sweep_offline` / `test_channel_skip_offline` / `test_upgrade` / `test_transfer` | 116 项通过 |
| 完整门禁 | `sh scripts/verify.sh strimkeep-dev:0.8.36` | **`全部通过`，640 项通过 / 1 项跳过，11 组全绿** |

## 四、已知边界

- 侧车只按「同名 stem / `-media-info.json`」与 `SIDECAR_EXT` 关联；不同名的用户文件不动。
- 目录级侧车（poster/fanart/tvshow.nfo）只在目录里已无任何视频时才清。
- 离线上限只在提交入口拦截；115 账号自身配额/会员限制仍以云端返回为准。
