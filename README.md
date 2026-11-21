# StrimKeep

面向 NAS 的 115 网盘影视整理与 STRM 管理工具。归档网盘里的视频，按目录生成 STRM，再交给 Emby 收录和播放。网盘库和分享库可以分别管理，也可以对照库存。

[快速部署](#docker-部署) · [连接 Emby](#连接-emby) · [更新记录](CHANGELOG.md) · [GitHub Releases](https://github.com/xcgtb/StrimKeep/releases)

## 主要功能

| 功能 | 用途 |
| --- | --- |
| 资源入库 | 接收 115 分享、磁力、ED2K 和直链。支持通过 Telegram 频道监控按规则接收资源。 |
| 影视整理 | 通过 TMDB 识别作品，按类型和地区归档，按配置比较画质、音轨和版本。 |
| STRM 同步 | 全量或增量同步按 115 原路径生成 STRM。已有目录可以直接同步。 |
| 分享 STRM | 从 115 分享生成 STRM，整理到独立的分享影视库。 |
| Emby 反代 | 每台 Emby 可使用独立地址、API Key 和反代端口。识别到 115 媒体源后，支持直链播放和 302 跳转。 |
| 库存与治理 | 核对两库库存和缺集，预览双库治理及失效清理计划。 |

## Docker 部署

在 NAS 上选择一个项目目录，下载部署模板和口令示例。镜像已包含 Web 前端。

```bash
mkdir -p strimkeep
cd strimkeep
curl -fL -o docker-compose.yml https://raw.githubusercontent.com/xcgtb/StrimKeep/main/docker-compose.yml
curl -fL -o .env https://raw.githubusercontent.com/xcgtb/StrimKeep/main/.env.example
chmod 600 .env
```

首次安装请在空目录中执行下载命令。在 `.env` 里填写至少 12 个字符的 `ADMIN_PASSWORD`，然后启动。

```bash
docker compose up -d
```

打开 `http://NAS地址:9000`，用 `.env` 里的管理员账号和密码登录。两项设置用于首次初始化。

在「115 网盘」登录账号。需要自动整理时，配置 TMDB API Key，再到「影视整理」确认待整理目录和归档目录。

| 宿主机目录 | 容器目录 | 用途 |
| --- | --- | --- |
| `./data` | `/data` | 数据库、配置和主密钥，升级时应保留 |
| `./strm` | `/strm` | STRM、字幕及同目录元数据 |

已部署的用户在原项目目录更新镜像，沿用现有 Compose 配置。

## 连接 Emby

如果已有 Emby，把 StrimKeep 的宿主机 STRM 目录挂到 Emby 的 `/media`，在 Emby 中添加对应的子目录为媒体库。在 StrimKeep 的「Emby 反代」里填写主 Emby 地址和 API Key，并检查默认路径映射 `/strm` ↔ `/media`。

如果希望同一个 Compose 项目启动 Emby，可以下载 [可选 Emby 模板](docker-compose.emby.yml)。

```bash
curl -fL -o docker-compose.emby.yml https://raw.githubusercontent.com/xcgtb/StrimKeep/main/docker-compose.emby.yml
docker compose -f docker-compose.yml -f docker-compose.emby.yml up -d
```

可选模板把 Emby 服务映射到 NAS 的 `8100` 端口。播放器使用 StrimKeep 上所选反代实例的监听端口，例如 `9001`。第二台 Emby 可以单独配置自己的地址、API Key 和监听端口。

### 已整理好的目录

在「STRM 管理」新建同步任务，选中 115 上的目录，执行全量或增量同步。使用外部刮削工具时，可选择「STRM + 技术媒体信息」模式。它保留本地 NFO 与海报，并直接从 115 源视频提取编码、分辨率和音轨，保存为同目录的 `*-media-info.json`。

### 可选的 Emby 媒体信息补写

如需向 Emby 的数据库补写媒体信息，把对应 Emby 的数据目录以可写方式挂到 StrimKeep，例如把 `./emby/data` 挂到 `/emby-data`，并在 StrimKeep 的 `.env` 里设置 `STRIMKEEP_EMBY_LIBRARY_DB=/emby-data/library.db`。使用默认 `/media` 路径映射时，补写沿用该映射。

## 更新与备份

在现有 Compose 项目目录中更新 StrimKeep。

```bash
docker compose pull strimkeep
docker compose up -d --no-deps strimkeep
```

使用可选 Emby 模板的项目，在以上命令里也带上两个 `-f` 参数。需要固定版本时，可从 [GitHub Releases](https://github.com/xcgtb/StrimKeep/releases) 选择发布标签。

备份数据库时要一并保存 `data/config/master.key`。运行中的 SQLite 请使用一致性备份。 [备份与恢复说明](docs/operations/BACKUP_RESTORE.md)

### 启动检查

```bash
docker compose ps
docker compose logs --tail 100 strimkeep
curl -fsS http://127.0.0.1:9000/health/ready
```

本项目提供媒体管理工具，请仅处理你有权使用的内容。许可证为 [MIT](LICENSE)。
