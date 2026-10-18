# StrimKeep Vue 3 前端

正式首页使用 Vue 3 + TypeScript + Vite + Pinia + Vue Router（Hash 路由），共 27 个业务页面。本轮以用户确认的「StrimKeep 10 模块交互原型·目录流程修正版」HTML 为视觉和交互基线：252px 侧栏、双栏工作台、统一深浅主题、手机抽屉导航。原型中的演示数据和演示按钮改接现有 Python API；功能补齐记录见 [本轮实现与验证](../docs/vue3-prototype-replica-20261018.md)。

## 使用完整源码包

后端接口与功能覆盖记录见 [前后端功能审计](../docs/frontend-backend-parity-v1411.md)。片库变更使用 SSE 提示配合变化接口，断线每 15 秒检查；NAS 目录检索、分类统计和观看进度由用户在对应页主动读取。

包内 `static/` 已编译，可直接随现有 Python 应用运行。Dockerfile 会在 Node 构建阶段重新编译 Vue，运行阶段仅使用 Python；NAS 无需安装 Node。访问 `http://NAS地址:8321/`，使用原管理账号登录。

本轮源码包同时包含只读容器目录浏览接口和扫描错误日志明细，需要前后端一起更新。建议将完整源码放在独立目录构建，沿用现有部署配置与数据。**整个替换 `static/` 目录**，清除旧的 `static/js/`、`static/css/` 和临时 `static/vue3/`，避免保留无用旧文件。原有部署配置、数据目录、数据库、密码和媒体挂载保持使用原值。预编译文件不是可双击运行的离线网页，需要当前 StrimKeep 后端。

使用现有 Docker 部署时，在完整源码根目录重建镜像，并沿用原部署目录、环境变量、挂载和端口。直接拉取现有远程镜像不会带上这份尚未发布的源码修改。

## 本地开发

Node 24（最低 22.18）、Python 3.12。

```bash
npm ci --prefix frontend
npm --prefix frontend run dev
```

默认代理 `/api` 到 `http://127.0.0.1:8321`。可通过 `STRIMKEEP_API_TARGET` 指定开发后端；不要把生产密码写进源码。API 使用同源 HttpOnly 会话，只有主题偏好进入 localStorage。

## 构建与检查

```bash
npm --prefix frontend run format:check
npm --prefix frontend test
npm --prefix frontend run build
node dev/tools/verify_frontend.cjs
cd frontend
npx playwright install --with-deps chromium --only-shell
npm run test:e2e
npm run test:production
```

`build` 整体重建 `static/`，Vite 使用 `/static/` 资源路径。图标和内置图片原文件位于 `frontend/public/`，构建时复制。路由采用 `/#/...`，沿用后端 `/` 与静态文件服务，无需修改后端路由或增加兼容层。

真实后端的隔离接口检查：

```bash
python frontend/tests/backend_smoke.py
pytest dev/tests/ -q
```

浏览器交互测试使用受控接口数据；隔离后端检查使用临时数据库、空媒体目录和测试密码。它们不代表已在实际 115、NAS、Emby 或 Telegram 环境验收。

功能与验证范围见 [完整迁移记录](../docs/frontend-vue3-progress.md)。
