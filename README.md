# 🎬 StrimKeep

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.12-3776AB?style=for-the-badge&logo=python" alt="Python">
  <img src="https://img.shields.io/badge/FastAPI-Ready-009688?style=for-the-badge&logo=fastapi" alt="FastAPI">
  <img src="https://img.shields.io/badge/Vue-3-42b883?style=for-the-badge&logo=vue.js" alt="Vue 3">
  <img src="https://img.shields.io/badge/Docker-Ready-2496ED?style=for-the-badge&logo=docker" alt="Docker">
  <img src="https://img.shields.io/badge/License-MIT-green?style=for-the-badge" alt="License">
</p>

<p align="center">
  <strong>一套面板，管理两套 STRM 影视库：115 转存 / 离线 / 频道监控 → 整理洗版 → 生成 STRM → Emby 收录 → 本地库与分享库逐季逐集对比治理</strong>
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

- Docker 20.10+、Docker Compose 2.0+
- 能访问 115 网盘与 TMDB 的网络环境
- 115 网盘账号（扫码或账号登录）

### 🚀 三步安装

```bash
# 1) 下载编排文件
curl -fsSLO https://raw.githubusercontent.com/xcgtb/StrimKeep/main/docker-compose.install.yml

# 2) 打开文件，只改这 3 行：ADMIN_PASSWORD、STRIMKEEP_DATA_DIR、STRIMKEEP_STRM_DIR
vi docker-compose.install.yml

# 3) 启动
docker compose -f docker-compose.install.yml up -d
```

浏览器打开 `http://NAS_IP:9000`，用户名 `admin`，密码就是第 2 步填的那个。
首次进入后在面板里登录 115 账号，再配整理与治理规则，**环境变量只用来启动服务**。

### 📄 编排文件长什么样（每行都有注释）

```yaml
services:
  strimkeep:
    image: ghcr.io/xcgtb/strimkeep:latest     # 镜像，固定版本写 0.9.150
    network_mode: host                        # 直接占用 9000 端口
    environment:
      - TZ=Asia/Shanghai                      # 时区
      - ADMIN_USERNAME=admin                  # 面板用户名
      - ADMIN_PASSWORD=改成你的密码            # ★必填：至少 12 位
    volumes:
      - ./data:/data                          # ★必填：数据库/密钥/日志（务必备份）
      - ./strm:/strm                          # ★必填：STRM 输出目录（给 Emby 用）
    restart: always
```

完整文件见仓库根的 `docker-compose.install.yml`（含 Emby 可选段、健康检查、日志轮转）。

> 也可以完全不动环境变量：`ADMIN_PASSWORD=你的密码 docker compose -f docker-compose.install.yml up -d`
> 想连 Emby 一起装：加 `--profile emby`。

### 📁 数据持久化

| 目录 | 内容 | 说明 |
|---|---|---|
| `./data` | `state.db`、`master.key`、日志 | 库状态与凭据密文，**丢了无法恢复** |
| `./strm` | 生成的 STRM 媒体树 | 同时挂给 Emby |

备份（两者必须成对）：
```bash
tar -czf strimkeep-backup-$(date +%Y%m%d).tar.gz data/ strm/
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
洗版规则里可以调分辨率/编码/HDR 等 7 个维度的优先级，平局策略可配。

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
docker compose -f docker-compose.install.yml ps        # 容器在不在
curl -s http://127.0.0.1:9000/health/ready             # 应用健康
docker compose -f docker-compose.install.yml logs -f   # 看日志
```
- `9000` 被占用：改 `network_mode: host` 为 `ports: ["9100:9000"]`
- 密码太短：至少 12 位，否则容器反复重启并提示

### 提示「网盘源文件未登记，无法删除」
说明本地有 STRM、但台账查不到它对应的 115 文件。分享侧可以正常清理，
网盘侧需要先补齐登记（重新整理该作品或走「STRM 同步」），系统不会猜着删。

### 治理页出现「静默期」
作品刚入库 15 分钟内不进治理清单，卡片上会写「还剩 N 分钟」，等时间到自动进。

---

## 🚀 版本更新

```bash
docker compose -f docker-compose.install.yml pull
docker compose -f docker-compose.install.yml up -d
```

固定版本：编辑编排文件 `image: ghcr.io/xcgtb/strimkeep:0.9.150`（可用 tag 见 Releases）。
回滚：把版本号改回上一个，再 `pull && up -d`；数据兼容上一个版本即可直接回退。

---

## 📄 目录结构

```
backend/strimkeep/          后端模块：转存、离线、频道、整理、STRM、库存、治理、Emby、通知
backend/tests/              后端测试（pytest）
frontend/src/               前端源码（Vue 3 + TS）
docs/operations/            运维文档：备份恢复、指标、告警
scripts/                    构建、部署、校验、运维脚本
docker-compose.install.yml  拉取镜像部署（推荐，只需改 3 行）
docker-compose.yml          源码构建部署（开发用）
```

关键约束（改代码前请先读）：STRM 身份写在文件里，台账只做来源索引；
删除必须有证据（计划快照 + 逐文件核验）；库存来自目录扫描，单个文件的问题不影响整页。

---

## 🤝 致谢与声明

- 感谢 [goodmen001/tgto123-public-](https://github.com/goodmen001/tgto123-public-) 等项目在
  网盘自动化上的开源实践，本项目的部署文档结构参考了同类工具的写法
- 本项目只做管理与整理，**不提供任何影视资源**；请只处理你合法拥有的内容
- 删除操作可回滚：网盘文件进 115 回收站，分享侧可回到原分享
- 许可证：MIT，见 [LICENSE](LICENSE)

<p align="center">
  <strong>🎉 一套面板，把两套库管明白</strong>
</p>

<p align="center">
  如果这个项目对你有帮助，给个 ⭐ Star 支持一下
</p>
