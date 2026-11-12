# StrimKeep 0.8.40 — 离线链路留痕与自动化

- 日期：2026-11-04

## 现场问题
1. 从 Telegram 机器人提交 ed2k/磁力离线下载，`/events` 实时日志里没有记录。
2. 只有「转存」提交会立刻整理；手动提交的磁力和 ed2k 不自动整理，「待整理」的 5 分钟扫描也扫不到，只能手动触发。

## 根因
1. `OfflinePostProcess.submit()` 自己不记事件，唯一那条 `store.event('offline', '已提交 …')` 写在 Web 路由
   `/api/v1/offline/submit` 里。机器人直连 `engine.offline.submit()`，所以没有日志。
2. `submit()` 之后靠 `check()` 认领产物，三道门都可能卡住：
   - 每条链接必须**唯一匹配**到 115 离线任务；磁力按 btih 匹配，**电驴只按 URL 包含匹配**。115 会规范化链接，
     ed2k 常常一条都匹配不上，等到 `EXPIRE_SECONDS`（24 小时）落到 `needs_review`；
   - 即使匹配上，产物还要用任务返回的 `file_id` 在目录增量里唯一确认，多文件磁力/目录型产物经常确认不了；
   - 认领失败就永远走不到 `_enqueue_organize()`——表现就是「只有转存会自动整理」。
3. 5 分钟扫描 `share_scan_tick()` 只扫本地 `待整理` 目录里的分享 STRM，**不扫 115 离线目标目录**；
   `organize` 的定时扫描范围是配置里的 `source_cids`，离线下载目录通常不在里面，兜底也没有。

## 改动
- `offline_api.submit(..., origin='web')`：提交成功即写一条 `offline` 事件（登记号、来源 `web`/`bot`、链接数、
  目标目录、是否自动整理）；删除 Web 路由里那条重复日志；机器人调用处传 `origin='bot'`。
- 新增 `_link_key()`：磁力（btih）与**电驴（32 位 ed2k hash）**都按 hash 认领，其余链接退回 URL 包含匹配；
  `_matching_tasks()` 统一走它。电驴从「几乎必然认领失败」变成和磁力同一条路径。
- 新增 `Organizer.offline_scan_tick()` + `offline_new_videos()`：每 `offline_scan_minutes`（默认 5，0 关闭）
  **轮询一个**离线目录（磁力/电驴/直链/机器人三处去重），用 `fresh_children()` 取实时清单，
  只挑「是视频且 `file_id` 不在 `organized_files` 里」的条目；有就入队 `origin=offline-scan:<cid>` 的整理任务
  （`cleanup_disabled=True`，不删源目录），已有同源 queued/running 任务则跳过。
  - 直接视频为 0 时，每轮最多再看**一个**子目录（bt 目录型产物），避免 API 风暴。
  - 不看链接归属：归属仍由整理任务自己的识别与核对决定，不认领其他文件。
  - 同一批文件只入队一次：签名 = `目录 + 排序后的 file_id`，签名没变就不再重排——
    否则整理失败的残留会一直留在目录里，让「没进台账」始终成立，变成每 5 分钟死循环。
    目录里出现新视频则签名变化，照常入队。
- `scripts/verify.sh` 自检组加入 `test_ops0840.py`。

## 验证
- 新增 `test_ops0840.py` 6 项：ed2k 按 hash 认领（含反例）、`submit` 记事件且带 `bot` 来源、
  兜底扫描只入队一次（含任务结束后残留不重排）、已入台账或开关关闭时不入队、提交失败也留痕、刚提交的目录优先扫。
- 受影响的离线/整理回归 42 项通过（`offline_throttle`、`sweep_offline`、`channel_skip_offline`、`organize`）。
- 完整门禁 `verify.sh` 全部通过：**652 项通过 / 1 项跳过，11 组全绿**。

## 已知边界
- `offline_scan_minutes` 是 meta 键，默认 5 分钟，未在 UI 暴露；每轮只扫一个目录，
  目录多时兜底按轮询摊开，不会同一轮把所有目录列举一遍。
- 兜底按「目录里有未整理视频」入队，不判断是否本批提交的产物；归属核对仍在整理任务里。
- 电驴 hash 取链接里的 32 位 MD4；若 115 返回的 `info_hash` 与链接 hash 不一致（极少见），仍会落到 `needs_review`。
- 兜底扫描接管后，原来那几条卡在 `submitted` 的提交记录不会自动改状态：满 24 小时仍会翻成
  `needs_review`（内容是真实情况——无法把文件归属到某一次提交）。这是「账实分离」的取舍：
  整理照常进行，登记表只作为对账线索。
