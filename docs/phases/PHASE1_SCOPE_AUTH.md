# 第一阶段：扫描目录唯一授权（问题定位 + 改造方案 + 验收用例）

基准：现场源码 `0ea1276`（0.8.41）。本文只做定位与方案，**不含代码改动**。

## 一、频道三种落盘方式的现状（实测）

配置结构见 `channel_monitor.py:42 RULE_DEFAULTS`：

```
share_mode, share_cid, offline_cid, magnet_cid, ed2k_cid, include, exclude, routes
```

| 方式 | 代码位置 | 目标目录来源 | 关键词分流 | 落盘后 |
| --- | --- | --- | --- | --- |
| 115 分享转存 | `channel_monitor.py:407 _transfer` | `share_cid` | **支持**：`route_target()` 按 `routes.keywords` 覆盖 `share_cid`（:410-413） | `transfer.enqueue(..., auto=False)`（:523）→ 不建整理任务 |
| 磁力 | `channel_monitor.py:383` | `magnet_cid` | **不支持** | `_offline(..., organize=False)`（:421） |
| ED2K | `channel_monitor.py:383` | `ed2k_cid` | **不支持** | 同上 |
| 115 分享 STRM | `channel_monitor.py:445 _strm` | 本地待整理目录 | 不适用（不落 115） | 生成待整理 STRM，由 `share_scan_tick` 接手 |

**与你的要求不符的一处**：`磁力、ED2K 也遵守对应的目标选择规则`——现状只有 115 分享转存走 `routes`；磁力和 ED2K 各自用固定目录，命中关键词也不会分流。

**已符合要求的**：三种方式都保留；磁力/ED2K 硬编码 `organize=False` 只落盘；分享 STRM 走独立链路、不转存视频、不改云端文件。

## 二、整理任务的入队路径（全部 7 条）

| # | 路径 | 位置 | 扫描范围从哪来 | 过授权校验 |
| --- | --- | --- | --- | --- |
| 1 | 手动整理 / 定时扫描 | `main.py:784`、`tasks.py:138` | 当前 `organize_config` | ✅（`tasks.config('organize')` 校验配置合法） |
| 2 | 频道 115 转存 | `channel_monitor.py:523` | —— | 不建任务（`auto=False`），仅 `tasks.wake('organize')` |
| 3 | 频道 磁力/ED2K | `channel_monitor.py:421` | —— | 不建任务（`organize=False`） |
| 4 | 频道 分享 STRM | `channel_monitor.py:445` | 本地待整理目录 | 独立链路，由扫描接手 |
| 5 | Web/TG 转存后整理 | `transfer.py:185`（子任务） | 入队时校验 `dest ∈ source_cids` | ⚠️ 只校验 `dest`，实际扫描的是 `batch` |
| 6 | **Web/API 离线完成** | `offline_api.py:345 _enqueue_organize` | **强制覆盖** `source_cids=[target_cid]` | ❌ |
| 7 | **离线兜底轮询** | `organizer.py:637 offline_scan_tick` | 离线配置目录，与 `source_cids` 无关 | ❌ |
| 8 | **任务恢复** | `organizer.py:2490 run_job` | 任务 config 快照 | ❌ |

（#1 与 #5 已在正文说明，编号按实际执行顺序排列。）

## 三、四个缺口（都能绕过扫描授权）

### 缺口 A：Web/API 离线完成直接改写扫描范围

```python
# offline_api.py:347
config = self.engine.tasks.config('organize')
config = {**config, 'source_cids': [row['target_cid']], ...}
```

`source_cids` 被替换成离线目标目录，不判断该目录是否在用户配置的扫描范围。**频道路径不受影响**（频道传 `organize=False`），但 Web/API 提交时 `auto_organize` 默认 `True`（`offline_api.py:54`），因此走这条分支。

### 缺口 B：离线兜底轮询不看授权

`organizer.py:637 offline_scan_tick()` 每 `offline_scan_minutes` 轮询离线配置里的目录，`offline_new_videos()` 挑「不在 `organized_files` 里的视频」直接入队。目录来源与 `source_cids` 无关。

同处还有两个已知问题：`offline_scan_signature` 是**全局签名**（跨目录互相压制）；只挑「直接视频，为 0 时最多再看一个子目录」，深目录会漏。

### 缺口 C：执行时用任务快照，不复校

整理扫描直接遍历任务 config 的 `source_cids` 快照，随后 `process()` 直接 `move`/`rename`/`recycle`。**文件在排队期间被移出授权范围，仍会被移动或回收。**

### 缺口 D：任务恢复不复核

`run_job()` 沿用旧 config。配置收缩后，旧任务继续对已失去授权的目录动手。

## 四、改造方案（最小范围）

### 唯一原则

**落盘永远成功；能否整理，只看目标目录是否在「扫描范围」内（含子目录）。在 → 由扫描模块建精确批次任务；不在 → 只落盘。**

不引入排除目录、不引入策略开关、不动频道三种方式、不动分享 STRM。

### 4.1 新增一个授权函数（`organizer.py`）

```python
async def scan_scope_allows(self, cids):
    """把候选目录按真实父子链判定是否在待整理范围内。返回 (allowed, rejected)。

    口径与现有 validate() 一致（organizer.py:432）：用 cloud.directory(cid)
    取真实祖先链，判断是否与 organize_config.source_cids 有交集；
    读不到目录链的一律按拒绝处理（不确认就不动）。
    """
```

复用 `validate()` 已有的 `cloud.directory()` 调用方式，不新增 115 接口。

### 4.2 三处调用

| 位置 | 改动 |
| --- | --- |
| `offline_api._enqueue_organize` | 先判定 `target_cid`；不在范围 → 走「仅落盘」分支（任务 `completed`，消息与事件写明「目标目录不在待整理范围」）。**不再改写 `source_cids`** |
| `organizer.offline_scan_tick` | 轮询前过滤：只保留通过判定的目录；未授权目录跳过，每轮合并一条日志。顺带把全局 `offline_scan_signature` 改成按目录签名 |
| `transfer.run`（organize_after） | 判定 `dest` **和** `batch`；任一越界 → 不建整理任务，转存任务照常完成并说明原因 |

### 4.3 执行前与恢复时复校

- 扫描阶段取到 `files` 后、`process()` 之前：重新判定 `config['source_cids']`，把被移出的目录整批跳过并留痕，其余照常处理。
- `run_job()` 进入主流程前调用同一函数，恢复后的任务自动收缩到当前授权范围。

### 4.4 磁力/ED2K 补上关键词分流

`channel_monitor.py:383` 现在直接取 `rule[kind+'_cid']`。改为与 115 转存一致：先过 `route_target({'routes': rule['routes'], 'target_cid': rule[kind+'_cid'], 'target_label': rule[kind+'_label']}, text)`，命中关键词则用分流目录，未命中保持原目录，未配置目录仍然跳过。

为此 `routes` 的目标需要能同时承载「分享目录 / 磁力目录 / ED2K 目录」。两种做法：

- **方案 1（改结构）**：`routes` 每条增加 `kinds: ['share','magnet','ed2k']`，一个关键词可分别指定三类目标；
- **方案 2（改字段）**：`routes` 每条增加 `magnet_cid` / `ed2k_cid`，与现有 `target_cid`（分享）并列。

建议方案 2：改动小、与现有 `RULE_DEFAULTS` 的 `magnet_cid`/`ed2k_cid` 命名一致，前端只需在每条分流规则里多两个目录选择器。

### 4.5 频道三种方式保持不变

- 115 分享转存：`auto=False` 不变（落盘后由扫描接手）。
- 磁力/ED2K：`organize=False` 不变。
- 分享 STRM：`_strm` 不变。
- 「仅落盘」的含义按你的文档：当前获取任务不触发整理；若目录恰好在扫描范围内，定时扫描仍会处理——这一点会在事件里写明，不做"永久排除"的承诺。

## 五、验收用例

| # | 场景 | 期望 | 对应测试 |
| --- | --- | --- | --- |
| 1 | 频道转存到 AV 目录 | 落盘成功，不建整理任务 | `test_scope_authority.py::test_channel_transfer_outside_scope_only_lands` |
| 2 | 磁力离线到普通目录 | 落盘成功，兜底轮询也不入队 | `test_offline_scan_respects_scope` |
| 3 | 转存到待整理目录 | 建**限定本批**的整理任务（`source_entries` 只含本批） | `test_transfer_waiting_dir_enqueues_exact_batch` |
| 4 | 非扫描目录请求整理 | 拒绝建任务，转存本身成功 | `test_enqueue_rejects_unauthorized_scope` |
| 5 | 入队后文件被移出范围 | 执行时跳过该文件，其余继续 | `test_file_moved_out_of_scope_is_skipped` |
| 6 | 重启恢复旧任务 | 复校后收缩范围，越界部分跳过 | `test_resume_revalidates_scope` |
| 7 | 手动扫描 | 正常 | 既有 `test_organize.py` |
| 8 | 定时扫描 | 正常 | 既有 `test_scheduler_guards.py` |
| 9 | 分享 STRM | 功能不回退 | 既有 `test_share_*.py` |
| 10 | 磁力/ED2K 命中关键词分流 | 落到分流目录而非默认目录 | `test_channel_offline_routes` |

真实环境（NAS）：建 `待整理/电影`、`AV资源`、`普通资源` 三个目录，跑 #1 #2 #3 #5 #10，对比「是否建任务」与「115 上实际目录」。

## 六、风险与回滚

| 风险 | 控制 |
| --- | --- |
| 判定过严导致正常整理被拒 | 拒绝只跳过不报错，写事件带原因；#7 #8 守住手动/定时 |
| 每文件多一次 `directory()` | 任务内按父目录缓存 |
| 旧任务因范围收缩变 `partial` | 只影响越界文件，其余照常完成 |
| 磁力/ED2K 加路由影响现有配置 | `routes` 新字段缺省为空 → 行为与现在一致 |

回滚：改动集中在 `organizer.py` / `offline_api.py` / `transfer.py` / `channel_monitor.py`（+ 前端分流规则表单），逐项 `git revert` 即可；`routes` 新字段为空时行为与 0.8.41 等价。

## 七、本阶段不做

- 不重写整理主链路（第二阶段）
- 不动分享 STRM 管线
- 不改洗版/命名/杜比共存规则
- 不把离线目录加入扫描目录
- 不把归档目录纳入待整理扫描
- 不引入排除目录（按你的口径：目录隔离靠「待整理」与「其他资源」平级配置实现）

## 八、两个待确认

1. **磁力/ED2K 分流的字段形态**：方案 2（`routes` 每条加 `magnet_cid`/`ed2k_cid`）可以吗？
2. **Web/API 离线提交时目标不在扫描范围**：按方案是「落盘成功 + 不整理 + 事件说明」。另一种是「提交时就提示该目录不会被整理」。你要哪种？
