# StrimKeep 0.8.35：深度删除联动清理专属附属文件（nfo/海报/mediainfo），不误伤其它集

## 一、问题

0.8.34 把深度删除改成「只删 STRM（受管的还删云端源文件）」，故意不碰 nfo / 海报 /
`-media-info.json` / 字幕，理由是无法从 webhook 推断这些侧车归谁。结果：删完单集后，
目录里留下该集的 nfo 和 `-media-info.json`；删完整剧/单季后，`poster.jpg`、`tvshow.nfo`
和空目录也一直堆着，Emby 还可能把这些「只剩 nfo、没有视频」的死条目再显示出来。

## 二、改动

| 文件 | 改动 |
| --- | --- |
| `fsops.purge_strm()` | 增加**专属侧车清理**：先删本条目同名的 `<stem>.*`（nfo/srt/ass…）与 `<stem>-media-info.json`；只有当目录里再无任何视频时，才清目录级侧车（tvshow.nfo / poster.jpg / fanart.jpg）并自下而上删空目录（库根永不动）。单集只删单集，单季/整剧删到最后一集时自动清目录级侧车 |
| `deep_delete.guarded_managed_delete()` | 托管真删把 `path.unlink()` 换成 `purge_strm(path, root, store)`，删 STRM 时联动清其专属侧车 |
| `deep_delete.apply()` 分享分支 | 分享 STRM 删除同样走 `purge_strm`（只删分享自己的 STRM + 侧车，不碰其它） |
| `share_service.check_alive()` | 分享存活检测撤销时走 `purge_strm`，清该分享 STRM 的专属侧车（目录级侧车仅在目录无其它 STRM 时清） |
| `share_api.revoke()` | 手工撤销分享 STRM 时走 `purge_strm` |
| `tests/test_ops0830.py` | 新增 2 项：单集删除只清本集侧车不动同季其它集；最后一集删除清目录级侧车 + 空目录 |

范围保证：侧车按「同名 stem / `-media-info.json`」精确关联，`S01E01` 的删除不会碰到
`S01E02` 的 strm/nfo；网盘库（`媒体库/...`）与分享库（`分享影视库/...`）仍是两条独立路径。

## 三、验证

| 层次 | 命令 | 结果 |
| --- | --- | --- |
| 新增用例 | `tests/test_ops0830.py`（2 项） | 通过 |
| 受影响回归 | `test_deep_delete` / `test_share_check_safety` | 40 项通过 |
| 完整门禁 | `sh scripts/verify.sh strimkeep-dev:0.8.34` | **`全部通过`，633 项通过 / 1 项跳过，11 组全绿** |

## 四、已知边界

- 目录级侧车（poster/fanart/tvshow.nfo）只在「目录里已无任何视频」时才清——多集目录里删其中一集，海报和 tvshow.nfo 会保留（它们属于整部作品）。
- 只清 `SIDECAR_EXT` 里列出的扩展名；用户手工放的同名非标准文件不会被误删。
