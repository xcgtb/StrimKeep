# 项目架构目录树

一份「谁在哪、谁进版本库、谁只存在于运行时」的地图。发 GitHub 前照这张图核对。

## 一、顶层：源码 vs 运行时数据

```
StrimKeep/                          ← 仓库根（源码）
│
├─ backend/                         ✅ 源码：Python 后端
│   ├─ strimkeep/                     ✅ 业务包（见第二节分层）
│   └─ tests/                         ✅ pytest 测试
├─ frontend/                        ✅ 源码：Vue 前端
│   ├─ src/                           ✅ 页面与逻辑
│   └─ dist/                          ❌ 构建产物（gitignore）
├─ scripts/                         ✅ 运维/工具脚本（见第三节）
├─ tools/                           ✅ 开发工具（见第三节，含一个杂物抽屉）
├─ docs/                            ✅ 文档（archive/operations/quality/releases）
├─ local-patches/                   ⚠️ 部署补丁（backup-db.sh + README）
├─ docker-compose.yml / Dockerfile  ✅ 部署定义
├─ README.md  .env.example          ✅ 说明 + 配置模板
├─ .gitignore  .dockerignore        ✅ 排除规则
│
├─ .env                             ❌ 真密钥（gitignore，绝不进库）
├─ data/                            ❌ 运行时：sqlite 库 + config（gitignore）
├─ emby/                            ❌ 运行时：Emby 数据（gitignore）
└─ strm/                            ❌ 运行时：生成的 STRM 输出（gitignore）
```

> `data/ emby/ strm/ dist/ .env` 是**运行时数据**，物理上跟源码同目录，但都被
> `.gitignore` 排除，`git ls-files` 也确认它们没被跟踪。它们由 `docker-compose.yml`
> 挂载，**移动路径会破坏正在运行的部署**，所以整理时不碰。

## 二、backend/strimkeep 模块分层

### 数据与基础（无环的底层）
| 模块 | 职责 |
| --- | --- |
| `store.py` | sqlite 封装：事件、任务、台账、schema 版本门（0.8.21 起 `Store.db` 是**一线程一连接**的属性） |
| `metrics.py` | Prometheus 文本指标 `GET /metrics`（0.8.22，默认关闭，见 `docs/operations/METRICS.md`） |
| `library_roots.py` | 本地库根的三方对照（配置/台账/磁盘）、可回滚的库根改名与启动自检（0.8.30，见 `docs/operations/LIBRARY_PATHS.md`） |
| `core.py` | 全局唯一的状态异常（`Stopped`/`Discarded`/`UncertainState`/`NeedsReview`）+ `dumps` + `atomic_write` |
| `fsutil.py` / `fsops.py` | 文件安全写、越界/符号链接防护、边车文件 |
| `media.py` / `audio.py` | 媒体身份识别、命名、季集/分段解析 |
| `organize_options.py` / `strm_options.py` / `library_name.py` | 命名与规则配置 |

### 云与网络
| 模块 | 职责 |
| --- | --- |
| `cloud115.py` | 115 网盘 SDK 封装、限流、回收站 |
| `login_clients.py` | 登录态/客户端管理 |
| `network.py` / `emby_connection.py` | 网络层、Emby 连接 |

### 业务服务（协作模块，由 organizer 编排）
| 模块 | 职责 |
| --- | --- |
| `organizer.py` | 中枢：任务调度、发布、锁、审计 |
| `sync.py` + `sync_worker.py` + `sync_hash_worker.py` | STRM 同步 + 巡检 + 哈希子进程 |
| `transfer.py` | 分享转存 |
| `upgrade.py` / `quality.py` / `recycle.py` | 洗版、画质、回收 |
| `archive.py` / `cleanup.py` / `untyped_cleanup.py` / `outputs.py` | 归档、清理、输出管理 |
| `deep_delete.py` | Emby Webhook 深度删除 |
| `native_media_info.py` / `cover_service.py` / `probe.py` / `playaddr.py` | 媒体信息、封面、探测、播放地址 |
| `notifications.py` / `telegram_bot.py` / `message_templates.py` | 通知、TG 机器人 |
| `channel_monitor.py` / `resource_discovery.py` / `tasks.py` | 频道监控、资源发现、任务入队 |
| `emby.py` / `metadata_files.py` | Emby 交付、元数据边车 |

### HTTP 接口层
| 模块 | 职责 |
| --- | --- |
| `main.py` | FastAPI 应用组装 + `job_public` 等公共序列化 |
| `console_api.py` / `channels_api.py` / `explore_api.py` / `offline_api.py` / `phase5_api.py` / `share_api.py` / `transfer_api.py` | 各域 REST 路由 |
| `cards.py` | 卡片/面板数据 |

## 三、scripts / tools / docs

| 目录 | 内容 | 分类 |
| --- | --- | --- |
| `scripts/verify.sh` `deploy.sh` `test_in_container.sh` `fault_pytest.py` | 门禁、部署、容器测试、挂死定位 | ✅ 核心运维 |
| `scripts/usage_audit.py` `partial_reasons.py` `review_reasons.py` `gate_coverage.py` | 只读诊断/覆盖检查 | ✅ 工具 |
| `scripts/check_secrets.py` `pre-commit` `set_password.py` | 密钥扫描、提交钩子、改密 | ✅ 安全 |
| `scripts/version.py` `move_deepdelete_records.py` | 版本号、一次性搬移 | ⚠️ 一次性 |
| `tools/` 顶层（`import_graph.py` `except_audit.py` `health_report.py` 等） | 依赖图/审计/健康报告 | ✅ 工具 |
| `tools/acc/` | **杂物抽屉**：大量一次性脚本 + `*.orig.json` 备份 + `run-*.log` + `__pycache__` | ⚠️ 需清理 |
| `docs/` | archive（历史）/operations（操作）/quality（质量）/releases（版本） | ✅ 文档 |
| `docs/operations/METRICS.md` | 指标口径、`/metrics` 开启方式、建议告警与抓取配置（0.8.22） | ✅ 运维 |

## 四、已确认的结构问题（整理清单）

| 问题 | 处理 | 风险 |
| --- | --- | --- |
| `backend/strimkeep/test_sync_stale.py` 测试文件误放源码包里 | 移到 `backend/tests/` | 零（无 import） |
| `tools/acc/` 里混了 `.orig.json`、`run-*.log`、`__pycache__` | 删备份/日志/缓存 | 零（非运行依赖） |
| `tools/acc/` 一次性脚本（decbak/readbak/reverttry/revert_one/xfer_crash/trash_dump 等） | 归类到 `tools/acc/oneoff/` 或删 | 低（确认无引用后） |
| `local-patches/` | 保留（backup-db.sh 是部署辅助） | — |
