# StrimKeep 部署、迁移与排错指南

> 适用于 **StrimKeep** 的 Docker Compose 部署示例，主要以飞牛 OS / Linux NAS 为例。GitHub 仓库模板默认使用 `ghcr.io/xcgtb/strimkeep:latest`（滚动标签）。**并非容器自动更新**；具体功能与升级兼容性应以实际拉取的镜像版本和发布说明为准。

[返回 README](../README.md) · [查看完整 Compose 配置](../docker-compose.yml)

## 一、部署前准备

请确认 NAS 已安装 Docker、Docker Compose v2，并准备以下路径：

| 用途 | 容器内固定路径 | 宿主机路径示例 | 说明 |
|---|---|---|---|
| 应用持久化 | `/data` | `./data` | SQLite、配置、缓存、日志、订阅、治理计划等 |
| 本地 STRM 库 | `/media/local` | `/vol1/1000/TgtoDrive/strm/115网盘/影视媒体库` | **STRM 文件目录**，不是视频源目录 |
| 分享 STRM 库 | `/media/share` | `/vol1/1000/TgtoDrive/strm/115网盘/分享影视库` | 分享库的 **STRM 文件目录** |
| CloudDrive2 源目录 | `/media/cloud` | `/vol1/1000/docker/clouddrive2/CloudDrive/影视媒体库` | 对应本地 STRM 的 **115 真实源文件目录** |

**路径规则**：以上仅为飞牛 OS 示例，部署时需按你的实际目录修改宿主机路径。冒号右侧的 `/media/local`、`/media/share`、`/media/cloud` 及各自挂载选项应保持不变。

### 为什么要区分本地 STRM 与 CD2 源目录？

本地 STRM 与 115 源文件应具有可对应的相对目录层级。例如：

```text
NAS 本地 STRM：
/你的本地STRM库/剧集/日韩剧集/示例剧/Season 1/S01E01.strm

CloudDrive2 实际源：
/你的CD2源文件库/剧集/日韩剧集/示例剧/Season 1/S01E01.mkv
```

这里的文件名只是演示。实际删除仍受项目的源文件匹配规则约束，**不能把“目录看起来一致”当成可安全删除的证据**。如果源文件匹配不到、结果有歧义，或目标文件删除失败，应按项目安全策略跳过，先排查原因。

## 二、首次部署

### 1. 创建部署目录

通过 SSH 执行：

```bash
mkdir -p /vol1/1000/docker/StrimKeep
cd /vol1/1000/docker/StrimKeep
```

将本项目的 [`docker-compose.yml`](../docker-compose.yml) 放进这个目录。该文件是 **GitHub 公开部署模板**，镜像为 `latest`，但登录密码与 NAS 挂载路径均为示例值；请在首次启动前自行修改。可以通过飞牛文件管理上传，也可以使用你熟悉的文本编辑器创建文件。

### 2. 只修改需要修改的设置

优先修改以下四项：

- **Web 密码：** 把 `WEB_PASSWORD: "CHANGE_ME_TO_A_STRONG_PASSWORD"` 换成自己的强密码。Compose 中的字面量 `$` 要写成 `$$`。
- **本地 STRM 路径：** 对应 `/media/local` 的冒号左侧。
- **分享 STRM 路径：** 对应 `/media/share` 的冒号左侧。
- **115 源文件路径：** 对应 `/media/cloud` 的冒号左侧。此处应能看到真实视频文件。

`ENABLE_CD2_WATCHDOG` 默认是 `"1"`：容器启动时会等待 CD2 挂载就绪。如果仅测试 Web 或不使用 CD2，可将其改为 `"0"` 跳过等待。**这个参数不负责开启或关闭源文件联动删除**，切勿将其视为清理权限开关。

Emby 地址 / API Key、TMDB Key、Telegram Token 等应用设置，可在 Web 的「规则设置 → 服务连接」中配置，无需全部写进 Compose。

### 3. 启动前检查

```bash
cd /vol1/1000/docker/StrimKeep

docker compose config -q
```

无输出且命令成功，通常表示 Compose 语法可解析；**它不会检查路径是否真实存在、CloudDrive2 是否挂载成功，也不会判断媒体删除是否安全**。请到飞牛文件管理确认那三个媒体目录。

### 4. 拉取并启动

```bash
docker compose pull
docker compose up -d
docker compose ps
```

浏览器访问：

```text
http://你的NAS_IP:8321
```

账号默认为 `admin`，密码以你在 Compose 中实际设置的值为准。首次启动后，当前目录将有 `data/`，后续更新必须保留。

> 如果拉取报 `manifest unknown`、无权限或镜像不存在，请核对 [GitHub Releases](https://github.com/xcgtb/StrimKeep/releases) 和 [GHCR 镜像包](https://github.com/xcgtb/StrimKeep/pkgs/container/strimkeep) 是否已公开，并确认发布流程包含 `latest` 标签。

## 三、第一次登录后怎么设置

建议按顺序完成：

1. 打开「规则设置 → 服务连接」，设置并测试 Emby、TMDB；如需 Telegram 通知，再配置 Bot Token、Chat ID 与允许用户。
2. 核对本地库、分享库的映射是否正确；确认没有把视频源目录错误挂成 STRM 目录。
3. 查看默认治理规则，包括「画质优先」「多季保护」「特殊篇 S00」和默认 15 分钟入库静默期。
4. 打开「双库治理」，先**扫描并预览**，检查标题配对、待删路径、理由和源文件匹配情况。
5. 确认无误后才主动执行。首次部署建议至少完成一次只读预览，**不要将测试验证和正式清理合并为一步**。

### 哪些操作会真正删除文件？

| 操作 | 是否会删除文件 |
|---|---|
| 扫描双库、预览治理清单 | 不会 |
| 双库定时巡检 | 不会自动删除，仅扫描 / 通知 |
| Web 中确认执行治理计划 | **会**，依据计划和安全校验执行 |
| 片库映射详情页确认删除 | **会**，具体范围以页面二次确认为准 |
| 目录清理预览 | 不会 |
| 目录清理确认执行 | **会** |
| Telegram Bot `/clean` | **会**尝试执行最近一次治理计划，务必限制操作权限 |

**本地库清理**还可能删除 115 源文件；**分享库清理**按项目说明只删除 NAS 侧 STRM 和附属文件，不删除远端分享源。工具本身不提供回收站或撤销功能。

## 四、升级与备份

### 1. 停止服务并备份

```bash
cd /vol1/1000/docker/StrimKeep

docker compose stop strimkeep

tar -czf "strimkeep-data-$(date +%Y%m%d_%H%M%S).tar.gz" data
```

这样备份的是当前 `data/`。备份后建议再将压缩包保存到部署目录之外的独立位置，以免目录迁移或误删时一同丢失。

### 2. 升级镜像

阅读新版本发行说明并确认数据兼容后，若使用默认 `latest` 标签，无需改动 `image:`；若使用固定版本，请先把 `image:` 改成目标版本，再执行：

```bash
docker compose pull
docker compose up -d
docker compose ps
```

**当前 YML 默认使用 `latest`，但 Docker 不会自动更新运行中的容器。** 每次升级仍需主动执行拉取和重新创建容器的命令；正式环境需要可复现、可回滚时，建议固定版本号。

### 3. 从旧项目迁移

**不要把旧项目的整个 `data/` 或旧数据库直接覆盖到新部署后启动。** 新版文档中的 SQLite 路径为 `/data/state/strimkeep.db`，数据库结构、迁移脚本和向后兼容要求需要针对实际源码确认。

稳妥做法是：先停止旧服务并完整备份，再在全新部署目录验证新版本；如需保留历史配置、订阅与治理计划，必须先核实对应版本的正式迁移逻辑，不能仅重命名数据库就视为完成迁移。

## 五、反向代理与网络

此 Compose 使用：

```yaml
network_mode: host
```

服务默认监听宿主机 `8321`，因此**不需要额外写 `ports:`**。同一 NAS 上的 8321 若已被占用，需要检查并调整实际应用监听端口；如按原文档通过 `command` 改 uvicorn 监听端口，还需同步更新 `PORT`，避免健康检查读错端口。

如果通过域名、HTTPS 反向代理访问后，登录成功但写操作出现 **403**，请先确认反向代理正确传递 `Host` / `X-Forwarded-Host`，并按需在 `environment` 中补充：

```yaml
ALLOWED_ORIGINS: "https://你的域名"
```

域名应与实际浏览器访问源一致。不要为了排查方便将管理端口直接暴露到公网；建议使用 HTTPS 并配置可靠的访问控制。

## 六、常见故障排查

### 容器没有起来

```bash
cd /vol1/1000/docker/StrimKeep
docker compose ps
docker compose logs --tail=100 strimkeep
```

检查 YML 语法、镜像版本、Web 密码和挂载路径。如果镜像未发布或没有公开权限，拉取阶段就会失败。

### CD2 挂载没就绪、应用一直等待

检查 YML 的 `ENABLE_CD2_WATCHDOG`。设置为 `"1"` 时，项目会等待 CD2 就绪；如果正在测试界面或当前没有 CD2，可使用 `"0"` 跳过这项等待。**跳过等待不等于可以在源目录缺失时安全执行本地库清理**。

### 看不到本地库或分享库

请优先确认宿主机三个目录是否存在且数据可见。Compose 在部分场景下可能自动创建尚不存在的宿主机目录，这会导致容器启动了、实际却扫描到空目录。不要只检查容器是否为 `running`。

### `unhealthy` 是什么意思？

YML 使用镜像中的 `/app/scripts/healthcheck.py` 进行健康检查。`unhealthy` 表示检测未通过，不代表 Docker 会自动重启容器；应查看应用日志和健康检查输出，排查接口或依赖问题。

### 数据为什么越来越大？

`./data:/data` 保存数据库、缓存、日志等应用状态，和 Docker 的标准输出日志不是同一回事。Compose 里的 `max-size: "10m"`、`max-file: "3"` **只限制 Docker 的 `json-file` 日志**，并不对 `/data` 中的 SQLite 数据库、缓存和计划存档设置大小上限。不要直接删除数据库文件来“清缓存”。

### 修改密码后没有生效

在部署目录里修改 `WEB_PASSWORD` 并执行：

```bash
docker compose up -d
```

如果仍旧登录异常，排查浏览器旧会话和容器中的实际环境值。不要在截图、日志或 Issue 中公开真实密码、Token 或 API Key。

## 七、文件与数据保护原则

- 优先做**只读扫描 / 预览**，再验证清理建议。
- 云端源文件匹配不确定时不应删除，不能仅凭目录唯一匹配进行危险清理。
- 新入库的标题应受静默期保护；更改画质或配对规则后要重新扫描。
- `/data` 是持久化目录，重建容器时需要保留。
- 直接删除 STRM、附属文件或云端源文件均可能不可恢复，执行前应自行准备可验证的备份。

---

**相关文件：** [README.md](../README.md) · [docker-compose.yml](../docker-compose.yml) · [GitHub Releases](https://github.com/xcgtb/StrimKeep/releases)
