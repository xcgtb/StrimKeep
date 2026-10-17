# 我的网盘 UI V2（试用版 · v1.3.6 基线）

基线：`xcgtb/StrimKeep` main `ac31b1274fa94810ae49419cb3e2316d20d5f124`，2026-10-15。

## 范围

- 新增「工作概览」：常用入口、已保存账号数、最近任务和待处理任务。
- 简化工作区左侧导航；手机使用横向导航。
- 「资源接收」区分常规分享转存、分享免转存 STRM、磁力/ED2K/秒传。
- 「影视整理」拆分六个操作页签：开始整理、识别与命名、分类归档、八维洗版、定时任务、过滤与高级。
- 保持原有 DOM 字段 ID、`data-cwa` / `data-svc` / `data-link-action` 与请求入口不变；不需要修改 SQLite、API、Docker/YML 或 Python 依赖。
- 保留 StrimKeep 现有蓝色/暗色主题变量，不重新定义主题配色。
- 新布局保留所有后端功能。初次使用或修改设置，请先点「保存当前整理配置」，再点「识别并预览」。

## 安装

在飞牛 NAS SSH 中（先解压到 Git 工作区目录，再执行）：

```bash
cd /vol1/1000/git/StrimKeep
python3 dev/tools/apply_my_drive_ui_v2.py
node --check static/js/cloud-v2-layout.js
node dev/tests/verify_cloud_v2_layout.cjs
node dev/tools/update_frontend_asset_versions.cjs --check
```

`apply_my_drive_ui_v2.py` 在 `static/index.html` 插入两个有哈希参数的静态资源引用；同时将 `verify_frontend_assets.cjs` 的旧脚本数量检查从 18 调整到 19；修改 `CHANGELOG.md` 未发布项。重复执行不会叠加引用。

创建正式标签前，将新增内容移入版本日志：

```bash
python3 dev/tools/apply_my_drive_ui_v2.py --release 1.3.7
```

之后核对差异、提交并推送 `main`，再推送新的 `v1.3.7` 标签触发正式镜像。**已有标签不得重打或移动。**

## 验证结果与限制

- 本地使用 Chromium + Playwright 对 1440 px 桌面和 390 px 手机进行 DOM/交互演练：概览、账号计数、任务读取、资源接收三模式、影视整理六页签、导航回退及横向溢出均通过。
- 该浏览器演练使用与仓库 DOM 键名对应的模拟页面和虚构 API 响应，而非实际生产容器中的整页验收。
- 未登录真实 115、未调用写接口、未测试 Emby 实播和 Docker 镜像。
- 当前改版仅是 UI 第一批，频道规则、播放连接和任务列表暂使用旧组件布局；后续可逐页继续重构。

## 撤回 UI

不想保留新版时删除 `index.html` 中 `cloud-v2-layout.css`、`cloud-v2-layout.js` 两个引用，恢复 `dev/tests/verify_frontend_assets.cjs` 的脚本数 18，并重新运行 `node dev/tools/update_frontend_asset_versions.cjs`。无需回滚数据库。
