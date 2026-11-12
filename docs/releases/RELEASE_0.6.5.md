# StrimKeep 0.6.5

在 0.6.4 的现场部署上做的一轮运维与稳定性修补：不改变整理、洗版、转存、Emby 通知的业务规则，不动数据库结构（只加两个索引）。

## 前端改为镜像内构建

0.6.4 的镜像直接 `COPY frontend/dist`，而 `frontend/dist` 既没有进版本库，也不参与任何构建流程。结果是：改了 `frontend/src` 之后 `docker compose up -d --build` 出来的仍是旧界面；一旦 `frontend/dist` 被清理或缺失，镜像会直接构建失败或前端资源不完整。历史上的 UI 修复只能用「手改编译产物 + unzip 覆盖」的方式交付，原因就在这里。

0.6.5 恢复多阶段构建：

```
FROM node:22-alpine AS frontend   →   npm ci + npm run build
FROM python:3.12-slim-bookworm    →   COPY --from=frontend /build/dist ./static
```

- NAS 主机仍然不需要安装 Node/npm，构建发生在 Docker 内。
- `.dockerignore` 排除 `frontend/dist`，防止「改了 src 却在跑旧包」重演。
- 需要换 npm 镜像时：`docker build --build-arg NPM_REGISTRY=https://registry.npmmirror.com -t strimkeep:0.6.5 .`。
- `update.sh` 不再强制要求发布包内带 `frontend/dist`，只要求 `frontend/src` 与 `frontend/package-lock.json`。
- 已用同一份源码在 NAS 上实测重建，产出的资源哈希与线上 `dist` 完全一致（`index-zMH3-ngh.js`、`index-_Uc48poa.css` 等），因此切换构建方式不会带来界面差异。

## 登录限流不再误伤管理员

原来的顺序是「先看这个 IP 失败次数是否超限，再校验密码」。StrimKeep 通常通过 frp / 反代的 TCP 隧道访问，所有外部请求的 `request.client.host` 都是 `127.0.0.1`，于是任意一个人连续输错 8 次，管理员本人用正确密码也会被 429 挡在门外 15 分钟。

0.6.5 改成先校验密码：正确即登录，错误才计数并进入限流。暴力尝试仍然被限制在每 15 分钟 8 次，但任何人无法再借限流把管理员锁在门外。

## 静态资源缓存

中间件对所有响应强制 `Cache-Control: no-store`，把 Vite 的哈希资源也一起关掉了缓存：每次打开页面都要重新下载 545 KB JS + 215 KB CSS，经公网隧道访问时尤其明显。

现在 `/assets/*`（内容由哈希决定、永不变化）返回 `public, max-age=31536000, immutable`，HTML 和所有 API 仍是 `no-store`。首次加载之后的再次进入只走 304。

## 长期运行的数据库体量

`events`、已读 `notifications`、过期 `sessions` 原先只增不删，定时扫描每天都在写日志。现在由调度器每小时执行一次保留清理：日志保留 90 天且最多 20000 条，已读通知保留 180 天，过期会话顺带清理；新增 `events(at)`、`notifications(at)` 两个索引。清理只在有条目被删除时写一条系统日志，失败也不会把调度器标记为异常。

## 配置提示

`ADMIN_USERNAME` / `ADMIN_PASSWORD` 只在首次创建数据库时生效，此前在 Compose 里改密码是完全静默的。现在如果环境变量与新库对不上，启动时会写入一条一次性系统日志说明原因，避免误以为密码已更新。

## 升级

```bash
cd "/path/to/StrimKeep" && docker compose up -d --build
```

镜像标签为 `strimkeep:0.6.5`，旧镜像 `strimkeep:0.6.4` 保留用于回滚。数据目录、`config/master.key`、播放 Token、7000/9000 端口与挂载路径不变。

## 验证

- 后端回归：405 项原有用例 + 新增 5 项（登录限流顺序、静态缓存头、保留清理、镜像构建链路、密码提示）。
- NAS 实测：镜像内构建成功后启动影子实例（数据为线上库的 SQLite 副本、独立数据目录、独立端口），核对登录、各页接口、静态资源缓存头与限流行为，再替换生产容器。
- 未覆盖：真实 115 转存与 Emby 入库仍以现场任务为准；本次不改这些流程。
