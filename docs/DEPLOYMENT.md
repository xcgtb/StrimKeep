# StrimKeep 部署

日常运行只需要一个部署目录和一份 [docker-compose.yml](../docker-compose.yml)。使用预构建镜像，外部服务参数在 Web 设置中填写。

## 首次部署

1. 创建目录，例如 `/vol1/1000/docker/StrimKeep`。
2. 将仓库的 `docker-compose.yml` 放入该目录，修改登录密码与三个媒体路径。
3. 在该目录执行 `docker compose up -d`。
4. 访问 `http://NAS_IP:8321`，登录后填写 Emby、TMDB 和 Telegram 配置。

首次启动自动创建 `data/`。镜像必须在 GitHub 发布流程成功后才能拉取；未登录拉取要求 GHCR 包为公开。

| 容器路径 | 宿主机路径内容 | 用途 |
| --- | --- | --- |
| `/data` | 部署目录的 `./data` | 配置、SQLite、缓存、日志和计划存档 |
| `/media/local` | 本地影视库 STRM 根目录 | NAS STRM 与附属文件清理 |
| `/media/share` | 分享影视库 STRM 根目录 | 只清理 NAS |
| `/media/cloud` | CD2 挂载的对应本地库 115 源目录 | 联动清理 115 源文件和附属文件 |

只修改挂载项冒号左边，右边保持不变。CD2 源目录须与本地 STRM 对应，不能挂成另一个库。`ENABLE_CD2_WATCHDOG` 只控制启动时等待挂载，不关闭联动删除。

配置保存到 SQLite；如另行设置了非空的服务环境变量，其值优先于 Web 配置。不要把私人密码或密钥提交到 GitHub。

## 查看状态与更新

```bash
docker compose ps
docker compose logs --tail=80 strimkeep
```

更新时先修改 YML 中的镜像版本，然后：

```bash
docker compose pull
docker compose up -d
```

保留 `data/` 就能保留配置和记录，更新镜像不会清空用户数据。首次使用先扫描、查看清单，再执行清理。

## 从旧项目迁移

StrimKeep 1.0.0 使用服务名 `strimkeep`、数据库名 `strimkeep.db` 和新的登录会话，更新后需要重新登录。

如果要保留旧配置、订阅及记录：

1. 在旧部署目录执行 `docker compose down`，确认旧应用停止，保留原数据目录。
2. 新 YML 的 `/data` 左侧仍指向原数据目录；不要同时启动两个应用使用同一数据库。
3. 在停止状态下查看原 `data/state` 的数据库名称，将旧数据库重命名为 `strimkeep.db`。如仍有同组 `-wal`、`-shm` 文件，必须一并重命名。已有 `strimkeep.db` 时不能直接覆盖，应先确定要沿用哪套数据。
4. 放入新 YML，填写原媒体路径、密码并启动。需要旧 JSON 首次迁移时，让旧文件继续留在原 `/data`。
5. 检查设置、订阅、日志中心和计划存档。

复用原数据目录会保留旧记录；新部署使用空数据目录则从空记录开始。版本日志从 1.0.0 开始，应用不会自动清空已有数据库。

## 反向代理与端口

登录写操作检查 Origin。反代应传递正确的 `Host` 或 `X-Forwarded-Host`；必要时在 YML 的 `environment` 中添加 `ALLOWED_ORIGINS: https://你的域名`。直连 IP:端口无需配置。

默认使用 host 网络和 8321 端口。若覆盖容器 `command` 修改 uvicorn 端口，请同步设置 `PORT` 环境变量，确保健康检查请求同一端口。

源码开发和 GitHub 镜像发布步骤见 [维护者文档](../dev/README.md)，普通部署无需测试工具。
