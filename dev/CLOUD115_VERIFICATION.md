# 115 工作台验收记录

本文件为 v1.3.0 的历史验收记录。当前未发布阶段的 1245 项回归及新增能力见 [阶段记录](../docs/CLOUD115_NEXT_STAGE.md)。


日期：2026-10-14。源码基线为 v1.2.12 的 `d607fd978a7ce9f9b29398377d55f88a4a9f6a53`。本记录用于 v1.3.0 源码发布准备；不能据此认定已推送 GitHub、构建镜像或部署飞牛。

| 检查 | 结果 |
| --- | --- |
| Python 全量 `pytest dev/tests/ -q` | 1,176 通过；包含 52 项新增网盘测试 |
| 前端 `node dev/tools/verify_frontend.cjs` | 44 组通过，含新工作台账号切换、晚到响应及二维码过期重启检查 |
| 静态名称、源码发布边界、资源内容版本、Git diff 空白检查 | 通过 |
| 仅运行依赖：历史数据库 `verify_docs_schema_compat.py` | 4/4 通过，覆盖 package/CLI 与两种旧 schema |
| 仅运行依赖：本地 HTTP `verify_release_startup.py` | 新库、历史库和失败迁移诊断检查通过 |
| 仅运行依赖：`verify_sqlite_single_store.py` | 10/10 通过 |
| 仅运行依赖：`verify_explore_background.py` | 8/8 通过 |

运行环境为 Python 3.12，安装本源码 `requirements.txt` 中固定的运行依赖；运行依赖环境未安装 pytest 或 Black 等开发工具。历史数据库验证器禁止加载 FastAPI/Starlette TestClient；本地 HTTP 验收不使用 TestClient。httpx 为本次反代功能的生产依赖，因此该环境确实安装 httpx。测试环境另装 pytest。

全量测试有一条 FastAPI TestClient 使用 httpx 的弃用提示，测试通过；不因此给生产环境加入 httpx2 等未验证依赖。前端检查使用 DOM 替身，没有宣称完成手机视觉验收。

新增测试覆盖：实际 SDK 导入和写入参数契约，Web 会话与 CSRF，扫码成功/取消/归属，加密/密钥丢失，预览单次领取，真实 SQLite 与文件落盘，模拟远端改名移动，源快照变化，同名文件保护，转存源 ID 与目标 ID，部分接收拒绝继续，超时只读核实，授权版本变化，撤销不可自动恢复，STRM 路径更新，外部文件保护及连续两次扫描清理，来源变化与无效签名拒绝网络调用，当前 Emby 鉴权、多来源/用户隔离、Range Cookie 代理、WebSocket 转发、定时队列去重、在途任务重启保护、通知仅明确启用后发送、季集修正及 NFO 的剧集 ID/单集 ID 区分、原治理 TMDB 标记识别、同步沿用已确认作品身份而不重新靠标题匹配、相同 ID 内容变化禁止静默重新绑定。

模拟服务没有访问用户网盘、删除用户媒体或发送真实 Telegram 消息。尚未完成真实 115 账号与 CDN 播放联调、不同 Emby 客户端、Docker 多架构构建和 NAS 部署。OpenAPI、其他网盘、官方文件删除与旧库身份迁移尚未实现。

可复现命令见 [开发说明](README.md)，使用边界见 [工作台说明](../docs/CLOUD115_WORKBENCH.md)，闭源分发的未决许可问题见 [依赖记录](../docs/CLOUD115_DEPENDENCIES.md)。
