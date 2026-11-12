# StrimKeep 0.8.30：STRM 路径改为镜像 115 目录名，并修掉「台账与磁盘对不上」那次事故

## 一、现场（2026-11-04 实测）

| 项 | 实测值 |
| --- | --- |
| 115 归档目录 / 同步根 | 都叫 **媒体库**（`directory_labels['3547068638915724608'] = '115 网盘 / 媒体库'`） |
| 配置 | `library_dir_name = 影视媒体库`（0.6.3 时代的默认名） |
| 磁盘 | `strm/媒体库`：530 个 `.strm`（含 nfo/海报共 1892 文件，最新 11-01 21:25）；`strm/影视媒体库`：**只有 12 个** `.strm`（今天两个任务） |
| 台账 | 978 行**全部**是 `影视媒体库/...`，只有 12 行在磁盘上存在；把首段换成 `媒体库` 也只有 42 行命中 → **924 行指向不存在的文件** |
| Emby | 剧集 `['/media/分享影视库/剧集','/media/影视媒体库/剧集']`、动漫 `['/media/影视媒体库/动漫']`、影片 `['/media/分享影视库/电影','/media/影视媒体库/电影']` → **没有 `/media/媒体库`**，530 个 STRM 落在任何库之外 |

根因不是「某次写错目录」，而是**库根名被当成自由配置**：

1. STRM 的本质是 115 目录的映射，本地第一段必须和网盘目录同名；
2. 一旦配置和网盘不一致，整理/同步会**按配置重算 `relative_path` 并直接写台账**
   （`sync.py:549-553`、`organizer.py:1983`），但**不会移动已经存在的文件**；
3. 于是台账被改到「影视媒体库」、文件留在「媒体库」，两边分叉，而且**没有任何提示**。

## 二、改动

| 文件 | 改动 |
| --- | --- |
| `organize_options.py` | 新增 `cloud_dir_name()` / `library_root()`；`library_dir_name` 默认改为 **空 = 镜像 115 目录名**（显式填写仍优先）；校验允许留空 |
| `organizer.py` | 批量准备阶段用 `library_root(config)`；`fill_directory()` 用扫描到的真实路径 `library_root(config, trail)`（比标签更权威） |
| `sync.py` | `relative()` 改 `library_root(config, path)` |
| `tasks.py` | 同步任务配置带上 `directory_labels`，复用同一个 `library_root()` |
| `main.py` | 设置页 `library_prefix` 走同一函数；启动时调 `library_roots.startup_check()` 做一致性自检 |
| `fsops.py` | 空目录清理**不再依赖名字白名单**：`/strm` 下的一级目录（库根）永远保留。原来库根一改名，清理就可能把空库根删掉 |
| `library_roots.py`（新增） | 只读对照 `inspect_state()`、可回滚的库根改名 `apply_rename()`/`rollback()`、启动自检 `startup_check()`、CLI |
| `scripts/verify.sh` | 「自检与计数」组加入 `test_ops0830.py` |

## 三、库根名的新规则

`library_root(config, path=None)` 按顺序取值：

1. `library_dir_name` 显式填写（兼容老配置，也允许本地用别的名字）；
2. `path`（扫描到的 115 路径 `[{'id','name'},…]`）里归档目录的真实名字；
3. `directory_labels[archive_cid]` 的最后一段（`115 网盘 / 媒体库` → `媒体库`）；
4. 兜底 `影视媒体库`（只在完全推不出来时用）。

⇒ 115 上目录叫「媒体库」，本地就写 `strm/媒体库/...`；Emby 只需把库根指向
`/media/媒体库/<分类>`，四处（本地树 / 台账 / Emby / 磁盘）天然同名。

## 四、库根改名工具（可回滚）

```sh
cd /path/to/StrimKeep
docker compose stop strimkeep          # 迁移期间不要让整理/同步在写

# 1) 只读对照：配置 / 台账 / 磁盘
docker compose run --rm --entrypoint python strimkeep -m strimkeep.library_roots --state
# 2) 演练：要搬多少文件、改多少行、有没有同名冲突
docker compose run --rm --entrypoint python strimkeep -m strimkeep.library_roots --rename 影视媒体库 媒体库
# 3) 执行
docker compose run --rm --entrypoint python strimkeep -m strimkeep.library_roots --rename 影视媒体库 媒体库 --apply
# 4) 让库根以后一直跟随 115 目录名（留空即可）
docker compose run --rm --entrypoint python strimkeep -m strimkeep.library_roots --set-config ''
docker compose up -d

# 回滚（文件搬回去、台账改回去）
docker compose run --rm --entrypoint python strimkeep -m strimkeep.library_roots --rollback
```

行为约定：

- **有冲突就整体拒绝**（目标目录已有同名文件时一个文件都不动），只报告冲突清单；
- 改名会重写 `playbacks` / `organized_files` / `output_registry`（含 `files` JSON）/
  `owned_outputs` / `organize_items`（`plan`、`original`）/ `organize_jobs`（`config`）/
  `emby_deliveries`（`payload`），清空 `sync_snapshots`，并在 `meta.library_root_migration`
  留下 manifest 供回滚；
- **不改 Emby 的媒体库设置**（0.6.3 起的边界），改名后需要人工把库根改到新目录；
- JSON 列先 `json.loads` 再判断，不能靠子串匹配：`json.dumps` 默认把中文转义成 `\uXXXX`，
  这样才能覆盖历史数据里被转义保存的行。

## 五、验证

| 层次 | 命令 | 结果 |
| --- | --- | --- |
| 新增用例 | `tests/test_ops0830.py` | **13 项通过** |
| 受影响回归 | `test_config063` / `test_strm_tree` / `test_sync_stale*` / `test_sync_sweep` / `test_deep_delete` / `test_empty_directories` / `test_ownership_selfheal` / `test_phase1_foundation` / `test_app` / `test_emby_delivery_states` / `test_emby_scan_fallback` / `test_phase5` / `test_ops0819/0820/0821/0822` | **185 项通过** |
| 完整门禁 | `sh scripts/verify.sh strimkeep-dev:0.8.30` | **`全部通过：strimkeep-dev:0.8.30`，605 项通过 / 1 项跳过，12 组全绿** |

门禁这一次还抓到一件与本版无关但必须处理的事：`test_merged_episode_copy.py` 的两个断言
**自 0.8.28 起就是红的**（未识别分流已改成「来源根目录或已有成功兄弟时逐个转移文件」，
不再整包搬目录）。已按新规则修正断言并写明原因，0.8.30 的门禁是真绿。

## 六、数据迁移现场（已在 NAS 执行）

```text
1) 停服务 → 2) 备份 state.db 与旧库根 tar → 3) --state
4) 演练：moves 30 / collisions 0
5) 执行：status applied，moved 30
   rows: playbacks 1070, organized_files 978, output_registry 1069, owned_outputs 1210,
         organize_items 7090, organize_jobs 270, emby_deliveries 37
6) --set-config ''（以后跟随 115 目录名）
7) 迁移后 --state：expected_root = 媒体库；disk 媒体库 542 个 STRM / 1922 文件；
   影视媒体库 已消失；ledger playbacks 1070 全部指向 媒体库
8) 起服务：{"status":"ready","version":"0.8.30","stage":5}
```

**迁移过程中现场踩到并修掉的问题（已修，写在这里以免重犯）**：
`--set-config ''` 最初用 `options()`（只含整理选项的模型）重建配置，`model_config=extra:'ignore'`
把 `archive_cid` / `source_cids` / `directory_labels` / `notify_emby` 等**整体丢掉了**，
导致 `expected_root` 又退回兜底的「影视媒体库」。发现后立刻从迁移前的备份里取回完整配置、
只把 `library_dir_name` 置空写回；CLI 改成「合并写回 + 只校验」，并补了
`test_set_config_keeps_other_organize_fields` 防止再犯。

## 七、还要你做一步：把 Emby 库根改到新目录

程序不远程改 Emby 设置（0.6.3 起的边界），所以这一步必须人工：

| Emby 库 | 改前 | 改成 |
| --- | --- | --- |
| 剧集 | `/media/影视媒体库/剧集` | `/media/媒体库/剧集` |
| 动漫 | `/media/影视媒体库/动漫` | `/media/媒体库/动漫` |
| 影片 | `/media/影视媒体库/电影` | `/media/媒体库/电影` |

（`分享影视库` 的库根保持不变。）改完逐个库「扫描媒体库文件」。
之后 StrimKeep 侧可以用 `/api/v1/emby/coverage` 确认新路径已被某个库覆盖。

接着若要补齐那 924 行指向不存在的历史条目，需要跑一次**全量 STRM 同步**（从 115 重新生成），
这一步等你确认后再跑，因为它会按新的库根重写一整棵树。

## 八、升级 / 回滚

```sh
docker tag strimkeep-dev:0.8.30 strimkeep:0.8.30
cd /path/to/StrimKeep && cp docker-compose.yml "docker-compose.yml.before-0.8.30-$(date +%Y%m%d-%H%M%S)"
sed -i s/strimkeep:0.8.29/strimkeep:0.8.30/ docker-compose.yml && docker compose up -d
curl -s http://127.0.0.1:9000/health/ready

# 回滚镜像（无 schema 变更）
sed -i s/strimkeep:0.8.30/strimkeep:0.8.29/ docker-compose.yml && docker compose up -d

# 回滚库根改名（文件搬回去、台账改回去）
docker compose stop strimkeep
docker compose run --rm --entrypoint python strimkeep -m strimkeep.library_roots --rollback
docker compose up -d
```

## 九、已知未修

- 那 924 行历史台账原本就指向不存在的文件，改名只把前缀改对；补齐要一次全量 STRM 同步。
- 显式写死旧库根名时仍会与网盘不一致：由启动自检报警（只报警，不自动搬文件）。
- 分享库根（`分享影视库`）仍是固定名字，不在本次范围。
- 清理空目录只保护「一级目录」，`strm/媒体库/剧集` 这类空分类目录仍会被清掉（原有行为）。

