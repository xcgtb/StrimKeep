# 更新记录

每个版本的完整说明在 [`docs/releases/`](docs/releases/)。GitHub Release 使用同一份内容。

## 3.6.59

- 未识别项新增「手动匹配」：搜索/选择 TMDB 作品，电影直接匹配，剧集填写季号与集号；
- 手动匹配会新建一个只处理该文件的整理任务，复用归档、洗版、STRM、Emby 通知链路；
- 原任务记录保留；进行中的任务拒绝手动匹配；匹配失败时文件原位保留，不再转入未识别目录。

## 3.6.58

- 任务详情新增「全部 / 未识别 / 失败 / 已处理」筛选，未识别项单独展示原因；
- 未识别项支持复制原始文件名，便于到「识别测试」核对；
- 新增人工忽略未识别项接口：只标记任务记录为已跳过，不删除/移动云端文件；无其他未完成项时自动结项；
- 进行中的任务拒绝人工处理，避免与 Worker 抢写。

## 3.6.57

- 新增系统健康页（参考 Sonarr Health Checks）：数据库、Worker、命令队列、磁盘、备份、TMDB/Emby/115、失败项一页汇总；
- 前端 API 客户端测试：GET 去重、CSRF、401、超时、非 JSON 响应；
- 后端 Worker 命令分发测试：share_check、STRM rebuild、rollback、失败记录。

## 3.6.56

- 设置页新增自动备份列表、单项下载与刷新；注明保留最近 5 份。

## 3.6.55

- 版本变化时在 schema 迁移前自动备份，保留最近 5 份；
- 新增历史备份列表/下载 API；
- 修复 `last_started_version` JSON 读取导致的重复备份。

## 3.6.54

- 新增可验证的备份/恢复模块（SHA-256、拒绝覆盖、integrity_check）；
- SQLite `busy_timeout` 15 秒，关键写入遇 locked/busy 指数退避重试。

## 3.6.53

- inventory 定时扫描迁到 Worker，生产 Web 不再跑任何后台循环；
- 条目进展会刷新任务租约，超时从 30 分钟放宽到 2 小时；
- DbWorker 修正 running 计数，`/metrics` 暴露分池队列/容量/拒绝数。

## 3.6.52

- CI 增加生产模式 smoke：真实镜像启动 Web+Worker，验证 ready/heartbeat；
- Worker 心跳改为 JSON（lane_faults / worker_alive / scheduler_alive）；
- 新增 `worker_commands` 命令队列：share check、STRM rebuild/rollback 由 Worker 执行。

## 3.6.51

- supervisor 改为分级监督：Web/Worker 独立重启，不再一个挂全容器崩；
- DbWorker 拆 interactive/background 双优先级池；
- 新增任务租约超时回收；
- 双库治理“残次品”文案写明前序缺失/中间断层与具体缺集。

## 3.6.50

- 受控拆分 Web / Worker 两进程，supervisor 监督；
- Web 保留 API/反代，Worker 负责整理、同步、调度、深删、频道、Telegram、Emby 交付等后台。

## 3.6.49

- 引入有界 DbWorker（4 线程 / 容量 64）；
- 删除慢请求日志与 `strimkeep_slow_requests_total`。
