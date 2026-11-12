# StrimKeep 0.8.11

分享监控的「存活检测」现在有两种执行方式：**固定间隔**（小时）和 **Cron 表达式**。此前只有「每 N 小时」一个数字。

## 现象

分享监控卡片里，「存活检测」只有一个数字输入框加「小时」单位：

```
存活检测        定期检查分享，填 0 则关闭。        [ 24 ] 小时
```

它实际的行为是「每 24 小时一轮，锚定在 03:00」。想改成「每天 12:30 检查一次」「只在周末检查」「每 6 小时一次」都没有入口——只能靠小时数近似，且时刻被钉死在 03:00。

对比：定时整理、定时同步 STRM、频道监控都已经是「间隔 / Cron」二选一。

## 改法

### 后端

`ShareSettingsInput` 增加两个字段：

```python
share_check_mode: Literal['interval', 'cron'] = 'interval'
share_check_cron: str = Field(default='0 3 * * *', max_length=100)

@model_validator(mode='after')
def _validate_cron(self):
    if self.share_check_mode == 'cron':
        expression = (self.share_check_cron or '').strip()
        if len(expression.split()) != 5 or not croniter.is_valid(expression):
            raise ValueError('cron 需要 5 段表达式，例如 0 3 * * *')
        self.share_check_cron = expression
    return self
```

`organizer.share_check_tick()` 改为按模式取下一次执行时间：

```python
if mode == 'cron' and not cron:
    mode = 'interval'        # 表达式缺失时退回间隔，不把整块功能静默关掉
if mode != 'cron' and hours <= 0:
    ...清掉计划时间并返回...  # 间隔模式的「0 = 关闭」保持不变
```

新增两个方法：

| 方法 | 作用 |
| --- | --- |
| `next_share_check_due(mode, hours, cron, now)` | 按模式分派 |
| `next_cron_due(cron, now)` | `croniter(cron, 当地时区 now).get_next()`，并保证至少 30 秒后才触发，避免立即重复 |

间隔模式仍然走原来的 `next_check_due(hours, now)`，03:00 锚点行为一个字没改。

### 前端

分享监控卡片改成「间隔 / Cron」单选：

```
存活检测  [间隔][Cron]                     [ 24 ] 小时
存活检测  [间隔][Cron]        [ 0 3 * * * ] 分 时 日 月 周
```

说明文案随模式变化：间隔模式显示「每 N 小时检查一次分享」，Cron 模式显示「按 Cron 表达式检查分享，只在明确失效时撤销」。

## 边界行为

| 场景 | 结果 |
| --- | --- |
| `mode=cron` 且表达式为空 | 退回间隔模式 |
| `mode=cron` 且小时为 0 | **仍然执行**（小时只对间隔模式有意义） |
| `mode=interval` 且小时为 0 | 关闭，并清掉已排的计划时间 |
| 表达式不是 5 段 / 语法错误 | 保存时 422，提示 `cron 需要 5 段表达式，例如 0 3 * * *` |
| 表达式永不成立（如 `0 0 30 2 *`） | `max_years_between_matches=8` 直接抛错，不会静默挂住 |

## 验证

`tests/test_share_schedule.py` 8 项通过，其中新增 3 项：

1. `test_next_cron_due_follows_the_expression`：`0 3 * * *` 从 01:00 算出当天 03:00；`0 */6 * * *` 算出 06:00。
2. `test_share_check_runs_on_cron_schedule_even_with_zero_hours`：Cron 模式下 `share_check_hours=0` 仍会跑一轮，并把计划顺延到下一次。
3. `test_interval_mode_still_closes_on_zero_hours`：间隔模式小时=0 不写计划时间。

原先的 `test_share_check_runs_once_per_due_time`、`test_next_check_due_is_anchored_to_three_am` 保持不变并通过。

## 升级

```bash
cd "/path/to/StrimKeep"
docker compose up -d
curl -s http://127.0.0.1:9000/health/ready
```

## 回滚

```bash
cd "/path/to/StrimKeep"
sed -i s/strimkeep:0.8.11/strimkeep:0.8.10/ docker-compose.yml
docker compose up -d
```

## 相关版本

- 0.8.10：STRM 失效清理不再被账号级 revision 阻断
- 0.8.9 / 0.8.8 / 0.8.7：任务中心桶与清理入口
