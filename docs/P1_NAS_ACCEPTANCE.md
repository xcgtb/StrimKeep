# P1 飞牛现场只读验收指南（P0 生产容器保持不变）

**适用基线：** GitHub `xcgtb/StrimKeep` main `78fc0234539e0d00cc26daab12d5d7f9149bddf5`。
**本次包：** P0 + P1 合并；不需要发布 Tag。**不可将本地模拟测试声称为 NAS 验收。**

## 一、在飞牛上运行隔离测试

先将对应的完整源码 `StrimKeep-P0-P1-verified-source.tar.gz` 上传到 NAS 的 `/vol1/1000/git/`。不要解压到正式 Docker `data`，也不要提前覆盖 Git 工作区。

```bash
sudo -i
mkdir -p /vol1/1000/git/StrimKeep-P0-P1-test
tar -xzf /vol1/1000/git/StrimKeep-P0-P1-verified-source.tar.gz \
  -C /vol1/1000/git/StrimKeep-P0-P1-test
cd /vol1/1000/git/StrimKeep-P0-P1-test
sha256sum -c MANIFEST.sha256
bash dev/checks/run_p1_nas_readonly.sh "$PWD"
```

自动化测试中，正式容器 `strimkeep` 只被查询配置和挂载路径，绝不停止、升级或重新配置。

- 构建独立 `strimkeep:p1-acceptance` 镜像。
- 使用独立临时 SQLite、无真实媒体挂载的 HTTP 进程，检查 `/api/library/events` 的鉴权、Basic/Cookie 登录、SSE 事件和 `/api/library/changes` 的保守重载。
- 通过管道读取正式容器 Emby 连接设置；不将密钥打印或写入源代码。
- 第二个临时进程直接访问 Emby，NAS 本地和分享 STRM 目录均以 `:ro` 挂载，不挂载 `/media/cloud`。
- 用《美人余》TMDB 294446 作为已知样本，验证新剧保守建档、已有剧补集、失联保护和 SQLite 变更事件；额外执行两轮最近 24 小时入库查询。
- 测试进程里只允许 Emby `GET`，绝不调用清理接口；只有**临时 `/data`** 会被写入。
- 任一测试失败即停止并保留正式 P0 不动。

若找不到样本 Series ID 会打印 `SKIP`，**不等于 P1 的 NAS 新剧功能通过**。应先确认该作品仍在 Emby 媒体库，或修改验收工具中的样本 TMDB ID 和剧名，重新执行。

**安全前提：** 完整源码包中测试脚本必须先通过 SHA-256 验证；正式媒体目录存在且只读挂载；使用 root SSH；不运行任意治理删除。不要在公共聊天中粘贴 Emb​y Token、会话 Cookie 或私有数据库内容。

## 二、浏览器 / SSE 实际体验验收（仍需人工）

后台协议通过不等于真实 Safari 的视觉验收通过。待隔离后台测试成功，需在仅测试使用的 P1 Web 实例中，用浏览器完成：

1. 登录后确认 EventSource 通过 Cookie 建连，`GET /api/library/events` 返回 `text/event-stream`；未登录返回 401。
2. 刷新 30 秒内的 `GET /api/library/changes?since=N`，修改样本数据库的新剧事实后应只返回受影响 Series ID；同版本不应重复重载。
3. 《美人余》S01E05 的影视探索、片库映射、弹窗中的已入库季集一致；局部更新海报图片不闪烁，新增作品状态为“待对照”而非“完整”。
4. 停止 SSE 后允许 30 秒轮询恢复；刷新、返回前台及双浏览器标签页不会错误倒退版本。
5. 浏览器外观和卡片布局须在 Safari 实机验收，Node 的 DOM 替代测试不等于浏览器实机验收。

本指南**不自动起一个能访问正式 Emby 和真实挂载的 Web 管理台**，因为即使媒体挂载是只读，未经审计的后台任务/鉴权暴露仍有风险。若需要这一项，需再单独搭建禁用所有写入任务的 P1 测试服务，并审查其端口及访问权限。

## 三、治理不变性与 30–60 分钟稳定性（上正式版前）

本地已跑现有治理金样本和错误防护回归；未在真实媒体挂载上自动执行删除。对同一份双库目录，正式上线前还需比对旧版 P0 与 P1 的只读治理预览输出，确保文件候选、置信度、保护项与计划相同。禁止执行正式删除以制造测试数据。

P1 在现场实际启动后，连续 30–60 分钟记录：

```bash
docker inspect -f '{{.Config.Image}} | {{.State.Health.Status}}' strimkeep
docker stats --no-stream strimkeep
docker exec strimkeep sh -c "grep -E 'VmRSS|VmHWM|Threads' /proc/1/status"
docker logs --since 30m strimkeep 2>&1 | \
  grep -E '入库增量|完整时间窗|拉取分集|ERROR|Traceback' | tail -n 80
```

必须确认正常轮询不重新抓取所有十几万分集，RSS 不持续上升，异常期间仍保留旧事实。所有结果通过后才建议更新 GitHub 或替换正式 P0 镜像。

## 四、本地验收结果与未完成项

- Python 回归 806 项，前端 28 组，历史 SQLite 结构兼容、真实本地 HTTP 服务启动和认证、真实 SSE 事件与 Cookie 鉴权：**已在本地通过**。
- SQLite 事件有界日志、版本回退/首次事件、全新剧集 pending、跨库 location 标记、失败保留：**测试通过**。
- Emby 15 万集规模下的 P0 已有正式 NAS 测试证据，不代表 P1 在真实 NAS 的全部功能已测。
- P1 真实 Emby + 只读媒体挂载、Safari 多客户端、治理计划现场比对、30–60 分钟完整 P1 运行：**等待执行并提供输出**。


## 五、2026-10-10 首轮 NAS 验收失败与修复重测

实际首轮已通过独立镜像构建、Local/Share `:ro` 挂载和 HTTP/SSE 认证与事件；
《美人余》的 Series ID `931319` 和 5 集也通过真实 Emby 查询。
但生产实现对 `GET /Items/931319` 收到 HTTP 404，导致新剧建档为 0，**本次现场验收失败，不能上传 GitHub**。

修复版只修改 P1 对新 Series 元数据的读取：改为 `GET /Items?Ids=931319&...`，
要求响应只有一个 `Type=Series` 且 `Id` 完全一致、总数为 1，并要求存在有效媒体路径。
若 Emby 忽略 `Ids` 过滤或返回多个条目，将拒绝写入缓存，保持旧事实。
验收脚本现会先检查该读取接口；样本不存在会失败，不再以 `SKIP` 返回成功。

修复只通过本地模拟与单元回归，不可代替 NAS 上再次运行完整 `run_p1_nas_readonly.sh`。
随后仍须按本指南执行 Safari 实机、治理预览一致性和 30–60 分钟稳定性检查。
