# StrimKeep 架构

> 本文以源码为准。若与代码不符，改文档，不要改代码来迁就文档。
> 最后核对：v1.6.3 基线及频道链路修复（未发布）/ 2026-10-20

---

## 一、运行形态

单个 Docker 容器，FastAPI + uvicorn，端口 **8321**。

```
浏览器 ──HTTP──> uvicorn(单进程) ──> FastAPI 路由 ──> 云盘域 / 纯规则 ──> SQLite
                                          │
                                          ├──> Telegram Bot（同进程，长轮询）
                                          └──> 后台调度线程（30 秒一拍）
```

- 进程数：1（没有 gunicorn / 多 worker）
- 并发主力：**线程**，不是进程
- 业务数据主存储：SQLite（`/data/state/strimkeep.db`）
- 配置：`/data/state/config.json`（环境变量可覆盖，见 `app/config.py`）

---

## 二、请求链路（从点击到落库）

```
frontend/src/views/*.vue
   └─ frontend/src/api/{client,domain,media}.ts      ← 统一请求封装
        └─ app/routers/*.py                          ← 只做校验 + 鉴权 + 派发
             ├─ 轻任务：直接调用 app/cloud/* 或根目录业务模块
             └─ 重任务：app/cloud/jobs.enqueue() → 落库 → 返回 job_id
                  └─ jobs._loop() 取出 → jobs.execute(job_id)
                       └─ jobs.step(job_id, key, payload, fn)  ← 每一步单独记状态
                            └─ app/cloud/store.py → SQLite
```

**两条硬规则：**

1. **路由不写业务。** `app/routers/` 里只允许：参数校验、`deps.auth` 鉴权、`deps.spawn` 或 `jobs.enqueue` 派发。
2. **写操作必须有 job。** 任何会改远端或改文件的操作，都要走 `jobs`，否则没有停止点和回执。

---

## 三、并发模型：三层，各管一件事

| 层 | 载体 | 职责 | 状态存在哪 |
|---|---|---|---|
| **HTTP** | uvicorn 主线程池 | 收请求、立刻返回 | 无 |
| **执行槽** | `app/tasks.py` 的 `ExecSlot` / `TaskManager` | 重任务**互斥**、取消信号、日志流 | 内存（`MAX_KEEP=200`，完成态 `FINISHED_TTL=3600`） |
| **业务任务** | `app/cloud/jobs.py` + `cloud_jobs` 表 | 排队、步骤、进度、停止、回执 | **SQLite** |
| **定时** | `app/scheduler.py` | 入库检查 / TMDB 预热 / 追更 / 巡检 / 晨报 | `kv` + `sub_state` |

### ⚠️ 两个"任务"不是一回事

这是全项目最容易混淆的地方，改代码前先分清：

| | **执行槽 ExecSlot**（`app/tasks.py`） | **业务任务 Job**（`cloud_jobs` + `app/cloud/states.py`） |
|---|---|---|
| 状态词 | `running` / `success` / `error` / `cancelled`（`EXEC_STATUSES`） | `queued` / `running` / `success` / `stopped` / `failed` / `uncertain` / `needs_review`（`JOB_STATES`） |
| 生命周期 | 进程内，1 小时后淘汰 | 永久，重启还在 |
| 回答什么问题 | "现在有没有人在跑重活？" | "这次转存做到哪一步了？" |
| 前端该展示谁 | 只在排障页 | **业务页面一律用它** |

**约定：`cloud_jobs` 是业务状态的唯一来源。** `tasks.Task` 只是执行许可。

`app/cloud/jobs.py` 的注释就是纪律本身：

> `"""Durable queue backed by the existing task manager; never replay uncertain writes."""`

**`uncertain` 绝不重放。** 写操作结果未知时，宁可停在 `needs_review` 让人看，也不要自动重试。

---

## 四、任务状态机

```
enqueue
   │
   ▼
 queued ──request_stop──> stopped
   │
   ▼ (jobs.execute)
 running ──checkpoint()──> stopped（安全边界停止，不硬断）
   │
   ├── 成功 + 无遗留  ──> success
   ├── 有错误项       ──> needs_review
   ├── 写结果未知     ──> uncertain   ← 永不自动重放
   └── 抛异常         ──> failed
```

配套表：

| 表 | 作用 | 关键约束 |
|---|---|---|
| `cloud_jobs` | 任务主体 | `id` 主键，`state` 文本 |
| `cloud_steps` | 步骤级状态 | `(job_id, step_key)` 主键 |
| `cloud_job_due` | 延迟 / 子任务 | `UNIQUE(origin_job, file_id)` ← 幂等钉在这 |
| `cloud_job_controls` | 停止请求 | `stop_requested` |
| `cloud_progress` | 进度 | 每 job 一行 |

停止不是杀线程：`request_stop()` 置标志 → 执行侧在 `checkpoint()` 处抛出 `JobStopped` → 停在**安全边界**。

---

## 五、存储三平面

| 平面 | 内容 | 位置 | 谁写 |
|---|---|---|---|
| **配置** | 账号、路径、阈值、规则开关 | `config.json`（+ `cloud_service_settings` 等表） | `app/config.py`、`app/cloud/settings.py` |
| **业务状态** | 任务、步骤、资产、入库、删除、播放 | `strimkeep.db` | `app/cloud/store.py`、`app/storage.py` |
| **产物** | STRM 文件、海报缓存、清理存档 | 文件系统（`S_ROOT` / `L_ROOT` / `CLOUD_L_ROOT`） | `app/cloud/strm.py`、`app/lib.py` |

历史 JSON/JSONL 只在第一次迁移时读取，成功标记后不再参与主读或双写；未知额外列与已有数据保留。

状态文档的迁移标记、进程内修订与 SQLite 连接共用 `storage._LOCK` 可重入锁，允许在网盘事务内读取文档，避免文档锁与数据库锁逆序等待。异步 HTTP 入口（片库 SSE、Emby 回调、直连播放）通过工作线程执行同步存储调用，数据库等待不占用 Web 事件循环。

### 删除边界（重要）

- **本地库**：删 NAS STRM 与附属文件，并经 CD2 清理对应 115 源
- **分享库**：只清 NAS，不动云端
- 逐季策略只处理选中季；主目录仅在为空时清理
- 目录清理跳过含 STRM / 视频 / 未知文件 / 链接的目录
- 媒体清理不产生特殊备份 —— 所以 `cloud_deletions` 必须留回执

### 日志边界

实时日志约保留 10,000 条；日志页保留最近 500 行，每 2 秒增量刷新，切页或隐藏暂停。治理清理的结构化存档不随实时日志回收。顶部运行状态每 15 秒读进程内状态，不扫描媒体、不探测远端。

---

## 六、目录职责

### 后端

| 路径 | 职责 |
|---|---|
| `app/main.py` | 应用装配、生命周期、静态挂载、CD2 看门狗 |
| `app/routers/` | 8 个路由模块：`auth` `system` `governance` `library` `subscribe` `settings` `upstream` `cloud` |
| `app/routers/deps.py` | 鉴权 / 会话 Cookie / `spawn()` 派发 / 公共常量 |
| `app/cloud/` | 网盘域：账号、频道、转存、整理、STRM、发布、入库、删除、恢复 |
| `app/domain/` | **纯规则，零 IO**：`quality.py`（画质）、`governance.py`（治理判定） |
| `app/tasks.py` | 执行槽：互斥、取消、日志流 |
| `app/scheduler.py` | 定时编排（`TICK_SEC=30`，启动延迟 15 秒） |
| `app/cloud/jobs.py` | 任务编排：`enqueue` / `step` / `checkpoint` / `progress` / `execute` |
| `app/cloud/states.py` | **业务任务状态词表**：`JOB_STATES`、`JOB_STATE_LABELS`、`ACTIVE/TERMINAL_JOB_STATES` |
| `app/cloud/pacing.py` | **频道轮询节奏**（纯计算、无依赖）：`next_poll_delay`、`flood_wait_seconds`、`ChannelFloodWait` |
| `app/cloud/channel_reader.py` | 频道读取：Telegram 会话、增量游标、限流退避、积压排空 |
| `app/cloud/channel_network.py` | 频道公共代理路由、无敏感信息的连接诊断、只读检查并发上限 |
| `app/cloud/channel_recovery.py` | 加密历史暂存、修订核验、暂停规则读取位置预览与确认 |
| `app/domain/workflow_progress.py` | 根据持久步骤与入库回执汇总阶段证据，零 IO |
| `app/cloud/store.py` | 云端域全部表结构与访问函数 |
| `app/storage.py` / `state_store.py` | SQLite 事务、计划存档、缓存、旧 JSON 迁移 |
| `app/config.py` | 配置读写、环境变量覆盖 |
| `app/engine.py` / `compat.py` | **兼容导出层**（206 个旧名字），见第七节 |
| `app/morning.py` `governance.py` `subscribe.py` `wash.py` `bot.py` `ingest.py` `stats.py` `tmdb.py` `emby.py` `lib.py` `core.py` | 根目录业务模块：片库健康、治理执行、追更、洗版、Bot、入库、报表、外部服务 |

账号断开的未完成任务检查与删除在 `store.disconnect_account()` 的同一写事务内完成；同时移除账号服务凭证、频道及游标，未投递接收记录标记已处理并写明断开原因，任务历史保留。接收与开关互斥忽略历史孤儿账号；捕获消息在入库前再次核对账号。规则启停在写事务内重新核对账号修订和规则内容。取消未启动任务写入 `stopped`，历史 `cancelled` 仍可读取。

STRM 全量扫描由 `scheduler` 调用 `cloud/strm_scan.tick()`，从已保存的 `{id,name}` 目录对象提取来源，并在事务内按账号、目录复用排队/运行中的同步任务。Cron 与时区在配置保存时校验；无效历史排期不执行；首次开启、重新启用或修改排期时，可选择立即扫描一次或等待下一 Cron 时刻；关闭排期保留历史。`strm_scan_anchor/pending` 记录新排期起点与待启动意图，`last_run` 为调度水位，`last_enqueued` 单独记录实际投递。调度回写做配置快照比较和 SQLite CAS，避免覆盖新排期。STRM 页内展示这些状态，可保存 Cron、时区、启动方式、失效引用清理与可选 UA 拦截。直连播放拦截默认关闭，仅完整匹配指定 UA；UA 本身不能证明客户端身份。

`workflow.sync()` 在入队时冻结本轮是否清理失效 NAS 引用。连续两次完整扫描缺失、同一挂载身份及登记内容一致的清理保护保持不变；补生成结果与 `reconciliation.removed/deferred` 分开记录。自动及手动入口均明确清理范围，不删除 115 源文件。

分享版本决策由 `cloud/share_selection.py` 管理，复用无 IO 的质量解析/比较函数，但只读取 `share_quality_*` 配置和当前账号当前输出根的分享资产，不参与个人整理八维或双库治理。先按作品/集与 DV 分组选出本批优胜版本，再比较已有版本；预览记录旧资产令牌、配置快照和根目录身份。替换在 `share_library.run()` 的持久 `share_replace:*` 步骤内完成：同路径保持签名字节，CAS 切换分享来源并刷新附件；不同路径先生成并核验，再移除旧的精确自建内容，已修改附件保留。中断/未知步骤不自动重放，步骤、结果与旧引用均可核查。新来源始终使用分享只读适配器，不调用个人网盘写操作。

`emby_library.mapping_status()` 只读展示每个连接实际使用的 personal_root/share_root、NAS 根与映射示例，比较反代识别根以提示差异；不把本地路径推算当作远端入库成功。`workflow.transfer_from_saved()` 校验接收页引用的已保存归档目录并冻结目录修订，转存前及继续整理前重新核对；有已核实转存结果时保留在异常回执中。

云任务执行槽占用期间，工作线程每两秒继续执行只读调度与持久化排队（频道读取、接收派发、维护排期和自动规则），实际远端写入仍在当前独占任务结束后串行执行。Emby 入库检查沿用原独立轮询。
| `app/emby_events.py` `overview.py` `activity_stream.py` `runtime_logs.py` | 事件、总览、活动流、运行日志 |

### 前端

| 路径 | 职责 |
|---|---|
| `frontend/src/views/` | 页面：`DashboardView` `CloudSettingsView` `OrganizeView` `LibraryView` `GovernanceView` `ExploreView` … |
| `frontend/src/api/` | `client.ts`（请求 / 错误）、`domain.ts`、`media.ts` |
| `frontend/src/stores/` | Pinia：`cloud` `session` `subscriptions` `tasks` `ui` |
| `frontend/src/composables/` | `useRequest` `useOperation` `useVisiblePoll` `useActivityUpdates` `useLibraryUpdates` `useCloudSettingsDraft` `format` |
| `frontend/src/router/` `layouts/` `components/` | 路由、布局、组件 |
| `frontend/src/config/` `types/` `styles/` | 常量、类型、样式 |
| `static/` | **Vite 构建产物，不要手改** |

**构建约定**：`frontend/vite.config.ts` 里 `base='/static/'`、`build.outDir='../static'`。后端把 `/static` 挂到仓库根 `static/` 目录（`app/routers/deps.py` 的 `STATIC_DIR`）。

所以：**改前端 → `cd frontend && npm run build` → 产物落到 `static/` → 刷新页面。** 不存在 `static/js/*.js` 这种手写脚本。

---

## 七、兼容导出层（迁移中的结构）

`app/engine.py`（362 行）+ `app/compat.py`（88 行）把 **206 个旧名字**重新导出，让老测试与 CLI 继续用 `engine.xxx`。

这是绞杀者模式的垫片，**不是设计目标**。`dev/tests/test_architecture_boundaries.py` 锁住了这 206 个导出。

**纪律：**

- 新增功能**禁止**往 `engine` 加导出
- 每次功能 PR，导出数量应**净减少**；改动涉及的旧导出顺手删掉
- 删导出时同步删对应测试行

目标：让 `engine` 变成薄转发，最终消失。

---

## 八、已知边界

- 前端全局调用与 `engine` 共享可变缓存仍存在
- 任务协调只覆盖已注册重任务；轻量接口与部分后台缓存有各自刷新逻辑
- 大库 TMDB 对照仍有短期资源占用，缓存聚合后内存是否回落在真实 NAS 观察
- DOM 替身测试不代替手机浏览器视觉验收；挂载权限、115 延迟、反代需部署验证
- 单容器是刻意选择：不需要为 NAS 工具拆微服务

---

## 九、改代码前的三问

1. **这是规则还是流程？** 规则进 `app/domain/`（可单测、零 IO）；流程进 `app/cloud/`
2. **它会改远端或改文件吗？** 会 → 必须 `jobs.enqueue`，并给 `checkpoint()`
3. **它的幂等键是什么？** 写之前先定 `UNIQUE(...)`，不要靠"先查后插"

具体步骤见 `docs/ADDING_A_FEATURE.md`。


## 频道命令转发边界（v1.6.0）

- `app/domain/telegram_intake.py` 归一化命令、直接回复、说明与隐藏链接，校验命令头并按 UTF-16 计算转发长度；`provider_errors.py` 只输出白名单诊断；`media_labels.py` 只影响通知显示，不影响命名和质量比较。
- `receiving` 统一频道筛选、持久收件箱、内置分享 STRM/转存调度及接收历史；`channel_forwarding` 只拥有通过用户会话发送命令文本的流程，以现有 channel 任务及 telegram-forward 步骤保存回执。没有新增第二套业务状态或执行队列。
- `public_channel_reader` 负责有界公开网页读取；`channel_reader` 负责 MTProto 读取与会话；两者均在同一事务中提交消息和游标并核对账号/规则/游标修订。`cloud_public_channel_status` 保留最初补丁表名，兼容保存两类轮询读取的状态，不是任务业务状态。
- 外部消息写入只在 jobs 内发生；未知发送不会重放，观察模式无远端操作。接收历史按当前子任务只读补充失败，不重写父任务已提交的事实。未修改双库治理和版本替换策略。

### 频道检查与交互改进（未发布）

- `app/cloud/channel_inspection.py` 只读取得已保存规则及最近五条消息，复用现有公开预览/用户会话读取和筛选逻辑。它不捕获收件箱、不推进游标、不提交业务任务。Bot 模式明确说明只能接收实时更新；暂停与观察模式不会因检查被开启。
- `app/domain/telegram_intake.py` 补充纯计算的处理路由和资源类型可处理性，供测试结果解释使用。实际执行继续通过 `receiving`、`jobs` 与原步骤记录。
- `receiving.set_channel_enabled()` 暂停时只作账号内、配置版本匹配的本地更新；启用时仍复用原账号、目录及 Telegram 权限检查。暂停规则不撤销已发出的远端请求，也不自动停止已派发的后续子任务。
- Vue 频道页使用既有 `ModalDialog`、`useRequest`、`useOperation` 和 `useVisiblePoll`；配置草稿与自动刷新分离。检查和筛选结果直接展示，接收历史通过持久任务 ID 跳转任务中心。

### 盘内库存（1.6.2）

- `app/domain/inventory.py` 从完整目录快照推导作品目录、类型与已有季集，零 IO。S00、连续/多集文件保留原始证据；单独 E01/[01] 仅在明确季目录下归属，不默认 S01。
- `app/cloud/inventory.py` 复用 `workflow.walk()` 与适配器完整分页，扫描受现有 20,000 文件/目录上限约束，超限失败并要求缩小目录。TMDB 只读取明确类型与编号，按 `(kind,id)` 复用，单轮最多 200 个唯一编号。
- 新任务类型 `inventory` 复用 `cloud_jobs.state`、步骤、停止与进度。`cloud_inventory_items` 以 `(job_id,cid)` 保存目录记录；单事务核验账号修订/停止信号后写入，列表只选当前授权修订下、相同账号/扫描根的最近成功任务。写入后若任务失败或停止，仍不发布该批结果。账号断开时移除库存记录，保留原任务历史。
- `CloudInventory.vue` 嵌入文件管理，不新增侧栏层级。`useInventorySource.ts` 只在内存选择已连接账号及数字目录，后续预览/同步仍走原服务端校验和确认；不自动写入目录配置。库存计数是作品目录数，季集是文件名观察值，不承诺整剧完整或 Emby 已入库。

## 频道补读与日志事务（v1.6.3 后未发布）

- `channel_network` 为公开网页和 Telethon 统一决定代理路由；只读检查通过两个并发许可限制网络等待，不持有 SQLite 锁进行网络请求。
- `channel_recovery` 使用 `cloud_channel_backfill` 加密暂存有界历史区间，以账号、规则、原游标及暂存令牌核验并发；完整覆盖后由 reader 一次性提交收件箱与主游标。暂停规则重新定位必须先预览，再提交五分钟有效且修订匹配的确认令牌。
- `receiving` 新保存规则通过内部 pipeline_version 标记明确启用后续整理；执行前核对归档目录修订，再复用原转存持久任务。旧规则不自动激活。`pipeline.status` 只读返回已有账号目录，前端没有新增第二套处理配置。
- `workflow_progress` 仅汇总任务族持久步骤与 Emby 入库回执，缺少步骤证据时不宣称阶段成功；业务状态仍来自 cloud_jobs。
- `runtime_logs` 的缓冲锁和 Handler 锁共用 storage._LOCK，避免日志查询与事务内日志相互等待；事务内写日志复用活动连接，不提交外层事务。storage_status 在释放自身诊断锁后才记录日志。
- 频道消息加密可接收调用方连接，只查询账号是否存在，避免打开嵌套事务导致游标与收件箱提前提交。
