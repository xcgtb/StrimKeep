# StrimKeep 0.8.41 — 令牌口径修正 + 整理节奏可调

- 日期：2026-11-04

## 一、`meta.metrics_token` 配了却还是 404
`METRICS.md` 给了两种开启方式：环境变量，或直接 `INSERT` 一条裸字符串进 `meta`。
但 `metrics_token()` 用的是 `store.meta()`（原样返回），而 `store.set_meta()` 写进库的是
**JSON 序列化**结果。于是用 `set_meta` 写入的令牌会变成带引号的 `"abc"`，和请求里的 `abc`
永远不相等 —— 现象就是「明明配了令牌，`/metrics` 还是 404」。

改动：`metrics_token()` 换成「环境变量 → meta（先按 JSON 解，解不动就当裸串）」，
两种写法都认；顺手 `strip()` 掉首尾空白（手抄令牌最常见的坑）。
`docs/operations/METRICS.md` 的两种写法都不用改，仍然有效。

## 二、离线兜底扫描的节奏不可调
0.8.40 的 `offline_scan_tick()` 只认隐藏 meta 键 `offline_scan_minutes`，界面上没有任何入口，
用户只能接受默认 5 分钟。

改动：
- `OfflineConfig` 新增 `scan_minutes`（默认 5，`ge=0, le=1440`，0 = 关闭），走既有的
  `PUT /api/v1/offline/config` 保存，与目录/分流一起落库。
- 离线下载页「直链与通用目录」面板新增「整理节奏」下拉（关闭 / 5 / 10 / 30 分钟），
  改动计入该面板的未保存提示，和目录设置一起保存。
- `offline_scan_tick()` 主口径改为 `offline_config.scan_minutes`；`meta.offline_scan_minutes`
  仍然生效，作为运维临时压过（0 立刻停）。

## 验证
- 新增 `test_ops0841.py` 3 项：令牌三种来源（env / 裸 meta / JSON meta）、
  `OfflineConfig.scan_minutes` 默认值与上限、节奏来自配置且 meta 可压过。
- 受影响回归（`ops0839`/`ops0822`/`ops0840`）24 项通过。
- 完整门禁 `verify.sh` 全部通过：**655 项通过 / 1 项跳过，11 组全绿**。

## 已知边界
- 节奏只影响「兜底扫描」，不影响 115 侧下载速度与整理任务本身的写节流。
- 页面下拉只给 4 档；要别的值走 API（`scan_minutes` 支持 0–1440 任意整数）。
