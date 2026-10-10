# P1：片库局部事实同步与 Web 增量通知（2026-10-10）

## 已实现

- 在 P0 的 5 分钟入库同步中，仅对本轮变更的 Emby Series ID 进行局部回读。允许 Emby 返回已确认的新剧集后，保守创建片库映射条目；已存在同 TMDB ID **且名称与年份完全匹配**的系列可按双库季集集合并，不使用目录唯一性推断合并。
- 新作品在下一次完整 TMDB 校准前标记 `pending`（无 TMDB 标为 `no_tmdb`），**不自动认定完整/可删除**；原治理匹配、七维画质和删除流程没有改变。
- 单剧查询失败、不完整、超过 5000 条分页限制、无可信库路径时，保留旧数据并记录日志，避免将网络错误当作删除/缺集。
- 新增 SQLite `library_ui_updates` 文档，保留最多 64 次事件和每事件最多 100 个 Series ID。`GET /api/library/changes?since=<revision>` 只提供变更行；客户端落后、事件不完整或完整校准时要求重载。
- 带原有 Cookie/Basic Auth 的 `GET /api/library/events` 提供 SSE 版本通知；前端 30 秒轻量轮询兜底。已有映射海报仅更新卡片正文与角标，**保留已加载 img 节点**；新增作品或筛选条件变化时可重排可见卡片。影视探索复用原可见卡片事实局部刷新；总览收到版本变更时按需更新。
- 保留完整 TMDB 对照时间 `ts`，单剧事实更新时间 `facts_ts` 单独更新；不把局部补集冒充已完整复核。
- Emby 完整片库校准成功时发布“全量同步”通知。SQLite 不新增表或删除历史文档。

## 明确限制

- P1 不监听 CD2/STRM 挂载的跨系统文件事件。Emby `DateCreated` 不覆盖删除、旧时间戳重命名与离线期间所有变化；继续保留 P0 的六小时最近 24 小时入库对账及低频完整媒体事实校准。**不能保证仅依靠事件发现全部删除**。
- P1 的即时回写仅针对 `Episode` 入库和已验证的 `Series`。新电影、删除、改名等仍通过原有显式操作与定时完整校准发现。
- SSE 为通知通道，不传输原始媒体路径或 API 密钥，SSE 断线自动重连与 30 秒轮询兜底。Safari 等浏览器无需在 URL 中传入密码。
- 对陌生 Emby 库路径或元数据不完整的作品，安全地跳过局部建档，待下一次完整校准；**不会以缺失/空数据驱动删除行为**。
- 本次在本地模拟环境完成测试；**真实飞牛 CD2 挂载、Emby 15.6 万集的 P1 场景及多客户端 SSE 尚需 NAS 现场验收**。此前生产 P0 内存与入库增量实测不等于 P1 已通过现场验收。

## 自动验收

- `python -m pytest -q dev/tests/`
- `node dev/tools/verify_frontend.cjs`
- `python dev/tools/static_check.py`
- `python dev/tools/release_check.py`
- `python dev/checks/verify_docs_schema_compat.py`
- `python dev/checks/verify_release_startup.py`

## NAS 现场验收（升级前先备份）

1. 先将补丁覆盖 Git 工作区，在该工作区跑 `git diff --check`、完整测试和 Docker 测试构建。不得直接覆盖正式 `/vol1/1000/docker/StrimKeep/data`。
2. 抽样验证已有剧补集、本地/分享双库同时补集、首次出现新剧、请求失败后的旧事实保留；分别检查探索、映射、弹窗与总览。
3. 用浏览器开发者工具检查 `/api/library/events` 已鉴权、返回 `text/event-stream`，新增分集后 `/api/library/changes` 只返回受影响的剧集；Safari 不支持/断开时确认 30 秒轮询生效。
4. 比较旧版和 P0+P1 在相同媒体文件、相同规则下的治理预览与保护结果；治理删除需继续按原流程人工确认。
5. 连续 30–60 分钟记录 `docker stats`、`VmRSS`、同步日志；确认每 5 分钟不会全量抓取 15.6 万集，异常时按已有镜像/数据库备份回滚。

## 最终 NAS 验收入口

使用 `docs/P1_NAS_ACCEPTANCE.md` 与 `dev/checks/run_p1_nas_readonly.sh`。先检查 SHA-256，使用独立 P1 镜像、临时 SQLite 与只读双库媒体挂载执行，不切换正式 P0；浏览器实机、多客户端和正式容器 30–60 分钟监控仍须现场确认。
