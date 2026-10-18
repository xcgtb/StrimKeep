# 工作台主线整合（基于 v1.5.14）

源码基线：main / feb0330b4cbc6bb2029525f04575b24a9645b699。
本次内容位于 CHANGELOG 的未发布区，未覆盖旧标签，未自动部署 NAS。

## 交互与后端能力

| 页面 | 行为 | 后端契约 |
|---|---|---|
| 影视整理 | 来源、未识别、归档三个用途卡片；模块保存、撤销与修订冲突保护；先预览、修正识别、确认后归档；任务安全停止 | accounts/{id}/directories、plans、plans/apply、jobs/{id}/stop |
| STRM | 生成时机、输出映射、内外网播放、入库核验与通知分卡；秒/分钟/小时显示统一保存为秒 | cloud/settings，新增 external_url、ingest_retry_seconds、ingest_max_attempts |
| 播放核验 | 核对本地 STRM 文件身份、签名、115 源文件及播放解析；只读解析，不下载视频、不返回 CDN Cookie | POST cloud/assets/{id}/playback/check |
| 任务中心 | 网盘任务、治理与扫描摘要统一入口；处理过程显示命名、移动、STRM、入库、通知及耗时；延时子任务纳入原任务过程 | cloud/jobs/{id}/timeline、tasks/recent、task/{id} |
| 治理任务历史 | 最近 200 条状态摘要持久化；重启前未结束的任务显示需核实，不自动重放文件操作 | task_history，沿用 SQLite 文档存储 |
| 日志中心 | 单个 SSE 连接共享任务更新；事件触发增量读取，15 秒补读；暂停、继续、清屏保留游标；从任务跳转自动筛选 | activity/events、logs?after=&task= |
| 追更与探索 | 入库变化触发订阅列表更新；探索仅请求受影响的可见 TMDB 卡片，身份不明确或全量变化时完整核对 | library/events、library/changes、tmdb/explore/status |
| 首页 | Emby 续播和更新提醒；后台单任务刷新、短期缓存、换连接隔离旧数据；不以网络请求次数伪造播放时长 | overview/playback、overview/playback/refresh |

设置保存不执行文件任务。入库重试参数仅用于只读核验 Emby，保留每十次一次的批量扫描请求规则；不会自动重复转存、改名、移动或删除。
external_url 只用于外部播放器复制链接；public_url 继续作为写入 STRM 的地址，兼容已有配置。

## 部署后验收

自动验证使用隔离 SQLite、真实 FastAPI 和有状态的网盘/Emby 测试替身；浏览器截图来自本次构建与测试数据，不是用户 NAS 的实时画面。

在实际 NAS 上依次确认：每模块目录保存并重载 → 预览一部电影和一集剧集 → 确认归档 → STRM 落到挂载输出目录 → Emby 路径映射与入库回执 → 在实际播放设备核验内网/外网播放 → 预览双库治理结果。
真实源文件删除仍由预览计划、明确授权和手动确认控制，不以页面“任务成功”替代网盘或 Emby 回执。

## 仍不冒充已完成的未来能力

播放总时长、完整观众历史、历史趋势和播放排行榜需要可靠的播放事件或 Emby 报告数据源；本轮接通续播，不把它冒充完整播放统计。HTML 中所有示意数值均未作为业务数据写入。
外网域名、FRP、NAS 挂载和真实 115/Emby/TG 凭据只能在用户部署环境验收；本次无法代替实际环境验收。
