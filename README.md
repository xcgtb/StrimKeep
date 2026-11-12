# 🎬 StrimKeep 3.0.2

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.12-3776AB?style=for-the-badge&logo=python" alt="Python">
  <img src="https://img.shields.io/badge/FastAPI-Ready-009688?style=for-the-badge&logo=fastapi" alt="FastAPI">
  <img src="https://img.shields.io/badge/Vue-3-42b883?style=for-the-badge&logo=vue.js" alt="Vue 3">
  <img src="https://img.shields.io/badge/Docker-Ready-2496ED?style=for-the-badge&logo=docker" alt="Docker">
  <img src="https://img.shields.io/badge/License-MIT-green?style=for-the-badge" alt="License">
</p>

一句话：**把 115 网盘的影视资源整理成 Emby 能直接收录的 STRM 库，并且能跟分享库对照着治理。**

```
115 转存 / 离线 / 频道监控  →  TMDB 识别 + 规范命名  →  生成 STRM  →  Emby 收录
                                      ↓
                       片库映射（缺哪集、画质如何）→ 双库治理（先出计划，逐文件核验后清理）
```

---

## 🚀 怎么跑起来

### 情况 A：你已经有这套在跑（升级 / 换版本）

在**放 docker-compose.yml 的那个目录**执行三条命令就完事：

```bash
cd /vol1/1000/strimkeep          # 换成你自己建 yml 的目录
docker compose pull              # 拉最新发布镜像（ghcr.io/xcgtb/strimkeep:latest）
docker compose up -d             # 用新镜像重建容器
docker compose logs -f           # 想确认起没起来就看日志，Ctrl+C 退出
```

确认活没活：

```bash
curl -s http://127.0.0.1:9000/health/ready
# {"status":"ready","version":"3.0.2","stage":5}
```

> **仓库里那个 `docker-compose.yml` 不是给你部署用的**，它只服务源码构建（`docker-compose.dev.yml` 配合）。
> 自己部署永远只用下面「情况 B」这份模板——**值全部写死，不引用任何变量、不需要 `.env`**。
> 模板里加了新东西（比如新的可选环境变量），升级时照样能直接用，这正是拿模板自测的意义。

### 情况 B：第一次装（还没有任何目录）

1）建两个目录（`data` 存数据库，`strm` 存生成的播放文件）：

```bash
mkdir -p /vol1/1000/strimkeep/data /vol1/1000/strimkeep/strm
cd /vol1/1000/strimkeep
```

2）在这个目录新建 `docker-compose.yml`，整段照抄：

```yaml
services:
  strimkeep:
    image: ghcr.io/xcgtb/strimkeep:latest
    container_name: strimkeep
    network_mode: host          # 面板 9000、Emby 回调、播放地址都能直接访问
    environment:
      - TZ=Asia/Shanghai
      - ADMIN_USERNAME=admin
      - ADMIN_PASSWORD=         # ← 只改这一行：等号后面填登录密码（至少 12 位）
    volumes:
      - /vol1/1000/strimkeep/data:/data     # 左侧换成你的数据目录，右侧固定 /data
      - /vol1/1000/strimkeep/strm:/strm     # 左侧换成你的 STRM 目录，右侧固定 /strm
    restart: unless-stopped
    stop_grace_period: 30s
```

3）启动：

```bash
docker compose up -d
```

4）浏览器打开 `http://NAS的IP:9000`，用户名 `admin`，密码就是你刚填的那个。

> 密码少于 12 位或留空时，容器会启动即退出，日志里会直接写原因。

### 一句话记住

- **部署 = 你自己目录里那份模板**（就是上面「情况 B」的 yml），密码直接写在里面
- **仓库里的 `docker-compose.yml` = 开发用**，只跟 `docker-compose.dev.yml` 一起做源码构建，跟你的部署无关

两种都行，差的只是「密码写哪儿」，跑起来的服务完全一样。

---

## 🧭 第一次登录后，按这个顺序做

左边菜单从上到下就是这个顺序，建议照着走一遍：

| 步骤 | 去哪 | 干什么 |
|---|---|---|
| 1 | **115 网盘** | 扫码或账号登录你的 115（只存密文，登录态可续） |
| 2 | **系统设置 → 服务连接** | 填 TMDB API Key；要用 Emby 就填 Emby 地址 + API Key |
| 3 | **系统设置 → 资源来源** | 设置转存 / 离线的**落盘目录**（115 上的目录） |
| 4 | **影视整理** | 选一个目录 → 预览识别结果 → 执行，得到 `剧名 (年份) {tmdb-id}/Season N/` 这种规范结构 |
| 5 | **STRM 管理** | 生成 STRM（播放走 302 跳转，不占本地空间），再让 Emby 扫库 |
| 6 | **库存管理** | 平时看「片库映射」查缺集与画质；要清理就进「双库治理」 |

### 1. 登录 115 网盘

菜单 **115 网盘**：扫码或填 Cookie 登录。这一页还能看签到状态和回收站（删错了从这里捞）。

### 2. 填服务连接

菜单 **系统设置 → 服务连接**：

- **TMDB API Key**：必填，影视识别靠它（themoviedb.org 账号设置里免费申请）
- **Emby**：地址（如 `http://192.168.1.10:8096`）+ API Key（Emby 后台 → 高级 → API 密钥）
- 要通知就顺带填 **系统设置 → Telegram 通知**

### 3. 设置落盘目录

菜单 **系统设置 → 资源来源**：转存、离线、频道监控各自落到 115 的哪个目录。
三类来源**各自独立**决定「落盘后是否立即整理」，默认只落盘、不自动整理。

### 4-5. 转存 → 整理 → STRM → Emby

1. **资源入库**：粘贴分享链接（支持 `?password=` 提取码）/ 磁力 / ED2K / 直链，提交
2. **任务中心**：看进度，失败原因也写在这里
3. **影视整理**：选目录 → 预览（先给你看 TMDB 匹配和改名计划）→ 执行
4. **STRM 管理**：生成 STRM
5. Emby 里把媒体库指到 **`strm` 目录**（不是 `data`），扫库就能看到片

STRM 的身份信息**写在文件里**，以后改名、换目录、换 Emby 库路径，都不用再维护登记表。

### 6. 日常：片库映射 + 双库治理

菜单 **库存管理**，三个页签：

| 页签 | 干什么 |
|---|---|
| **片库映射** | 按 Emby 媒体库分类，直接标 `完整 / 缺集 / 超集 / 在更 / 待对照 / 已入库 / 待核实`，点开看逐季逐集 |
| **双库治理** | 本地库和分享库对照画质与集数，找出该清的残次品 |
| **目录残留清理** | 清掉目录里剩下的空壳、孤儿文件 |

---

## ⚖️ 双库治理怎么用（有安全阀，别怕点错）

```
扫描双库 → 勾选要处理的项 → 生成计划 → 看计划里的文件清单 → 执行
```

- **生成计划**时给每个文件记指纹（SHA-256 / 大小 / mtime / inode + 来源指纹）
- **执行**前逐作品、逐文件核对；只要有一个文件变了、或发现新文件，**立刻停**并说明原因
- 计划有上限：一次最多 100 部 / 1000 个文件
- 清理**可回滚**：网盘侧进 115 回收站，分享侧回到原分享

三种典型情况：

| 情况 | 处理 |
|---|---|
| 分享库画质更好 | 删本地那份，改挂分享 |
| 本地更全 | 删分享那份 |
| 残次品（前序缺失 / 中间断层） | 只清残的那一侧，不动另一侧 |

---

## 📥 三类资源怎么进来

| 来源 | 在哪 | 说明 |
|---|---|---|
| **分享转存** | 资源入库 | 粘贴分享链接，支持提取码，批量转存 |
| **离线下载** | 资源入库 | 磁力 / ED2K / 直链，跟踪进度与产物 |
| **频道监控** | 频道监控 | 公开频道预览页或私密频道（用户会话，只读），关键词过滤后分流落盘 |

---

## 🆙 升级 / 回滚 / 备份

```bash
cd /vol1/1000/测试/StrimKeep
docker compose pull && docker compose up -d      # 跟最新版
```

固定版本：把 yml 里 `image:` 改成 `ghcr.io/xcgtb/strimkeep:3.0.2`（可选 tag 见 Releases）。
回滚：版本号改回上一个再 `pull && up -d`，数据库向下兼容上一版，可直接退。

备份（`data` 与 `strm` **成对**备份）：

```bash
tar -czf strimkeep-backup-$(date +%Y%m%d).tar.gz \
  /vol1/1000/测试/StrimKeep/data /vol1/1000/测试/StrimKeep/strm
```

| 目录 | 里面是什么 | 丢了会怎样 |
|---|---|---|
| `data` | `state.db`、`master.key`、日志 | 库状态与凭据密文没了，**无法恢复** |
| `strm` | 生成的 STRM 媒体树 | 重新生成即可，但要 Emby 重扫 |

---

## 🐛 常见问题

**打不开面板**

```bash
docker compose ps                            # 容器在不在
curl -s http://127.0.0.1:9000/health/ready   # 应用活没活
docker compose logs -f                       # 日志
```

- 起来就退出、日志写「首次启动请设置 … 至少 12 位的 ADMIN_PASSWORD」→ 把密码补齐
- `9000` 被占用 → 把 `network_mode: host` 换成 `ports: ["9100:9000"]`

**治理页看不到作品，显示「静默期」**：刚入库 15 分钟内的作品不进治理清单，到点自动进。

**提示「网盘源文件未登记，无法删除」**：本地有 STRM、台账查不到对应的 115 文件。分享侧照常可清；
网盘侧要先补登记（重新整理该作品，或走「STRM 管理」同步），系统不会猜着删。

**约 141 个没带 `{tmdb-id}` 的分享 STRM 不进片库映射**：有意的——它们是要清掉的对象，不做映射。

**全选只选了本页**：列表分页时「全选」= 选本页，跨页要翻着勾，或直接用筛选后的批量操作。

---

## 🗂 目录结构（想改代码再看）

```
backend/strimkeep/          后端：转存、离线、频道、整理、STRM、库存、治理、Emby、通知
backend/tests/              后端测试（pytest，1575 个用例）
frontend/src/               前端源码（Vue 3 + TS）
docs/operations/            运维文档：备份恢复、指标、告警
scripts/                    构建、部署、校验脚本
docker-compose.yml          开发用：拉 GHCR 镜像（部署请用 README 里的模板）
docker-compose.dev.yml      源码构建覆盖：本地 build，镜像打 strimkeep:dev
```

三条不能破的约定：STRM 身份写在文件里，台账只做来源索引；删除必须有证据（计划快照 + 逐文件核验）；
库存来自目录扫描，单个文件的问题不影响整页。

源码构建（改代码时用）：

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build
```

---

## 📌 声明

- 本项目只做管理与整理，**不提供任何影视资源**；请只处理你合法拥有的内容
- 删除操作可回滚：网盘文件进 115 回收站，分享侧可回到原分享
- 许可证：MIT，见 [LICENSE](LICENSE)

<p align="center">
  <strong>🎉 一套面板，把两套库管明白</strong>
</p>
