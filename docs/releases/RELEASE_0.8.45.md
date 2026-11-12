# StrimKeep 0.8.45

修一处前后端不一致：**「分享 STRM 落盘」模式下不该出现磁力 / ED2K 落盘目录选择器。**

## 问题

`normalize_rule` 在 strm 模式下会清空这两个目录：

```python
# channel_monitor.py:88-89
rule[kind + '_cid'] = rule['offline_cid'] if rule['share_mode'] != 'strm' else ''
rule[kind + '_label'] = rule['offline_label'] if rule['share_mode'] != 'strm' else ''
```

但前端那块 `channel-section` 没有条件，两种模式都渲染：

```html
<div class="channel-section">
  <div class="offline-destinations-grid">  ← 磁力 / ED2K 选择器
```

结果是：选了「分享 STRM 落盘」仍然能看到两个目录选择器，选完保存后目录被后端清空，看起来像"设了没生效"。

## 改法

```diff
-  <div class="channel-section">
+  <div class="channel-section" v-if="c.share_mode==='transfer'">
     <div class="offline-destinations-grid">
```

现在两种模式各自只显示相关内容：

| 模式 | 显示 |
| --- | --- |
| 115 网盘落盘 | 转存目录 + 关键词分流 + 磁力落盘 + ED2K 落盘 |
| 分享 STRM 落盘 | 只有频道开关、落盘模式、过滤规则（include / exclude） |

## 验证

- 前端在镜像内重新编译（`frontend/src` 改完 `docker build` 即生效）
- `test_channel_monitor` / `test_destination_matrix` 结果与 0.8.44 一致：**37 项通过**，`test_bot_mixed_links_use_independent_destinations` 仍是既有失败（干净基线同样失败）

真实环境：容器运行 `0.8.45`，`/health/ready` → `ready`。

## 升级 / 回滚

```bash
cd "/path/to/StrimKeep"
docker compose up -d                                   # 升级
sed -i s/strimkeep:0.8.45/strimkeep:0.8.44/ docker-compose.yml && docker compose up -d   # 回滚
```
