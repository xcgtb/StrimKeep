# StrimKeep 0.8.8

任务中心三个分类问题（「深度删除」挡住「进行中」、「未识别」和「过滤」永远是 0）全部修掉，另外补了「清理未被引用的播放身份」。

## 现象

任务中心的分类栏如下，用户反馈三处不对：

```
全部 40 | 深度删除 | 进行中 0 | 完成 40 | 过滤 0 | 未识别 0 | 失败 0
```

1. **「深度删除」夹在「全部」和「进行中」之间**，看着像把「进行中」挤到旁边、挡住了分类。
2. **「未识别」永远是 0**，从来没有显示过内容。
3. **「过滤」永远是 0**，也从来没显示过内容。

## 原因

| 现象 | 原因 |
| --- | --- |
| 深度删除挡路 | 它是 `DeepDeleteHistory` 组件，却被当成一个分类 `el-tab-pane` 插在「全部」和「进行中」之间 |
| 未识别恒为 0 | `bucketOf()` 用 `counts.pending > 0` 判定。`pending` 只是中间态，任务跑完就变 `done`/`skipped`，几乎不可能残留。真正的「未识别」是 `state='skipped'` + `step='未识别转移未完成，保留目录与记录'`（现场那 5 个 `Love in Disguise` 就是） |
| 过滤恒为 0 | 条件是 `!job.total && job.scan_filter.scanned`，要求「一个文件都没进队列」。现场 `#183` 是 `scanned=5, excluded=0, total=5`，永远不命中 |

## 改法

**1. 「深度删除」移到最后一个 tab**（`Tasks.vue`）

它仍然是独立视图（切过去渲染 `DeepDeleteHistory`），只是不再打断任务分类的顺序。

**2. 后端提供 `unidentified`**（`main.py` 的 `job_public`）

```python
result['unidentified'] = store.db.execute(
    "SELECT count(*) FROM organize_items WHERE job_id=? AND step LIKE '%未识别%'",
    (row['id'],)).fetchone()[0]
```

按条目 `step` 统计，而不是看状态。前端改成：

```ts
if((job.unidentified||0)>0||(counts.pending||0)>0)return 'unidentified'
if(!counts.done&&((job.scan_filter&&job.scan_filter.excluded)||0)>0)return 'filtered'
```

**3. 「过滤」改为看 `excluded`**

语义从「一个文件都没进队列」改为「有文件被扩展名/关键词/体积挡掉，且这批没有成功入库」。这样 `#159` 那种「入库 1、跳过 25」不会再被算成「完成」，也不会因为 `total>0` 就漏掉。

**4. 清理未被引用的播放身份**（方案 D）

`POST /api/v1/playbacks/cleanup`：删掉没被任何在册输出引用的 `playbacks` 记录。

```python
'SELECT p.id FROM playbacks p WHERE NOT EXISTS ('
'SELECT 1 FROM output_registry o WHERE o.playback_id=p.id AND o.active=1)' + clause
```

- 默认只删 `revoked=1` 且无引用的（现场 65 条）
- `include_active=1` 才连未撤销的孤儿一起删（现场另有 682 条）
- 正在被 `output_registry.active=1` 引用的一个都不动
- 入口在 **STRM 生成 → 播放地址** 卡片

## 边界

- 清理只删 `playbacks` 表记录，不动本地 STRM、不动云端文件。因为 STRM 写的是 `/play115/{pickcode}/...`，取链不查这张表，删了不影响播放。
- 「深度删除」只是位置变化，仍是独立视图。

## 验证

- `vue-tsc --noEmit` 与 `vite build` 通过（构建日志可见）。
- 镜像内确认：`result['unidentified']`、`api/v1/playbacks/cleanup` 存在；前端产物含「清理未被引用的播放身份」与 `unidentified`。
- 冒烟实例 `/health/ready` 返回 0.8.8、首页 200。
- 生产容器运行 0.8.8，健康检查 ready。

## 升级 / 回滚

```bash
cd "/path/to/StrimKeep"
docker compose up -d      # 升级
# 回滚：sed -i s/strimkeep:0.8.8/strimkeep:0.8.7/ docker-compose.yml && docker compose up -d
```

## 相关版本

- 0.8.7：任务「结项」、目录清理按钮（见 [RELEASE_0.8.7.md](RELEASE_0.8.7.md)）
- 0.8.6：探索页每页 45、卡片来源文案（见 [RELEASE_0.8.6.md](RELEASE_0.8.6.md)）
