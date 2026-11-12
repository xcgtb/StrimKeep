# StrimKeep 0.6.0 — UI/UX 覆盖修复包

适用基线：用户提供的 `1.zip` / StrimKeep 0.6.0；**不是一个全新安装包**。请勿把它用于不同版本的项目。

## 修复内容

1. 主导航拆分“媒体管理 / 运行与系统”；115 账号与链接转存、任务中心与日志、通知分类与系统设置有可直接点击的独立入口。
2. 已访问的总览、115、整理、STRM、系统设置会保留组件实例。切换侧栏不再每次销毁这些页面、重新拉取它们的所有配置。任务与实时日志按需加载，避免后台同时保有多条 SSE 连接。
3. 缓存页面处于非当前栏目时暂停定时轮询；重新进入后恢复。保留未保存的页面草稿，切换时提醒，刷新会丢失未保存修改。
4. 修正折叠卡片无视 `defaultOpen` 的问题；高级设置可以默认收起。
5. 影视整理、STRM 的“查看任务进度”指向任务中心；转存页面的 TG 通知设置跳转到具体通知标签。
6. 支持侧栏路径 `#/organize`、`#/sync`、`#/tasks` 等以及浏览器历史导航；优化菜单密度、分组、手机端和可访问性提示。
7. 给带哈希的静态 JS/CSS 启用长期缓存，而 API、HTML 保持 `no-store`；生成了新静态资源文件名，避免覆盖后仍读取旧包。
8. 在 **前端源码**中改用异步组件，为未来按路由分包做好准备。本次交付的已编译 JS 包仍以原版构建产物为基础并直接修补，**首次加载的 JS 体积尚未减少**。

## 覆盖方式（飞牛 NAS / Docker Compose）

先在 NAS 上备份现有项目，尤其是 `.env`、数据目录与数据库。不要删除旧目录，不要清理 Docker volume。

把 ZIP 上传到 NAS，例如 `/path/to/StrimKeep-0.6.0-UI-UX-patch.zip`。如果实际项目目录是 `/path/to/StrimKeep`，执行：

```bash
cd '/path/to/StrimKeep'
# 备份本次会覆盖的程序目录（实际运行数据和 .env 不在本次更新范围）
tar -czf "../StrimKeep-ui-before-$(date +%Y%m%d-%H%M%S).tar.gz" frontend/src frontend/dist backend/strimkeep/main.py
unzip -oq '../StrimKeep-0.6.0-UI-UX-patch.zip' -d .
docker compose up -d --build --no-deps strimkeep
docker compose ps
```

如果项目路径不同，按实际情况修改上面的路径。如果你运行的是 GitHub/GHCR 远程镜像、Docker Compose 不含 `build: .`，则不能用本地源码 `--build` 生效，需先调整发布方式。本包针对原 `1.zip` 所附的源码构建型 Compose。

覆盖包**不包含** `.env`、数据库、媒体/STRM、日志、`docker-compose.yml`、管理员密码或登录密钥，也不更改 115/Emby 的核心整理逻辑。覆盖后用浏览器硬刷新一次，验证顶部与侧边栏出现“任务中心”“链接转存”等入口。

## 已完成 / 未完成验证

- 已完成：修改后打包 JS `node --check`、TypeScript `<script setup>` 语法解析、Python 后端 `compileall`、HTML 静态资源引用完整性检查。
- 未完成：本环境无外网 npm 依赖，无法执行完整 `vite build` / `vue-tsc`；自动浏览器运行测试被浏览器策略拦截；后端 pytest 收集时缺少 `httpx_request` 依赖；未连接真实 NAS、115、Emby。因此请先在 NAS 进行网页手工验收，再继续日常任务。
- 建议检查：侧边栏切换速度、修改配置后切换再返回能否保留草稿、任务中心是否直达、TG 分类是否直达、STRM 同步按钮是否仍能执行、日志实时连接是否正常。

## 回退

如发生界面问题，恢复第一步备份的 `frontend/src`、`frontend/dist`、`backend/strimkeep/main.py`，再执行 `docker compose up -d --build --no-deps strimkeep`。回退程序不需要重置 `.env` 或删除数据目录。
