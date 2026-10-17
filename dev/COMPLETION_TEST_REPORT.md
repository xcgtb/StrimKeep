# StrimKeep v1.4.12 整合候选 · 验证报告（2026-10-17）

> 这是一份源码候选包，不是已发布的镜像；未触碰 GitHub main、飞牛正式容器、115/Emby 凭据或媒体内容。

## 新增链路

- 115 磁力/ed2k 在提交时**明确选中自动整理**才进入离线接续表；依据 115 返回的 40 位任务哈希、接收目录、同目录任务状态、账号和目录版本进行只读核验，通过后一次性创建整理任务；没有可靠证据时不自动执行，不重发原下载操作。手动安全停止可暂停尚未接续的任务。
- 分享免转存 STRM 在开启整理八维升级模式时，先选优并核对已注册版本，再按实际快照二次校验；更优版本写入**独立版本目录**，低于/等于现有版本不重复写入。**保留已有 STRM 与 115 分享源文件**；不会在 Emby 核验前自动删除旧版本。
- UI 显示离线接续的等待、整理中、完成、待人工确认等状态；本项目已有的一条龙就绪检查仍区分 STRM 写出与 Emby 真实入库确认。

## 实际执行并通过

1. `pytest -q dev/tests/test_generated_only_governance_scope.py dev/tests/test_cloud_share_versions.py dev/tests/test_cloud_offline_handoff.py dev/tests/test_cloud_receiving.py dev/tests/test_architecture_boundaries.py dev/tests/test_quality_defaults.py dev/tests/test_domain_quality.py dev/tests/test_domain_governance.py dev/tests/test_static_checks.py dev/tests/test_release_check.py dev/tests/test_ci_publishing.py dev/tests/test_image_version.py dev/tests/test_workspace_readiness_regression.py`: **154 passed**（隔离 SQLite、分享 STRM 真实写盘、离线只读核验/失败保护）。
2. `python dev/tools/static_check.py`：**PASS**。
3. `python dev/tools/release_check.py`：**PASS**；ZIP 解包后 `--archive` 再运行。
4. `python -m compileall -q app`：**PASS**。
5. 49 个 Vue SFC 的 TypeScript AST 语法解析：**49 parsed, 0 errors**（**不等于** Vue 类型检查/编译）。
6. `python dev/checks/verify_release_startup.py`：新库+历史库 **2/2**，真实隔离本地 HTTP、重启、鉴权和数据库读写。打包前复跑 3 次均通过；此前有两次偶发“重启后的总览缓存时间戳变化”断言失败，需留意 CI 中是否复现，**不能认定稳定性问题已彻底修复**。
7. `python dev/checks/verify_docs_schema_compat.py`：旧/新 SQLite 文档兼容 **4/4**。
8. 完整源码 ZIP 排除缓存/敏感运行数据后，重新解压，校验 `MANIFEST.sha256`，并执行发布边界检查。

## 未通过 / 未执行，不可当作发布验收

- `pytest dev/tests/` **全量测试**：本环境 Python 缺 `p115client`、`guessit` 等依赖，且 DNS 无法访问包管理服务器，测试收集或执行被阻断；不能将定向154项算成全量测试通过。
- `npm ci`、`vue-tsc`、Vite 构建、Vitest、Playwright **完整前端测试**：Node 22.16.0 低于仓库要求，缺少 npm 依赖且无法联网安装。仅完成脚本 AST 检查。
- NAS 上真实 115 账号登录、频道读取、磁力下载完成触发、云文件移动、STRM 302 播放、Emby 入库确认、Telegram 消息、双库治理源文件清理均**没有联机测试**；不得据此声称真实端到端已通过。
- 分享洗版采用安全的**非破坏性发布**，已完成的是版本选优/独立发布/持久回执，并非旧引用的自动清理。现有播放链只有在实际 Emby 入库核实后，才应另行人工确认清理。

## CI 放行要求

在网络/依赖正常的 Git 工作区（**不是**生产媒体目录）执行：

```bash
python -m pip install -r dev/requirements.txt
python dev/tools/static_check.py
python dev/tools/release_check.py
pytest dev/tests/ -q
python frontend/tests/backend_smoke.py
npm ci --prefix frontend
npm --prefix frontend run format:check
npm --prefix frontend test
npm --prefix frontend run build
node dev/tools/verify_frontend.cjs
cd frontend && npx playwright install --with-deps chromium --only-shell && npm run test:e2e && npm run test:production
```

之后还需用隔离的 115 测试账号/目录和 Emby 测试库核实真实任务链路，不能以模拟测试代替。GitHub Actions `.github/workflows/tests.yml` 有相关检查；只有通过并复核删除保护后才考虑打**新** tag。不要覆盖原有 tag、Git main 或运行目录。
