# StrimKeep 0.6.1：Emby 路径通知与入库延迟

基于 0.6.0 的完整源码包，包含六栏工作台及预编译前端。可直接覆盖 0.5.8 或 0.6.0，保留现有配置、data、strm、密钥与播放 Token。

## 本次修改

- 自动整理/同步仅调用 `POST /Library/Media/Updated` 提交本批路径变化。取消 HTTP 400/404/405 时自动回退 `POST /Library/Refresh` 的逻辑。路径接口不可用时在任务和日志中报告，不偷偷启动全库扫描。
- 仅删除的同步批次也按 Emby 侧路径提交 `Deleted`；改名移动同时提交旧路径删除和新路径创建，沿用路径映射。
- 没有变化不发送扫描请求；没有明确路径的旧任务不再触发全库扫描。每次最多合并 200 条路径，避免每一集分别触发请求。
- 首次约 3 秒后核对入库，前两分钟每 10 秒，之后每 30 秒。只读取相应作品/季的条目，收到通知后不会因暂未入库而反复重发扫描。
- 日志记录接口、路径变更数量、实际 Emby 侧路径示例、失败状态和“通知到核对确认”耗时；此耗时包含轮询等待，不伪称 Emby 精确扫描耗时。
- 接口异常仍最多尝试 3 次，不阻塞后续可执行任务。播放请求通道保持独立。

Emby 接受路径通知不等于立即完成入库。其增量扫描可能检查父目录，元数据抓取、服务端合并等待或其他任务仍可能造成延迟。本工具取消的是自动全库扫描请求，不控制 Emby 内部扫描实现。纯删除的路径通知提交成功也不声称已核实 Emby 删除结果。

## 更新

将包上传到 `/vol1/1000/测试`，复制执行：

```bash
cd "/vol1/1000/测试" &&
unzip -oq "StrimKeep-v0.6.1-embyfix.zip" -d "StrimKeep-v0.6.1-embyfix" &&
bash "/path/to/StrimKeep-v0.6.1-embyfix/StrimKeep/update.sh" "/path/to/StrimKeep"
```

刷新后确认版本 0.6.1。影视整理中的 Emby 映射应与实际挂载相符，例如本程序 `/strm` → Emby `/strimkeep`，具体文件应在该媒体库范围内。

用一个新片测试，日志中心筛选 `emby`，应出现 `/Library/Media/Updated`、本次映射路径和随后入库确认的耗时。如仍需数分钟，结合相同时段 Emby 服务端日志中的 LibraryMonitor、抓取与 FFprobe 记录定位；Telegram 的两条消息时间不能单独证明触发过全库扫描。

本地模拟已验证路径接口出错不会回退全库、删除与改名路径通知、无变化不发送、3 秒首次核对和断线恢复。未连接真实 NAS/115/Emby，不承诺你的实际入库耗时。

官方接口说明：
- https://dev.emby.media/reference/RestAPI/LibraryService/postLibraryMediaUpdated.html
- https://emby.media/community/topic/150142-true-incremental-library-scanning-for-specific-folders-or-file-paths-via-api/
