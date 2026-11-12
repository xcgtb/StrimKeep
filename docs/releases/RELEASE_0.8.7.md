# StrimKeep 0.8.7

三件事，都是「未完成」收不了尾的问题：任务卡片补上「结项」，把后端早就有的「重试空目录清理」「重新核查目录残留」接到界面上，并清掉业务代码目录里误放的测试文件。

## 现象

任务列表里长期堆着 19 条「部分完成」（`organize` 10 条、`share_organize` 6 条、`strm_sync` 3 条），界面上没有任何入口能把它们收掉：

- `organize`：「1 个目录读取失败，已保留待下次清理」
- `share_organize`：「27.mkv：文件名只有集号，缺少作品线索」
- `strm_sync`：「扫描期间网盘内容发生变化，已跳过失效清理」

后端 `POST /api/v1/organize/jobs/{id}/{action}` 早就支持 `retry-cleanup`、`recheck-cleanup`、`retry-output`、`pause/resume/cancel`，但前端只接了 `retry-output` 一个；也没有任何「结项」语义的动作。

## 改法

**1. 新增「结项」动作（`main.py`）**

```python
if action == 'close' and row['state'] in ('partial', 'needs_review', 'cancelled'):
    note = '（已人工结项，不再重试）'
    ...
    app.state.organizer.update_job(job_id, state='completed', stage='已结项', message=message)
```

只改任务状态与 `message`，不动条目：点进详情仍能看到每个文件当时失败的原因。结项后任务从「部分完成」变「完成」，不再计入未完成。

**2. 补 `can_recheck_cleanup`（`job_public`）**

前端需要知道「重新核查目录残留」能不能点，条件与后端校验一致：已结束（`completed`/`partial`）+ 有清单 + 条目状态正常 + config 里有 `empty_cleanup.entries`。

**3. 任务卡片加三个按钮（`Tasks.vue`）**

- `结项`：`partial` / `cancelled` 状态显示
- `重试空目录清理`：`job.can_retry_cleanup`
- `重新核查目录残留`：`job.can_recheck_cleanup`

「更多操作」的展开条件同步放宽，否则没有 `done`/`archived` 任务时这些按钮看不到。

**4. 删掉误放的测试文件**

`backend/strimkeep/test_sync_stale.py`（4476 字节）混在业务包里，`backend/tests/test_sync_stale.py`（4623 字节）才是正主。

## 边界

- 结项不改任何文件、不动云端、不发通知。
- 目录清理只回收空目录和海报/NFO 残留，不碰视频与字幕。
- `retry-cleanup` 要求视频处理已结束；未结束的任务后端照旧返回 409。

## 验证

- 镜像内确认：`can_recheck_cleanup`、`已人工结项`、`结项`、`重试空目录清理` 均存在；`/app/strimkeep/test_sync_stale.py` 已不存在。
- 冒烟实例 `/health/ready` 返回 `version 0.8.7`，首页 200。
- 生产容器运行 0.8.7，健康检查 ready。

## 升级

```bash
cd "/path/to/StrimKeep"
docker compose up -d
curl -s http://127.0.0.1:9000/health/ready
```

## 回滚

```bash
cd "/path/to/StrimKeep"
sed -i s/strimkeep:0.8.7/strimkeep:0.8.6/ docker-compose.yml
docker compose up -d
```

## 相关版本

- 0.8.6：探索页每页 45、卡片来源文案（见 [RELEASE_0.8.6.md](RELEASE_0.8.6.md)）
- 0.8.5：播放日志、取链缓存分层、播放地址一键重写（见 [RELEASE_0.8.5.md](RELEASE_0.8.5.md)）
