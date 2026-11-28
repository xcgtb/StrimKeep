# 资源接入与收件箱

StrimKeep 的资源接入分成两段：

1. **收件箱**：手动粘贴、RSS、Webhook 先把链接写入 `resource_inbox`，做去重和重试；
2. **执行**：后台 Worker 再复用现有的 115 转存、离线下载和影视整理链路。

收件箱只负责“把资源安全地送进现有管线”，不重新实现转存和整理。

## 支持的来源

| 来源 | 入口 | 说明 |
|---|---|---|
| 手动 | 资源入库与收件箱 → 收件箱 → 粘贴资源 | 支持 115 分享、磁力、ED2K、HTTP/HTTPS/FTP 直链 |
| RSS | 资源入库与收件箱 → 来源与设置 → RSS 订阅 | RSS 2.0 / Atom，定时拉取 |
| Webhook | 资源入库与收件箱 → 来源与设置 → Webhook 收件 | 给 Prowlarr、下载器或自建脚本推送 |
| Telegram | 频道监控 / Telegram 机器人 | 正常走原有独立链路；频道离线提交结果不确定时写入收件箱待核查 |
| 影视订阅 | 影视探索 → 订阅电影 / 订阅追更 | 搜索已配置的 PanSou、Prowlarr 与 Telegram，转存和离线任务进入收件箱 |

## 去重规则

| 链接类型 | 去重键 |
|---|---|
| 115 分享 | `share:<分享码>` |
| 磁力 | `magnet:<btih>` |
| ED2K | `ed2k:<32 位哈希>` |
| 直链 | `url:<URL 的 SHA-1>` |

重复资源不会重复提交。失败、跳过、待核查的旧条目再次提交时，会复用原条目并重置为待处理。

## 状态

- `pending`：等待 Worker 处理
- `processing`：Worker 已领取，租约 300 秒；进程异常退出后过期会自动回到 `pending`
- `queued`：已经交给转存或离线任务
- `duplicate`：上游明确返回重复
- `needs_review`：结果不确定或缺少目录，需要人工核查
- `failed`：明确失败，可重试
- `skipped`：内容没有可用链接，或手动跳过

`queued` 和 `processing` 不会自动重试，也不能手动重试、跳过或删除（接口返回 409），避免重复转存；结果不确定的离线提交也不会自动重试。清理旧记录时会保留仍被订阅提交引用的行。

## RSS

RSS 条目会读取标题、正文、`enclosure` 和正文里的链接。普通文章页链接不会被当作资源；只有 115 分享、磁力、ED2K、`.torrent` 或直接媒体文件才会进入收件箱。

拉取采用流式读取，单次最多 2 MB；每一跳重定向都会重新校验，禁止 localhost、回环、链路本地和云元数据地址。同一个 RSS 地址重复添加会返回 409。

每个 RSS 源可以单独设置：

- 拉取间隔（60–86400 秒）
- 115 落点（留空跟随默认目录）
- 包含/排除关键词
- 落盘后是否立即整理

## Webhook

在“资源入库与收件箱 → 来源与设置”生成令牌后，地址形如：

```text
https://你的地址/api/v1/resources/webhook/<token>
```

支持 JSON 对象或数组。会从以下字段和完整 JSON 中提取链接：

```json
{
  "title": "示例电影",
  "guid": "external-event-id",
  "magnetUrl": "magnet:?xt=urn:btih:...",
  "text": "可选正文",
  "target_cid": "可选 115 目录 ID"
}
```

没有令牌或令牌错误返回 404；请求体超过 1 MB 返回 413。Webhook 只入收件箱，不等待 115 请求。重新生成令牌后，旧令牌继续有效 24 小时，方便外部发送方切换。

## 安全边界

- 不抓取资源站 HTML，不绕过登录、验证码或反爬；
- 只接官方 API、RSS、用户自己的 Webhook、公开频道或用户主动粘贴的链接；
- 不提供任何资源，也不内置资源站；
- “系统设置 → 资源来源”用于影视探索的实时搜索。可配置 PanSou 和仅用于搜索的 Telegram 频道。RSS 订阅仍在资源入库的自动来源中配置。

### 影视探索搜索来源

| 来源 | 配置方法 |
| --- | --- |
| PanSou | 添加允许直连的服务地址。 |
| Prowlarr | 填写 Prowlarr 服务地址和 API Key；索引器在 Prowlarr 内配置。只接收磁力结果，API Key 加密保存。 |
| Telegram | 登录用户会话后，在资源来源输入公开频道的用户名或链接，或点「读取已加入的频道」选择会话可访问的频道。添加搜索来源不会开启轮询或自动入库。 |

## 电影与剧集订阅

从影视探索的剧集卡片选择季号和 115 分享的入库方式。系统按 TMDB 播出日期定时检查，对照网盘库和分享库的 STRM。可按需补以前的缺集。

匹配作品和季集的单集资源可自动入队，整季包、身份不明的候选以及结果不确定的提交可在订阅列表核查。入队后等待库里出现该集 STRM，再更新为已入库。暂停订阅会停止新的搜索和提交。待核查或失败的订阅可在「追更订阅」页重试，重试会清理旧提交占用并重新排队；状态变化会进入现有通知队列。

电影可提前订阅，接近上映时开始搜索。正片需核对作品、年份或 TMDB 标识及片源标签。枪版、预告片和无法确认的片源留待核查。两库确认出现对应电影的 STRM 后，订阅自动结束。

## 相关接口

- `GET /api/v1/resources/config`
- `PUT /api/v1/resources/config`
- `GET /api/v1/resources/inbox`
- `POST /api/v1/resources/inbox`
- `POST /api/v1/resources/inbox/{id}/retry`
- `POST /api/v1/resources/inbox/{id}/skip`
- `DELETE /api/v1/resources/inbox/{id}`
- `GET /api/v1/resources/sources`
- `GET/POST /api/v1/resources/feeds`
- `PUT/DELETE /api/v1/resources/feeds/{id}`
- `POST /api/v1/resources/feeds/{id}/run`
- `POST /api/v1/resources/webhook-token`
- `POST /api/v1/resources/webhook/{token}`
