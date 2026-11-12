# StrimKeep 0.8.43

磁力 / ED2K 接入频道关键词分流。不改落盘方式，也不改整理授权。

## 问题

频道支持按关键词把资源分流到不同目录（`routes`），但此前**只有 115 分享转存**用了这套规则：

```python
# channel_monitor.py 现状（115 转存）
route_cid, route_label = route_target({'routes': ..., 'target_cid': rule['share_cid'], ...}, text)

# 磁力 / ED2K 直接取固定目录，命中关键词也不分流
cid = rule[kind + '_cid']
```

所以同一个频道里「电影 → 待整理/电影、AV → AV资源」这种分流，对磁力链接不生效。

## 改法

```diff
+ route_cid, route_label = route_target(
+     {'routes': rule['routes'], 'target_cid': '', 'target_label': ''}, text)
  for kind, prefix in (('magnet', 'magnet:'), ('ed2k', 'ed2k://')):
      ...
-     cid = rule[kind + '_cid']
+     if route_cid:
+         cid, label = route_cid, route_label
+     else:
+         cid = rule[kind + '_cid']
+         label = rule[kind + '_label'] or cid
```

两个关键点：

1. **只认「命中」**：`route_target` 传入空默认值，没命中时返回空 → 不回退到统一离线目录。这样「分享 STRM 模式下跳过同帖磁力链接」的既有行为保持不变（该场景只配了 `offline_cid`，没配 `magnet_cid`）。
2. **三种落盘方式共用一条 `routes`**：同一个关键词规则对 115 转存、磁力、ED2K 同时生效，不需要为磁力/电驴单独配一套。

## 未改动的部分

- 落盘后是否整理：仍由 0.8.42 的扫描范围授权决定（目标目录在不在 `source_cids` 内）
- 磁力/ED2K 仍是 `organize=False`，只落盘
- 分享 STRM 链路、洗版规则、识别与命名

## 验证

| 范围 | 结果 |
| --- | --- |
| `test_channel_monitor.py` | **32 passed**（新增 2 项：磁力命中分流目录、ED2K 共用同一规则） |
| 既有「分享 STRM 模式跳过同帖磁力」 | 保持通过 |

真实环境：容器运行 `0.8.43`，`/health/ready` → `ready`。

## 升级

```bash
cd "/path/to/StrimKeep"
docker compose up -d
curl -s http://127.0.0.1:9000/health/ready
```

## 回滚

```bash
cd "/path/to/StrimKeep"
sed -i s/strimkeep:0.8.43/strimkeep:0.8.42/ docker-compose.yml
docker compose up -d
```

## 用法

频道规则里的一条分流规则，现在对三种落盘方式都生效：

| 关键词 | 分流目录 | 生效范围 |
| --- | --- | --- |
| `4k,原盘` | `待整理/电影4K` | 115 分享转存 / 磁力 / ED2K |
| `纪录片` | `待整理/纪录片` | 同上 |
| （未配置磁力/电驴单独目录时） | 命中分流则用分流目录，否则跳过 | —— |
