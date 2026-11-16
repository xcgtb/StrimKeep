# StrimKeep 功能与架构审计（v3.6.33，2026-11）

> 目的不是再堆功能，而是回答三个问题：
> 1. StrimKeep 现在到底强在哪、弱在哪；
> 2. MoviePilot / TgtoDrive / AutoFilm / MediaWarp 哪些东西值得借，哪些不能照搬；
> 3. 哪些代码/状态是「羁绊」，应该删掉而不是继续加开关。
>
> 结论先行：**双库治理、计划快照、文件身份、深度删除是护城河，不能为了对齐别人而稀释；真正拖后腿的是硬编码单体、历史软删除状态、前端无请求层、CI 不跑测试。**

---

## 1. 量化盘点

| 项目 | 数值 | 说明 |
|---|---:|---|
| 后端模块 | 92 个 `.py` | 单包 `strimkeep/` |
| 后端代码 | 32,257 行 | `wc -l *.py` |
| 最大模块 | `organizer.py` 3,403 行 | 识别、整理、洗版、发布、调度、清理都在里面 |
| 前五模块 | 9,758 行 | 占后端 30%：organizer / cloud115 / main / share_service / sync |
| HTTP 路由 | 198 条 | 其中 181 条直接挂 `Depends(admin)` |
| 数据表 | 36 张 | 单 SQLite，`SCHEMA_VERSION='2'` |
| 直接 DB 调用 | 666 处 | `organizer` 95、`share_service` 84、`main` 51 |
| 宽泛异常 | 199 处 `except Exception` | `organizer` 37、`sync` 16、`deep_delete` 15 |
| 异步任务 | 38 处 `asyncio.create_task` | 分散在 organizer / main / emby_proxy 等 |
| 后端测试 | 196 个文件 / 30,479 行 / 1,672 通过 | 本次删除死代码/死状态后；清理前 1,685 通过 |
| 前端页面 | 30 个 Vue，4,301 行 | `Inventory.vue` 940 行 |
| 前端请求 | 请求已收口到 `src/api/client.ts`，178 处 `props.api(...)` | 之前请求闭包写在 `App.vue`，没有 API 层 |
| 前端组件测试 | 0 个 | 只有 11 个纯 TS 辅助函数测试，261 行 |
| CI | 1 个 workflow，仅发布镜像 | 打 tag 不跑测试；本次改为先测后发 |

这些数字的含义：

- **不是小项目**，后端测试覆盖广，治理逻辑复杂，不能推倒重来。
- **单体集中度太高**：三个文件承担了大部分业务和 SQL，任何新需求都容易继续往 `organizer.py` 里塞。
- **测试强在后端、弱在前端/契约**：没有 Vue 组件测试、没有 API 类型契约测试，页面回归靠手点。
- **发布流程没有质量门**：这是最便宜、收益最高的修复之一。

---

## 2. 功能矩阵（只对比能力，不比较代码量）

图例：✅ 完整；△ 局部/需外部服务；— 没有。

| 能力 | StrimKeep | MoviePilot v3 | TgtoDrive | AutoFilm / MediaWarp |
|---|---|---|---|---|
| 115 分享转存 | ✅ | ✅ | ✅ | △（AutoFilm 主要做 STRM） |
| 115 离线/磁力/ED2K | ✅ | ✅ | ✅ | — |
| 多网盘（123/光鸭/天翼/Alist） | — | △（Alist/网盘插件） | ✅ 123/光鸭/天翼 | △（Alist） |
| Telegram 频道监控 | ✅ | ✅ | ✅ | — |
| RSS / 站点索引 / 订阅下载 | — | ✅ 完整 | △（部分来源/监控） | — |
| 下载器（qB/TR） | — | ✅ | △ | — |
| TMDB 识别/季集解析 | ✅ | ✅ | ✅ | △（AutoFilm 不做深度识别） |
| 洗版/画质比较/多版本 | ✅ 7 维 + 平局策略 | ✅ | ✅ | — |
| 目录规范/重命名 | ✅ | ✅ | ✅ | △ |
| STRM 生成 | ✅ 302 | ✅ | ✅ | ✅ |
| Emby 302 反代/直链注入 | ✅ 多实例 + PlaybackInfo 注入 | ✅ | ✅ Emby/飞牛 | ✅ MediaWarp 专精 |
| Jellyfin / Plex / 飞牛 | — | ✅ | ✅ 飞牛 | ✅ Jellyfin/飞牛，Plex 计划中 |
| 片库映射/缺集/海报墙 | ✅ 强 | △ | ✅ | — |
| 双库治理（本地 vs 分享） | ✅ 计划快照 + 逐文件核验 | — | △ 库存去重 | — |
| 深度删除（Webhook 联动） | ✅ 队列 + 云端/本地双重核验 | △ 删除插件 | △ | — |
| 分享 STRM 管理 | ✅ | △ | ✅ | — |
| 通知 | △ 仅 Telegram | ✅ 多通道 | ✅ Telegram/企微 | — |
| 任务中心/日志/指标 | ✅ | ✅ | ✅ | — |
| OpenAPI / 外部调用 | — | ✅ API + MCP | ✅ OpenAPI token | — |
| 插件系统/市场 | — | ✅ 完整 | ✅ Python 插件 + 市场 | — |
| AI 辅助识别/助手 | — | ✅ Agent/MCP | ✅ AI 回退 | — |
| 音乐整理 | — | ✅ | ✅ MusicBrainz | — |
| 字幕/海报同步 | △ NFO/海报随片，字幕仅随分享附件 | ✅ | ✅ | ✅ MediaWarp 字幕缓存 |
| 内置备份/恢复 | —（文档教 tar） | ✅ 应用内备份 | △ | — |
| 多用户/RBAC | —（单管理员） | ✅ | △ | — |
| i18n/PWA | — | ✅ | — | — |
| 自动化测试/CI | ✅ 后端强 / 前端弱 / CI 无门禁 | ✅ 后端+前端+CI | 未公开 | 未公开 |

### 结论

- **StrimKeep 的差异化不是「功能更多」**，而是「STRM 双库治理 + 可复核的删除」。这两块比 MoviePilot 更专、比 TgtoDrive 更保守。
- **明显的功能缺口**：多网盘、多媒体服务器、多通知通道、OpenAPI、插件化。不要一次全补，先做「适配器骨架」，让后续每个缺口只加一个文件。
- **不能照搬**：MoviePilot 的 96k 行后端 / 269 个模块 / 340 个 Vue 文件是它多年迭代的结果；StrimKeep 现在照搬会直接把维护成本翻倍。

---

## 3. 别人的优势（可借的具体设计）

### 3.1 MoviePilot v3

| 设计 | 位置 | 可借什么 | 不借什么 |
|---|---|---|---|
| 模块适配器 | `app/modules/_base/{downloader,media,mediaserver,notification}.py` | 每类外部系统一个基类 + 注册表；新增 Jellyfin/123/企微只实现差异方法 | 不要为每个小功能建抽象层 |
| 处理链 | `app/chain/base.py`、`app/chain/*` | 「搜索 → 下载 → 整理 → 媒体库」串成可替换链；每步失败可退避/换源 | 不要引入 1,200 行 ChainBase |
| 事件总线 | `app/runtime/events.py` | 事件类型 + 优先级 + 关联 ID + 错误策略；异步消费者与主流程解耦 | 不要做多线程 EventManager + 动态线程池 |
| 领域/契约分层 | `app/domain/`、`app/schemas/` | Pydantic 契约与领域对象分开，API/插件共用 | 不要为内部函数也建 schema |
| 运行时配置/缓存 | `app/runtime/config.py`、`cache.py` | 配置快照 + 文件缓存 + 失效策略 | 不要引入 Redis |
| 前端工程 | `src/api/client.ts`、`src/composables`、`src/pages`、404 个测试 | 统一请求层、组合式轮询、页面拆分、组件测试 | 不要复制 340 个页面/重型 UI 依赖 |
| 迁移 | Alembic | 版本化 DDL，升级可回滚 | 不要为了迁移引入 SQLAlchemy 全家桶 |

### 3.2 TgtoDrive

TgtoDrive 是功能面最接近的竞品，值得借的是「集成广度」和「运维入口」：

- 115 / 123 / 光鸭 / 天翼多网盘；
- 影视 + 音乐整理，MusicBrainz；
- STRM 定时/增量同步，字幕/海报实体同步；
- Emby / 飞牛反代，302 播放；
- 库存快照、去重、清理；
- Python 插件 + GitHub 插件市场；
- OpenAPI token；
- AI 辅助识别、Pansou 聚合搜索、B 站下载；
- 企微通知、SSH 终端、连通性/限速工具。

**借什么**：多网盘/多媒体服务器的「适配器接口」、OpenAPI 的只读端点、插件清单的权限模型。
**不借什么**：SSH 终端、任意 Python 插件执行、AI 自动改文件、多网盘同时全量整理——这些会把安全面和状态面放大，和「简单」相反。

### 3.3 AutoFilm / MediaWarp

- AutoFilm：Alist/115 → STRM，定时/增量，库海报；说明「STRM 工具可以很薄」。
- MediaWarp：Emby/Jellyfin/飞牛前置反代，STRM 302、客户端过滤、外置播放器、字幕缓存、弹幕/美化脚本。
- **借什么**：播放代理只做「改响应里的 DirectStreamUrl / 302」，不要代理视频字节；客户端过滤与 UA 识别；字幕缓存。
- **不借什么**：把大量前端注入脚本塞进反代；StrimKeep 的反代应保持「只改播放地址」的窄职责。

---

## 4. 逐模块审计

### 4.1 入口、鉴权、配置

**现状**

- `main.py` 1,508 行，`create_app` 单函数约 1,266 行；198 条路由中 181 条直接依赖 `admin`。
- 安全基础不错：scrypt 密码、`HttpOnly/SameSite=Strict` Cookie、CSRF 双提交、Origin 校验、CSP、请求体 64KB 上限、登录失败 15 分钟窗口。
- `docs_url=None / openapi_url=None`，没有对外 API 文档；没有 API token。

**问题**

1. 路由注册、生命周期、模型、业务辅助函数全在 `main.py`，新接口继续往里加。
2. 配置是 `settings` 表 + 各模块自己的 `*_config` JSON，没有统一 schema/迁移。
3. 没有 OpenAPI，外部脚本/插件只能模拟浏览器 Session + CSRF。

**动作**

- P1：把 `main.py` 的 lifespan、middleware、auth、静态服务拆到 `app_factory.py` / `security.py`；路由按领域拆 `api/organize.py` 等。
- P2：增加只读 OpenAPI token（`Authorization: Bearer`），先开放 status/tasks/inventory 三类查询。

### 4.2 115 云盘与传输

**现状**

- `cloud115.py` 1,517 行，覆盖登录、目录、分享、转存、离线、回收站、取链、限速。
- 有读/写节流、重试、`ServiceHTTP` 代理分流，播放链接独立连接池。
- 分享/转存/离线/频道四条入口都直接依赖 `Cloud115` 具体类。

**问题**

1. 云盘能力与 115 协议耦合，新增 123/Alist 必须改业务层。
2. 大量 `CloudError` 分支散落在 organizer/share/sync。
3. 没有统一的「能力声明」（某网盘是否支持分享/离线/回收站/秒传）。

**动作**

- P2：引入 `CloudDrive` Protocol（`list/file/url/share_save/offline/recycle/live_exists`）+ `capabilities`；`Cloud115` 只是实现。
- 不要现在拆 115 协议；先把业务层依赖从具体类改成 Protocol。

### 4.3 识别、整理、洗版

**现状**

- `organizer.py` 3,403 行；`identify` 129 行、`run_job` 462 行、`process_item` 200 行。
- 识别有规范化、短语变体、年份证据、TMDB 404 负缓存；洗版 7 维比较。
- 直接 DB 95 处，`except Exception` 37 处；`pending.rglob('*.strm')` 在异步路径里（见 4.4 风险）。

**问题**

1. 识别、计划、执行、发布、清理、调度、通知全在一个类；改一处容易碰全局。
2. 异常宽泛，失败原因经常被折叠成一句话；测试能兜住，但线上定位靠日志。
3. TMDB 请求没有统一的重试/退避/`Retry-After`（审计批里已准备但未落地）。
4. 预取和识别使用的语言/缓存键曾不一致（已记录），说明「同一外部请求两套参数」是结构性问题。

**动作**

- P1：按「识别器 / 计划器 / 执行器 / 发布器」拆 organizer；先搬纯函数与无状态逻辑，不动状态机。
- P1：统一 `TmdbClient`（缓存、负缓存、重试、语言），预取与识别共用。
- P2：把 `except Exception` 改成领域异常 + 明确失败阶段（识别/云端/本地/Emby）。

### 4.4 STRM、播放、同步

**现状**

- STRM 身份写在文件里，播放走 `/play115/...` 302；`playbacks` 表仍保留给受管输出和旧播放身份。
- `sync.py` 1,213 行，负责全量/增量、清单、校验、旧位置清理、失效清理。
- `phase5_api.py` 仍有 4 处 `os.walk`、1 处 `rglob`，部分通过 `to_thread`。

**问题**

1. `playbacks.revoked` 是历史软删除字段，仍被升级/洗版使用；语义和分享侧 `revoked` 混在一起，容易再次出现「删了又复活」。
2. 同步/清理/库存/深删各自实现路径扫描，存在重复与口径差异。
3. 本地文件删除入口多（outputs、sync、share、deep_delete、inventory），虽然都走 `purge_strm`，但前置校验各不相同。

**动作**

- P0（本次）：删除分享侧撤销状态；保留 `playbacks.revoked` 仅用于受管洗版，不再扩散。
- P1：把「本地 STRM 删除」收口为 `LocalStrms.delete(relative, reason)`，所有入口只传身份，不各自拼校验。
- P2：`sync` 与 `inventory` 共用同一个目录扫描器，避免三套 `os.walk`。


### 4.5 分享 STRM

**现状**

- `share_service.py` 1,483 行；生成、整理、洗版、检查、恢复、清理都在里面。
- 直接 DB 84 处，`except Exception` 14 处。
- 历史「撤销」概念在 UI 已删除，但后端还保留 `share_revocations` 表、生成时的撤销拦截、恢复模块，形成「删除后不能再入库」的隐性状态。

**问题**

1. `share_revocations` 会让删过的分享在重新添加时被跳过——与「删了就是没了，重新来是全新的」直接冲突。
2. `recover_share_batch.py` 是没人调用的恢复 CLI，仍带着撤销表依赖。
3. 分享清洗的意图表 `share_wash_intents` 是必要的（原子替换），但和撤销表混在一起增加理解成本。

**动作（本次已做）**

- 删除 `share_revocations` 表与所有检查/清理代码；启动时 `DROP TABLE IF EXISTS`。
- 删除 `recover_share_batch.py` 与相关测试。
- 保留 `share_wash_intents`，它只服务「原子替换」，不是历史状态。

### 4.6 库存、片库映射、双库治理

**现状**

- `inventory_api.py` 867 行、`inventory_preview.py` 794 行；`Inventory.vue` 940 行。
- 明确「目录是库存事实，台账只提供来源身份」；这是正确的方向。
- 双库治理有计划快照、逐文件 SHA/大小/mtime/inode 核验、多季保护、画质/集数比较。

**问题**

1. 页面单文件 940 行，状态/表格/治理/残留清理混在一起，难以做组件测试。
2. 后端预览/执行/删除适配器仍有重复的路径与身份校验。
3. `archive_entries` 历史快照会保留已删除作品，直到目录级清理；用户容易把它当成「台账又回来了」。

**动作**

- P1：把 `Inventory.vue` 拆成 `InventoryTable` / `GovernancePlan` / `OrphanCleanup` / `InventoryFilters` 四个组件；先搬模板与纯函数。
- P1：治理计划执行前统一调用 `inventory_scope.verify_work`，不要再让每个 adapter 自己写一套。
- P2：库存页增加「历史快照」与「当前库存」的显式分区，避免概念混淆。

### 4.7 深度删除

**现状**

- `deep_delete.py` 787 行；队列持久化、批次、预演、云端回收确认、分享侧直接删行。
- 整剧/整季会清 `playbacks revoked`、`output_registry inactive`、媒体信息队列、库存快照、内容缓存（3.6.33）。
- 受管删除仍要求 `output_registry + owned_outputs + 本地 SHA + 文件身份`，云端走 115 回收站。

**问题**

1. `internal_delete` 已变成空壳，但 15 个模块仍在调用/判断它；本次已删除。
2. 分享侧的「撤销」状态与深删「删干净」语义冲突；本次已删除。
3. `purge_history` 用 `LIKE prefix%` 清历史行，索引缺失时会全表扫描；本次补索引。

**动作**

- 已完成：删除 `internal_delete.py` 与所有调用；删除 `drop_internal` / `recently_deleted`；深删只保留「匹配 → 排队 → 核验 → 删」。
- P1：把批次清理的 `prefix` 存入批次元数据，队列排空后统一清历史，避免依赖即时事件。
- P2：为 `deep_delete_queue(batch,state)` 增加可观测指标（排队时长、失败原因分布）。

### 4.8 Emby、反代、媒体信息

**现状**

- `emby.py` 934 行、`emby_proxy.py` 827 行；反代只转发 API、在 `PlaybackInfo` 注入直链，不代理视频字节。
- 媒体信息队列、sidecar 注入、封面服务、库路径自检都有。
- 仅支持 Emby，没有 Jellyfin/Plex/飞牛适配。

**问题**

1. Emby 具体 API 渗透到 organizer/sync/inventory，换媒体服务器成本高。
2. 反代与媒体信息共享部分播放身份逻辑，但错误码各自定义。
3. 没有客户端过滤/UA 策略（MediaWarp 的强项）。

**动作**

- P2：引入 `MediaServer` Protocol（`test/scan/items/playback_info/delete/refresh`），先包一层 `EmbyServer`，业务层只依赖 Protocol。
- P2：统一播放错误码（`no_identity/share_missing/cloud:*`），反代、直链、日志共用。
- P3：按需加 Jellyfin；不要同时加 Plex，测试成本会翻倍。

### 4.9 通知、任务、日志、指标

**现状**

- 仅 Telegram；通知有持久队列、重试、海报原位更新、分类开关。
- 任务中心、SSE 日志、`/metrics`、慢请求/循环延迟看门狗都有。
- 没有企微/钉钉/Bark/ntfy/webhook。

**问题**

1. 通知通道与 Telegram 消息格式耦合（海报、分组、编辑消息）。
2. 任务/事件/通知三套状态，前端需要拼；没有统一 task DTO。
3. CI 不跑测试（本次修复）。

**动作**

- P2：`Notifier` Protocol（`send(title, body, attachments?)`），Telegram 只是实现；先支持通用 webhook，再加企微/Bark。
- P2：任务中心使用统一 `TaskView` schema，后端组装，前端不再各自推断状态。

### 4.10 前端

**现状**

- Vue 3 + Element Plus + Vite；30 个页面，`App.vue` 负责登录、导航、主题、请求函数。
- 请求函数是 `App.vue` 里的一个闭包，通过 `api` prop 传给 20+ 页面；没有 `src/api/`。
- 页面大量单行压缩模板；`Inventory.vue` 940 行；只有 11 个纯 TS 测试，没有组件测试。

**问题**

1. 没有请求层：超时、取消、错误码、401、CSRF、并发去重散在一个函数里；页面无法单独测试。
2. 页面 props 全是 `any`，接口字段改名只能靠运行时发现。
3. KeepAlive + 页面内轮询，缺统一的「可见时刷新/离开取消」约定（部分页面用 `useVisiblePolling`，部分自己 `setInterval`）。
4. 大页面不可组合，改一个治理按钮要读 940 行。

**动作**

- P1：新建 `src/api/client.ts`（fetch + timeout + AbortController + `ApiError` + 401/CSRF），`App.vue` 只创建一次并注入；保持现有 `api(path, method, body)` 签名，先不改 178 个调用点。
- P1：把 `Inventory.vue` 拆组件；至少把「治理计划」「残留清理」独立。
- P2：为 `taskBuckets` / `inventorySeason` 这类纯函数补测试（已有基础），再加 3-5 个关键组件测试（登录、任务表、治理确认）。
- P2：引入 Prettier/ESLint（先只检查改动文件），停止继续写单行压缩模板。


---

## 5. 债务清单（按「先止血」排序）

### P0：本次已处理

| 项 | 证据 | 处理 |
|---|---|---|
| 分享撤销状态阻止重新入库 | `share_service.generate` 查 `share_revocations` 后跳过 | 删表、删检查、启动 DROP |
| `internal_delete` 空壳仍在 15 个模块调用 | `internal_delete.py` 只剩 no-op | 删模块、删调用、删 `drop_internal/recently_deleted` |
| 死代码 | `recover_share_batch.py`、`sync_worker.py` 无调用方 | 删除 |
| 热路径全表扫描 | `playbacks.relative_path`、`share_strms.strm_path` 无索引 | 加 8 个索引 |
| 前端内联请求 | 请求闭包写在 `App.vue`，页面无法单测 | 抽到 `src/api/client.ts`，保留兼容签名 |
| 打 tag 不跑测试 | 只有 docker-publish | CI 先跑后端全量 + 前端测试/构建，再发布 |

### P1：下一个迭代（1–2 周）

1. 拆 `organizer.py`：先搬纯函数（识别、命名、画质比较）到 `organize/`，保留 `Organizer` 作为门面。
2. 拆 `share_service.py`：生成 / 整理 / 检查 / 清理四个模块，`ShareEngine` 只做编排。
3. 建 `src/api/client.ts`，统一请求、超时、401、CSRF、错误 DTO。
4. 建 `migrations/`：用 `schema_version` + 顺序 SQL 文件替代「启动时 ALTER TABLE 猜列」；每个迁移有 downgrade 说明。
5. 把 `except Exception` 收口到领域异常；至少覆盖 organizer/share/sync 的写路径。
6. 任务中心统一 `TaskView` DTO，前端不再各自拼状态。

### P2：能力补强（按用户价值）

1. `MediaServer` 适配器 → Jellyfin（Emby 先包一层）。
2. `CloudDrive` 适配器 → 123 / Alist（先只读 + STRM，不做转存）。
3. `Notifier` 适配器 → 通用 webhook / 企微 / Bark。
4. 只读 OpenAPI token（status/tasks/inventory）。
5. 内置备份/恢复（SQLite `VACUUM INTO` + 配置导出）。
6. 字幕搜索/缓存（先复用现有分享附件与 MediaWarp 思路，不引入新供应商）。

### P3：明确不做（除非用户明确要求）

- RSS/站点索引/下载器全家桶：StrimKeep 的定位是「115 → STRM → Emby 治理」，不是 MoviePilot 的下载自动化。
- 多用户/RBAC、SSH 终端、任意 Python 插件执行、AI 自动改文件：安全和状态成本远高于收益。
- 微服务/Redis/消息队列：单进程 SQLite 在当前规模足够；先把 SQL 和状态收口。

---

## 6. 目标架构（轻量版，不照搬 MoviePilot）

```
strimkeep/
  domain/            # 纯逻辑：命名、识别、画质、治理规则（无 IO）
  ports.py           # Protocol: CloudDrive / MediaServer / Notifier / ResourceProvider
  adapters/
    cloud_115.py     # 现有 cloud115 的包装
    emby.py          # 现有 emby 的包装
    notifier_telegram.py
  app/
    organize/        # identify / plan / execute / publish
    share/           # generate / organize / check / cleanup
    inventory/       # snapshot / preview / execute
  store/
    schema.sql       # 当前 schema
    migrations/      # 001_xxx.sql, 002_xxx.sql
  api/               # 按领域拆路由
  workers/           # organizer worker / scheduler / media info
```

原则：

- **一个外部系统一个 Protocol + 一个 adapter**；业务层不 import 具体实现。
- **事件用现有 `events` 表 + 进程内订阅**，不引入多线程 EventManager；需要跨进程时再加 outbox。
- **先拆纯函数，再拆有状态类**；每一步都有测试，不做「大爆炸重构」。
- **删除优先于新增**：每加一个开关/状态，先问「能不能直接删行/重算」。

---

## 7. 建议路线图

| 阶段 | 目标 | 验收 |
|---|---|---|
| 0（本次） | 清死状态、补索引、CI 门禁 | 全量测试通过；`EXPLAIN` 走索引；tag 发布前必过测试 |
| 1（1–2 周） | 拆 organizer/share 纯函数；前端 API 层；迁移目录 | 行为不变；新增代码不再直接写 SQL；前端构建/测试通过 |
| 2（2–4 周） | MediaServer/Notifier 适配器；只读 OpenAPI | 新增 Jellyfin 或 webhook 不改业务层 |
| 3（按需） | 多网盘/字幕/音乐/插件 | 每个能力独立 adapter + 独立测试，不污染核心 |

---

## 8. 这次审计的直接产出

- `internal_delete`、`recover_share_batch`、`sync_worker` 已删除；
- `purge_strm` 不再依赖任何「删除登记」，只做本地文件与 sidecar 清理；
- 新增 `playbacks(relative_path/file_id)`、`share_strms(strm_path)`、`organize_items(state/job_id,state)`、`notifications(job_id,event_key)`、`deep_delete_queue(batch,state)`、`organized_files(relative_path)` 索引；
- 前端新增 `src/api/client.ts`，`App.vue` 不再内联请求闭包，保留 `api(path, method, body)` 兼容签名；
- CI 改为「后端全量 + 前端测试/构建 → 才允许发布」；
- 这篇文档作为后续重构的基线：每次改架构，先更新矩阵和债务表，不再靠记忆。

