# StrimKeep 0.8.44

频道落盘模式收敛为两种：**115 网盘落盘** / **分享 STRM 落盘**。

## 问题

前端下拉里是三个选项，后端 `share_mode` 也是三个值：

```
免转存 STRM（推荐）   → strm
转存到 115 目录       → transfer
不处理分享            → none
```

按职责划分，频道只该提供两种落盘模式：115 网盘（分享转存 / 磁力 / ED2K）和分享 STRM（免转存）。

## 改法

**前端** `Channels.vue`：

```diff
-     <el-option label="免转存 STRM（推荐）" value="strm"/>
-     <el-option label="转存到 115 目录" value="transfer"/>
-     <el-option label="不处理分享" value="none"/>
+     <el-option label="115 网盘落盘（分享转存 / 磁力 / ED2K）" value="transfer"/>
+     <el-option label="分享 STRM 落盘（免转存）" value="strm"/>
```

**后端**：

| 文件 | 改动 |
| --- | --- |
| `channels_api.py` | `share_mode: Literal['', 'strm', 'transfer']`（去掉 `none`）；legacy `mode='offline'` 映射到 `transfer` |
| `channel_monitor.py` | `normalize_rule` 把旧的 `none` 归一为 `transfer`；删掉 `share_mode == 'none'` 的提前返回 |

**校验放宽**：

```diff
-if self.share_mode == 'transfer' and not self.share_cid:
-    raise ValueError('分享转存需要先选一个 115 保存目录。')
+if self.share_mode == 'transfer' and not (self.share_cid or self.offline_cid
+                                           or self.magnet_cid or self.ed2k_cid):
+    raise ValueError('115 网盘落盘需要至少选一个目录（分享转存 / 磁力 / ED2K）。')
```

这样「只处理磁力/ED2K、不处理分享」仍然可以表达（转存目录留空），不需要第三个选项。

## 旧配置兼容

| 旧值 | 归一为 | 效果 |
| --- | --- | --- |
| `share_mode='none'` | `transfer` + 空转存目录 | 不处理分享，磁力/ED2K 照常落盘 |
| `mode='offline'`（legacy） | `transfer` + `offline_cid` | 与之前一致 |
| `share_mode='strm'` | 不变 | 分享 STRM |

## 验证

| 范围 | 结果 |
| --- | --- |
| `test_channel_monitor` + `test_destination_matrix` | **38 passed（修复后 37）** |
| `test_bot_mixed_links_use_independent_destinations` | 失败；**干净 0.8.43 基线同样失败**，既有问题 |

真实环境：容器运行 `0.8.44`，`/health/ready` → `ready`。

## 升级 / 回滚

```bash
cd "/path/to/StrimKeep"
docker compose up -d                                   # 升级
sed -i s/strimkeep:0.8.44/strimkeep:0.8.43/ docker-compose.yml && docker compose up -d   # 回滚
```
