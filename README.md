# 🎬 StrimKeep 3.0.1

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.12-3776AB?style=for-the-badge&logo=python" alt="Python">
  <img src="https://img.shields.io/badge/FastAPI-Ready-009688?style=for-the-badge&logo=fastapi" alt="FastAPI">
  <img src="https://img.shields.io/badge/Vue-3-42b883?style=for-the-badge&logo=vue.js" alt="Vue 3">
  <img src="https://img.shields.io/badge/Docker-Ready-2496ED?style=for-the-badge&logo=docker" alt="Docker">
  <img src="https://img.shields.io/badge/License-MIT-green?style=for-the-badge" alt="License">
</p>

<p align="center">
  <strong>面向 NAS 的 STRM 双库影视管理与治理面板：115 转存 / 离线 / 频道监控 → TMDB 识别洗版 → 生成 STRM → Emby 收录 → 片库映射与双库治理</strong>
</p>

---

## 🌟 项目特色

### ✨ 核心功能

- 🔗 **115 分享转存** - 粘贴分享链接（支持 `?password=` 提取码），批量转存到指定目录
- ⚡ **离线下载** - 磁力 / ED2K / 直链提交，跟踪进度与产物
- 📡 **Telegram 频道监控** - 公开频道预览页或私密频道（用户会话，只读消息），关键词过滤后分流落盘
- 🎬 **影视整理洗版** - TMDB 识别 → 规范目录 `剧名 (年份) {tmdb-id}/Season N/`，7 维画质比较选优，字幕/NFO/海报随片落盘
- 🔗 **STRM 生成与同步** - 输出 302 播放地址 STRM，**身份写在文件里**，改名换库都不需要同步登记表
- 🧹 **失效清理** - 逐条核对网盘文件仍存在才删，三层保护，附事件留痕
- 🖼️ **片库映射** - 按 Emby 媒体库分类，7 种状态（完整 / 缺集 / 超集 / 在更 / 待对照 / 已入库 / 待核实），缺集对照与海报墙
- 🧭 **影视探索** - TMDB 搜索与详情、演员/相关推荐，看中的资源可以直接转存入库
- 🔔 **追更订阅与入库通知** - 分享监控与频道订阅跟踪更新，入库/失败通过 Telegram 通知
- ⚖️ **双库治理** - 本地库 vs 分享库逐季逐集比画质与集数，残次品（前序缺失 / 中间断层）逐侧清理，多季保护、库内多版本去重
- 🛡️ **计划即快照** - 生成计划时记录每个文件的 SHA-256 / 大小 / mtime / inode 与来源指纹，执行前逐作品核对、逐文件核验，任何变化立即停止
- 📊 **运维配套** - 任务中心、事件日志、`/metrics` 指标、Telegram 通知

### 🏗️ 技术架构

- **后端**：Python 3.12 + FastAPI + SQLite（单进程，无外部数据库）
- **前端**：Vue 3 + Element Plus + Vite（镜像内已含构建产物，运行不需要 Node）
- **部署**：单镜像 + Docker Compose
- **集成**：115 网盘 API、Emby、Telegram（Bot 与用户会话）、TMDB

---

## 🐳 Docker 快速部署（推荐）

### 📋 系统要求

准备一台可以运行 Docker 的 NAS 或 Linux 设备，安装 Docker 20.10+ 和 Docker Compose 2.x。
需要 115 网盘与 TMDB 的网络可达；用到 Telegram 通知或频道监控时，容器还要能访问 Telegram API。

### 1. 准备目录

```bash
mkdir -p /vol1/1000/strimkeep/data /vol1/1000/strimkeep/strm
cd /vol1/1000/strimkeep
```

### 2. 创建 `docker-compose.yml`

```yaml
services:
  strimkeep:
    image: ghcr.io/xcgtb/strimkeep:latest
    container_name: strimkeep
    network_mode: host  # 推荐 host 模式：面板 9000、Emby 回调、播放地址都能直接访问
    environment:
      # --- 基础配置 ---
      - TZ=Asia/Shanghai
      # 必填：面板登录密码（至少 12 位）。把等号后面填上你的密码，不要留空
      - ADMIN_PASSWORD=
      # 面板登录用户名，一般不用改
      - ADMIN_USERNAME=admin
      # 可选：/metrics 访问令牌，留空 = 不校验
      - STRIMKEEP_METRICS_TOKEN=
    volumes:
      # 数据库与日志持久化，右侧固定为 /data
      # 左侧换成你自己的目录（飞牛一般是 /vol1/1000/...）
      - /vol1/1000/strimkeep/data:/data
      # STRM 输出目录：右侧固定为 /strm，左侧换成你的目录
      # Emby 要挂载同一份（详见下方可选的 Emby 段落）
      - /vol1/1000/strimkeep/strm:/strm
    restart: always
    # 停止时给整理任务 30 秒收尾，避免留下半截文件
    stop_grace_period: 30s
    # 健康检查：面板 /health/ready 返回 200 才算健康
    healthcheck:
      test: ["CMD-SHELL", "python -c \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:9000/health/ready', timeout=3)\""]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 40s
    # 日志轮转：最多保留 3 个 10MB 文件
    logging:
      driver: json-file
      options:
        max-file: "3"
        max-size: 10m

  # --- 可选：连 Emby 一起装 ---
  # 用不到就把本段整段删掉；要用就在启动命令后加 --profile emby
  emby_strimkeep:
    profiles: ["emby"]
    container_name: emby_strimkeep
    image: emby/embyserver:latest
    environment:
      - TZ=Asia/Shanghai
      - UID=0
      - GID=0
    ports:
      - "8096:8096"  # Emby 网页端口
    volumes:
      # Emby 配置与缓存
      - /vol1/1000/strimkeep/emby:/config
      # 和上面的 STRM 目录保持一致（左侧必须相同）
      - /vol1/1000/strimkeep/strm:/media
    restart: always
```

### 3. 改这几处

| 配置 | 填写说明 |
|---|---|
| `ADMIN_PASSWORD=` | **必填**：等号后面填自己的面板登录密码，至少 12 位（留空时容器启动即退出，日志会提示） |
| `/vol1/1000/strimkeep/data:/data` | 左侧换成你的**数据目录**，右侧保持 `/data`；里面是 `state.db`、`master.key` 与日志，更新和迁移时保留该目录 |
| `/vol1/1000/strimkeep/strm:/strm` | 左侧换成你的 **STRM 输出目录**，右侧保持 `/strm`；Emby 要挂载同一份输出内容 |
| `ADMIN_USERNAME=admin` | 可选：登录用户名，一般不用改 |
| `STRIMKEEP_METRICS_TOKEN=` | 可选：采集 `/metrics` 时填令牌，留空不校验 |
| `image: …:latest` | 可选：想固定版本就写 `:3.0.1` |

### 4. 启动

```bash
docker compose up -d
```

浏览器打开 `http://NAS_IP:9000`，用 `admin` + 你设置的密码登录。
首次进入后在面板里登录 115 账号，再配整理与治理规则，**环境变量只用来启动服务**。
需要连 Emby：`docker compose --profile emby up -d`。
需要连 Emby：`docker compose --profile emby up -d`。

### 📁 数据持久化

| 目录 | 内容 | 说明 |
|---|---|---|
| `/vol1/1000/strimkeep/data` | `state.db`、`master.key`、日志 | 库状态与凭据密文，**丢了无法恢复** |
| `/vol1/1000/strimkeep/strm` | 生成的 STRM 媒体树 | 同时挂给 Emby |

备份（两者必须成对）：
```bash
tar -czf strimkeep-backup-$(date +%Y%m%d).tar.gz \
  /vol1/1000/strimkeep/data /vol1/1000/strimkeep/strm
```

---

## 💻 使用指南

### 🎯 首次配置

1. 打开面板 → **设置 → 115 网盘** → 扫码或账号登录
2. **整理设置**：确认归档目录、命名规则、画质规则（默认已可用）
3. 需要 Emby 时：填 Emby 地址与 API Key（可选反代）

### 📥 资源入库

- **转存**：粘贴分享链接 → 选择目标目录 → 转存
- **离线**：磁力 / ED2K / 直链 → 提交 → 任务中心看进度
- **频道监控**：添加频道 + 关键词规则，支持「公开预览页」或「用户会话」
- 三类来源各自决定「落盘后是否立即整理」，默认只落盘

### 🎬 影视整理

扫描目录 → 预览识别结果（TMDB 匹配、重命名计划）→ 执行整理。
洗版规则可调分辨率/编码/HDR 等 7 个维度的优先级，平局策略可配。

### 🔗 STRM 与 Emby

生成 STRM 后 Emby 扫描即可收录；播放走 302 跳转，不占本地空间。
同步与失效清理在「STRM 同步」页，删除前会逐条确认文件仍在网盘上。

### 🖼️ 片库映射

按 Emby 媒体库分类展示两库作品，状态直接标出缺集/超集/在更；点开可看逐季逐集对照。

### ⚖️ 双库治理

1. **扫描双库** → 页面列出可清理项（分享择优删本地 / 本地更全删分享 / 残次品 / 库内多版本）
2. 勾选要处理的项 → **生成计划**（此时记录每个文件的指纹）
3. 核对计划里的文件清单 → **执行**；执行中任何文件变化都会立即停止


---

## 🐛 故障排除

### 打不开面板
```bash
docker compose ps                            # 容器在不在
curl -s http://127.0.0.1:9000/health/ready   # 应用健康
docker compose logs -f                       # 看日志
```
- 容器启动即退出、日志写「首次启动请设置 ADMIN_USERNAME 和至少 12 位的 ADMIN_PASSWORD」：把 yml 里的 `- ADMIN_PASSWORD=` 等号后面补上密码
- `9000` 被占用：把 `network_mode: host` 改成 `ports: ["9100:9000"]`

### 提示「网盘源文件未登记，无法删除」
说明本地有 STRM、但台账查不到它对应的 115 文件。分享侧可以正常清理，
网盘侧需要先补齐登记（重新整理该作品或走「STRM 同步」），系统不会猜着删。

### 治理页出现「静默期」
作品刚入库 15 分钟内不进治理清单，卡片上会写「还剩 N 分钟」，时间到自动进。

---

## 🚀 版本更新

```bash
docker compose pull
docker compose up -d
```

固定版本：把 yml 里 `image:` 改成 `ghcr.io/xcgtb/strimkeep:3.0.1`（可用 tag 见 Releases）。
回滚：版本号改回上一个，再 `pull && up -d`；数据库向下兼容上一版即可直接回退。

---

## 📄 目录结构

```
backend/strimkeep/          后端模块：转存、离线、频道、整理、STRM、库存、治理、Emby、通知
backend/tests/              后端测试（pytest）
frontend/src/               前端源码（Vue 3 + TS）
docs/operations/            运维文档：备份恢复、指标、告警
scripts/                    构建、部署、校验、运维脚本
docker-compose.yml          源码构建部署（开发用）
```

关键约束（改代码前请先读）：STRM 身份写在文件里，台账只做来源索引；
删除必须有证据（计划快照 + 逐文件核验）；库存来自目录扫描，单个文件的问题不影响整页。

---

## 📌 声明

- 本项目只做管理与整理，**不提供任何影视资源**；请只处理你合法拥有的内容
- 删除操作可回滚：网盘文件进 115 回收站，分享侧可回到原分享
- 许可证：MIT，见 [LICENSE](LICENSE)

<p align="center">
  <strong>🎉 一套面板，把两套库管明白</strong>
</p>

<p align="center">
  如果这个项目对你有帮助，给个 ⭐ Star 支持一下
</p>

