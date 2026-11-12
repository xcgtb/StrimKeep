# StrimKeep 0.8.31：本地多余 STRM 的清理恢复工作（按文件身份判定）+ 旧命名副本收敛

## 一、问题

0.8.30 把库根改成镜像 115 目录名之后，现场还留下一批**旧命名副本**：同一个云端文件，
本地有两份 `.strm`（旧版把 TMDB 分集标题写进文件名：`S01E01.初潮`；现版本用 `S01E01.第1集`）。
全库 1465 个 STRM 里，395 个属于这种「同一集两份」，另有 47 个是**只有旧命名**的孤本。

追查下来，同步**本该**清掉它们，但清理链路上有三个叠加的窟窿：

| # | 位置 | 问题 |
| --- | --- | --- |
| 1 | `sync.sweep_orphans()`（唯一在跑的「扫本地多余 STRM」） | 判定写死 `playback_base + '/play/'` 前缀。0.7.12 起 STRM 写的是 `/play115/<pickcode>/<fid>/<sha1>/<size>/<name>`，**一个都不匹配** → 全部按「保留」处理。等于这条清理自 play115 切换后就失效了 |
| 2 | `sync.cleanup_orphans()`（语义正确：不在本次 expected 且无归属 → 删） | **只有定义、没有任何调用**（死代码）。它用「本次任务的条目」当 expected 却扫整个同步根，直接接上会误删，所以被停用了 |
| 3 | `outputs.retire()` | 只处理 `output_registry.files` 里**登记过**的路径，且要求 `owned_outputs` 有归属。旧命名这批从没进登记表 → 天然跳过（防误删的好设计，但也意味着「本地多出来的文件」无人负责） |

## 二、改动

| 文件 | 改动 |
| --- | --- |
| `sync.py` | `sweep_orphans()` 改为**按文件身份**判定：旧形态 `/play/<token>` 照旧按播放身份是否撤销处理；新形态 `/play115/...` 从地址里取 `file_id` 查 `output_registry`，**同一云端文件已被登记到另一个路径、且那份还存在** → 这条是旧命名副本，删；查不到登记、或登记的正是自己 → 只报数不猜。新增 `_duplicate_output()` |
| `library_roots.py` | 新增只读 `legacy_report()`、`prune_legacy(apply=False)`、`restore_legacy()`；CLI 增加 `--prune-legacy`（默认演练）、`--restore MANIFEST`、`--backup-dir`。判定口径：登记集合 = `organized_files ∪ output_registry`，只有「同一作品同一集已有一条有效登记」的未登记文件才会被删；整部只有旧命名、或这一集没有登记 → 保留 |
| `tests/test_ops0831.py`（新增） | 4 项：play115 副本被删且正式输出保留；未登记/覆盖不到的只报数；旧 `/play/<token>` 形态仍照旧清；`prune_legacy` 的 dry-run / apply / restore 全链路（含清单） |
| `scripts/verify.sh` | 「自检与计数」组加入 `test_ops0831.py` |

清理清单（`prune-legacy-<时间>.json`）里存了每个被删 STRM 的**原文**，`--restore` 可以原样写回；
删除本身走 `fsops.purge_strm()`（只 unlink 单个文件 + 清无视频目录的 sidecar 和空目录，绝不 `rmtree`）。

## 三、怎么用

```sh
# 只读演练：会删多少、哪些保留（按作品列出来）
docker compose run --rm --entrypoint python strimkeep -m strimkeep.library_roots --prune-legacy
# 执行（先写清单再删）
docker compose run --rm --entrypoint python strimkeep -m strimkeep.library_roots --prune-legacy --apply
# 反悔
docker compose run --rm --entrypoint python strimkeep -m strimkeep.library_roots --restore /data/data/prune-legacy-<时间>.json
```

跑完在 Emby 里重扫对应库，重复条目消失。

## 四、验证

| 层次 | 命令 | 结果 |
| --- | --- | --- |
| 新增用例 | `tests/test_ops0831.py` | **4 项通过** |
| 受影响回归 | `test_sync_sweep` / `test_sync_stale*` / `test_ops0830` / `test_phase5` / `test_app` | **86 项通过** |
| 完整门禁 | `sh scripts/verify.sh strimkeep-dev:0.8.31` | **`全部通过：strimkeep-dev:0.8.31`，609 项通过 / 1 项跳过，12 组全绿** |

现场执行（NAS）：`--prune-legacy` 演练 **deletable 395 / keep 47**；`--apply` 后 `removed 395`；
再演练 `deletable 0`。清单 `data/data/prune-legacy-20261104-120606.json`（188 KB，含每个被删 STRM 原文）。
清理后：媒体库 **1070** 个 STRM（1023 已登记 + 47 只有旧命名）、分享库 34，Emby 覆盖 **1104/1104**、0 个漏网。

## 五、已知边界

- 它只清「库根目录下、台账没登记、且同一集已有有效登记」的副本；**不会**清理「整部只有旧命名」的作品
  （那种情况删了就少集，必须先让云端重新整理/同步把它们接管）。
- 判定用的是「作品目录 + SxxExxx」；命名里没有集号的文件（例如电影）按「同一作品一条」处理。
- 旧 `/play/<token>` 形态的清理逻辑保持原样（按播放身份是否撤销），未做改动。
