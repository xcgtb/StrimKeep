# StrimKeep

**一套面板，管理两套 STRM 影视库。** 面向 115 网盘 + Emby 的影视整理与库存治理工具：
转存 / 离线 / 频道监控 → TMDB 识别与整理洗版 → 生成 STRM → Emby 收录 →
本地库与分享库逐季逐集对比，找出重复版本、残缺剧集与缺集，先出计划、确认后再执行清理。

- 后端：Python 3.12 + FastAPI + SQLite（单进程，无需外部数据库）
- 前端：Vue 3 + Element Plus + Vite（已内置编译产物，运行不需要 Node）
- 部署：单个 Docker 镜像 + docker compose

---

## 功能

### 资源入库
- **115 转存**：粘贴分享链接（支持 `?password=` 提取码），批量转存到指定目录
- **离线下载**：磁力 / ED2K / 直链提交，跟踪进度与产物
- **频道监控**：Telegram 公开频道预览页，或私密频道（用户会话，只读消息）；规则过滤后按关键词分流落盘
- **落盘后处理**：转存 / 离线 / 频道三类来源各自决定是否立即整理，默认只落盘

### 影视整理
- 扫描网盘目录 → TMDB 匹配 → 生成规范的 `剧名 (年份) {tmdb-id}/Season N/…` 目录与文件名
- 洗版与多版本：按 7 维画质规则（分辨率 / 编码 / HDR / 帧率 / 位深 / 音轨 / 声道）选优，平局按设置
- 字幕、NFO、海报等附属文件随片落盘；可选把媒体信息写入 Emby
- 整理任务并发与限流、暂停恢复、失败重试、事件留痕

### STRM 与 Emby
- 生成 302 播放地址形式的 STRM（`/play115/<pickcode>/<file_id>/…`、`/play115/share/<id>/…`），
  **身份写在文件里**，改名、换库、重排都不需要同步登记表
- STRM 同步与失效清理：逐条核对网盘文件仍存在才删，附三层保护
- Emby 反代、媒体信息注入、播放进度与最近添加聚合
- 片库映射：按 Emby 媒体库分类，7 种状态（完整 / 缺集 / 超集 / 在更 / 待对照 / 已入库 / 待核实）

### 双库治理
- **以目录为事实**：库存来自实际 STRM 扫描，台账只做来源索引——登记表缺行不会让作品从片库里消失
- 逐季逐集对比本地库与分享库：共同集比画质、集数优先、残次品（前序缺失 / 中间断层）逐侧清理
- 多季保护（分享合集 / 整剧零和 / 部分重叠）、决策模型（按画质 / 保留本地 / 保留分享）、特别篇 S00 独立策略
- 库内多版本去重（同季同集多份留最优），与跨库结论互不重复
- 白名单、入库静默期、合并集人工核实等保护
- **计划即快照**：生成计划时记录每个文件的 SHA-256 / 大小 / mtime / inode 与来源指纹，
  执行前逐作品核对、逐文件核验，任何变化立即停止；网盘侧删除必须有可靠来源登记

### 运维
- 任务中心（分类统计、明细、历史）、事件日志、`/metrics` 指标
- Telegram 通知、告警与限流说明见 `docs/operations/`
- 备份与恢复：`data/`（含 `state.db` 与 `master.key`）与 `strm/` 必须成对备份

---

## 快速开始

### 方式一：拉取镜像（推荐，不需要源码）

```bash
mkdir strimkeep && cd strimkeep
curl -fsSLO https://raw.githubusercontent.com/xcgtb/StrimKeep/main/docker-compose.install.yml
curl -fsSL  https://raw.githubusercontent.com/xcgtb/StrimKeep/main/.env.example -o .env
# 改 .env：至少 ADMIN_PASSWORD、STRIMKEEP_DATA_DIR、STRIMKEEP_STRM_DIR
docker compose -f docker-compose.install.yml up -d
```

升级：`docker compose -f docker-compose.install.yml pull && docker compose -f docker-compose.install.yml up -d`
固定版本：`.env` 里写 `STRIMKEEP_VERSION=0.9.150`（默认 `latest`）。

> 镜像在 GHCR：`ghcr.io/xcgtb/strimkeep`。公开仓库的镜像拉取不需要登录；
> 如果你的仓库是私有的，先 `docker login ghcr.io -u <用户名>`。

### 方式二：源码构建

```bash
git clone https://github.com/xcgtb/StrimKeep.git
cd StrimKeep
cp .env.example .env        # 改管理员口令与目录
docker compose up -d --build
```

### 打开面板

`http://<主机>:9000`，用 `ADMIN_USERNAME` / `ADMIN_PASSWORD` 登录。
健康检查：`curl -s http://127.0.0.1:9000/health/ready`。
可选一并托管 Emby：`docker compose -f docker-compose.install.yml --profile emby up -d`。

### 主要环境变量

| 变量 | 说明 |
|---|---|
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | 面板账号，口令至少 12 位 |
| `STRIMKEEP_DATA_DIR` | 数据目录（`state.db`、`master.key`、日志），**必须持久化并备份** |
| `STRIMKEEP_STRM_DIR` | STRM 输出目录，同时挂给 Emby |
| `STRIMKEEP_METRICS_TOKEN` | `/metrics` 访问令牌，留空则不校验 |
| `EMBY_*` | 可选：把 Emby 一并交给 compose，或注入媒体信息到 Emby 库 |

其余 115 账号、整理规则、治理规则都在面板里配置，保存后写入 `settings` 表（敏感字段加密）。

---

## 使用流程

1. **接账号**：115 扫码 / 账号登录；需要私密频道再登录 Telegram 用户会话
2. **资源入库**：转存 / 离线 / 频道监控，按规则落盘
3. **影视整理**：扫描 → 预览识别结果 → 执行整理（可先 dry-run）
4. **STRM 与 Emby**：生成 STRM → Emby 收录 → 反代与媒体信息
5. **片库映射**：看两库状态、缺集对照、分类与海报墙
6. **双库治理**：扫描 → 核对计划里的每个文件 → 执行清理（可分批）

---

## 目录结构

```
backend/strimkeep/   后端模块：转存、离线、频道、整理、STRM、库存、治理、Emby、通知
backend/tests/       后端测试（pytest）
frontend/src/        前端源码（Vue 3 + TS）
docs/operations/     运维文档：备份恢复、指标、告警
scripts/             构建、部署、校验、运维脚本
docker-compose.yml          源码构建部署
docker-compose.install.yml  拉取镜像安装（配合 .env）
```

关键设计约束（改动前请先读）：

- **STRM 身份写在文件里**，台账（`organized_files` / `share_strms` / `output_registry`）
  只做来源索引与业务状态，不决定「库里有什么」
- **删除必须有证据**：计划带快照指纹，执行前逐作品 + 逐文件核对，失败即停
- 库存投影来自目录扫描，单个文件解析不了只影响它自己，不阻塞整页

---

## 开发与测试

```bash
# 后端测试（在容器里跑，避免污染宿主环境）
sh scripts/test_in_container.sh                 # 默认目标集
sh scripts/test_in_container.sh "" tests        # 全部
```

- 依赖锁定：`backend/requirements.lock`、`frontend/package-lock.json`
- 前端构建：`cd frontend && npm ci && npm run build`（产物会打进镜像）
- 版本号：`backend/strimkeep/__init__.py`、`frontend/package.json`、`docker-compose.yml` 需同步

---

## 说明

- 本项目只做管理与整理，**不提供任何影视资源**；请只处理你合法拥有的内容
- 所有删除操作都要求你先生成计划并逐个核对，删除后的网盘文件通过 115 回收站 / 原分享恢复
- 许可证：MIT，见 `LICENSE`
