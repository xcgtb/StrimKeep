# StrimKeep 0.8.9

0.8.8 的「过滤」口径不对：它只看 `scan_filter.excluded`（扫描阶段的扩展名过滤），而现场真正的过滤是 91 条「已有同集或同名条目，保留已有」。

## 现象

「过滤」栏一直是 0，但任务中心里明明有 `#145`（42 个）、`#159`（25 个）、`#40`（24 个）这种「入库 1 部、其余全部跳过」的批次。

## 原因

- `scan_filter.excluded` 只统计**扫描阶段**被挡掉的文件（扩展名、关键词、低于最小体积、小文件回收），现场全是 0；
- 真正的跳过发生在**整理阶段**，记在条目 `step` 上：`已有同集或同名条目，保留已有`（洗版/同名保留）。

## 改法

后端 `job_public` 新增 `filtered_items`，按条目 step 统计：

```sql
SELECT count(*) FROM organize_items WHERE job_id=? AND (
  step LIKE '%保留已有%' OR step LIKE '%未列入%'
  OR step LIKE '%低于最小%' OR step LIKE '%命中忽略关键词%')
```

`filtered = filtered_items + scan_filter.excluded`，前端 `bucketOf()` 只要 `filtered > 0` 就归到「过滤」。

**「小于 100MB 不整理」**（`min_video_size_mb=100`、`recycle_small_videos=True`）属于扫描阶段，落在 `scan_filter.excluded`，同样计入「过滤」。

## 提示文案

原来写死「另有 N 个文件未列入整理扩展名」，会把体积过滤也说成扩展名问题。现在按 `scan_filter.reasons` 的真实原因显示，例如「低于最小视频大小 100 MB，已回收（3 个）」；整理阶段的跳过则提示「另有 N 个文件因已有更好版本或同名条目未入库」。

## 验证

- 用生产库按新口径统计：`#145=42 / #159=25 / #40=24`，合计 91，与预期一致。
- `vue-tsc --noEmit` 通过（第一版漏了 `Task` 的类型字段，被构建拦住并补齐）。
- 冒烟 `/health/ready` 返回 0.8.9、首页 200；生产容器运行 0.8.9，健康检查 ready。

## 升级 / 回滚

```bash
cd "/path/to/StrimKeep"
docker compose up -d
# 回滚：sed -i s/strimkeep:0.8.9/strimkeep:0.8.8/ docker-compose.yml && docker compose up -d
```

## 相关版本

- 0.8.8：任务中心三分类修复、清理孤儿播放身份（见 [RELEASE_0.8.8.md](RELEASE_0.8.8.md)）
