# Vue 3 前后端功能完整性审计（1.4.11 候选）

## 范围与结论

本轮审查对象是上一份完整源码交付提交 `47e1dc88bb4ddfbcbd815ad2ebab5aace8d6f100`，基于 `origin/main` 的 `455cd287681fbdc2028df4d3cdf0e4c54b9ec671`。对照当前后端注册的 OpenAPI、v1.4.7 旧前端 JS、现有 Vue 页面及后端配置白名单。不是把「路由能打开」当作功能完整。

OpenAPI 注册共 162 条路径、181 个常规读写操作（含首页）；播放器还支持 HEAD，收藏夹及静态挂载不作为业务操作计数。当前发布序列为 v1.4.10；历史最高可达语义标签为 v1.12.11。本次修复纳入 1.4.11 正式发布；审查时未核实用户正在运行的镜像/容器。

确认存在迁移遗漏，已修复下表。后端 `app/` 无改动，不增加重复的兼容前端。每个后端功能都有页面入口、等价流程或明确的外部协议用途；这不等于所有第三方服务的线上行为均已验证。

## 已确认和补齐的项目

| 项目 | 复查前的问题 | 本轮实现 |
| --- | --- | --- |
| 片库实时变化 | 只每 30 秒重读首屏；加载第二页后不再刷新 | 接入 SSE 提示与变化版本；断线 15 秒小型检查；仅在版本变化时核对已加载范围，保留分页和有效选中项；不触发全库扫描 |
| 探索/总览联动 | 没接入现有变化事件；探索仅更新最后 200 张角标 | 变化提示触发事实刷新；探索分批核对所有已加载角标，保留海报节点 |
| 影视题材 | 页面只有 7 项，后端有 24 项 | 从真实 `/api/cloud/resources/catalog` 读取完整字典，不再维护缩水副本 |
| 频道筛选 | 只能手填地区与题材编号 | 12 地区、24 题材中文按钮；动作等题材同时保存电影/剧集编号；保留高级代码输入 |
| 片库状态筛选 | 缺少未匹配 TMDB 与等待对照入口 | 补齐 `no_tmdb` 和 `pending`；原待对照合并行为保留 |
| 任务回执 | 进度、当前文件、执行步骤和洗版记录未展示 | 进度条、阶段/文件、逐步回执、洗版原因单独呈现，原确认操作继续使用既有接口 |
| 大列表与深层证据 | 通用详情只显示 100 项；治理只显示 200 项；深层证据无法继续看 | 分批继续显示和深层展开；不再把有数据的尾部隐藏为不可访问 |
| 入库监控 | 配置/回调已有，但最近入库缓存的时间和失败告警缺失 | 展示缓存时间、缓存告警和统计；轮询状态不覆盖未保存开关 |
| NAS 目录搜片 | `/api/search` 没有页面入口，未入 Emby 的目录无从查询 | 片库映射增加按需目录检索，结果按文本转义展示 |
| 分类统计 | `/api/library_stats` 没有入口 | 汇报页增加本地/分享 STRM 分类统计及时间；主动点击才读取，首次可能触发后端目录统计 |
| 观看进度 | `/api/played` 没有入口 | 汇报页增加续播记录和相对观看位置的新集提示，主动读取 Emby |
| 外部资源候选 | 规格藏在通用详情里且缺少版本筛选 | 来源、分辨率、动态范围、解锁状态筛选；显式展示发布组/音轨/字幕/人数等后端字段，提示 50 项返回上限 |
| 系统诊断与凭据提示 | NAS 根路径状态缺失；外部令牌保存状态不明确 | 系统页展示实际根路径及服务时区；外部设置标明已保存令牌 |

## 核对后已有的功能

账号 Cookie/扫码/官方授权及检查、目录角色与版本校验、分享预览与选择转存、磁力/ed2k/秒传接收、机器人接收、用户会话/频道历史、整理识别修正、命名五模板、分类/地区/过滤、八维整理、七维治理、STRM 生成与撤销、源删除回执只读核实、Emby 多连接与入库重试、追更设置/检查、晨报预览/发送、孤儿/目录/云端残留预览清理、单作品/批量删除、日志导出、治理计划预演/执行均有前端入口。

云设置的完整默认键与前端字段由真实后端测试核对；治理、七维、晨报、入库等使用各自专用配置接口，不要求把内部存储键再重复做成一个大表单。保存型动作保留原确认、账号版本及未知结果不重放约束。

## 验证边界

- 新增 10 组功能测试，桌面/手机共 20 项：SSE、连接释放、断线回退、80 张已加载卡片、完整题材、频道实际请求 ID、任务进度/105 步回执、205 项治理、入库草稿不被轮询覆盖、分类/观看按需读取、NAS 文本转义、候选规格与筛选。
- 全量浏览器回归：85 项通过、1 项桌面不适用用例跳过；Vitest 22 项通过；正式构建 27 页 × 桌面/手机无页面异常、无缺失哈希资源、无横向溢出；类型、格式、Python 静态检查及正式入口检查通过。
- 真实 FastAPI 核对 29 个接口读取、会话、配置保存回读、字典与后端逐项相同、变化版本、入库缓存、路径状态和空片库语义。
- 原后端 1,692 项回归已通过，本轮后端源码未变化。没有连接真实 NAS/115/Emby/Telegram，未自动发送消息、移动源文件或执行媒体删除；这些端到端行为不能凭隔离测试冒称验收通过。

## 逐接口对照

「等价入口」表示前端使用同一业务能力的聚合/分页/异步安全流程，不需要每个后端兼容别名再增加一颗按钮。资源高级直接提交接口仍在后端，页面沿用先预览再确认的同能力流程。外部回调和播放器协议由其对应客户端调用。

| 方法 | 后端路径 | 页面或用途 | 核对说明 |
| --- | --- | --- | --- |
| POST | `/api/login` | 登录 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| POST | `/api/logout` | 侧栏退出 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| GET | `/` | 正式首页 | 登录后加载 Vue 首页 |
| GET | `/api/overview` | 运行总览 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| POST | `/api/overview/refresh` | 运行总览 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| GET | `/api/overview/recommendations` | 运行总览 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| GET | `/api/runtime/status` | 系统状态/页头 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| GET | `/api/ready` | 系统探针 | 供启动/容器检查；运行状态页显示 readiness |
| GET | `/api/health` | 系统状态→NAS 路径与时区 | 本轮补齐 L_ROOT/S_ROOT/CLOUD_L_ROOT/DATA_DIR 状态 |
| GET | `/api/storage/status` | 系统状态 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| GET | `/api/consistency` | 系统状态 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| GET | `/api/library/health` | 系统状态 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| GET | `/api/dashboard` | 总览等价入口 | Vue 使用 /api/overview 聚合读取 |
| GET | `/api/task/{tid}` | 任务回执组件 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| POST | `/api/task/{tid}/cancel` | 任务回执组件 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| POST | `/api/cache/refresh` | 总览等价入口 | 后端兼容别名；Vue 使用 /api/overview/refresh |
| GET | `/api/bot/status` | 日志中心/系统状态 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| GET | `/api/plan/{plan_id}` | 治理计划详情 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| GET | `/api/plans` | 治理总览 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| POST | `/api/check` | 治理扫描 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| POST | `/api/clean` | 治理计划执行 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| GET, POST | `/api/manual_done` | 片库映射 | 写入此接口；读取使用作品详情的 _md 标记 |
| GET | `/api/orphans` | 目录清理 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| POST | `/api/orphans/clean` | 目录清理 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| GET | `/api/wash/empty-dirs/browse` | 目录清理 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| POST | `/api/wash/empty-dirs/scan` | 目录清理 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| POST | `/api/wash/empty-dirs/clean` | 目录清理 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| POST | `/api/wash/cloud-residue/preview` | 目录清理 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| POST | `/api/wash/cloud-residue/clean` | 目录清理 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| GET | `/api/governance/latest` | 治理等价入口 | Vue 使用 /api/governance/summary，含扫描结果和有效性 |
| GET | `/api/governance/summary` | 治理总览 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| GET, POST | `/api/governance/auto` | 七维与治理规则 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| POST | `/api/emby/webhook` | Emby 外部回调 | 在连接设置中配置/复制回调地址，由 Emby 调用，非手动页面动作 |
| GET | `/api/ingest` | 入库汇报/总览 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| GET | `/api/ingest/status` | 连接与设置→入库监控 | 本轮补齐，最近缓存时间、告警与统计 |
| GET | `/api/stats` | 入库汇报等价入口 | 与 /api/ingest 共用统计能力 |
| GET | `/api/played` | 入库汇报→观看进度 | 本轮补齐，主动点击读取 Emby 续播与新集提示 |
| GET | `/api/search` | 片库映射→NAS 目录检索 | 本轮补齐，用户输入关键词后读取 NAS 目录 |
| GET | `/api/records` | 执行记录及完整详情 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| GET | `/api/records/{rid}` | 执行记录及完整详情 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| GET | `/api/logs` | 实时日志 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| GET | `/api/logs/download` | 实时日志 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| GET | `/api/emby/poster/{item_id}` | 海报组件 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| GET | `/api/tmdb/poster/{filename}` | 海报组件 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| GET | `/api/explore` | 影视探索 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| POST | `/api/explore/library-status` | 影视探索 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| GET | `/api/tmdb/progress` | 片库对照进度 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| GET | `/api/library/changes` | 片库/探索/总览 | 本轮补齐，SSE 后读取变化版本，断线小型轮询 |
| GET | `/api/library/events` | 片库/探索/总览 | 本轮补齐，同源会话 SSE；离页或隐藏时释放连接 |
| GET | `/api/emby/library/preview` | 片库分页等价入口 | Vue 直接使用 /page 的有界卡片接口；不需要整库载入 |
| GET | `/api/emby/library/page` | 片库映射 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| GET | `/api/emby/library/item` | 片库映射 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| GET | `/api/emby/library` | 片库映射 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| GET | `/api/library_stats` | 入库汇报→STRM 分类统计 | 本轮补齐，按需读取，不在打开总览时自动扫描 |
| GET, POST | `/api/ingest/settings` | 连接与设置→入库监控 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| GET | `/api/emby/series/{series_id}/episodes` | 片库详情/批删 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| POST | `/api/emby/{media}/batch-delete/preview/start` | 片库批删预览 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| GET | `/api/emby/{media}/batch-delete/preview/status` | 片库批删预览 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| POST | `/api/emby/series/batch-delete/preview` | 片库批删等价入口 | Vue 使用异步 preview/start 与 preview/status |
| POST | `/api/emby/series/batch-delete/execute` | 片库详情/批删 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| POST | `/api/emby/movies/batch-delete/preview` | 片库批删等价入口 | Vue 使用异步 preview/start 与 preview/status |
| POST | `/api/emby/movies/batch-delete/execute` | 片库批删 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| POST | `/api/emby/series/{series_id}/delete` | 片库详情/批删 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| POST | `/api/emby/series/{series_id}/resync` | 片库详情/批删 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| POST | `/api/emby/movie/delete_by_tmdb` | 片库详情删除 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| GET | `/api/emby/delete/progress/{operation_id}` | 删除任务回执 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| GET, POST | `/api/subscriptions` | 追更订阅/探索订阅 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| POST | `/api/subscriptions/settings` | 追更订阅/探索订阅 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| GET | `/api/subscriptions/status` | 追更订阅/探索订阅 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| POST | `/api/subscriptions/check` | 追更订阅/探索订阅 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| GET, POST | `/api/morning` | 入库汇报→晨报 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| POST | `/api/morning/preview` | 入库汇报→晨报 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| POST | `/api/morning/send` | 入库汇报→晨报 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| GET, POST | `/api/appearance` | 外观设置等价入口 | 读取 appearance；保存使用 /api/config 的同名背景字段 |
| GET, POST | `/api/strategy` | 七维与治理规则 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| GET, POST | `/api/cover-strategy` | 七维与治理规则 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| POST | `/api/cover-compare` | 七维与治理规则 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| GET | `/api/exempt/scan` | 豁免目录核对 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| GET, POST | `/api/config` | 连接与设置 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| POST | `/api/config/test/emby` | 连接与设置 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| POST | `/api/config/test/tmdb` | 连接与设置 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| POST | `/api/config/test/telegram` | 连接与设置 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| POST | `/api/config/test/proxy` | 连接与设置 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| GET, POST | `/api/upstream/config` | 外部资源服务/资源候选组件 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| POST | `/api/upstream/test` | 外部资源服务/资源候选组件 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| POST | `/api/upstream/search` | 外部资源服务/资源候选组件 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| GET | `/api/upstream/providers` | 外部资源服务/资源候选组件 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| GET | `/api/upstream/directories` | 外部资源服务/资源候选组件 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| GET | `/api/upstream/resource-subscriptions` | 外部资源服务/资源候选组件 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| GET | `/api/upstream/subscription-events` | 外部资源服务/资源候选组件 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| POST | `/api/upstream/operations/{action}` | 外部资源服务/资源候选组件 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| GET | `/api/upstream/jobs/{operation}/link` | 外部资源服务/资源候选组件 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| POST | `/api/upstream/transfer` | 外部资源服务/资源候选组件 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| GET | `/api/upstream/jobs` | 外部资源服务/资源候选组件 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| POST | `/api/upstream/jobs/{operation}/acknowledge` | 外部资源服务/资源候选组件 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| GET | `/api/upstream/history` | 外部资源服务/资源候选组件 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| GET | `/api/upstream/history/{record_id}` | 外部资源服务/资源候选组件 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| GET | `/api/cloud/status` | 账号选择器/账号管理 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| POST | `/api/cloud/qr` | 账号扫码授权 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| GET | `/api/cloud/qr/{flow_id}/image` | 账号扫码授权 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| POST | `/api/cloud/qr/{flow_id}/poll` | 账号扫码授权 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| DELETE | `/api/cloud/qr/{flow_id}` | 账号扫码授权 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| POST | `/api/cloud/accounts/{account_id}/check` | 账号授权与目录管理 | 已接入账号范围及配置版本校验 |
| POST | `/api/cloud/accounts/cookie` | 账号 Cookie 导入 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| GET, POST | `/api/cloud/accounts/{account_id}/directories` | 账号授权与目录管理 | 已接入账号范围及配置版本校验 |
| POST | `/api/cloud/accounts/{account_id}/open/check` | 账号授权与目录管理 | 已接入账号范围及配置版本校验 |
| DELETE | `/api/cloud/accounts/{account_id}` | 账号授权与目录管理 | 已接入账号范围及配置版本校验 |
| GET | `/api/cloud/files` | 文件管理/目录选择器 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| POST | `/api/cloud/shares/preview` | 分享浏览/转存/分享 STRM | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| POST | `/api/cloud/transfers` | 分享转存 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| POST | `/api/cloud/shares/strm/preview` | 分享浏览/转存/分享 STRM | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| GET, POST | `/api/cloud/accounts/{account_id}/receiving` | 资源接收→机器人配置 | 已接入账号范围及配置版本校验 |
| POST | `/api/cloud/resources/preview` | 资源接收 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| POST | `/api/cloud/resources/apply` | 资源接收 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| GET | `/api/cloud/resources/offline` | 资源接收 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| GET | `/api/cloud/resources/catalog` | 探索/频道中文筛选 | 本轮接入完整 24 题材与 12 地区字典 |
| GET, POST | `/api/cloud/accounts/{account_id}/channels` | 频道与机器人 | 已接入账号范围及配置版本校验 |
| DELETE | `/api/cloud/accounts/{account_id}/channels/{channel_id}` | 频道与机器人 | 已接入账号范围及配置版本校验 |
| POST | `/api/cloud/accounts/{account_id}/channels/{channel_id}/test` | 频道与机器人 | 已接入账号范围及配置版本校验 |
| GET | `/api/cloud/accounts/{account_id}/inbox` | 频道与机器人 | 已接入账号范围及配置版本校验 |
| GET, POST | `/api/cloud/accounts/{account_id}/maintenance` | 账号管理→维护 | 已接入账号范围及配置版本校验 |
| POST | `/api/cloud/accounts/{account_id}/maintenance/run` | 账号管理→维护 | 已接入账号范围及配置版本校验 |
| POST | `/api/cloud/jobs/{job_id}/maintenance/verify` | 任务中心/回执/诊断/继续与停止 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| POST | `/api/cloud/shares/strm/apply` | 分享浏览/转存/分享 STRM | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| GET | `/api/cloud/tmdb` | 识别修正弹窗 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| POST | `/api/cloud/plans` | 影视整理 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| POST | `/api/cloud/plans/apply` | 影视整理 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| POST | `/api/cloud/strm/sync` | STRM 同步 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| GET | `/api/cloud/assets` | STRM 引用、源关系、删除核验 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| POST | `/api/cloud/assets/{asset_id}/revoke` | STRM 引用、源关系、删除核验 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| GET | `/api/cloud/assets/{asset_id}/source` | STRM 引用、源关系、删除核验 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| POST | `/api/cloud/assets/{asset_id}/deletion/verify` | STRM 引用、源关系、删除核验 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| POST | `/api/cloud/assets/{asset_id}/deletion/reset` | STRM 引用、源关系、删除核验 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| GET | `/api/cloud/jobs` | 任务中心/回执/诊断/继续与停止 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| GET | `/api/cloud/jobs/{job_id}` | 任务中心/回执/诊断/继续与停止 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| GET | `/api/cloud/jobs/{job_id}/diagnostics` | 任务中心/回执/诊断/继续与停止 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| POST | `/api/cloud/jobs/{job_id}/verify` | 任务中心/回执/诊断/继续与停止 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| POST | `/api/cloud/jobs/{job_id}/continue` | 任务中心/回执/诊断/继续与停止 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| POST | `/api/cloud/jobs/{job_id}/cancel` | 任务中心/回执/诊断/继续与停止 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| POST | `/api/cloud/jobs/{job_id}/stop` | 任务中心/回执/诊断/继续与停止 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| GET, POST | `/api/cloud/settings` | 整理播放/命名/八维设置 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| GET | `/api/cloud/accounts/{account_id}/pipeline` | STRM 输出与播放配置检查 | 已接入账号范围及配置版本校验 |
| POST | `/api/cloud/naming/preview` | 命名预览与说明 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| POST | `/api/cloud/naming/details` | 命名预览与说明 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| POST | `/api/cloud/categories/create` | 影视整理→创建分类目录 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| POST | `/api/cloud/emby/refresh` | Emby 入库跟踪 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| GET, POST | `/api/cloud/rules` | 整理自动化 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| POST | `/api/cloud/jobs/{job_id}/upgrades/continue` | 任务中心/回执/诊断/继续与停止 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| POST | `/api/cloud/schedules/preview` | 整理自动化→下次时间预览 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| DELETE | `/api/cloud/rules/{rule_id}` | 整理自动化 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| POST | `/api/cloud/rules/{rule_id}/enabled` | 整理自动化 | 已接入；需真实第三方服务的动作未在用户 NAS 验收 |
| GET, POST | `/api/cloud/accounts/{account_id}/telegram-reader` | 频道与机器人 | 已接入账号范围及配置版本校验 |
| GET, POST | `/api/cloud/accounts/{account_id}/emby-connections` | Emby 入库跟踪 | 已接入账号范围及配置版本校验 |
| POST | `/api/cloud/accounts/{account_id}/emby-connections/{connection_id}/test` | Emby 入库跟踪 | 已接入账号范围及配置版本校验 |
| GET | `/api/cloud/accounts/{account_id}/ingestion` | Emby 入库跟踪 | 已接入账号范围及配置版本校验 |
| POST | `/api/cloud/accounts/{account_id}/ingestion/{record_id}/retry` | Emby 入库跟踪 | 已接入账号范围及配置版本校验 |
| GET, POST | `/api/cloud/accounts/{account_id}/organizer-sources` | 整理自动化 | 已接入账号范围及配置版本校验 |
| POST | `/api/cloud/shares/receive` | 资源接收等价能力 | 高级直接接收接口保留；Vue 使用分享预览→选择→转存/整理 |
| POST | `/api/cloud/resources/receive` | 资源接收等价能力 | 高级直接提交接口保留；Vue 使用资源预览→确认→接收 |
| POST | `/api/cloud/accounts/{account_id}/organizer/run` | 整理自动化 | 已接入账号范围及配置版本校验 |
| POST | `/api/cloud/accounts/{account_id}/organizer/stop` | 整理自动化 | 已接入账号范围及配置版本校验 |
| GET | `/api/cloud/play/{asset_id}` | 播放器协议入口 | STRM 播放器调用；页面不构造无签名播放请求；另支持 HEAD |
