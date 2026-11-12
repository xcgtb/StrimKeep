# 0.2.2：由 Emby 和插件负责刮削

2026-10-23，依据用户最新确认覆盖原计划中本地 NFO 和图片输出分工。

## 当前职责

- StrimKeep 保留 115 扫描、TMDB 身份与季集识别、FFprobe 技术参数、分类、作品与季目录、云端改名及移动、播放 Token 和 Emby 扫描通知。
- 本地只生成 STRM，下载关联的外挂字幕并与 STRM 同名放在一起。
- 不再生成或覆盖电影 NFO、tvshow.nfo、单集 NFO；不再请求、下载或写入海报和背景图。
- Emby 和神医助手负责刮削、NFO 和图片。具体插件配置沿用用户现有设置，本包不改变插件配置。
- 新任务、恢复中的旧任务和“补齐已归档输出”都遵循本规则。历史已有 NFO、图片保持原样，避免误删或覆盖 Emby/插件已经写入的内容。
- TMDB 详情与 FFprobe 结果仍存数据库供命名、页面展示和核查使用；不再导出为本地 NFO。

## 更新 StrimKeep

解压至 `/path/to/StrimKeep-v0.2.2-phase2`：

```bash
cd '/path/to/StrimKeep-v0.2.2-phase2/StrimKeep' &&
bash update.sh '/path/to/StrimKeep-v0.1.0-phase1/StrimKeep'
```

原运行目录仍为包含现有 .env 的目录；数据库结构、账号、Cookie、密钥、挂载路径与 Token 不变。此操作只更新 StrimKeep，不修改或重建 、Emby。

## Emby 改成可写挂载

修改 **EmbyServer 所在的 Compose 项目**（与  共用的项目），将原来的：

```yaml
- /vol1/1000/测试/strimkeep-strm:/strimkeep-phase1-strm:ro
```

替换为：

```yaml
- /vol1/1000/测试/strimkeep-strm:/strimkeep-phase1-strm:rw
```

其他 /config、 的 /strm、端口和网络模式保留。在飞牛 Docker 项目里保存并应用配置，重建 Emby 服务的容器；单纯 restart 不会应用新的挂载模式。使用原项目的 Emby 服务，避免额外启动第二套 Emby。

验证：

```bash
docker inspect EmbyServer --format '{{range .Mounts}}{{if eq .Destination "/strimkeep-phase1-strm"}}{{.Source}} -> {{.Destination}} RW={{.RW}}{{end}}{{end}}'
```

结果应为 `RW=true`。这只能核实挂载可写；实际 Emby 服务用户还必须有目录读写权限。前次终端显示的目录 `000` 权限仍需修正，可写挂载不会自动修改文件系统权限。不要为了此目录而修改整个数据盘或配置数据库权限。

Emby 电视剧库继续引用 `/strimkeep-phase1-strm/StrimKeep媒体库/剧集/欧美剧集` 等实际已输出地区目录，使用电视节目内容类型；根据现有插件配置执行刮削、保存 NFO/图片和库扫描。

## 验证

本地后端 52 项测试通过，包含电影/剧集只生成 STRM 与字幕、无图片请求、修复输出不覆盖插件 NFO/图片、身份核查和恢复等回归。前端完成 TypeScript/Vite 构建。制作环境无 Docker 或用户凭据，本次未在 NAS 修改 Emby 挂载或测试插件写入。
