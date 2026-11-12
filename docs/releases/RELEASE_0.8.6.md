# StrimKeep 0.8.6

两处改动：PC 端「影视探索」每页 40 个改成 45 个；TG 卡片「来源」文案统一为「115网盘」/「115网盘分享」。不改后端接口、分页逻辑与入库角标。

## 现象

PC 端影视探索每页显示 40 个作品，最后一行只剩 4 个：

```
9 列 × 4 行 = 36
+ 第 5 行 4 个 = 40
```

看起来像图片没加载完或者列表被截断。实际上网格是 9 列，任何不是 9 的倍数的每页数量都会在最后一行留空缺。

## 改法

`frontend/src/Explore.vue`：

```diff
-const perPage=window.innerWidth<640?30:40
+const perPage=window.innerWidth<640?30:45
```

45 = 9 × 5，正好填满 5 行。移动端（宽度 < 640）保持 30，因为窄屏是单列或双列，按列数换行本来就不会留固定空缺。

后端 `GET /api/v1/explore/list` 的 `per_page` 约束是 `ge=10, le=60`，45 在范围内，接口无需改动。分页总数 `total_pages`、入库角标查询都按实际返回条数计算，不受影响。

## 来源文案

入库/整理卡片的「来源」一行原来分三种写法。转存后的文件本来就在自己网盘里，和网盘原有文件不该有两种说法，所以收成两种：

| 场景 | 改前 | 改后 |
| --- | --- | --- |
| 分享免转存（STRM 直指别人分享） | `🔗 来源：115 分享 STRM（免转存）` | `🔗 来源：115网盘分享` |
| 分享链接转存进自己网盘 | `☁️ 来源：115 分享转存` | `☁️ 来源：115网盘` |
| 自己网盘原有文件 | `☁️ 来源：115 网盘` | `☁️ 来源：115网盘` |

`backend/strimkeep/cards.py` 里 `transferred` 参数保留在 `media_card()` 签名中，继续供调用方区分批次来源（留档与排查用），不再影响这张卡片的文案。

`tests/test_cards062.py` 的两处断言同步更新；`test_cards062`、`test_message_templates`、`test_tg_notification_flow` 纳入门禁新增的「通知卡片」组。

## 验证


- 前端由 Dockerfile 的 `node:22-alpine` 阶段重新编译（改动 `frontend/src` 后 `docker build` 即生效，宿主机不需要 Node）。
- 容器 `/health/ready` 返回 `version 0.8.6`。
- `tests/test_explore_badge.py` 使用的是显式 `per_page` 参数（30 / 20），不依赖前端默认值。

## 升级

```bash
cd "/path/to/StrimKeep"
docker compose up -d
curl -s http://127.0.0.1:9000/health/ready
```

## 回滚

```bash
cd "/path/to/StrimKeep"
sed -i s/strimkeep:0.8.6/strimkeep:0.8.5/ docker-compose.yml
docker compose up -d
```

## 相关版本

- 0.8.5：播放日志、取链缓存分层、播放地址一键重写（见 [RELEASE_0.8.5.md](RELEASE_0.8.5.md)）
- 0.8.4：`halt()` 把 `discovered` 条目落到 `review`（见 [RELEASE_0.8.4.md](RELEASE_0.8.4.md)）
- 0.8.3：识别按文件名语言检索 TMDB（见 [RELEASE_0.8.3.md](RELEASE_0.8.3.md)）
