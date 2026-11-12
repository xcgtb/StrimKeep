# StrimKeep 0.8.42

收拢扫描授权：**能否整理只看目标目录是否在「待整理目录」范围内，落盘本身永远成功。**

## 问题

0.8.41 有四个入口能绕过扫描配置直接建整理任务：

| 入口 | 位置 | 实际行为 |
| --- | --- | --- |
| 离线完成 | `offline_api._enqueue_organize` | `source_cids` 被强制替换成离线目标目录 |
| 离线兜底轮询 | `organizer.offline_scan_tick` | 轮询离线配置目录，与 `source_cids` 无关 |
| 转存后整理 | `transfer.run` 的 `organize_after` | 只校验过 `dest`，实际扫描的是 `batch` |
| 任务恢复 | `organizer.run_job` | 沿用任务快照，不复核当前配置 |

另外 `transfer.enqueue` 在目标不在 `source_cids` 列表里时**直接抛 ValueError**，于是把资源分流到 `待整理/电影` 这类子目录时，转存会直接失败。

## 改法

新增两个方法（`organizer.py`）：

```python
async def allowed_scan_cids(self, cids)   # 返回允许整理的目录列表
async def scan_scope_allows(self, cids)   # 任一通过即 True
```

判定口径：目录等于某个 `source_cids` 根，或它的**真实后代**——用 `cloud.directory(cid)` 取 115 的祖先链判断，**不做字符串前缀比较**；读不到链一律不授权。

四处调用：

- `offline_api`：目标不在范围 → 只落盘（不再改写 `source_cids`，不再写额外事件）
- `offline_scan_tick`：只轮询授权目录，未授权的静默跳过
- `transfer.run`：建 `organize_after` 前校验 `dest`
- `run_job`：进入整理前复校，目录已移出范围的整批取消并留一条说明；**没有 `organize_config` 时跳过复校**（不影响一次性任务与测试）

`transfer.enqueue` 不再因为目标不在「待整理根」列表里报错。

## 未改动的部分

- 频道三种落盘方式（115 转存 / 分享 STRM / 磁力、ED2K 离线）
- 频道关键词分流（`routes`）
- 分享 STRM 的识别、命名、洗版、输出登记、Emby 联动
- 整理、洗版、STRM、Emby 的全部业务规则

## 验证

| 范围 | 结果 |
| --- | --- |
| `test_organize` + `test_channel_monitor` + `test_share_profile` + `test_share_flat_generation` | **67 passed** |
| 新增 `test_scan_scope_authority.py`（根目录 / 子目录 / 无关目录 / 无配置 / 读不到链 / dict 链） | **4 passed** |
| `test_transfer.py` | 容器内 20 项通过后挂起；**干净 0.8.41 基线同样挂起**，属既有问题 |

真实环境（NAS）：容器运行 `0.8.42`，`/health/ready` 返回 `ready`。

## 升级

```bash
cd "/path/to/StrimKeep"
docker compose up -d
curl -s http://127.0.0.1:9000/health/ready
```

## 回滚

```bash
cd "/path/to/StrimKeep"
sed -i s/strimkeep:0.8.42/strimkeep:0.8.41/ docker-compose.yml
docker compose up -d
```

## 尚未做

- 磁力/ED2K 的关键词分流（现状用固定 `magnet_cid` / `ed2k_cid`）
- 排除目录机制（按目录隔离：待整理与非扫描目录平级配置）
- 第二阶段（逐文件状态、云端写操作回读核验、断点续跑、字幕冲突）
