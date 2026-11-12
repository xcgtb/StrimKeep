# StrimKeep 0.8.47

把「转存 / 离线 / 频道」拆成三套独立配置，每套各自决定**落盘后是否立即整理**，Web 与 Telegram 机器人分开。

## 改前的混乱

| 入口 | 落盘后 | 问题 |
| --- | --- | --- |
| Web 转存 | 看开关，默认 **False** | 和离线默认值相反 |
| TG 转存 | 看开关，默认 **False** | 与 Web 共用一个开关 |
| TG 离线 | 看开关，默认 **True** | 同一个人操作，转存不整理、离线整理 |
| 频道 转存 | 硬编码 **不整理** | 没有任何开关 |
| 频道 磁力/ED2K | 硬编码 **不整理** | 没有任何开关 |

## 改后：三个页面，字段一一对应

### 转存页（`Transfer.vue`）

| 项 | Web | 机器人 |
| --- | --- | --- |
| 目录 | `target_cid` | `bot_target_cid` |
| 落盘后立即整理 | `auto_organize` | `bot_auto_organize`（新增） |

### 离线页（`Offline.vue`）

| 项 | Web | 机器人 |
| --- | --- | --- |
| 直链 / 磁力 / ED2K 目录 | `target_cid` / `magnet_cid` / `ed2k_cid` | `bot_magnet_cid` / `bot_ed2k_cid` |
| 落盘后立即整理 | `auto_organize` | `bot_auto_organize`（新增） |

### 频道页（`Channels.vue`，每条规则）

| 项 | 说明 |
| --- | --- |
| 落盘目录 | 一个（分享 / 磁力 / ED2K 共用） |
| **落盘后立即整理** | `organize_after`（新增，默认关闭） |
| 轮询 | 间隔或 cron（默认 5 分钟）+「立即检查一轮」 |

## 统一的默认值与措辞

- **所有「落盘后立即整理」默认关闭**：落盘即结束，要不要整理由「影视整理」的手动 / 定时扫描决定。
- 打开后，仍然受扫描授权约束：**目标目录不在待整理范围内时只落盘**（0.8.42 的 `scan_scope_allows`）。

界面上统一了这句说明：

> 开启后落盘完成立即入队整理（目标目录必须在待整理范围内）；关闭时只落盘，由「影视整理」的手动/定时扫描决定。

## 验证

- `test_channel_monitor` / `test_destination_matrix` / `test_channel_skip_offline` → **39 passed**
- `test_bot_mixed_links_use_independent_destinations` 失败；干净 0.8.46 基线同样失败，属既有问题
- 构建时发现一处 `:aria-label` 用中文表达式导致 `vue-tsc` 报错，已改为静态属性

真实环境：容器运行 `0.8.47`，`/health/ready` → `ready`。

## 升级 / 回滚

```bash
cd "/path/to/StrimKeep"
docker compose up -d                                   # 升级
sed -i s/strimkeep:0.8.47/strimkeep:0.8.46/ docker-compose.yml && docker compose up -d   # 回滚
```
