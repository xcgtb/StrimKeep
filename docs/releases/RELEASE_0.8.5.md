# StrimKeep 0.8.5

三块与「播放」直接相关的能力，外加一个前端按钮。不改整理、转存、洗版的业务规则，不动数据库结构。

## A 播放通道开始留痕

`/play115` 与 `/play/share` 以前成功失败都不写日志，`events` 里 play 类记录一直是 0 条。播放出问题只能看现象，无法回答「谁在什么时候取了哪条链、用了什么 UA、失败了还是超时了」。

现在两条通道都按入口节流 60 秒记录：

| 场景 | 记录内容 |
| --- | --- |
| 成功 | UA、pickcode、耗时 |
| 失败 | 错误码 |
| 超时 | 超时标记 |

细节：

- UA 是排查第一步——115 直链和 UA 绑定，换 UA 就取不到链。新增 `playaddr.ua_label()` 把 UA 截到 32 字符再入库，避免日志表被长 UA 撑大。
- 写库失败不影响播放本身，日志是旁路。

## B 取链缓存按客户端分层

Emby 每探测一集都会打一次 115。现场实测（18:40 一分钟内）：

```
14 次 GetPostedPlaybackInfo + 14 次 ffprobe，14 个不同 pickcode
```

也就是说同一批文件被反复取链。现在按 UA 分层 TTL：

| UA 类别 | TTL |
| --- | --- |
| 探测类（Lavf / ffprobe / Emby / Plex / Jellyfin / Kodi） | 60 秒 |
| 真实播放 | 15 秒 |

同时同一 `(账号, pickcode, UA)` 的并发请求只向 115 取一次链（single-flight）。扫一季时探测和播放撞在一起，也不会成倍放大请求数。

分层的原因：探测请求要的是「地址能不能用」，可以容忍一分钟的陈旧；真实播放要的是「立刻能播」，缓存必须短。

## C 播放地址一键重写

后端 `POST /api/v1/strm/rebuild` 早就具备「按当前 `playback_base` 重写全部受管 STRM、不读云端」的能力，但前端从来没有调用入口，换 IP、加域名、内网切公网时只能整库重扫。

STRM 生成页的播放地址卡片新增按钮，一键把存量 STRM（现场 776 个）换成新入口。

## 验证

新增 `backend/tests/test_play85.py`（5 项）：

1. 探测 UA 识别
2. UA 标签截断
3. 并发请求合并（single-flight）
4. TTL 分层
5. 缓存复用

并入 `scripts/verify.sh` 的「身份与上下文」门禁组。

## 升级

```bash
cd "/path/to/StrimKeep"
docker compose up -d
curl -s http://127.0.0.1:9000/health/ready
```

## 回滚

```bash
cd "/path/to/StrimKeep"
sed -i s/strimkeep:0.8.5/strimkeep:0.8.4/ docker-compose.yml
docker compose up -d
```

## 相关版本

- 0.8.3：识别按文件名语言选择 TMDB 检索语言、跨语言回退、旧批次闭环（见 [RELEASE_0.8.3.md](RELEASE_0.8.3.md)）
- 0.8.4：`halt()` 把 `discovered` 条目落到 `review`（见 [RELEASE_0.8.4.md](RELEASE_0.8.4.md)）

本版没有改动识别与整理逻辑。
