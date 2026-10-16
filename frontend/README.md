# StrimKeep Vue 3 前端

正式首页使用 Vue 3 + TypeScript + Vite + Pinia + Vue Router（Hash 路由），共 24 个业务页面。资源与工具页参考 dian115 演示的分组导航、页签与功能卡片组织；没有复制其私有应用代码，也没有添加后端不存在的插件安装器。

## 使用完整源码包

包内 `static/` 已编译，可直接随现有 Python 应用或 Dockerfile 运行；运行环境不需要 Node。访问 `http://NAS地址:8321/`，使用原管理账号登录。

在现有源码目录更新时，用包中的 `frontend/`、`static/`、相关文档和 CI 文件替换对应源码。**整个替换 `static/` 目录**，清除旧的 `static/js/`、`static/css/` 和临时 `static/vue3/`，避免保留无用旧文件。原有部署配置、数据目录、数据库、密码和媒体挂载保持使用原值。预编译文件不是可双击运行的离线网页，需要当前 StrimKeep 后端。

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
