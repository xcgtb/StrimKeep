# 非115工作台恢复（v1.4.7 参考）

## 目标与基线

本次以用户上传的 `StrimKeep-source(1).zip`（v1.4.12 源码）为唯一修改基线，参考 GitHub `v1.4.7` 的静态界面。除了115及其联动业务页面，媒体总览、双库治理、片库映射、影视探索、追更订阅、规则设置、入库与晨报、日志、计划存档、残留清理等页面恢复为 v1.4.7 交互。**这不是 Python 治理和删除引擎回滚。**

## 页面组织

- `/`：恢复的 v1.4.7 非115页面；本次源码保存在 `static/classic/`。
- `/workspace/embed?embedded=1#/...`：115 Vue3 页面，供同源旧版页面内嵌；不加载旧版的115脚本。
- `/static/index.html#/...`：完整 Vue3 原始入口仍可独立访问，以便排障和测试。
- 左侧“我的网盘”仅保留六个子项：网盘配置、频道监控、资源接收、影视整理、分享 STRM、文件管理；命名模板、八维洗版、自动整理、STRM 生成和 Emby 入库核验在115区域内部工具导航。

## 安全边界

- `app/governance.py`、`app/routers/library.py`、`app/batch_library_delete.py`、`app/deletion_progress.py` 及全部 `app/cloud/` 未从老版本覆盖。
- 使用现有来源身份校验、确认快照、二次确认、批量删除上限和删除任务进度接口。
- 默认 `X-Frame-Options: DENY`/`frame-ancestors 'none'` 保持不变。**仅** `/workspace/embed?embedded=1` 允许 `SAMEORIGIN`/`frame-ancestors 'self'`，防止第三方嵌入。
- Vue3 仍由 Docker 构建阶段从 `frontend/` 构建；之后 `COPY static/classic/ ./static/classic/` 补回恢复的页面，避免构建覆盖。
- 嵌入页采用服务器端、固定的 CSS 隐藏 Vue3 第二层导航，因此即使源代码中的 Vue3 模块尚未重新构建也可以保留单一导航。

## 验证与部署

```bash
python dev/tools/static_check.py
python dev/tools/release_check.py
pytest -q dev/tests/test_classic_restore.py dev/tests/test_architecture_boundaries.py \
  dev/tests/test_mapping_delete_progress.py dev/tests/test_batch_library_delete.py \
  dev/tests/test_mapping_response.py dev/tests/test_library_first_screen_p4.py \
  dev/tests/test_explore_search.py dev/tests/test_explore_shared_facts.py \
  dev/tests/test_domain_governance.py dev/tests/test_library_pagination_p5.py
python frontend/tests/backend_smoke.py
cd frontend && npm ci && npm run format:check && npm test && npm run build
```

本次隔离环境未完成 npm 依赖安装、完整 Vue3 编译及真实 NAS/115/Emby 联机操作；不能把恢复页面及接口单测当作源文件删除的生产验收。

**注意：** 单纯替换 `static/` 而不更新 Dockerfile 和 `app/routers/auth.py` 无法恢复首页；必须使用完整源码包构建。不要覆盖已有 `v1.5.0` 标签。
