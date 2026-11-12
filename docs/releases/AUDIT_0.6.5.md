# StrimKeep 现场审计与 0.6.5 改进

审计对象：飞牛 NAS `192.0.2.10` 上的 `/path/to/StrimKeep` 与容器 `strimkeep`（host 网络，9000 端口）。
审计时的运行数据：8 个整理任务（7 completed / 1 partial）、4 条播放映射、28 条通知、101 条实时日志。

## 一、结论

代码本身的整理/洗版/转存链路是稳的：每一步都有「意图 → 操作 → 回读核实」的痕迹，异常时宁可停下也不重复写，云端改动前会重新核对文件身份。真正的问题集中在**部署链路**和**长期运行的运维面**，其中两个会在真实场景里直接咬人。

## 二、发现（按影响排序）

### P0-1 前端改动无法进入镜像，只能手改编译产物

`Dockerfile` 直接 `COPY frontend/dist ./static`，而 `frontend/dist` 被 `.gitignore` 排除、也不参与任何构建步骤。后果：

- 改 `frontend/src` 后 `docker compose up -d --build`，界面没有任何变化；
- `frontend/dist` 一旦缺失或被清理，镜像构建失败或前端资源不完整；
- `UI_HOTFIX_README.txt` 里「直接打补丁覆盖 dist 里的 JS」不是偷懒，而是当时唯一的交付方式；
- 仓库里 `b290f51 fix(build): 前端改为镜像内构建` 已经试过一次，但工作区又被改回 `COPY frontend/dist`，说明这个改动没有真正落地。

补充事实：在 NAS 共享目录里直接跑 `npm ci` 会失败——`spawnSync .../esbuild EACCES`，该共享不允许执行新写入的二进制。所以前端构建只能发生在镜像里，没有第二种选择。

### P0-2 登录限流顺序错误，陌生人可以锁死管理员

原实现先判断「这个 IP 的失败次数是否已达 8 次」，再校验密码。而 StrimKeep 是通过 frp 的 TCP 隧道暴露的（`frpc.toml` 中 `strimkeep-115-dev`：`127.0.0.1:9000 → 公网 9000`），隧道把所有外部访客都变成同一个 peer `127.0.0.1`。

于是：任何人连续输错 8 次，管理员**用正确密码**也会收到 429，被挡在门外 15 分钟；而且这条限制对真正的攻击者只是 8 次/15 分钟，收益与代价完全倒挂。

### P1-1 带哈希的静态资源被强制 `no-store`

`main.py` 的中间件对**所有**响应写 `Cache-Control: no-store`，包括 Vite 生成的 `assets/index-*.js`。实测：

```
GET /assets/index-zMH3-ngh.js  →  content-length: 545125,  cache-control: no-store
```

首屏 545 KB JS + 215 KB CSS，每次打开都重下；走公网隧道时尤其明显。`UI_PATCH_README.md` 第 7 条宣称「给带哈希的静态 JS/CSS 启用长期缓存」，但服务端响应头并没有实现这一点。

### P1-2 日志、通知、会话只增不减

`events` 没有保留策略，定时任务每次轮询都写入，`notifications`、`sessions` 同理。这台机器一天就积累了 101 条日志；按每天几次定时扫描算，一年后数据库和它的每次备份都会带上全部历史。

### P1-3 `ADMIN_PASSWORD` 改了不起作用，且没有任何提示

环境变量只在首次建库时生效。之后在 Compose 里改密码是完全静默的——用户会以为密码已经换了。本次实测该部署里 Compose 的密码与库内一致，属于运气好，不是机制保障。

### P2 其他（本次未改）

- **公网明文 HTTP**：9000 端口经 frp 直接对公网开放，`SESSION_COOKIE_SECURE=false`，管理密码和会话 Cookie 都是明文传输。建议前面挂一层 TLS 反向代理，或只在内网/VPN 内暴露。
- **Compose 明文密码**：`docker-compose.yml` 里 `ADMIN_PASSWORD` 是可读明文，且该文件进了 `SHA256SUMS`（哈希不含明文，但文件本身在项目目录里）。既然是单机自用，风险可接受，但别把项目目录打包外发。
- **`/api/v1/dashboard` 全表扫描**：每次打开总览都遍历 `organized_files` 全部行并解析 JSON，再串行请求 Emby。数据量小无感，几千条后会明显变慢，且阻塞事件循环。可改为 SQL 聚合 + 短缓存。
- **`/api/v1/tasks/events`**：每 2 秒对最多 30 个任务重建 `job_public`（每个任务多次查询）。多标签页同时打开时数据库压力线性上升。
- **115 全局 0.35 秒串行节流**：`Cloud115.call` 对普通请求统一限速，大目录首次扫描会慢（几千文件量级）。这是有意的保护，不建议调，但值得知道瓶颈在哪。
- **主包 545 KB**：`index-*.js` 超过 Vite 的 500 KB 警告线，Element Plus 全量引入。后续可按页面拆包。
- **无修改密码入口**：目前改密码只能改数据库里的 `meta.salt/password_hash`，建议后续加一个受 CSRF 保护的改密接口。

## 三、本次改动

| 项 | 文件 | 做法 |
| --- | --- | --- |
| 前端镜像内构建 | `Dockerfile`、`.dockerignore`、`update.sh` | 恢复 `node:22-alpine` 阶段，`COPY --from=frontend /build/dist ./static`；`.dockerignore` 排除 `frontend/dist` 防止误用旧包；`update.sh` 改为要求 `frontend/src` 与 lock 文件，dist 可选 |
| 登录限流 | `main.py` | 先校验密码，失败才计数；正确密码永不被 429 |
| 静态缓存 | `main.py` | `/assets/*` 返回 `public, max-age=31536000, immutable`（含 304），HTML 与 API 保持 `no-store` |
| 记录保留 | `store.py`、`organizer.py` | 新增 `Store.prune()`：日志 90 天且最多 2 万条、已读通知 180 天、过期会话；调度器每小时执行一次，失败不影响调度状态；新增 `events(at)`、`notifications(at)` 索引 |
| 配置提示 | `main.py` | 环境变量密码与库内不一致时写入一次性系统日志 |
| 播放限流字典 | `main.py` | 条目超过 512 时清理空桶，避免长跑无界增长 |
| 版本 | 全局 | 0.6.4 → 0.6.5，新增 `docs/RELEASE_0.6.5.md`、`backend/tests/test_ops065.py` |

## 四、验证

1. **回归**：NAS 容器内跑完 `pytest`，410 项通过（原 405 + 新增 5），耗时 4 分 50 秒。
2. **构建**：`docker compose build` 成功，镜像内 `npm ci + vite build` 耗时约 35 秒；构建前先用同一份源码做过对照，产物哈希与线上 `dist` 完全一致，确认切换构建方式不会带来界面差异。
3. **影子实例**：用线上数据库的 SQLite 副本（`sqlite3.backup`，不改动生产库）在 `127.0.0.1:9100` 起 0.6.5，关闭 TG 与 Bot 轮询后跑了 25 项检查，全部通过：版本、HTML/资源缓存头与 304、8 次失败后的限流与「限流中正确密码仍可登录」、16 个只读接口、115 根目录读取、启动事件、无 error 事件。
4. **切换生产**：`docker compose up -d --build`，容器健康，`/health/ready` 返回 `0.6.5`，首页与资源头符合预期；任务、播放映射、通知、Token、115 与 Emby 配置均未变动。
5. **回滚**：旧镜像 `strimkeep:0.6.4` 保留；项目在改动前已打包 `/path/to/StrimKeep-backup-20261025-093149.tgz`；代码改动见 commit `278dd60`。

## 五、Emby 链路复核

**结论：0.6.4 改的 Emby 连接代码没问题，不需要回滚；断掉的是 Emby 媒体库路径。**

连接侧逐项实测（容器内直连，非模拟）：

| 检查 | 结果 |
| --- | --- |
| `saved_base` | `http://192.0.2.10:8096/emby`，指纹与记录的 server id 一致 |
| `probe` → `/System/Info` | 200，`NAS` / `4.10.2.0` |
| `/Items/Counts` | 5436 电影、3868 剧集、136779 单集 |
| `/Sessions` | 2 条会话 |
| `/api/v1/dashboard` | 200，`emby.connected=true`，`error` 为空 |
| `/api/v1/connections/emby/test` | 200，`connected=true` |

真正的断点在媒体库这一侧：Emby 库「影片」（movies，Id 936507）的唯一路径是
`/strimkeep/StrimKeep媒体库/电影/欧美电影`，而 0.6.3 已经把本地输出目录改名为 `影视媒体库`。
Emby 日志里写得很直白：`Skipping realtime monitor for /strimkeep/StrimKeep媒体库/电影/欧美电影 because the path is currently not available`。
StrimKeep 新生成的 STRM 全在 `影视媒体库/` 下，Emby 永远不会扫到，入库核对只能干等到 30 分钟超时，然后报「未确认」。这和 Emby 连接通道、API Key、挂载都没有关系。

修复过程（全部通过 Emby 官方 API，可逆）：

1. `POST /Library/VirtualFolders/Paths` 给「影片」加上 `/strimkeep/影视媒体库/电影`；
2. `POST /Library/Media/Updated` 触发靶向扫描（注意 `Items/{id}/Refresh` 的 `ImageRefreshMode=None` 在 4.10 会 400，别用）；
3. 扫描约 90 秒后 3 个条目按新路径入库；
4. `POST /Library/VirtualFolders/Paths/Delete` 删除失效的旧路径。

复核结果：库内只剩 `/strimkeep/影视媒体库/电影` 一个根，3 个条目的 `Path` 均为新路径，`MediaSources` 指向 `http://192.0.2.10:9000/play/<pid>`；用 StrimKeep 自己的 `EmbyDelivery.matches()` 对 3 条播放映射实测 **3/3 命中**；`/play/...` 实测 302 跳转到 115 CDN。至此「STRM → Emby 索引 → 播放」整条链闭合。

**还没覆盖的两点**：

- 剧集/动漫/综艺/纪录片/儿童节目 目前没有 StrimKeep 产出，对应 Emby 库还没加 `影视媒体库` 下的路径。开始整理剧集时需要补 `/strimkeep/影视媒体库/剧集/欧美剧集` 等，否则同样静默不入库。
- `playback_base` 是 `http://192.0.2.10:9000`（内网地址）。通过 frp 在外网看 Emby 时，播放器拿到的也是这个内网地址，会打不开；要在外网看，需把 `playback_base` 改成公网可达地址并全量重写一遍 STRM。

## 六、下次可以继续的方向

1. 给 9000 套一层 HTTPS 反向代理（或只允许内网访问），再把 `SESSION_COOKIE_SECURE` 设为 `true`。
2. 管理界面加「修改密码」，替代现在只能改库的做法。
3. `dashboard` 改 SQL 聚合 + 30 秒缓存；`tasks/events` 改为增量查询。
4. 前端按页面拆包，压低首屏体积。
5. 真机跑一次完整的「转存 → 整理 → STRM → Emby 入库」，核对 0.6.5 在真实链路上的行为（本次的连接、索引与播放跳转均已单独验证，但没有新增云端文件）。
