# P0 + P1 合并补丁｜阶段验收报告

2026-10-10 · 本地验收环境（**不等于飞牛现场验收**）

## 测试通过

- Python：`806 passed`，涵盖原有七维治理/安全保护、入库 P0、片库 P1、失败保留等。
- 前端：`28` 组 JavaScript 测试通过。新覆盖首次收到版本事件不漏更新、映射文字/角标替换但保留海报图片节点。
- `dev/checks/verify_sqlite_single_store.py`：`10/10`，历史及当前 SQLite 存储。
- `dev/checks/verify_docs_schema_compat.py`：`4/4`，历史文档结构兼容。
- `dev/checks/verify_release_startup.py`：`2/2`，隔离数据下实际 HTTP 启动、鉴权、静态资源与重启。
- `dev/checks/verify_p1_stream_http.py`：实际 HTTP/SSE 持续流、匿名访问拒绝、Basic 和 Cookie 鉴权、事件通知与保守完整对账通过。
- `dev/checks/verify_p1_nas_readonly.py`：以本地模拟 Emby API + 临时 STRM 目录实跑，新剧、补集、超时保留、入库双轮增量均通过。
- `dev/tools/static_check.py`、`dev/tools/release_check.py` 和 GitHub main 基线补丁覆盖一致性检查：通过。

## 本轮修正的专项问题

1. 浏览器首次接收版本号时不再可能跳过恰好发生的新剧集更新。
2. 新剧首次建档及跨库强匹配合并后，`in_local` / `in_share` 与实际季集集合一致；已验证的新来源路径保留在 `paths`。
3. 新剧未知 TMDB 对照时仍标记 `pending`，不能冒充“完整”，也不能作为治理删除依据。
4. 校验脚本补充 Safari 使用的 Cookie 鉴权 SSE 流测试；JS 内容修改后同步静态文件指纹。

## 未完成（必须到用户的飞牛执行）

- P1 镜像在飞牛 Docker 引擎的构建、真实 Emby + NAS STRM 只读挂载试验（脚本已准备）。
- Safari / 多标签页实际视觉与断线重连验收（Node 模拟不等于真实浏览器）。
- 同一实际媒体文件下 P0 与 P1 的治理计划逐项对比（仅进行预览，禁止测试性删除）。
- P1 全面运行 30–60 分钟的真实 RSS/CPU 变化及 5 分钟轮询情况。

以上项目没有经过现场输出前，不应说“P1 所有验收都完成”或据此覆盖生产 P0。

现场操作见 `docs/P1_NAS_ACCEPTANCE.md`，运行器为 `dev/checks/run_p1_nas_readonly.sh`。


## 2026-10-10 现场失败后的兼容修正（NAS 待复测）

现场运行器返回 `HTTP Error 404`：`/Items/931319` 在当前 Emby 上无法读取，
虽然同一剧可通过 `/Items` 搜索和 `ParentId` 列表返回 5 集。该问题不属于 Python 模拟通过范围。
P1 新剧建档改用 Emby 支持的 `GET /Items?Ids=<SeriesId>`，并要求结果唯一且身份完全匹配。
追加 HTTP 模拟器（详情路径明确 404）、ID 错误/忽略过滤/多结果/不完整结果的拒绝写入测试。
**本地** Python 813 项与前端 28 组通过；**NAS 新剧建档仍未通过复测**。
