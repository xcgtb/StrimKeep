# StrimKeep 0.8.4

修一处状态上报缺陷，其余内容与 0.8.3 相同。

## 现象

任务进入「等待核查」（TMDB 报错、识别失败、需要人工确认）时，任务列表显示状态是对的，但点进任务详情，每个文件仍然是「发现文件」。看起来像任务根本没开始跑，也看不到失败原因。

## 根因

`organizer.halt()` 只更新任务（`organize_jobs`）的状态和消息，没有管条目（`organize_items`）。而这些卡在识别阶段的文件**从没进过 `update_item`**，状态一直停在扫描时写入的 `discovered`。

`tests/test_identity_real_names.py::test_tmdb_failure_is_reported_not_guessed` 一直在测这件事：

```
E   assert 'discovered' in ('pending', 'review')
```

该用例在 0.8.2、0.8.3 上都失败，只是不在 `scripts/verify.sh` 门禁清单里，所以一直没被拦住。

## 改法

```python
# halt() 里补一段：卡在识别阶段的文件还没进过 update_item，
# 状态留着 discovered：任务已经是「等待核查」，点进去每个文件却还是
# 「发现文件」，看起来像任务没启动。这里一并落库，并把失败原因写到条目上。
with self.store.db:
    self.store.db.execute(
        "UPDATE organize_items SET state='review', step=?, message=?"
        " WHERE job_id=? AND state='discovered'", (stage, message, job_id))
```

效果：任务进入「等待核查」时，未处理的条目变成 `review`，`step` 是当时的阶段（如「TMDB 请求失败，保留未完成文件并稍后重试」），`message` 是失败原因。

## 验证

- `tests/test_identity_real_names.py`（5 项）全绿——此前一直是 `1 failed, 4 passed`。
- `tests/test_multilang_identify.py`（8 项）全绿，确认与 0.8.3 的语言修复不冲突。
- 生产容器 `curl /health/ready` 返回 `version 0.8.4`，状态 `ready`。

## 升级

```bash
cd "/path/to/StrimKeep"
docker compose up -d
curl -s http://127.0.0.1:9000/health/ready
```

## 回滚

```bash
cd "/path/to/StrimKeep"
sed -i s/strimkeep:0.8.4/strimkeep:0.8.3/ docker-compose.yml
docker compose up -d
```

## 说明

0.8.3 的跨语言识别修复（按文件名语言选 `en-US`/`zh-CN`、缓存 key 带 `language`、跨语言回退、失败时记录候选分数、旧批次闭环）见 [RELEASE_0.8.3.md](RELEASE_0.8.3.md)。本版没有改动那部分逻辑。
