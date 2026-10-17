# StrimKeep v1.4.12 全仓整改与验证记录

> 本文件对应用户上传的 `StrimKeep-source.zip` 代码快照，**不是发布标签，不表示线上 NAS 已测试**。Docker 正式部署、GitHub main 和媒体数据未修改。

## 实际修改

- **导航**：我的网盘侧边栏收敛为115配置、频道、资源接收、影视整理、分享STRM和文件管理；离线、自动化、STRM、命名、八维等入口通过页面内导航及搜索保留。
- **治理**：扫描页显示四类根目录的参与状态、挂载状态、扫描失败原因、可读的删除/保护依据与个人/分享路径；原计划扫描/预演/确认删除链保持原样。
- **自生成-only 治理**：`app/cloud/settings.py` 增加 `governance_generated_only`（默认 `false`，需先开启 `governance_generated_enabled`）。用户主动启用后只扫描 `output_root` 和 `share_output_root`，不扫描旧挂载。两个根都必须可见且不与旧路径/彼此重叠；计划规则指纹包含该模式，切换后需重新扫描。
- **媒体总览范围一致性（本轮）**：`app/stats.py` 总览计数、分类统计、增量目录更新和 `app/overview.py` 缓存恢复共用显式治理根；自生成-only 不再遍历旧目录，混合模式分别统计多根。切换模式时不复用上一个库范围的数据；设备号/inode 变化、挂载丢失时旧计数可作为带过期警告的历史快照展示，但不当作实时核验结果，重新扫描失败不会覆盖上次成功的 SQLite 事实。**没有改变真实媒体文件及删除授权**。
- **索引接入**：`app/lib.py` 和 `app/subscribe.py` 的只读磁盘事实在启用自生成库时复用已启用的治理库，以支持片库和追更的磁盘事实兜底。**Emby 库映射/源文件删除仍需按真实容器路径逐项验收。**
- **115频道接收与云下载接续**：频道已保存 `organize` 开关传到统一接收流程；115 分享在转存回执核实后可直接整理。磁力/ed2k 只有用户明确勾选“接收后整理”才进入 `cloud_offline_handoffs` 持久化待核验列表。读取115任务状态匹配40位哈希及保存目录、同目录无未完成任务、账号/目录版本一致且归档不嵌套，才排队执行；不重复下发云下载。无法核验哈希、账号变化、手动停止均保守暂停并显示中文原因。未勾选自动整理的任务仍可使用定时整理规则。
- **影视整理/入库**：新增本地配置就绪清单；缺少开启状态、个人库 Emby 连接或路径映射时，清单为未就绪。校验仅是本地配置自检，不包含在线 Emby 身份验证。
- **片库映射**：改善已加载/全筛选选择逻辑，切换筛选时清空旧选择；多选状态下点击海报切换选择，降低详情与多选误触。
- **任务回执**：展示进度阶段、百分比和简明原因；编号/底层字段默认折叠供排查。
- **七维与默认值**：维度开关、档位入口更清楚；新安装平局默认保留个人库，已有落盘配置按旧行为处理，避免无声改变已有治理结果。

## 已执行的离线检查（此源代码包）

1. `python dev/tools/static_check.py`：通过。
2. `python -m compileall -q app`：通过。
3. 独立 TypeScript parser 读取 `frontend/src/**/*.vue` 的全部49个 `<script setup lang="ts">`：49个脚本语法解析通过（仅语法，不含 vue-tsc 类型检查）。
4. 在本轮干净测试运行中，以下定向回归 **154 passed**：

   ```bash
   pytest -q dev/tests/test_generated_only_governance_scope.py \
     dev/tests/test_cloud_share_versions.py dev/tests/test_cloud_offline_handoff.py \
     dev/tests/test_cloud_receiving.py dev/tests/test_architecture_boundaries.py \
     dev/tests/test_quality_defaults.py dev/tests/test_domain_quality.py \
     dev/tests/test_domain_governance.py dev/tests/test_static_checks.py \
     dev/tests/test_release_check.py dev/tests/test_ci_publishing.py \
     dev/tests/test_image_version.py dev/tests/test_workspace_readiness_regression.py
   ```
   新增测试覆盖云下载完成后一次性接续、已取消自动整理、跨目录/同目录未完成任务、账号配置变化、停止操作、分享八维择优、旧引用保护、预览过期时二次核验，以及独立 SQLite + STRM 实际写出。
5. `python dev/checks/verify_release_startup.py`：2/2（真实本地 HTTP、临时数据库写入与重启）。
6. `python dev/checks/verify_docs_schema_compat.py`：4/4（新旧文档结构迁移）。
7. 打包时 `python dev/tools/release_check.py --archive` 以及 ZIP 内容的禁止文件检查。

## 不能声明已通过的项目

- `npm ci / npm run build / vue-tsc / vitest / Playwright`：容器 Node 为22.16.0（仓库要求≥22.18）且 npm 离线缓存缺包；DNS 无法连接包仓库，因此**未完成真正 Vue3 编译、E2E**。
- 全量 `pytest dev/tests`：当前 Python 3.13 运行环境缺少 `p115client`、`guessit` 等依赖，部分用例报 ImportError / ModuleNotFoundError；外网 DNS 不可用，无法下载依赖。未通过全仓回归，不把154项定向测试冒充全部测试。
- 115 真实 Cookie、频道、磁力云下载完成、媒体移动、分享免转存真实链接和视频播放、Emby 入库核实、302 播放、Telegram 发送及源文件删除：**均未执行线上验证**。不能用隔离测试当作实际网盘运行成功。
- 分享免转存已支持**非破坏性版本选优**，但旧引用不会在新 STRM 写入后自动删除；它仍需要先核实真实 Emby 入库，再人工确认旧引用去留。离线接续采取每180秒只读轮询的机制，并非115推送回调；未返回完整哈希/下载历史不可查询时不会猜测结果。无真实账号端到端验收前，不能宣称全自动一条龙已在 NAS 上验证。

## 本轮追加验证（总览与治理一致性）

- `pytest -q dev/tests/test_generated_only_governance_scope.py`：**11 passed**，包含仅新库、混合库、缓存切换与切回、增量目录变化、同一路径换挂载识别。
- 上述测试合并 Emby 回调、原生产稳定性、115 离线接续、分享版本发布等定向回归：**209 passed**。
- `python frontend/tests/backend_smoke.py`：隔离 FastAPI 实际会话/29 个读取接口检查通过。
- `python dev/checks/verify_release_startup.py`：隔离 HTTP 启动、旧库迁移、重启保留缓存 **2/2 通过**；过去报告提及的偶发缓存时间戳断言未在本次复测中重现，但不能认定根因已完全排除。
- `pytest dev/tests/ --collect-only -q`：**1667 项收集成功、另 3 个文件在收集阶段被缺失的 `p115client` 依赖阻断**。跳过这 3 个文件后全量首个业务失败位于自动同步任务：`guessit` 不存在，触发 `ModuleNotFoundError`；这不能算通过全量测试。
- Vue3 的 npm/Vite/Vitest/Playwright 仍缺依赖及合适 Node 版本，**未完成完整前端构建**；只完成 49 个 Vue SFC 的 TypeScript 脚本语法解析。**真实 115/Emby 端到端验证未执行**。

## 使用建议

这是**完整源码候选包**，用于覆盖 `/vol1/1000/git/StrimKeep` 的 Git 工作区。先备份 Git 工作区并查看本次变更清单，**不要直接覆盖生产 `/vol1/1000/docker/StrimKeep`**；运行与仓库CI一致的 Python、Vue、浏览器测试并通过后，再用新 tag 发布。现有 tag 不得覆盖，未经确认不执行治理删除。`static/` 是上传源码时原有的历史静态构建，Dockerfile 在镜像构建时由 `frontend/` 重新生成最新静态产物；请不要通过直接运行旧静态文件判断新 Vue 页面是否生效。
