# 🎬 StrimKeep 3.0.4

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

## 🐳 Docker 部署

三步就够。

### 1. 建两个目录

```bash
mkdir -p /vol1/1000/strimkeep/data /vol1/1000/strimkeep/strm
```

### 2. 建 `docker-compose.yml`

```yaml
services:
  strimkeep:
    image: ghcr.io/xcgtb/strimkeep:latest
    container_name: strimkeep
    network_mode: host
    environment:
      - TZ=Asia/Shanghai
      - ADMIN_USERNAME=admin
      - ADMIN_PASSWORD=        # ← 填登录密码，至少 12 位（留空容器直接退出）
    volumes:
      - /vol1/1000/strimkeep/data:/data    # 数据库/配置，丢了无法恢复
      - /vol1/1000/strimkeep/strm:/strm    # STRM 输出，Emby 库指这里
    restart: unless-stopped
```

只需要改两处：`ADMIN_PASSWORD=` 后面填密码；两个 `/vol1/1000/strimkeep/...` 换成你的目录
（右侧的 `/data`、`/strm` 不要改）。

### 3. 启动

```bash
docker compose up -d
```

打开 `http://NAS的IP:9000`，用户名 `admin` + 你填的密码登录。

> 想固定版本：把 `:latest` 改成 `:3.0.2`（可用 tag 见 Releases）。

### 要 Emby 一起装？

把下面这段追加到 `docker-compose.yml` 末尾，`docker compose up -d` 即可：

```yaml
  emby_strimkeep:
    image: emby/embyserver:latest
    container_name: emby_strimkeep
    environment:
      - TZ=Asia/Shanghai
      - UID=0
      - GID=0
    ports:
      - "8096:8096"                      # Emby 网页端口，按需改
    volumes:
      - /vol1/1000/strimkeep/emby:/config
      - /vol1/1000/strimkeep/strm:/media # 必须和上面的 /strm 挂同一份
    restart: unless-stopped
```

### 升级 / 备份

```bash
docker compose pull && docker compose up -d                 # 升级（回滚就把版本号改回去）
tar -czf strimkeep-backup-$(date +%Y%m%d).tar.gz data strm  # 备份，两者必须成对
```

`data` 里有 `state.db`、`master.key` 与日志，丢了无法恢复；`strm` 可以重新生成，但 Emby 要重扫。

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

固定版本：把 yml 里 `image:` 改成 `ghcr.io/xcgtb/strimkeep:3.0.2`（可用 tag 见 Releases）。
回滚：版本号改回上一个，再 `pull && up -d`；数据库向下兼容上一版即可直接回退。

---

## 📄 目录结构

```
backend/strimkeep/          后端模块：转存、离线、频道、整理、STRM、库存、治理、Emby、通知
backend/tests/              后端测试（pytest）
frontend/src/               前端源码（Vue 3 + TS）
docs/operations/            运维文档：备份恢复、指标、告警
scripts/                    构建、部署、校验、运维脚本
docker-compose.yml          默认部署：拉 GHCR 发布镜像 `ghcr.io/xcgtb/strimkeep:latest`
docker-compose.dev.yml      源码构建覆盖：本地 build，镜像打 `strimkeep:dev`
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

