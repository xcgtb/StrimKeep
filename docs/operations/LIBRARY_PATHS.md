# 本地 STRM 路径怎么来的（0.8.30 起）

这份文档回答一个具体问题：**为什么本地 STRM 放在 `strm/<某目录>/…`，这个「某目录」是谁定的？**
以及出了偏差怎么查、怎么改、怎么回滚。

## 零、新装最简配置（三步，不需要碰库根名）

1. **115 上怎么叫都行**：`115 网盘/媒体库`、`115 网盘/媒体`、`115 网盘/影视` 都可以，
   程序会**照抄那层目录名**当本地第一段，不需要你在设置里填任何「库根名」。
2. **设置页正常保存一次**：整理目录 / 归档目录 / STRM 同步目录（这两处填同一个 115 目录即可）。
3. **Emby 库根指向同名目录**：`/<挂载点>/<115目录名>/剧集`、`.../动漫`、`.../电影`
   （本机挂载是 `./strm:/media`，所以就是 `/media/媒体库/剧集` 这样）。

之后新增内容会自动落到同一棵树、同一批二级分类里，Emby 只要扫库即可。
**不需要**填 `library_dir_name`；填了反而要自己保证它和 115 目录同名，否则启动自检会报「库根不一致」。

三个「一次性」而不是「反复」的注意点：

| 场景 | 需要做什么 |
| --- | --- |
| 首次配置 Emby | 库根按上面的模式指一次（程序不改 Emby 设置，这是 0.6.3 起的设计边界） |
| 想改分类层级（地区 / 年份 / 儿童节目 / 演唱会 / 关闭分类） | **先定好再开始跑**；开关只影响新生成，旧文件不会自动搬家 |
| 以后把 115 上那层目录改名 | 跑一次 `--rename 旧名 新名`（或全量 STRM 同步重建）；不是每次都要改 |


## 一、一条规则

> STRM 是 115 目录的映射。本地第一段目录名 = 115 上那一级目录的名字。

- 整理任务：`115 网盘/媒体库/剧集/某剧/Season 1/…mkv` → 本地 `strm/媒体库/剧集/某剧/Season 1/…strm`
- STRM 同步：`115 网盘/媒体库/…` → 本地 `strm/媒体库/…`
- 文件的相对结构（分类 / 作品 / Season）来自识别与命名规则，与网盘保持一致。

`library_root(config, path=None)` 按顺序取值：

1. `library_dir_name` **显式填写**（老配置兼容，也允许本地用别的名字）；
2. 扫描到的 115 路径里归档目录的真实名字；
3. `directory_labels[archive_cid]` 的最后一段（`115 网盘 / 媒体库` → `媒体库`）；
4. 兜底 `影视媒体库`（只在完全推不出来时）。

> 为什么不再固定成「影视媒体库」：0.6.3 起这个名字是写死的默认值，网盘目录一旦叫别的
> （例如「媒体库」），本地树、台账 `relative_path`、Emby 库根、磁盘目录就会各说各话。

## 二、三方对照（先看这个）

```sh
cd /path/to/StrimKeep
docker compose run --rm --entrypoint python strimkeep -m strimkeep.library_roots --state
```

输出三块：

- `library_dir_name` / `expected_root`：配置里写的、以及按规则算出来的库根；
- `disk`：磁盘上每个一级目录的 `.strm` 数与文件数；
- `ledger`：`playbacks` / `organized_files` / `output_registry` / `owned_outputs` 里
  `relative_path` 的第一段分布。

**健康的样子**：`expected_root` 出现在 `disk` 里、且 `ledger` 里其它库根的行数为 0
（`分享影视库` 除外）。

**不健康的样子**：`disk` 里同时有 `媒体库` 和 `影视媒体库`，`ledger` 却全在其中一个下面。

服务起来时这个检查也会跑一次（`startup_check`），不一致会在「操作记录」里留一条
「库根不一致：…」的系统事件，同一签名只报一次，恢复一致后自动清标记。

## 三、改名（可回滚）

```sh
docker compose stop strimkeep                                   # 迁移期间不要有任务在跑
docker compose run --rm --entrypoint python strimkeep -m strimkeep.library_roots --rename 旧名 新名           # 演练
docker compose run --rm --entrypoint python strimkeep -m strimkeep.library_roots --rename 旧名 新名 --apply   # 执行
docker compose run --rm --entrypoint python strimkeep -m strimkeep.library_roots --set-config ''               # 以后跟随 115
docker compose up -d
```

- 目标目录已有同名文件 → **整体拒绝**，不动任何文件（先人工处理冲突）；
- 会重写的表：`playbacks`、`organized_files`、`output_registry`（含 `files` JSON）、
  `owned_outputs`、`organize_items`（`plan`/`original`）、`organize_jobs`（`config`）、
  `emby_deliveries`（`payload`）；
- 会清空 `sync_snapshots`（路径快照已失效）；
- 会在 `meta.library_root_migration` 留 manifest，`--rollback` 据此反向搬回并改回路径；
- **不会**改 Emby 的媒体库设置。改完必须人工把 Emby 库根改到新目录。

## 四、改完 Emby 要做什么

以本机为例（Emby 容器 `./strm:/media`）：

| Emby 库 | 改前 | 改后 |
| --- | --- | --- |
| 剧集 | `/media/影视媒体库/剧集`（+`/media/分享影视库/剧集`） | `/media/媒体库/剧集`（+`/media/分享影视库/剧集`） |
| 动漫 | `/media/影视媒体库/动漫` | `/media/媒体库/动漫` |
| 影片 | `/media/影视媒体库/电影`（+`/media/分享影视库/电影`） | `/media/媒体库/电影`（+`/media/分享影视库/电影`） |

改完在 Emby 里对每个库执行一次「扫描媒体库文件」。StrimKeep 侧可以用
`GET /api/v1/emby/coverage`（0.8.19 起的媒体库路径自检）确认新路径被某个库覆盖。

## 五、Emby 看不到目录？先查挂载，别急着改路径

现象：宿主机 `strm/媒体库` 里明明有文件，库根也写对了，但 Emby 里就是空的 / 提示「无法访问」。

原因几乎总是 **bind mount 挂住了被替换掉的旧目录**：Docker 绑定的是 inode。宿主机上如果
`strm` 这一层被重命名、删除重建或换过权限，容器里的 `/media` 会继续指向那个已删除的空目录，
而且**不报错**——`ls` 只是什么都没有。

一条命令判断（宿主机建探针，容器里看得到才算正常）：

```sh
touch /path/to/StrimKeep/strm/.probe
docker exec emby_strimkeep ls -a /media          # 看不到 .probe → 僵尸挂载
stat -c '%i' /path/to/StrimKeep/strm      # 和容器里的 inode 不一致 → 同样是僵尸
docker exec emby_strimkeep stat -c '%i' /media
```

修复（配置在 `./emby` 里，重建容器不会丢）：

```sh
cd /path/to/StrimKeep
docker compose up -d --force-recreate emby_strimkeep
docker exec emby_strimkeep ls /media/媒体库       # 应能看到 电影/剧集/动漫/…
```

只有「`strm` 这一层目录本身」被替换时才需要重建；在它**里面**增删文件、改库根、跑同步都不需要。

2026-11-04 现场就是这种情况：迁移把库根从 `影视媒体库` 换到 `媒体库` 后，Emby 的 `/media`
仍挂在旧 inode 上（容器 inode 46222597 vs 宿主机 43396593，探针不可见），重建容器后立刻正常
（Emby 视角：电影 173 / 剧集 1272 / 动漫 20）。同一现场的 `strm` 目录权限还一度是 `mode=0`，
已改成 `755`（`data` / `emby` 本来就是 755）。


## 六、本地多余的 STRM 怎么清（--prune-legacy）

命名规则换代（旧版把 TMDB 分集标题写进文件名 `S01E01.初潮`，现版本用 `S01E01.第1集`）
会在本地留下**同一集的第二份 `.strm`**。它们不在台账里，所以洗版/失效检测都不会管它们，
Emby 里就会出现同一部片两条、或剧集集数翻倍。

```sh
# 只读演练：删多少、哪些保留
docker compose run --rm --entrypoint python strimkeep -m strimkeep.library_roots --prune-legacy
# 执行：先把清单（含每个 STRM 的原文）写进数据目录，再删
docker compose run --rm --entrypoint python strimkeep -m strimkeep.library_roots --prune-legacy --apply
# 反悔：按清单原样写回
docker compose run --rm --entrypoint python strimkeep -m strimkeep.library_roots --restore /data/data/prune-legacy-<时间>.json
```

判定口径（只删能证明是副本的）：

| 情况 | 处置 |
| --- | --- |
| 同一作品同一集，已有一条**登记路径且文件存在** | 未登记的那条 → 删 |
| 这一集**没有**登记路径 | 保留（旧文件是唯一来源） |
| 整部作品只有旧命名、没有任何登记 | 保留（删了就少集） |

删除走 `fsops.purge_strm()`：只 unlink 单个文件、清掉同目录已无视频的 sidecar、再自下而上删空目录，
**绝不做目录级 `rmtree`**；库根自身永远保留。

每日巡检（`sweep`）也修好了同一类判定：`sweep_orphans()` 现在会读 play115 地址里的 `file_id`，
同一云端文件已被登记到别的路径时，自动清掉多余的本地副本（0.8.31 前它只认旧 `/play/<token>` 形态，
对 play115 完全无效）。


## 七、常见误判

- **「台账有行、磁盘没文件」**：历史遗留（改名前后分叉）。改名只修前缀；要补齐必须跑一次
  全量 STRM 同步，从 115 重新生成。
- **「Emby 里看不到新剧」**：先确认 `expected_root` 是否等于 Emby 库根的最后一段；
  再看操作记录里有没有「库根不一致」或「媒体库路径自检未通过」（也可能是上一节的僵尸挂载）。
- **「空目录清理把库根删了」**：0.8.30 起不可能——`/strm` 下的一级目录永远保留
  （原来靠名字白名单，库根一改名就失效）。
