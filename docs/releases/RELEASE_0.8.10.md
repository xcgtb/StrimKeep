# StrimKeep 0.8.10

修「失效 STRM 清理」长期不生效：账号级 revision 一变就跳过，导致这个功能在现场几乎从未真正执行过。

## 现象

`strm_sync` 任务反复出现同样的收尾：

```
完整清单已核实；清理失效受管文件 0 个。扫描期间网盘内容发生变化，已跳过失效清理；请重新同步。
```

现场 `#92`、`#8`、`#3` 三个任务全是这条，`清理失效受管文件` 始终是 0。

## 原因

`sync.py` 在执行清理前比对账号级时间戳：

```python
revision = self.store.meta('sync_revision:' + account)
if saved['manifest_revision'] != revision:
    warnings.append('扫描期间网盘内容发生变化，已跳过失效清理；请重新同步。')
```

而 `sync_revision` 由 `invalidate(account)` 写入，它自己的注释就是 **"Invalidate before a possible remote mutation"** —— 只要有任何远端操作（转存、整理、改动别的目录）就先失效。所以**只要扫描期间你有转存在跑，这行必然命中**，清理永远不执行。

## 改法

把这条判断从「阻止」降为「只记日志」，由 `cleanup()` 内部已有的三层保护兜底：

1. `if old['file_id'] in present: continue` —— 清单里有的直接跳过；
2. `if old['updated'] > started: continue` —— 扫描开始后才登记的台账跳过；
3. `live_file_exists()` 逐条确认文件真的不在了才删；文件还在但脱离范围时 `raise UncertainState` 中止清理，不会误删。

```python
if saved['manifest_revision'] != revision:
    self.store.event('strm_sync', '扫描期间网盘内容有变化，仍继续核对失效输出（逐条 live 确认）。')
cleanup_removed, cleanup_warnings = await self.cleanup(jid, config)
```

## 请求量说明（重点）

- **扫描逻辑一行没动**，还是同一次全量扫描，不会因为这次改动多扫一遍云端。
- `cleanup()` 只遍历本地 `output_registry` 台账，**不复扫目录**。
- 台账在清单里的直接 `continue`，**零请求**；只有「清单里没有」的候选才打 **1 次** `live_file_exists`。
- 现场 776 个 STRM 全部可播放、云端未删文件，所以这次改动后**大概率是 0 次额外请求**。

## 验证

- `test_sync_sweep` / `test_sync_stale` / `test_sync_orphans` / `test_sync_isolation` 四组全绿（10 + 3 + 1 + 4 passed）。
- 镜像内确认 `仍继续核对失效输出` 存在；冒烟 `/health/ready` 返回 0.8.10。
- 生产容器运行 0.8.10，健康检查 ready。

## 升级 / 回滚

```bash
cd "/path/to/StrimKeep"
docker compose up -d
# 回滚：sed -i s/strimkeep:0.8.10/strimkeep:0.8.9/ docker-compose.yml && docker compose up -d
```

## 相关版本

- 0.8.9：「过滤」分类口径（见 [RELEASE_0.8.9.md](RELEASE_0.8.9.md)）
