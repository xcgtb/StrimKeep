# 工作日志

## 0.9.150 — 治理扫描提速 + 单库/缺集死分支清理

用户要求优化两件事：`/preview` 自动建计划时的重复全量扫描，以及 0.9.149 记下的死分支。

### 改动

**1）扫描提速（5.7s → 3.6s）**

`/inventory/preview` 除了判定，还会顺手建好「一键清理」的删除计划。原来 `capture()` 在
建计划时把整库重扫两遍（一次拿 rows，一次给快照做范围复核），每次 0.6~0.9s，加上逐文件
指纹，一次预览白等 2~3 秒。

现在预览把刚算出的库存随请求带过去（`result['_reuse']`，返回前 pop 掉，不进响应也不进
`inventory-last-scan`）：

- 建计划直接复用 `scoped['rows']` 与 `scoped['projection']`（少扫 1 遍、少 project 1 次）；
- 快照的安全复核换成轻量 `recheck()`：只重扫目录 + 重算来源指纹，再和计划里的 rows 比。
  一旦发现有文件变成「来源核不上」，退回完整 `partition`（宁可慢也不猜）。

实测（现场 208 部 / 1078 个 STRM）：治理扫描 **5.7s → 3.6s**，库存全量 0.83s → 0.75s。
安全面不变：计划仍带快照指纹，执行前仍逐作品核对。

**2）死分支与死参数清理**

- 删除 `governance_preview(allow_partial=…)`（全局闸撤掉后无人使用）及其调用点实参。
- 删除 `inventory_operations` 里 `projection.get('complete')` / `preview.get('scan_complete')`
  三处恒真检查——库存「完整」不再是任何计划的前置条件。
- 删除单库里永不产出的 `episode_gap` / `covered_episode` 分支：
  - `single_actions` 里两个 `elif` 和 `gap_sides` 守卫；
  - `inventory_api.scan_selections` 的原因过滤与「episode_gap 同侧冲突」判断；
  - `Selection.reason` 的 Literal 收窄为 `'quality'`；
  - 计划接口里 `episode_gap` 与 `single_gaps` 的耦合判断改成只认 `single_quality`。
- 前端「单库清理」提示改成实际含义：`多版本=同季同集多份留最优；缺集季=残次品（前序缺失/中间断层），逐侧清理。`
  （设置项 `single_gaps` 仍然有效，它管的是双库动作里带 `incomplete_sides` 的残次品。）

### 验证

- 195 项测试通过；`test_inventory_operations` 两处改成新语义（全局闸不再拦、只产 quality）。
- 现场 0.9.150：`dual 1 / single 0 / multi 2 / blocked 2（静默期）`，bundle `Inventory-Cw03bLdv.js`
  已含新文案。

### 备注

- 清理「空计划」时按 15 分钟窗口批量删过 `meta` 里的 `planned` 记录（含几条不是这次验证
  产生的），只删了计划记录，没动任何文件；以后按 plan_id 精确删。

## 0.9.149 — 双库治理向 1.2.12 收敛：单库去重粒度、跨库优先级、残次品边界

用户反馈「单库去重和跨库处理很模糊，也不是对标 1.2.12」。逐行对照
`/tmp/up1212/app/governance.py`（`_dedupe_lib_versions`、`_build_plan_with_libs`、
`_emit_season_act(s_full)`、`_emit_season_residuals`、`_emit_multi_protect_compare`、
`_emit_share_protect_acts`）后，确认了三处真实偏差并修正。

### 改动

- **单库去重粒度**：`_dedupe_candidates` 从「每集一条」改成「一季一条」，把该季所有
  重复集合并成一条结果（参考实现的 `dup_eps`），并新增 `losers`（要删的文件）、
  `keeps`（每集保留项）、`dup_episodes`、`dup_count`、`reason_label='库内多版本-删低版'`。
  一季十几集重复不会再在列表里冒出十几行。
  `inventory_operations.single_actions` 改为优先按 `losers` 逐路径核对（目标漂移就要求
  重新预览），电影仍保留 `files` + `winner` 的旧形状。
- **跨库优先级**：1.2.12 在多季保护 / 决策模型命中后直接 `continue`，不再做库内去重。
  我们原来仍会追加 `single` 行，导致同一部剧既在「跨库动作」又在「库内多版本」里出现。
  现在 `decision != 'quality_first'` 或多季保护生效时不生成去重行。
- **残次品边界**：与参考实现一致的部分（两侧都有的季，逐侧判定、缺集优先、写清缺什么）
  保持不变；**有意差异**（用户明确要求「另一侧没有这一季也照样给清理动作」）改为显式标注：
  新增 `single_side=True`，配合已有的 `lost_episodes` / `clears_show`，前端在动作卡片上
  提示「另一侧没有这一季：删掉后这一季在两库都不存在」。
- 删除死参数 `is_tv_identity`（1.2.12 里「同身份有剧集则不当电影去重」的保护由电影分支
  提前 `continue` 完成，该参数从未为 True）。
- `inventory_partition` 的来源核验先整批做一次，整批失败才退化到逐文件（逐文件在
  1000+ 文件时是几千次查询）。

### 验证

- 195 项测试通过（新增 4 项：单边季残次的显式标注、一季一条去重、多季保护抑制去重、
  决策模型抑制去重；`test_user_rules` 的库内多版本用例同步改成新粒度）。
- 现场（0.9.149）：`dual 2 / single 0 / multi 2 / blocked 0`；其中「走马上任」是单边季
  残次品（`single_side=1`、`clears_show=1`），现在会在卡片上说明「删掉后这一季两库都没有」。
- 性能实测：库存全量 0.83s；治理扫描 5.7s，其中「扫描 0.24s + 分区核验 0.59s + 判定 0.87s」，
  剩下约 4s 是 `/preview` 顺带自动建计划（`capture()` 又跑了两遍全量扫描 + 逐文件指纹）。

### 已知未做

- `/preview` 自动建计划时的重复全量扫描还没合并（可以复用预览刚算出的 rows 再只做一次
  范围复核），这是下一轮的性能项，不影响判定结果。

## 0.9.148 — 台账解耦：目录是库存事实，台账只做来源索引

用户的判断是对的：片库映射和双库治理被台账绑死了。原来的链路是
`scan() → project(registered_rows(store))`，而 `project()` 里
`'complete': not issues`、`governance_preview` 又用
`if not snapshot['complete']: blocked += inventory_incomplete; return`
——**一条与本作品无关的坏登记行就能让两库治理整页空**。另外
`capture()` / `partition()` 还要求「目录与台账完全一致」才肯生成计划。

### 改动

- 新增 `inventory_fs.py`：直接扫 `媒体库` + `分享影视库` 两个库根，把落地的 STRM
  变成库存行。台账里找得到就带上真实登记编号，找不到就用 `fs-<sha256>` 合成编号
  （`registered=False`），含义是「本地有这个文件、来源尚未登记」。
- `inventory.py`：`project()` 的 `complete` 只表示「这次扫描跑完了」，
  单文件解析不了记进 `issues`（并新增 `issue_count`），不再升级成整库不可用；
  `work_complete()` 改成只认**本作品目录内**的异常，别的作品目录里的问题、
  以及认不出作品的散文件都不再牵连。
- `inventory_partition.py`：去掉 `audit_roots` 的「台账=目录」硬闸，逐文件判定——
  路径认不出作品、或来源核不上的文件只进 `unsupported`，不再隔离整部作品。
- `inventory_sources.py`：分享侧缺登记也能出证据（以本地文件+指纹为准）；
  网盘侧缺登记直接拒绝（`网盘源文件未登记，无法核实要回收的 115 文件`）。
- `inventory_snapshot.py`：撤掉台账一致性前置条件；新增 `unsupported` 进快照
  （版本 2），这些文件在计划期间增删同样会让计划失效。
- `inventory_scope.py`：`verify_work` 改成「目录成员 + 网盘登记集合」双核对：
  计划文件必须在盘上且指纹未变，网盘登记必须与计划一致；分享侧登记缺行不拦。
- `inventory_delete_adapter.py`：分享侧支持合成编号（删完不改台账，没有行可改）；
  网盘侧遇到合成编号直接拒绝；`cleanup_empty_work` 改成看目录里还有没有 STRM
  （以前查台账，未登记的残留会被当成「已经空了」）。
- `inventory_operations.py` / `_api.py`：新增 `_reject_unsupported`，
  选中的文件里只要有一个来源核不上就明确报错并给出路径，不猜着删。
- `inventory_api.py`：`scan()` 改用目录扫描；没有 115 账号不再让整页治理不可用
  （只是网盘侧进不了计划）；治理扫描的隔离计数改成 `unsupported + issues`。
- `inventory_preview.py`：删掉 `inventory_incomplete` 全局闸；每条结果带
  `unregistered: {cloud, share}`，前端可以按作品提示「网盘源未登记 N」。
- 前端 `Inventory.vue`：治理列表（桌面表格 + 手机卡片）显示「网盘源未登记 N」；
  原来的「扫描中发现待核实记录，不能创建计划」改成说明「N 个文件来源不可核实，
  只影响这些文件」。

### 删除安全

没有放宽任何网盘侧门槛：网盘文件的来源指纹（`organized_files` +
`output_registry` + `owned_outputs`）仍然是删除前提；计划仍带快照指纹，
执行前逐作品核对、逐文件核对指纹。分享侧以前「没有登记行就不能删」，
现在改成「以文件为准」，删完不需要改台账。

### 验证

- 190 项测试通过（新增 `test_inventory_fs.py` 7 项：空台账仍能列作品、
  登记行挂到同一文件、撤销行退回归属、分享可删/网盘拒删、逐文件隔离、
  预览无全局闸、散文件不隐藏作品；`test_inventory_operations_api` 新增
  「台账里没有这一行也能清分享侧」端到端用例）。
- 现场（0.9.148）：库存 210 部 / 1096 个 STRM，未登记 0，issues 0，扫描 1.4s；
  治理 dual 3 · single 0 · blocked 0 · unsupported 0。
- 关键验证：在容器里用 `scan(root, roots, None)`（**完全不读台账**）扫描，
  仍然得到 1096 个文件 / 210 部作品 / 0 issues —— 台账已不再是库存前提。

## 0.9.142 — 静默期改成真正的「入库静默期」（不再看目录 mtime）

用户指出 0.9.141 的口径错了：**不是「目录 15 分钟内有变动」**，而是**「入库 15 分钟内的作品先不进治理清单，
过了 15 分钟自动纳入」**。查 1.2.12 源码确认配置项的注释就是「入库静默期（分钟）：目录 15 分钟内有新入库的标题
暂不进入治理队列」，但它的库是被外部工具写入的目录，`os.stat(目录).st_mtime` 天然等于「有新入库」；
我们这套 STRM 是自己生成的，同一个目录 mtime 还会被**删除、清理、补写侧车**刷新，
于是刚清完的作品又被静默 15 分钟，表现为「明明超过 15 分钟还是 blocked」。

### 改动

- `governance_preview` 的静默判定从 `max(目录 mtime)` 改为 `max(文件 mtime)`：
  只看 STRM 文件本身的入库时间（`file_mtime_ns`），删除/清目录不再触发静默。
- blocked 行字段：`directory_changed_at` → `ingested_at`（最近入库时间），`quiet_remaining` 保留；
  另给一句 `detail` 说明。
- 前端文案：「入库静默期 15 分钟（最近入库 18:34:05，还剩 3 分钟）」。
- 新增回归测试 3 条：
  - 目录刚被刷新、文件入库很久 → **不挡**；
  - 有文件刚入库 → 挡，且 `quiet_remaining` 在 1..15、`ingested_at` 正确；
  - 没有入库时间 → 视为没变动，**不无限期挡**。

### 实测（线上）

- 静默期临时调到 1440 分钟 → `伟大的长征` 被挡，`ingested_at` = **08:28（STRM 入库时间）**，
  而不是旧的目录变动 18:34 —— 口径已切到入库时间。
- 调回 15 分钟 → `blocked=0`。
- 后端 182 项测试全绿。


## 0.9.141 — 静默期（recent_activity）把时间说清楚，并自动补扫

用户问「伟大的长征早过了 15 分钟静默怎么还被挡」。核对结果：那次 blocked 是**当时正确**的
——目录 mtime 是 18:34，扫描发生在 18:34–18:49 之间，正好落在 15 分钟窗口里；
19:00 再扫就没有这条了。问题在**界面看不出来**：只写「静默期：15 分钟」，既没有目录变动时间，
也没有还剩多久，看起来像永久挡住。

### 改动

- `governance_preview` 的 `recent_activity` 行新增 `directory_changed_at`（目录变动时间）和
  `quiet_remaining`（还剩几分钟，向上取整）；判定本身不变（`max(目录 mtime) >= now - 静默期`）。
- 治理页「需人工核实」里显示成：
  `静默期 15 分钟（目录 18:34:05 有变动，还剩 3 分钟）`
- 进入治理页签时，如果`上一次扫描`已经超过 2 分钟，自动补扫一次（有预览、设置未改、不在忙才触发），
  时间相关的判定（静默期、连载状态）不会一直停在旧结果上。

### 验证

- 后端 179 项测试全绿。
- 线上验证：临时把静默期调到 180 分钟 → 预览出现 `recent_activity` 且带
  `directory_changed_at` / `quiet_remaining`；恢复 15 分钟后该条自动消失。


## 0.9.140 — 片库映射与双库治理的删除/扫描同步

两条功能共用同一套「投影 → 计划 → 执行」链路，这轮把剩下不一致的地方补齐。

### 1. 删除后两个页面立刻一致

- 新增 `app.state.inventory_invalidate`：片库映射缓存（60 秒 TTL）在每次清理执行结束
  （成功或失败）后立即失效，不用等 TTL 过期。删完再点治理页/映射页看到的就是同一份库存。
- 前端执行完成时本来就会清空 `preview`/`snapshot` 并重新拉当前页，配合上面的失效不再出现
  "这边删了、那边还显示"的情况。

### 2. 执行期核对只做一次（按作品）

`verify_work` 每次都要遍历整部作品的目录 + 核对登记/来源，31 集的季就是 31 次全量遍历。
现在同一次执行里按 `work_id` 只核对一次；逐文件的本地指纹、网盘来源身份仍由删除适配器
（`_prepare_cloud`）单独确认，安全约束没有放松。

### 3. 批量删除后清空作品目录

单条路径一直会调 `cleanup_empty_work`，批量路径漏了：整部作品删完后空的 TMDB 目录会留下。
现在批量结束按作品去重调用一次，和单条行为一致。

### 4. 混合计划也走批量

原来只有「整份计划全是网盘侧」才批量；治理计划经常网盘+分享混在一起，就退回逐文件。
执行器改成按顺序分段：连续的网盘动作合成一批（每组最多 200 条，`recycle_many` 一次提交），
分享动作仍逐条（本地 unlink 本来就快），进度、失败即停、快照校验的语义不变。

### 5. 实测

```
片库映射 /mapping/page（缓存命中）   0.02 s   15 KB
片库映射 /mapping/page?refresh=1     0.50 s   15 KB
治理预览 /inventory/preview          1.70 s   27 KB
整库投影 /inventory                  1.60 s  464 KB
```

后端 179 项测试全绿（执行器批量、核查三元组、身份口径、删除后缓存失效等）。


## 0.9.138 — 所有删除都失败的真因：核查函数成功分支返回的是字典

用户报「片库映射直接都不能删除了」。复现：建计划 200 正常，一执行就
`too many values to unpack (expected 3)`，进度 0/4。
`璀璨之城` 之前两次（17:15 / 17:42）也是同一条报错 —— 也就是说从 0.9.131 引入
`_prepare_cloud` 起，**所有**删除动作都在第一步就炸了，跟网盘、权限无关。

### 根因

`_prepare_cloud` 约定返回 `(info, status, message)`：

- 失败分支返回三元组 ✓
- 成功分支却写成了 `return {…}`（裸字典）✗

调用方 `info, status, message = _prepare_cloud(...)` 解包字典 → 10 个键 vs 3 个目标 →
`too many values to unpack (expected 3)`。异常分支当时也漏了参数，两条路径都错。

### 修复与验证

- 成功分支补成 `return {…}, None, ''`；异常分支补回三元组并带上异常摘要
  （`核查失败（TypeError: …）`，用户能看到真正原因而不是空报错）。
- 新增两个用例：
  - `test_prepare_cloud_success_returns_info_status_message`（成功分支三元组 + info 字段）
  - `test_prepare_cloud_returns_three_values_even_on_error`（异常分支三元组）
- **真机验证**：`璀璨之城 (2026)` 网盘 4 集（E12–E15）建计划 → 执行 → `completed 4/4`，
  网盘文件数 4 → 0；执行前逐条核验、执行后本地 STRM 与空目录清理都正常。

### 同时上线的可读性改动（0.9.136/0.9.137）

- 「最近清理计划」新增类型列（手动清理 / 双库洗版 / 单库清理 / 双库治理），
  弹窗标题/说明按类型与侧别显示；旧计划标「旧计划」。
- 失败信息带具体文件路径；卡在 executing 超过 15 分钟的计划自动标为失败
  「执行中断（进程重启或超时），未自动重试」。
- 后端 179 项测试全绿。


## 0.9.134 — 片库映射补齐 1.2.12 的手机端多选细节

对着 `static/js/mapping.js` 的 `mappingBatchBindSwipe` / `mappingBatchSyncContext` /
`mappingBatchSelectFiltered` 逐条搬：

### 1. 横向滑动连续勾选（原来的缺失项）

- 只有进入「多选清理」才启用（对应 `mappingBatchMode`）。
- `touchstart` 记录起点；`touchmove` 位移超过 12px 才判定方向，
  `|dx| > |dy| × 1.25` 才算「选择」，竖向手势仍然交给页面滚动。
- 判定为选择后每 18px 采样一个点，用 `document.elementFromPoint` 找卡片并逐个勾上，
  越过卡片时不会漏（采样上限 24 段，和参考实现一致）。
- 滑动期间 `preventDefault`，结束后 800ms 内抑制点击，避免滑动完误打开详情。
- 选中卡片：主色描边 + 右上角 ✓ 徽标；勾选框改成 38×38 触控区（深色底 + 白边），
  和参考实现的 `.mapping-batch-card-check` 一致。

### 2. 多选工具条

- 结果栏「多选清理 / 退出多选」；工具条显示已选数量、**全选本页**、清空、
  删除网盘库 / 删除分享库 / 删除两库，以及单次上限提示「单次最多 100 部 / 1000 个文件」。
- 切换筛选（类型/片库/分类/状态/关键词）时清空选择 —— 对应参考实现的
  `mappingBatchSyncContext`（筛选变了就重置选择，避免把看不见的旧选择带进计划）。

### 3. 与参考实现的差异（有意保留）

- 参考实现的「全选筛选结果」会跨页选中未加载的条目（后端按筛选条件展开）；StrimKeep 的
  计划上限是 100 部 / 1000 个文件，所以这里做「全选本页」，避免一次选中远超上限。
  需要跨页全选可以再加，但要同时把计划上限和后端展开逻辑一起改。

### 4. 验证

- 镜像 0.9.134 ready；前端包内确认含「多选清理 / 全选本页 / swipe-select / batch-selected /
  min-width:38px / 单次最多 100 部」。
- 后端 177 项测试不受影响（本轮只改前端）。


## 0.9.131 — 网盘侧删除提速（整批回收）+ 作品身份口径可切换

### 1. 网盘库删除慢的根因

`guarded_managed_delete` 每删**一个文件**都要做：

1. `cloud.file(fid)` 核验来源；
2. `cloud.recycle_files()` —— **整站回收站全量翻页**（回收站大时几十页）；
3. `cloud.recycle(fid, pid)`；
4. 再 `cloud.recycle_files()` 全量翻页一次证明已回收；
5. `live_file_exists(fid)`；
6. 本地 STRM 清理 + 每层空目录 `children()` 探测。

31 集的残次季 ≈ 62 次全量回收站翻页 + 上百次请求，所以"删一季要等很久"。
剧集本身是**同一个 115 目录下的连续文件**，本来就能一次批量回收。

### 2. 现在的做法

- 单条路径：复用 `cloud.recycle_snapshot()`（30 秒内的全量清单）作为"删除前状态"，
  删除后改用 `live_file_exists` 一次查询确认已离开活动空间，不再为了"证明回收"再全量翻两遍。
- 批量路径（新增 `guarded_managed_delete_many`）：网盘侧计划先逐条核验，
  再按 115 父目录用 `cloud.recycle_many` **一次请求删掉整批**，然后逐条 live 确认、
  本地清理，空目录链每组只探测一次。
- 执行器：计划里全是网盘侧动作时走整批删除（`execute_simulated(..., delete_many=...)`），
  混合计划仍逐条执行。进度、快照校验、失败即停的语义保持不变。

### 3. 作品身份可切换（不再写死）

`inventory_config.match_strategy`：

- `title_year`（默认）：规范化剧名 + 年份，TMDB 只作证据；
- `tmdb`：按 TMDB 编号配对；**同一编号被多个「剧名+年份」使用时自动回退到剧名+年份**
  （结果里 `identity_source` / `fallback` 标明，`tmdb_conflicts` 单独列出）。

治理设置里新增「作品身份」单选（剧名 + 年份 / TMDB 优先），保存后自动失效旧预览，
重新扫描生效。预览行的 `match_basis` 会写「TMDB 编号」/「剧名 + 年份」/「剧名 + 年份（TMDB 冲突回退）」。

### 4. 验证

- 后端 177 项测试全绿：新增身份口径用例（tmdb 配对 + 冲突回退、剧名模式对照）、
  执行器整批删除用例（一次 `delete_many`、进度逐条记录）。
- 线上 0.9.131 ready；`/inventory/config` 返回 `match_strategy`，映射/治理接口正常。


## 0.9.130 — 片库映射的分类改为读 Emby 媒体库（不再写死、不再按目录猜）

用户要求：分类应该读 Emby 的库，而不是写死的清单，也不是按 STRM 目录名猜。

### 实现

- 新增 `backend/strimkeep/emby_libraries.py`：按 `/Library/VirtualFolders` 读库列表
  （失败依次退回 `/Users/{id}/Views`、递归 `CollectionFolder`），再逐库
  `GET /Items?ParentId=…&IncludeItemTypes=Movie,Series&Fields=ProviderIds` 建立
  `tv:<tmdb> / movie:<tmdb> → 库名` 的映射；结果缓存 30 分钟（失败缓存 5 分钟）。
- `library_mapping.path_categories(item, library_map)`：**优先 Emby 库名**，Emby 未连接
  或未匹配时才退回目录关键词；分页参数 `category` 改为按数据里实际出现的分类校验。
- 映射页/详情/首屏响应新增 `category_source`（emby / path），`all_categories` 直接来自数据
  （不再按写死的目录清单过滤，因此 Emby 库名可以原样作为分类）。
- 新增 `GET /api/v1/inventory/emby-libraries?refresh=1`：返回读到的库、读取方式
  （VirtualFolders / Views / CollectionFolder）与失败原因，便于排查。
- 前端提示按来源显示：「分类来自 Emby 媒体库（按 TMDB 号匹配）」/「Emby 未连接或未匹配，
  暂按目录关键词分类」。

### 线上实测（0.9.130）

```
Emby 库：剧集 47 · 动漫 2 · 影片 163   （read_by=VirtualFolders）
分类下拉：影片（163）· 剧集（47）· 动漫（2）
筛选：category=剧集 → 47 部；影片 → 163 部；动漫 → 2 部；category_source=emby
```

以后在 Emby 里新建/改名媒体库（比如把 剧集 拆成 国产剧 / 欧美剧），下拉会自动跟着变，
不需要改代码。

验证：后端 175 项测试全绿（新增 Emby 分类优先、动态分类校验、分类计数三条用例）。


## 0.9.127 — 片库映射：分类筛选按真实目录走、对照按钮给反馈

### 1. 「分类一个都筛不到」

分类筛选的选项是写死的 11 个目录分类，全部来自参考实现的目录约定；用户库里只用到其中 6 个，
点其余 5 个（儿童节目 / 演唱会 / 综艺剧 / 动画电影 / 纪录片）自然是 0 部，看起来像"筛选不到"。
实际分类并非来自 Emby 媒体库，而是 STRM 路径里的目录关键词。

- 服务端 `library_mapping.category_counts` 统计真实分布，`/mapping/page` 与 `/preview`
  新增 `all_categories: [{name, count}]`（只列有作品的分类，按固定顺序）。
- 前端分类下拉改用这个列表，标签带数量：`国产剧（24）`、`外语电影（146）`…；
  标题改为「目录分类」，旁边一行说明"按目录关键词识别（如 …/剧集/国产剧集/…），与 Emby 媒体库无关"。
- 线上实测：全部 213 部 → 外语电影 146 / 国产剧 24 / 欧美剧 17 / 华语电影 17 / 日韩剧 7 / 动漫剧 2；
  带 `category=国产剧` 请求返回 24 部，`category=外语电影` 返回 146 部（服务端过滤正确）。

### 2. 「点对照好像没反应」

- 原来 `对照全部剧集` 先 `GET /inventory`（8 秒整库快照）再逐部对照，期间按钮没有 loading，
  结束后也没有任何提示；永久「待对照」的作品（TMDB 缺季）看起来就是"点了没用"。
- 现在：只对照**当前页**的电视剧，立即进入 loading + 进度（`对照中 3 / 45`），
  结束后弹汇总：`已对照 完整 17 · 缺集 2 · 在更 28；仍待对照 1 部：无瑕谎言（TMDB 缺第 2 季）`。
- 服务端已判「待对照」的卡片进入页面时自动补一次对照（同一会话只补一次），
  库存变动导致缓存失效的作品不用手动点。
- 详情抽屉的黄色提示改写：`数据不完整：TMDB 第 2 季取不到，这些季暂不能判缺集`。
- 卡片提示同样写明原因：`TMDB 第 2 季取不到；其余季仍按已播集统计`。

### 3. 验证

- 后端 172 项测试全绿；`/mapping/page` 返回 `all_categories`，分类过滤计数与服务端一致。
- 镜像 0.9.127 ready；前端包内含「目录分类 / 与 Emby 媒体库无关 / 仍待对照 / TMDB 第 … 季」。
- 当前状态：待对照 2 部（无瑕谎言 = TMDB 缺第 2 季；消失的裂痕 = 缓存随库存变化失效，进页面会自动重算）。


## 0.9.124 — 片库映射「一堆待核实」的根因：状态口径把三件事混成一个 unknown

用户问「片库映射一堆待核实什么东西」。查线上：213 部作品里 `unknown` 8 部，
外加「在更」被算成待核实。根因在 `library_mapping.remember_status` 的 `uncertain` 判断，
它把四件互不相关的事都归到 `unknown`：

1. 连载剧的未播集没有播出日期（`unknown_air_date`）—— 正常现象，未播集本来就不该算缺集；
2. 库存扫描有个别 issue（`inventory_complete=False`）—— 与这部剧的状态无关；
3. TMDB 这一季没取到（`failed_seasons`）—— 真正的「待对照」；
4. 该季没有已播集 —— 真正的「待对照」。
于是只要命中任意一条，整部剧就被打成「待核实」，连在更剧都被牵连。

### 现在的口径（对齐 1.2.12 mapping.js 的 7 个状态）

| 状态 | 含义 |
| --- | --- |
| ✓ 完整 | TMDB 已播集全部入库 |
| 缺 N 集 | TMDB 已播集里有没入库的 |
| 超 N 集 | 比已播集多（未播集已入库 / 集号错位） |
| 在更 | 连载中，只统计已播集；未播集不算缺 |
| 待对照 | 还没拿到 TMDB 季/集信息（唯一的数据缺口状态） |
| 已入库 | 电影，不按集号核对 |
| 库存待核实 | 扫描有小问题时**附加**标记，不再覆盖状态本身 |

- 服务端：`unknown` 类别删除（旧缓存/旧链接一律按 `pending` 处理并强制重新对照），
  `partial` / `unknown_air_date` / `reason` 单独返回。
- 前端：`inventoryStatus.ts` 按上表重写（每个状态带一句 `hint`），映射页加状态图例，
  详情抽屉在状态下面直接显示「为什么是这个状态」，去掉筛选里的「未匹配」。
- 治理侧：每个治理行加「查看作品详情」，切到片库映射并打开同一套详情抽屉。

### 全库刷新结果（0.9.124，50 部电视剧逐部重新对照）

```
刷新前：unknown 8 · ongoing 21 · missing 3 · extra 1 · complete 17 · movie 163
刷新后：unknown 0 · ongoing 28 · missing 3 · extra 1 · pending 1 · complete 17 · movie 163
```
—— 原来 8 部「待核实」里 7 部其实是连载剧（未播集无日期），1 部是真的没拿到 TMDB 季信息。

验证：后端 172 项测试全绿；映射首屏/详情、治理预览正常；前端包内确认含
「待对照 / 在更 / 库存待核实 / 查看作品详情 / 状态图例」。


## 0.9.122 — 治理设置重排：分组卡片 + 每项一行说明 + 固定保存条

用户反馈「治理设置一大堆挤在一起」。原来 8 个表单项（定时扫描、决策模型、多季保护、特别篇、
白名单、平局、达标率、静默期、规则顺序、单库开关）全部用 `.inventory-tools` 平铺，
长解释段落夹在中间，视觉上没有分组、保存按钮混在某个输入行里。

改成四组卡片，顺序按「先决定怎么选，再决定保护什么，最后决定什么时候不动」：

1. **怎么选**：两库都有的作品（按画质洗版 / 留网盘库 / 留分享库）、完全一样时（留哪边）、
   分享替换网盘的门槛（%）。
2. **多季与特别篇**：多季剧（防掏空 / 逐季比较）、特别篇 S00（比画质 / 不动 / 两库都清）。
3. **范围与节奏**：新入库多久内不动（分钟）、定时扫描（开关 + 间隔）、永不处理的片名关键词、
   单库清理（多版本去重 / 缺集季）。
4. **判定规则**：当前画质规则顺序 + 去影视整理改规则。

样式：每项一行卡片（`.gov-field`，标签固定宽度、控件右侧、说明 12px 灰色紧跟下一行），
分组标题带分隔线（`.gov-group h4::after`），底部 `.gov-save` 粘性保存条并显示「设置未保存」；
窄屏（≤639px）标签换行独占一行。

另外修掉一次自己引入的结构错误：曾尝试把「目录与台账核对」「最近清理计划」包进同一个两栏容器，
但两者分属不同的 `<template v-if>` 分支，容器会跨模板闭合；已回退，面板保持原样。

验证：镜像 0.9.122 ready；前端包内确认含 `gov-settings-body` / `gov-field` / `gov-save` /
「防掏空」「怎么选」「分享替换网盘的门槛」；后端 172 项测试仍全绿（本轮只动前端）。


## 0.9.120 — 按 1.2.12 完整迁移：身份=剧名+年份，其余分支逐条对齐

用户要求「逻辑完整迁移、保持兼容，唯一不同是 1.2.12 治理路径、StrimKeep 自己生成 STRM
再自己治理删除」。本轮把剩下的偏差全部对齐：

### 1. 身份键（此前最大偏差）

- 1.2.12 `core.governance_title_key`：身份 = 规范化剧名 + 年份，TMDB 只是证据，
  整理器写错 TMDB 时不能把两部片合并、也不能因此产生跨片动作。
- `inventory.project` 改为按 `(kind, 剧名, 年份)` 分组；`tmdb_ids` 记全部编号（兼容单值
  `tmdb_id`），同一个 TMDB 被两个身份使用只生成 `tmdb_conflicts` 诊断，**不再阻断治理**。
- 电影分支「同身份在任一库有分集 → 这是剧」改为按剧名+年份判断并静默跳过（1.2.12 行为）。
- 计划/删除层：按 TMDB 定位作品时若命中多部（元数据冲突）直接拒绝，避免误删；
  单库多版本、手动删除同样处理。

### 2. 标签与证据对齐 1.2.12

- 电影：`分享画质达标` / `本地画质更优`（决胜维度仍放在 `decisive` 字段与明细里）。
- 剧集：`分享择优-删本地` / `本地更全-删分享` / `本地择优-删分享`。
- 预览行带 `match_basis: 剧名 + 年份`、`match_basis_detail`、`tmdb_ids`；`identity_source` 改为
  `title_year`。映射卡同样输出 `tmdb_ids`/`year`。

### 3. 与 1.2.12 唯一的结构差异（按用户说明保留）

上游动作对象是路径集合，删本地走 CD2 深删；StrimKeep 的 STRM 是自己生成的，所以：
动作按注册表 id 定位文件，执行前用快照指纹（`inventory_plans.claim_plan`）校验，
库存/规则一变计划即失效（比上游的 15 分钟计划更严）。

### 4. 验证

- 全量（库存 18 文件 + 代理 + 1.2.12 差分 + 用户规则集）：**172 passed**。
- 新增：`test_same_title_and_year_with_different_tmdb_is_one_work`（同剧名+年份、不同 TMDB →
  同一部作品并正常洗版）、`test_same_tmdb_different_titles_are_diagnostic_not_blocking`
  （同一 TMDB 两个身份 → 只报诊断、不阻断）、`test_unreliable_records...` 允许目录出现多个 TMDB。
- 线上：`玉簟秋` 的 31 项清理已由用户执行完成（计划 `5034333…` 31/31 completed），
  因此 0.9.120 预览为 `dual 0 · single 0 · blocked 0`，库存 213 → 212 部作品；
  映射首屏/详情、治理设置、计划历史均正常。


## 0.9.119 — 双库治理按 1.2.12 深挖：残次品逐侧剔除，洗版才看完整

上一版我加了「删除前必须先确认对侧覆盖」的闸，把「前序缺失」「中间缺集」全判成需人工，
这是错的：1.2.12 里这两类就是残次品，直接剔除，而且**逐侧独立**、不跨库。

### 现在的判定（逐条对应 1.2.12 函数）

| 场景 | 判定 | 实现 |
| --- | --- | --- |
| 前序缺失（本侧从 E25 起、缺 E01–E24） | 残次品，剔除本侧整季 | `residual_flags` / `_gap_text` → `incomplete_delete_local/share` |
| 中间缺集（E01–E58 + E60，缺 E59） | 同上（描述为「中间断层…疑似被和谐」） | 同上 |
| 分享库同样两种情形 | 只删分享库（不动网盘） | 同上 |
| 单库内多版本（杜比/SDR） | 库内去重，只在本库比 | `_dedupe_lib_versions` / `_dedupe_candidates` |
| 跨库洗版（网盘 4K vs 分享 1080p） | 按洗版规则比，输的一侧整季/整片剔除 | `_season_actions` / 电影分支 |
| 决策模型（保留网盘/保留分享） | 逐季直接取舍，不比画质；保留分享仍要求覆盖 | `decision_keep_local/share` |
| 平局 | 全部打平时按「平局留哪边」 | `_share_wins` / `share_wins` |
| 两库都不完整 | **不洗版**，只做两侧各自的残次品 | `season_verdict` → `outcome=incomplete` |
| 多季保护 | 只防掏空：18 季 vs 8 季 → 淘汰分享 8 季；分享 18 季全季达标才整剧零和，否则淘汰分享 | `multi_protect_mode` / `multi_all_pass` / `share_protect_verdict` |
| 关闭多季保护 | 逐季替换，不完整的一季仍按残次品 | 逐季 `_season_actions` |
| 残次品季已整季清理 | 该季不再重复出「库内多版本」 | `covered` 过滤 |

### 这一轮修掉的三处实现偏差

1. **残次品成单位置**：残次品动作只在「单季/关闭多季保护」时独立成单；决策模型分支不产生残次品；
   多季保护由 `share_protect / full / overlap` 三个分支各自产出——同一季的「残次品」和
   「多季保护淘汰分享」必须同时存在，不能互相抵消（原来会被过滤掉，等于少删了分享库）。
2. **缺集描述对齐 `core.analyze_season_episodes`**：区分「缺失前序 (缺 E01-E24)」与
   「中间断层 (缺 E59，疑似被和谐)」，集号按 `fmt_nums` 压成区间。
3. **把后果写进界面**：每条动作带 `lost_episodes`（删完两库都不再有的集）与 `clears_show`
   （删完整部剧清空），列表打「删后清空」标记、展开写明缺哪些集、确认框汇总条数。

### 已知与 1.2.12 的剩余差异

- 身份：StrimKeep 仍以 TMDB ID 配对（`match_basis: 路径中的 TMDB ID`），1.2.12 用
  `governance_title_key`（剧名+年份，TMDB 仅辅助）。现在的失败模式是「同剧不同 ID 不配对、
  漏去重」，不会造成跨片误删；要完全对齐需改快照层的身份键，留作后续单独一轮。

### 验证

- 新增 `tests/test_user_rules.py` 16 项，逐条覆盖上面的规则（前序缺失、中间缺集、分享侧、
  单库多版本、两库都不完整、跨库洗版+决策+平局、多季保护三档、关闭保护、残次品覆盖、
  保留本地不删不完整本地、full 档两条并存、overlap 只清残次、后果字段）。
- 库存相关 18 个测试文件 + 代理：155 passed；规则集 16 passed。
- 线上（0.9.119）：`玉簟秋 (2026) S1` = 残次品-删本地，31 个文件，
  缺集「缺失前序 (缺 E01-E03) 且 中间断层 (缺 E34，疑似被和谐)」，并标记「删后清空」。


## 0.9.117 — 「受保护／待核实」改名并说明为什么不能自动清

- 筛选项与分区标题从「受保护／待核实」改为「需人工核实」，顶部指标「待核实」改为「需人工」。
- 分区下补一句原因：这些项目不会自动清理——要么两库凑不齐集数（删任一侧都会丢集），
  要么还在静默期或存在待核实记录。
- 线上复核：`dual 0 · single 0 · blocked 1`，玉簟秋 S1 停在 `cross_library_split`；
  前端包内含「需人工核实」「可清理」「两库都有的作品保留哪边」；库存+代理测试 159 项通过。

## 0.9.117 — 「受保护／待核实」改名并说明为什么不能自动清

- 筛选项与分区标题从「受保护／待核实」改为「需人工核实」，顶部指标「待核实」改为「需人工」。
- 分区下补一句原因：这些项目不会自动清理——要么两库凑不齐集数（删任一侧都会丢集），
  要么还在静默期或存在待核实记录。
- 线上复核：`dual 0 · single 0 · blocked 1`，玉簟秋 S1 停在 `cross_library_split`；
  前端包内含「需人工核实」「可清理」「两库都有的作品保留哪边」；库存+代理测试 159 项通过。

## 0.9.116 — 跨库拆集防护：清理不得减少可看集数

用户反馈"剧集单集跨库了"。查线上真实数据：`玉簟秋 (2026)` 网盘只有 S01E04–E33、E35，
分享侧 0 个文件，预览却仍给出「残次品-删本地 31 个文件」。

### 根因

1.2.12 的残次品判定是**逐侧独立**的：网盘缺 E01 就删网盘整季，分享缺 E05 就删分享整季，
判定时完全不看另一侧是否真的持有这些集。同一部剧被拆到两个库（网盘从 E04 起、分享只有
E01–E03）时，两侧都被判"残次"，两边都删 → 单集直接消失。0.9.114 又把 `dual_actions`
与自动计划里"同一季不能删两库"的防护对 `{incomplete_delete_local, incomplete_delete_share}`
放行，等于把这层兜底也拆了。

### 修复

- 新增 `_drop_cross_library_actions` 安全闸：删除动作必须满足**要删的一侧集号被保留侧完全覆盖**，
  否则降级为 `blocked`：
  - `cross_library_both`：本季两库互有对方没有的集，同时清两侧会丢集；
  - `cross_library_split`：要删的一侧有另一侧没有的集（含"本季只有这一库有片"）。
  提示里直接写出缺哪些集（`本季只有这一库有片，缺 E01、E02、E03、E34`），便于补集或手工合并。
- 撤销 0.9.114 的放行：`dual_actions`（单次计划）与 `scan_selections`（自动计划）恢复"同一季不能删两库"，
  只剩显式 S00 两库同删（`special_action=delete`）例外。
- 手动选择"保留网盘/保留分享"同样受该闸约束：对侧没覆盖的集不会被删。
- 存量风险计划：把修复前生成、仍处于 `planned` 的 6 个计划（含两个 38 文件的
  「删本地 31 + 删分享 7」）统一作废为 `cancelled`，避免误执行。

### 测试与线上

- 把 6 个固化"逐侧删除"的旧用例改成新不变量（清理不得减少可用集数），新增拆集、单侧残次、
  无对侧、`keep_local` 覆盖不足四类用例；库存相关 18 个测试文件 + 代理共 **159 passed**。
- 线上（0.9.116）：`dual 0 · single 0 · blocked 1`，玉簟秋 S1 现为
  `cross_library_split`，提示"本季只有这一库有片，缺 E01、E02、E03、E34；清理会把现有集一起删掉。"
- 事故遗留：0.9.114 期间执行过一次 7 个文件的「残次品-删分享」（计划 `5f980854…`，已完成），
  分享侧的 E01–E03 等文件需从 115 回收站或原分享重新入库。

## 0.9.115 — 双库治理页面文案瘦身

治理页原来一屏塞了 6 组说明、4 个数字、5 个筛选和十几行逐行技术明细。本轮只改文案与层次，
不动判定逻辑：

- 顶部数字改成说人话：可清理 / 待核实 / 已豁免 / 两库配对，并加一句"只比较同名同年的两库作品"。
- 删除与主列表重复的"单库检查""双库比较"原始明细区（保留两库配对明细与受保护明细）。
- 行内展开去掉"匹配依据 / 共同集达标数 / 当前决策 / 多季检查 / 原因逐条"等技术行，移动端同步。
- 设置项标题改成问句：两库都有的作品保留哪边、多季剧怎么处理、特别篇（第 0 季）、
  永不处理的片名关键词、完全一样时留哪边、分享要赢几成才替换网盘、新入库多久内不处理。
- 长提示句压缩成一句：同一季两侧都缺集时，一次只能清一侧。


## 0.9.114 — 片库映射核对 + 双库治理"没有可执行项"再修

用户要求"优化片库映射和双库治理"。先把两块的真实数据拉出来看，避免凭感觉改。

### 片库映射：已经是快路径，本轮只核对

```
/api/v1/inventory/mapping/page?media=all&limit=45   0.07 s   13.2 KB   （页面实际用的）
/api/v1/inventory/mapping/preview                   0.07 s   41.2 KB
/api/v1/inventory（整库，仅兼容用）                  8.13 s  479.0 KB
```

前端 `Inventory.vue` 已经在用 `mapping/page` 并带 `scan_revision`，所以这块不动。

### 双库治理：三条自造规则把动作吃掉了

改前线上：`dual 0 · single 2（全是 episode_gap）· blocked 2` —— 页面看起来"没东西可执行"。

1. **残次品被降级成待办**：某侧缺集只生成 `single.reason=episode_gap`，真正要删的 31 个文件不在主列表里。
   → 按 1.2.12 `_emit_season_residuals`：直接产出 `incomplete_delete_local/share` 动作，行内带缺集明细。
2. **`dirty` 闸**：只要该季有多版本或缺集，就把整部剧的所有动作挪进 `blocked: single_library_not_clean`。
   → 删除。残次品动作优先，该季其余画质结论让位（1.2.12 的 `incomplete` 分支）。
3. **"不得删唯一集"守卫**：把残次品动作也一起吃掉。→ 删除（1.2.12 依据集数直接判）。

另外两处配套：

- 库内多版本挪到双库结论**之后**并带 `covered` 过滤（1.2.12 `_dedupe_lib_versions` 的位置），
  避免对已被整季动作覆盖的一侧重复给去重结果。
- **两侧残次品可以同批**：`dual_actions` 与自动计划的冲突判定原本一律拒绝"同一季删两库"，
  现在放行 `{incomplete_delete_local, incomplete_delete_share}` 这一对（两库文件互不相交），
  其它同季组合仍拒绝。

### 线上实测（0.9.114，同一份库存）

```
预览 200 · dual 2 · single 0 · blocked 0
  [删本地] 玉簟秋 (2026) S1 · 残次品-删本地 · 删 31 个
  [删分享] 玉簟秋 (2026) S1 · 残次品-删分享 · 删 7 个
自动生成的清理计划：有，38 个动作（31+7），计划 id 5591ade6…
```

改前这两条只能靠"单库缺口"开关逐条点、还被 dirty 判成待核实；现在直接出现在主列表并可一次生成计划。

### 验证

```
库存与治理整组 16 个文件：全绿
test_inventory_preview.py 24 passed（新增残次品出动作/两侧可同批/多版本不阻断等 9 项）
```

## 0.9.110 — 9001（Emby 反代）播放慢的根因：视频字节在反代里被中转

用户反馈「Emby 公网反代 9001 播放很卡」，怀疑没走 115 CDN。

### 实测（同一台客户端，同一部 4K REMUX）

```
改前  9001 stream.mkv：206，ttfb 1.26s，2.4 MB/s   ← 字节经 VPS(frp)→NAS→Emby→115 再回来
      另一部 1080p   ：206，ttfb 2.70s，1.1 MB/s
改后  9001 + 302    ：64MB 连续读 20.0 MB/s（ttfb 0.94s，多出的 0.7s 是 302 那一跳）
      CDN 直连      ：64MB 连续读 24.9 MB/s（ttfb 0.21s）
```

### 根因

`emby_proxy.py` 只改写了 PlaybackInfo 的 JSON（补 `DirectStreamUrl`），
**其它路径一律 `aiter_raw()` 原样转发**。可播放器（SenPlayer）并不使用 DirectStreamUrl，
而是继续向反代请求 `/emby/videos/{id}/stream.mkv`，于是视频字节走了：
客户端 → VPS(frp) → NAS 反代 → Emby → 115 CDN，再沿原路返回，被 frp 带宽锁死（~2.4 MB/s）。

### 改动（`backend/strimkeep/emby_proxy.py`）

- `handle()` 前置：`GET/HEAD` 命中 `/emby/(videos|audio)/{id}/stream[.ext]`、
  `/emby/videos/{id}/original`、`/emby/Items/{id}/File` 时，先尝试 **302 到 115 直链**。
- 取址用 **PlaybackInfo**（`POST /emby/Items/{id}/PlaybackInfo`）：
  实测 `/emby/Items/{id}` 在这个 Emby 版本上是 404（`The file ... could not be found.`），
  而 PlaybackInfo 的 `MediaSources[0].Path` 就是 STRM 里那条 play115 地址；
  兜底再读本地 `.strm` 内容（`console_config.emby_strm_path` → `STRIMKEEP_STRM_DIR`，防目录穿越）。
- 解析失败一律**回退到原来的转发**，不因为改造把播放弄挂。
- `ID → Path` 缓存 600s（播放器每几秒一个 Range，不能每次都回源）；直链本身沿用
  `cloud115` 的 (文件, UA) 缓存。

### 效果

视频字节不再经过反代/frp，直接由 115 CDN 供给；一次播放只多 0.7s 的 302 跳转，
持续吞吐从 2.4 MB/s 提到 20 MB/s（CDN 上限 24.9 MB/s），68 Mbps 的 REMUX 不再卡。

### 验证

```
tests/test_emby_proxy.py：30 passed
  - stream.mkv → 302 且第二次 Range 不再回源问 Path
  - Path 不是 115（本地文件）→ 回退转发
  - 直链解析失败 → 回退转发
  - Emby Path 是 .strm 文件 → 读内容里的 play115 地址再 302
线上：0.9.110，health ready；14176 请求返回 302 → cdnfhnfile.115cdn.net
```

## 0.9.101 — 「双库治理不好用」的两个真因（前端构建失败 + 没带 scan_revision）

前几轮都在改观感，没验证"页面到底跑的是哪版、按钮到底通不通"。这轮把两端都打通：

### 1. 前端构建其实一直是失败的

`Inventory.vue` 的模板引用了 `govCards` / `govDetailItem`，但 `<script setup>` 里从未定义，
`vue-tsc` 报 `TS2339`，`npm run build` 退出码 2 —— 也就是说镜像里的前端一直是**旧页面**，
改成什么样用户都看不到。补齐两个符号，并把 `scripts` 里的部署脚本改成**构建失败即中止**，
不再出现"容器重建成功但镜像还是旧的"。

### 2. 点任何"执行"都返回 409

后端 `POST /inventory/delete/plan` 要求 `scan_revision`（`governance_revision` 的 64 位摘要），
`/inventory/preview` 也一直在返回它，但**前端从来没发送**，于是每次点「执行此项 / 全部执行」
都得到「库存或规则已变化，请重新扫描后再执行」。现在 `prepareDelete` 带上
`scan_revision: preview.scan_revision`。

```
改前：POST /delete/plan → 409 库存或规则已变化，请重新扫描后再执行
改后：POST /delete/plan → 200 actions=114
```

### 3. 引擎按 1.2.12 对齐（拆掉自造拦截）

- 残次品（某侧缺集）→ 直接出 `incomplete_delete_local` / `incomplete_delete_share` 动作，
  不再降级成"单库缺口"待办。
- 删掉 `single_library_not_clean` / `quality_rules_disabled` / `unknown_quality` /
  `coverage_conflict` 四道自造拦截，以及"不得删唯一集"守卫（1.2.12 依据集数直接判）。
- 库内多版本挪到双库结论之后、带 `covered` 过滤（1.2.12 `_dedupe_lib_versions` 的位置）。
- 同一季两侧都残次时允许同批执行（两库文件不相交），不再报"同一季不能同时删除两库"。

### 线上实测

```
预览：删本地 12 · 删分享 5 · 保留 2 · 单库 0 · 待核实 1（静默期内 1 部）
计划：全部执行 → 200，114 个文件
前端 bundle：Inventory-CCUMXoAl.js 含 gov-stat / gov-seg / gov-item / scan_revision
测试：库存与治理整组 77 passed
```

## 0.9.101 — 双库治理"没东西可执行"的根因：残次品被降级成待办

用户反馈治理页"依旧不好用"。把线上 `/inventory/preview` 的真实响应打出来才看清问题：

```
改前：删本地 0 · 删分享 0 · 保留 2 · 白名单 0 · 单库 16 · 待核实 5
```

16 个"单库缺口"全是 `reason=episode_gap`（缺 E01、缺 E22 之类），同一部剧
（玉簟秋/幻视任务/璀璨之城/谷雨）还同时出现在"待核实：单库尚有多版本或缺口"。
也就是说：**引擎把残次品降级成待办，双库比较永远不产出结论**，四个统计卡永远是 0。

对照 1.2.12 的三处差别（`app/governance.py`）：

1. `_emit_season_residuals`：某一侧缺集 → **直接产出 `loc`/`shr` 删除动作**，
   该季删完不再比画质（`outcome=incomplete`），而不是列一条"单库缺口"。
2. `_dedupe_lib_versions`：库内多版本是**独立一遍**，它不会把双库结论挂起。
3. "不得删除含唯一集的一侧"这条守卫只适用于**画质择优**；残次品动作本身就是
   删有问题的那一侧，套上守卫会把动作全部吃掉。

### 改动

- 缺集（残次品）→ 双库动作：`incomplete_delete_local` / `incomplete_delete_share`，
  行内带缺集明细（`缺 E01,E03`）。
- 多版本仍走 `single`（`reason=quality`）；按既有约定，**同季有多版本时该季双库结论暂缓**
  （`dirty` 只来自多版本，不再包含缺集）。
- `safe_actions` 守卫放行 `incomplete_delete_*` / `special_delete_*`。
- 同步重写 8 个用例到新语义（残次品出动作、两侧可分别执行但禁止同批删两库）。

### 线上实测（同一份库存）

```
删本地 12 · 删分享 4 · 保留 2 · 白名单 0 · 单库 0 · 待核实 1
东大高武学院 S01 删本地 1 个 · 伟大的长征 S01 删分享 8 个 · 小城良方 S01 删本地 28 个
玉簟秋 S01 删本地 31 个 · 谷雨 S01 删本地 5 个 …
```

### 验证

```
库存/治理整组 11 个文件：全绿（含 1.2.12 差分用例）
镜像按 docker-compose 的 tag 重建并 --force-recreate；health ready
```

## 0.9.101 — 双库治理页 UI 按 1.2.12 重做（前端）

用户指出治理页看不出"保留哪一侧、要删什么"，要求照 1.2.12 的前端来。
对照 `static/js/mapping.js`（`renderSeriesCard` / `sortedMappingSeries`）与
`static/js/governance.js`（`renderGovList` / `showGovDetail` / `pathListBlock`）重写：

- **统计条当筛选器**：全部 / 删本地 / 删分享 / 保留 / 白名单 / 待核实，每项带数量，
  点一下就是筛选（对齐 1.2.12 的 loc / shr / keep / exempt 四类 + 待核实）。
- **一行一条治理项**：`📺《剧名》S01 原因标签 → 删本地` + 灰字副标题
  （删几个文件 / 保留几个 / 分享达标率 / 缺集 / 决胜集 / 淘汰原因条数）。
- **展开即看依据**：剧名、季、两库季数、分享达标率、缺集、命中关键词、静默期；
  `画质对比依据（代表集 E05）` 给出分享/本地文件名和逐维规则结果；`逐集对比 N 集`
  与 `对比规则 N 项` 默认折叠。
- **路径分块（1.2.12 的 pathListBlock）**：`删本地/删分享 · 将删除 N 个 STRM` 与
  `保留 N 个 STRM` 两段，默认折叠，点开是完整路径。
- **库内多版本区**：区分 `同片多版本`（留最优删其余，打平时注明"按文件名保留第一份"）、
  `季集缺口`（删该侧整季）、`两库同集`（只删较差的一份）。
- 两库配对明细移到最底部、默认折叠，仅作参考。

### 与后端契约的核对（按线上真实响应改文案）

线上引擎把不完整季归到 `single.reason='episode_gap'`、把"单库不干净"归到
`blocked.reason='single_library_not_clean'`，页面按这些真实字段出标签，不再假设只有
`quality` 一种单库结果。

### 验证

```
vue-tsc + vite build 通过；镜像 strimkeep:0.9.101 重新构建并强制重建容器
运行中 bundle 校验：Inventory-CbKh3F4W.js 同时含 gov-chips 与 inventory/mapping/page
线上 /api/v1/inventory/preview：single 17 · blocked 4 · multi 2 · pairs 4，无异常日志
库存/治理测试 62 passed（含 1.2.12 差分用例）
```

## 0.9.99 — 双库治理与片库映射按参考实现 1.2.12 重抄

用户指出"1.2.12 是我为这个项目做的最后闭环"，要求把两块的逻辑直接抄过来。
这次不再按理解重写，而是把 1.2.12 的源码当唯一标准，逐函数对齐，并用**差分测试**
把参考实现本身跑起来做对照。

### 1. 双库治理：删掉 0.9.95–0.9.97 里三处"我自己的理解"

| 位置 | 之前（我发明的） | 现在（1.2.12 原样） |
|---|---|---|
| `season_decision` | 两侧各有对方没有的集就判 `coverage_conflict` 交人工 | 按 `season_verdict`：先残次品，再达标率+集数，落到 `share_wins` / `local_wins_fewer` / `local_wins_quality` |
| 画质判不出来 | `unknown_quality` 整条跳过 | `_cmp_versions` 判不出就是平局 0，由「平局保留本地/分享」开关决定 |
| 多季 `full` 档 | 共同季里连 `incomplete_delete_share` 也一起放 | 只放本地残次品（`residual_flags(cmp, has_local, False)`），其余一律 `multi_protect_partial_share` |
| 电影 | 只比画质，忽略决策模型 | `_share_wins`：决策模型优先，再画质，最后平局开关 |
| 库内多版本 | 与库间治理各算各的 | `_dedupe_lib_versions`：`(kind, media_key)` 已被治理覆盖的一侧跳过；同身份有分集时不按电影去重 |

另外补齐：白名单按标题聚合成一条（`_group_exempt_acts`）、静默期 stat 不到按"没变动"、
`_to_dual` 同时保留 `reason`（裁决词）与 `action_reason`（1.2.12 的 meta 原因码）。

### 2. 差分测试：把 1.2.12 的规则模块原样收进 tests

`backend/tests/reference_1212_governance.py` = 1.2.12 `app/domain/governance.py` 原文
（纯规则、无 IO）。`test_governance_parity_1212.py` 用同一套比较函数驱动两边：

```
E01..E04 全部覆盖组合（含空集/断档/集数不等）× 4 组画质 × 2 种平局开关
  → 逐例比对 outcome / residual_local / residual_share
multi_protect_mode / protect_s00 / share_protect_verdict / multi_all_pass 全组合比对
```

任何一个分支漂移都会立刻失败，不再依赖"我看懂了"。

### 3. 片库映射：把服务端分页/首屏/详情搬过来

新增 `backend/strimkeep/library_mapping.py`（对应 1.2.12 的
`library_first_screen` + `library_pagination` + `library_response`）：

- `GET /api/v1/inventory/mapping/preview` 首屏每类最多 48 张卡，**不下发任何 STRM 路径**
- `GET /api/v1/inventory/mapping/page` 过滤/计数先算完再切片，返回全局 `total` /
  `has_more` / `next_offset`，排序按 1.2.12 的状态优先级（缺集 → 超集 → 待核实 →
  在更 → 完整）再按差异绝对值倒序
- `GET /api/v1/inventory/mapping/item` 详情按 id 取单部作品，路径只在这里出现
- 三个入口都带 `Cache-Control: private, no-store`（映射页不允许被浏览器/反代缓存）

状态不再每次回源 TMDB：现有「缺集对照」接口返回时把结论写进 `inventory-status:tv:<id>`
缓存（缺失/超集/在更/完整/待核实），分页与首屏直接读；没有缓存的条目按 `pending`，
页面只需为当前页请求对照。

### 首屏为什么变快：stale-while-revalidate

映射快照原来每次全库遍历（本机 1234 个 STRM 目录约 9–12 秒、425 KB）。现在：

- 快照 60 秒内直接复用；过期时**先返回旧的**，同时后台重扫（响应带 `stale` / `age_sec`）
- 增删之后前端 `load()` 带 `refresh=1` 强制重扫，不会拿旧数据当新数据
- 列表接口只回当前页 45 张卡（10.7 KB），路径只出现在详情接口

NAS 实测（同一台机器）：

```
/api/v1/inventory                      8937–12222 ms   425.3 KB
/api/v1/inventory/mapping/page?limit=45      8–135 ms    10.7 KB   total=225
/api/v1/inventory/mapping/preview           10 s（仅重启后首次，冷扫）  33.4 KB
过期后第 1 次 / 后台扫完：8 ms / 9 ms（stale=true → 15 秒后转 fresh）
```

### 验证

```
tests/test_inventory_rules.py          10 passed
tests/test_inventory_preview.py        15 passed（新增电影决策模型/白名单聚合/平局回退/去重覆盖）
tests/test_governance_parity_1212.py   12 passed（E01..E04 全组合差分 + 三个纯规则函数对照）
tests/test_inventory_mapping.py         7 passed
库存与治理整组（audit/executor/operations/plans/orphans）：30 passed
前端 vue-tsc + vite build 通过；顺带修掉 0.9.98 遗留的 Inventory.vue TS2345（skipReason(code)）
```

门禁 `scripts/verify.sh` 的「库存与治理」组已加入两个新用例文件。

## 0.9.98 — 残留清理：备份改成可选、跳过必须给原因；双库行显示"做什么/为什么/删多少"

用户反馈两件事：①"每次清理都要备份多此一举"；②"跳过什么原因也不列出来，我怎么判断下一步"。
另外指出治理页每行只有"建议保留网盘库"，看不出动作和依据（参考实现每行是
`text`（动作）+ `detail`（原因明细）+ 展开的"将删除/保留"路径清单）。

### 1. 备份改成可选（默认不备份）

`clean_one` / `clean_sidecar` 增加 `backup` 参数；接口 `OrphanSelection.backup` 默认 `false`；
页面上是「清理前备份」开关，按钮文案随之变成「清理选中项」/「备份并清理选中项」。
不备份时仍然**逐项复核指纹**（`目录内容已变化，请重新扫描`），只是不再生成 tar。

### 2. 跳过必须给原因

`scan()` 原来把 `inventory()` 的异常吞成一个计数（现场就是"229 个目录…已跳过"）。
现在返回：

```
skipped_items: [{path, side, reason, detail}]   最多 500 条，附带 skipped_truncated
reasons: {has_media_or_unknown: 210, symlink: 12, unreadable: 7, ...}
```

原因码：`symlink` / `has_media_or_unknown` / `changed` / `too_large` /
`not_plain_directory` / `no_tmdb` / `unreadable` / `invalid`，页面按原因聚合显示数量，
并给可展开的"路径 + 原因 + 细节"清单（前 200 条）。

### 3. 双库判定行改成"动作 + 原因 + 数量"

每行标题变成 `作品 · 原因标签 · 删本地/删分享 N 个文件`，展开后显示：
保留哪一侧、达标率与阈值、缺集、决胜集号（sample），以及**将删除 / 保留的完整文件清单**。

### 验证

```
tests/test_inventory_orphans.py：10 passed
新增用例：scan 给出 has_media_or_unknown / symlink 原因与计数；
        backup=False 时清理成功且不生成归档
```

## 0.9.97 — 双库治理按参考实现重写（我之前理解错了）

用户指出"你没读懂 1.2.12 的双库治理"。拉取源码（`git clone --branch v1.2.12`）逐行核对后，
确认我此前的理解有三处根本性错误：

1. **我发明了 `covered_episode`（跨库逐集去重）**。参考实现里跨库只有"整季/整剧二选一"，
   逐集去重只存在于**库内**（同集多版本留最优）。已删除该动作。
2. **我把残次品当成"待核实/可选"**。参考实现里残次品是**必做动作**：谁不完整删谁，
   而且本地、分享**各自独立判定，两侧可以同时删**。
3. **我漏了整套决策层**：`decision`（quality_first / keep_local / keep_share）、
   `special_action`（特别篇 compare / delete / ignore）、多季保护 `full` 档的
   "逐季无条件淘汰分享"，以及 `share_protect` 档的 `share_fewer` 分支。

### 现在的判定（逐条对应参考实现）

```
电影：两侧各取最优文件比画质 → share_better 删本地 / local_better 删分享
剧集：静默期 → 白名单（标题或路径命中）→ 特别篇 S00 → 决策模型 → 多季保护 / 逐季
  逐季：先残次品（本地不完整删本地、分享不完整删分享），incomplete/no_common 到此为止
        否则 share_wins 删本地；local_wins 删分享（集数少的先淘汰，其次画质）
  多季 compare（任一侧 ≥2 季）：
        share_protect：逐季残次品优先 → share_fewer 删分享 / 否则删本地，S00 跟随
        full        ：逐季残次品优先 → 否则无条件删分享；全部季达标才整剧零和删本地，S00 跟随
        overlap     ：只处理残次品，其余不动
库内多版本：电影同片多份、剧集同集多份 → 各留最优（独立一遍，不阻断季级治理）
```

配置新增 `decision` / `special_action` / `multi_season_protect`（默认 quality_first / compare / compare），
删除我自造的 `single_covered`。

### 线上实测（0.9.97）

```
single 0 · dual 7 · blocked 0 · pairs 5 · multi 63（full 55 / share_protect 8）
dual 原因：incomplete_delete_local 3、incomplete_delete_share 4
动作类型：loc（删本地/CD2 删 115 源）3、shr（删分享）4
```

也就是说现在**有 7 个可执行动作**，都是残次品处理——与参考实现一致：
在更剧里不完整的那一侧副本会被清掉（两边都不完整就两边都清，靠后续重新追更）。

### 验证

```
库存治理整组 14 个文件：65 passed
重写 test_inventory_preview：电影择优、share_wins/local_wins、残次品两侧、
no_common、决策模型、多季三档、整剧零和、库内多版本
```

## 0.9.96 — 治理能真的动手了：逐集去重 + 删除执行提速

用户反馈："光扫没有实际操作我怎么用？"、"片库映射删除单库/多库很卡顿"。

### 1. 加一个"在更剧也能用"的操作：逐集去重

原来只有两类动作：整季双库去重（要求两侧都完整，在更剧永远不满足）和整季清理（对在更剧
是错的——会删掉独有集）。所以页面上看起来"0 可裁决、没得用"。

新增 `covered_episode`：整季虽不完整，但**两边都有同一集**时，只删较差的那一份，
一集都不会丢。线上实测可操作量：7 集（谷雨 S1 占 5 集、Doctor X 1 集、玉簟秋 1 集）。

- 预览：`single` 里新增 `reason='covered_episode'`，带 `files`（要删的那份）、`kept`（保留的那份）、
  `action_reason='incomplete_delete_covered'`
- 计划：`single_actions` 新增分支，按 (季, 集, path) 与预览结果逐一比对，对不上就要求重新预览
- 开关：`single_covered`（默认开）；执行前仍要人工确认
- 前端：单库页签显示"两库同集：删较差的一份（不会丢集）"并给出"清理此项"

### 2. 删除执行提速：快照校验从 2N 次降到 N+1 次

`execute_simulated` 原来对**每个文件**做两次全库快照核对（删前一次、删后一次）。
删前那次在"上一动作刚核对完、中间没有其它操作"时必然成立，纯属白扫——
删 10 集就是 20 次全库扫描，这才是"卡顿"的主因。

现在：批次开始核对一次，之后每次删除后核对一次（仍能拦住外部变动与删除结果不符）。
新增用例锁住这个数量：`captures == 2 + 动作数`（旧实现是 `1 + 2×动作数`）。

### 3. 其他

- 执行进度轮询 1500ms → 800ms，进度条更跟手
- 删除完成后仍会刷新片库映射（数据确实变了）

### 验证

```
使用执行/计划/预览相关 34 项：passed
新增用例：逐集去重给出可删的那一份（打平按 tie）、快照校验次数不翻倍
```

## 0.9.95 — 双库治理对齐 1.2.12 收尾：整剧零和 / 白名单 / 动作类型 / 残次品动作名

上一条还差四项，这次全补上。

### 1. 整剧零和（`multi_full_share`）

两边季集合互相包含、且**每一季**都"分享完整覆盖本地 + 达标率够 + 分享集数不少于本地"时，
一个整剧决定淘汰本地全部季；`protect_s00` 生效时 S00 一起参与
（1.2.12 golden 里《剧集丁》S00 就是这么被清掉的）。

写测试时抓到自己实现里的一个**真 bug**：最初只检查"每个有对阵的季都通过"，
但本地可能有分享没有的季（S02 只在本地），那样会连带把 S02 删掉。
现在补上 `local_seasons ⊆ share_seasons` 与季数校验——对应 1.2.12 `multi_all_pass`
里的 `if sn not in s_proper: return False`。

### 2. 白名单（`exempt_keywords`）

配置项新增 `exempt_keywords`（最多 50 个），作品名命中即整部豁免：
进 `exempt` 列表（`action_kind='exempt'`），不产生任何清理项、也不进 blocked。

### 3. 动作类型（`action_kind`）

对齐 1.2.12 的 `loc / shr / keep / exempt`：`winner='share'` → `loc`（删本地），
`winner='cloud'` → `shr`（删分享），豁免 → `exempt`。

### 4. 残次品动作名（`incomplete_delete_*`）

`episode_gap` 行新增 `action_reason`：`incomplete_delete_cloud` / `incomplete_delete_share`，
与 1.2.12 的计划归档口径一致（`reason` 仍保留 `episode_gap`，不动既有开关与判据）。

### 5. 目录级执行校验：本项目已更强，无需照抄

1.2.12 给残次品动作额外记"目录 STRM 集合"快照。我们的 `execute_simulated`
**每执行一个动作前都重新抓库存快照并比对哈希**（`snapshot_hash(await capture()) != snapshot_hash(expected)`
即中止），覆盖范围是整份库存（文件 + 来源身份），比目录级更严。这里不改代码，只在日志里记明依据。

### 验证

```
preview / operations / operations_api / rules / inventory：42 passed
新增用例：整剧零和含 S00、分享缺本地季时不许零和、白名单整部豁免、
        动作类型 loc/shr、残次品 incomplete_delete_* 动作名
```

## 0.9.94 — 双库治理：多季保护真正参与裁决（对齐 1.2.12 的 share_protect / overlap / full）

上一版只把 `multi_protect_mode` 算出来当字段展示。这次让它**影响判定**。

依据是 1.2.12 的 golden 夹具（`dev/tests/golden/build_plan.json`）——它固化了每种情形该产出什么动作
（`kind: loc/shr/keep/exempt`，reason 有 `share_wins`、`dup_version_local`、`multi_full_share`、
`incomplete_delete_*`、`whitelist`）。其中「整剧零和」那一条把 S00 也算进"分享全季达标 → 淘汰本地"，
正是 `protect_s00` 的作用。

### 改动

- `share_protect`（本地季 ⊂ 分享季）：某季被分享完整覆盖 → 直接判 `winner='share'`、
  `outcome='multi_share_protect'`，即淘汰本地同季副本，保住只在分享侧的季。
  对应 1.2.12 `share_protect_verdict` 的 `delete_local`。
- `overlap`（双方各有对方没有的季）：不给整季裁决，标 `blocked: multi_overlap_protected`
  （前端文案"双方各有对方没有的季，同季不动"），只保留残次品处理。
  对应 1.2.12 overlap 档"同季两边都不动，只处理残次品"。
- `full`（分享季 ⊆ 本地季）：维持按季三档裁决。
- 预览新增 `multi` 列表：每部作品给出 `mode` / 季集合 / `protected`，页面与被审计时都能直接看到判据。

### 线上实测（0.9.94，225 部）

```
multi 模式分布：full 55 部、share_protect 8 部
single 21（残次品，可清理）/ blocked 0 / dual 0
```

`dual` 仍为 0，但这次不是逻辑问题——真实库里两库重叠的剧目前全在更（`pairs` 5 部都不完整），
按 1.2.12 的规则也不该在这种状态下做整季替换。以前是"有能力也不判"，现在是"数据还不该判"。

### 还没对齐的部分（下一步）

| 1.2.12 | 我们 |
|---|---|
| `multi_full_share` 整剧零和：一次动作删掉本地全部季（含 S00） | 只到"逐季裁决"，没有整剧级动作；S00 不进裁决 |
| `kind: loc/shr/keep/exempt` 四类动作 + `whitelist` 豁免 | 只有单库/双库两类清理项，无豁免清单 |
| `incomplete_delete_local/share` 残次品动作 | 只有 `episode_gap` 单库清理项，语义相近但 reason 不同 |
| `plan_guard` 文件指纹 + 目录 STRM 集合快照 | 已有计划复核，但未对"残次品动作"做目录级校验 |

## 0.9.93 — 双库治理逻辑修正：残次品不是"待核实"，别把可处理项藏起来

顺着"看看双库治理逻辑"这条线查线上数据，发现一个**判定分类的硬伤**：

```
线上 225 部作品 → dual 0 条、single 21 条、blocked 19 条
blocked 19 条全部是 single_library_not_clean
single  21 条全部是 episode_gap
```

看具体作品：

```
Doctor X (2026) S1     cloud=[7,8]       share=[8,9,10]
东大高武学院 (2026) S1   cloud=[16]        share=[]
伟大的长征 (2026) S1     cloud=[]          share=[48..55]
```

这些都是**在更/跳集的残次品**。我们的逻辑：任一侧"集号不从 E01 连续" → `episode_gap`
→ 同时写进 `single`（可清理项）**和** `blocked`（待核实）→ 一整部在更的剧变成"待核实错误"。
结果治理页看起来"没有任何结论"，而实际上 21 个可清理项就在隔壁页签里。

对比 1.2.12 的 `season_verdict`：`incomplete` 分支不是"什么都不做"，而是带
`residual_local` / `residual_share` 旗标——**只清残次品**，照样给结论。

### 改动

`governance_preview` 把"残次品"与"真需要人看的"分开：

- 只有缺集/跳集（`episode_gap`）→ 只进「单库待处理」，**不再重复记 blocked**；
  这一季仍然不做整季替换裁决（不能拿不完整的一侧去换另一侧）。
- 多版本、合并集、质量不明、期间变动 → 仍然 `blocked`（这些确实需要人看）。

删除行为没变：`episode_gap` 的可执行性仍由 `single_gaps` 开关与执行前复核把关，
只是不再同时对外宣称"待核实"。

### 验证

```
单测：新增 2 项（gap-only 不进 blocked；多版本+缺口仍进 blocked）
组内回归：33 passed（preview / operations / operations_api / rules / inventory）
```

## 0.9.92 — 双库治理规则对齐上游 1.2.12（裁决词汇 + 多季保护 + 决胜依据）

对照公开仓库 `xcgtb/StrimKeep` 的 `v1.2.12` 源码（`app/domain/governance.py`），
逐条核对我们 `backend/strimkeep/inventory_rules.py` 的判定语义。

### 已对齐

| 1.2.12 | 我们原来 | 现在 |
|---|---|---|
| `season_verdict` 的 `share_wins` / `local_wins_fewer` / `local_wins_quality` | 只有笼统的 `reason='quality'` | 输出 `outcome` 三态，页面能区分"集数优先"和"画质优先" |
| `sample`：第一个非平局共同集，用于展示决胜依据 | 无（只有逐集 comparisons） | 有，`season_decision()['sample']` |
| `share_covers_local` / `residual_flags` | 无独立函数 | 有，语义相同 |
| `multi_protect_mode`（share_protect / full / overlap） | 无 | 有；预览里按整剧季集合算出并随每条判定返回 `multi_mode` |
| `share_protect_verdict`（skip / share_fewer / delete_local） | 无 | 有 |
| `partial_share_reasons`（残次品 / 集数不足 / 画质次级） | 无 | 有，缺集描述由 `episode_gaps` 生成 |
| `protect_s00`（S00 参与多季保护的条件） | 无 | 有（纯规则；等我们的多季保护配置落地后接入） |

### 有一处**故意不照抄**

1.2.12 的 `season_verdict` 在"两侧都完整（E01 起连续）"时按 `s_total >= l_total` + 达标率直接判胜负；
写测试时发现一个事实：**两侧都完整时集合必然互相包含**，所以我们的 `coverage_conflict`
其实不可达——三档胜负判定与 1.2.12 完全等价。

真正的差异只剩一处：`c ⊆ s`（分享集更多）但达标率不足时，1.2.12 判 `local_wins_quality`
淘汰分享，而分享那侧含有只有它才有的集。我们保留"不删含唯一集的一侧"这条底线，
返回 `coverage_conflict` 交人工。这条差异写在代码注释和测试里，避免以后被"对齐"掉。

### 验证

```
28 项（rules/preview/operations/api）→ 31 passed
新增用例：裁决词汇三态、sample 指向首个非平局集、coverage/residual 助手、
        多季三模式、share_protect 三态、protect_s00 条件、partial_share_reasons 三种原因
```

## 0.9.90 历史残留清理 — 2026-11-11 14:15（用户确认后执行）

0.9.90 上线时只做了只读扫描（14 个作品目录 + 5 个遗留侧车），一直没有执行清理。
今天按确认全部执行，走产品自己的 `POST /api/v1/inventory/orphans/clean`（先归档再逐项复核）。

执行方式值得记一笔：**不是一次把 19 项全清完**，而是每轮重新扫描再清——
因为某个目录被清掉后，同目录其他文件的"目录指纹"就变了，接口会拒绝（
`文件或同目录视频已变化，请重新扫描`）。这个拒绝是对的，它保证不会拿着过期快照删东西。
所以脚本跑了 4 轮：19 → 3 → 2 → 1 → 0 项。

结果：

```
候选       : 19 项（14 个作品目录 + 5 个遗留侧车）
删除       : 文件 25 个、目录 15 个
安全拦下   : 3 次（指纹过期，按提示复扫后正常清掉）
归档       : data/cleanup/orphans/20261110-141540-*.tar（19 个 tar，2.4 MB，逐项可还原）
复查       : 剩余候选 0
```

清理后的核对（避免"清完才发现删错"）：

```
媒体库/剧集/国产剧集/嫁金钗 (2026) {tmdb-300146}      已删除（原目录只有 poster/fanart/tvshow.nfo）
媒体库/剧集/国产剧集/伟大的长征 (2026) {tmdb-297253}    已删除（同上）
媒体库/电影/欧美电影/海底总动员 (2003) {tmdb-12}       已删除（空壳目录）
分享影视库/剧集/国产剧集/嫁金钗…                     仍在 ← 作品内容没丢，删的是网盘库那侧的空壳
分享影视库/剧集/国产剧集/伟大的长征…                  仍在
分享影视库/剧集/国产剧集/走马上任…/Season 1          6 个 STRM 全部保留（只清了 E12–E15 的孤儿 .nfo）
```

## 0.9.91 — 公网播放变慢的根因、兜底链路的 NameError（接管 0.9.90 收尾）

### 症状与定位

- 播放记录里从 12:45 起全是 `filled=0 / error`；但本机对同一条目请求又能注入成功。
  说明不是注入逻辑坏了，而是**偶发失败后直接放弃**。
- 记录里只有结果没有原因，而 `outcome` 把"本来就没有可注入的源"也记成 `error`——
  三种情况混成一个词：没身份 / 取链失败 / 不需要注入。这让人越查越乱。
- 操作记录里另有 `兜底写入媒体信息失败：NameError`（13:05 / 13:17 / 13:32，每次启动一条）：
  这是 0.9.84 我引入的——注入器工厂写成了 `install_media_info` 里的闭包，
  而 cron 兜底的 `inject_loop` 是模块级函数，根本引用不到。

为什么"偶发失败就放弃"值得修：取不到直链时播放器会退回走隧道（现场实测 4 MB/s 对 20–33 MB/s），
用户感受就是"公网播放又卡了"，而日志里只留一个说不太清的 `error`。

### 改动

1. `resolve_direct()` 返回 `(直链, 原因码)`：`no_identity` / `share_missing` / `share_revoked` /
   `timeout` / `cloud:<类型>`；**瞬时失败重试一次**（0.3 秒）。
2. 播放记录新增 `reason` 列（老库启动时自动 `ALTER TABLE` 补列），
   `outcome` 改为三态：`ok` / `empty`（没有可注入源）/ `error`（真的失败）。
   「取链记录」弹窗显示"已注入 / 无需注入 / 失败"并带原因列。
3. 媒体信息兜底：工厂提到模块级 `make_injector()`，一轮动作抽成 `inject_once()`（可测），
   失败时把异常信息写进操作记录，不再只记类型名。

### 接管 0.9.90 时补的漏项

- `test_inventory_orphans.py`（8 项，0.9.90 新功能）没进 `verify.sh`，已加入「库存与治理」组。
- 顺带核对：`backend/tests` 共 180 个文件，门禁清单只覆盖 92 个，88 个不在清单内。
  这是长期积累的缺口（不是这次引入），需要单独排期决定哪些该进组。

### 线上验证（0.9.91）

```
健康            : {"status":"ready","version":"0.9.91","stage":5}
兜底首轮         : 兜底写入媒体信息：补 24 条（372 条流、471 条章节）   ← 之前每次都是 NameError
NameError(10min): 0
播放记录列       : id, rule_id, name, port, target, user_agent, filled, outcome, at, reason
反代注入         : 8/8 成功，直链实测 33.5 MB/s（退隧道只有 4 MB/s）
```

那条"补 24 条"很说明问题：兜底链路一真跑起来，立刻补上了 24 个此前一直没写进 Emby 的条目——
它不是"没触发过"，而是每次触发都失败，只留下看不懂的一行日志。

## 仓库同步 — 2026-11-11（把 0.9.63–0.9.84 补进开发仓库）

这段时间的改动都是直接改线上部署树 `/path/to/StrimKeep` 并部署验证的，
飞牛开发区的 git 仓库 `/path/to/StrimKeep-dev` 一直停在 **0.9.62**。
这次一次性补齐并提交（`c5d1ce8`，36 个文件，+3480/−110）：

- 新增后端模块 8 个：`emby_proxy` / `emby_proxy_api` / `emby_media_inject` /
  `media_info_api` / `media_info_log` / `orphan_registry` / `output_reconcile` / `housekeeping`
- 新增测试 6 个：`test_emby_proxy` / `test_media_inject` / `test_media_info_log` /
  `test_orphan_registry` / `test_housekeeping` / `test_output_reconcile`
- 新增脚本：`cleanup_orphans.py`、`reconcile_outputs.py`
- 改：`main.py`、`store.py`（`forget()`）、`sync.py`（跳过原因）、`native_media_info.py`
  （入库即写 + 失败记录）、`emby.py`、`cloud115.py`、`Settings.vue`、`Sync.vue`、
  `verify.sh`、`README.md`、`.env.example`、`docker-compose.example.yml`

### 同步时发现并修掉的两个问题

1. **换行符被我的写入改掉，掩盖了真实改动**：`telegram_session.py`（250 行 CRLF）和
   `Sync.vue`（110 行 CRLF）原本是 CRLF，被写成了 LF，git 统计成整文件重写
   （501 行 / 222 行）。还原成 CRLF 后变成 `9 +-` 和 `38 +--`，才是真正的改动量。
   两个仓库现在逐字节一致。
2. **`check_secrets.py` 长期误报**：`telegram_session.py` 里那个保存设置项键名的常量
   （值是 `telegram_api`，只是键名）被当成明文凭据。变量名改成 `API_SETTING`（值不变），扫描从此是绿的：
   `OK：394 个跟踪文件里没有明文口令/令牌`。

### 仓库侧新增的发布物

- `docker-compose.example.yml`：线上部署模板（路径/口令/令牌全部走 `.env`）。
  仓库根目录的 `docker-compose.yml` 仍是开发实例配置（只绑 `127.0.0.1:19000`），两者不混用。
- 版本由 `scripts/version.py 0.9.84` 统一刷：`__init__.py`、`package.json`、
  `package-lock.json`、开发 compose 的镜像 tag、README 标题。

### 验证

- 从**开发仓库源码**跑针对性 36 项测试：全绿（证明同步完整、不依赖部署树）。
- `python3 scripts/version.py`：`版本一致：0.9.84`。
- `python3 scripts/check_secrets.py`：394 个跟踪文件无明文口令/令牌。
- 线上 `strimkeep:0.9.84` 全量门禁 12 组：`全部通过`，含「反代与媒体信息」31 项。

## 0.9.84 — 2026-11-11（功能债第四批：媒体信息逐条失败可查）

以前注入失败只剩一个汇总数字：

```
媒体信息写入 Emby：补 0 条（0 条流、0 条章节），7 条没有 sidecar 跳过、3 条失败。
```

**哪 3 条、为什么**——查不到。而根因是接口层面就丢了信息：`inject_one()` 用 `None`
同时表示"没有 sidecar"和"sidecar 坏了"，调用方根本分不出来。

### 改动

1. `inject_one()` 返回 `{'ok','streams','chapters','reason'}`，原因码区分
   `sidecar_missing` / `sidecar_unreadable` / `no_streams` / `write_failed`。
2. `inject_sidecar()` 返回三元组 `(状态, 流数, 原因)`；状态新增 `unreadable` / `no_streams`。
3. `Injector(recorder=...)`：批量补的时候逐条回报结果（成功传 `''`），
   `run()` 回包里带 `failures`（最多 50 条，避免上千条失败撑爆接口）。
4. 新增 `media_info_log.py`：表 `media_info_failures`，主键 `item_id`，
   记 `name/path/reason/attempts/first_at/last_at`；**成功即删该行** ——
   清单里永远只有"当前还没补上"的条目，不会越滚越多。
   原因码配了中文说明（`sidecar_missing` → "没有媒体信息文件（还没提取，或文件被删）"）。
5. 入口：
   - `GET /api/v1/emby/media-info/failures?limit=20`
   - `POST /api/v1/emby/media-info/failures/clear[?item_id=]`
   - `/emby/media-info/status` 增加 `failures: {count, latest}`
6. 设置页「写入媒体信息」按钮的提示会带上失败条目：
   `已写入 12 条（36 条流），失败 2 条（片名A.strm：sidecar_unreadable、片名B.strm：no_streams）`。

### 顺带修掉一个真实缺陷

`sidecar_path()` 直接对根做 `.rstrip('/')`：调用方传 `pathlib.Path` 就 `AttributeError`
（我的新测试第一次就是这么炸的）。已改成先 `str()`，两个根都兜底。

### 线上实测（0.9.84，真实库，事后已清空）

```
status      : configured True / pending 0 / failures {count: 0, latest: None}
写入两次    : count 1、attempts 2、reason_text「媒体信息文件损坏或不是 JSON」
清一条/清空 : 1 → 0
inject 回包 : {'candidates': 0, 'injected': 0, 'failures': []}
```

现状是"0 待补、0 失败"——之前 1028 + 105 条都已补齐，所以这套东西现在的意义是：
**下一次有片子没补上，能立刻知道是哪一部、什么原因**，而不是对着一个数字猜。

- 回归：`tests/test_media_info_log.py` 4 项（同条目只留一行且累加次数、成功即清、
  clear 单条/全部、`Injector` 逐条回报并喂进清单），已进门禁组。

## 0.9.83 — 2026-11-11（功能债第三批：清掉死配置项）

库里长期躺着几个**代码已经不读**的设置键，危害不是占空间，而是让人以为"改了有效果"：

| 死键 | 曾经的用途 | 现在的事实 |
|---|---|---|
| `emby_proxy_enabled/port/upstream/api_key` | 单台反代的配置 | 已改成 `emby_proxy_servers` 规则列表；老键还在被 `Config` 读，造成"设置页改了没效果" |
| `media_info_extract` | 媒体信息提取开关 | 由 `sync_config.emby_media_info` 单一来源控制 |
| `media_info_inject_minutes` | 任务完成后再等 N 分钟补一轮 | 已改为入库即写 + cron，不再存在"补一轮" |

做法不是"删掉就算了"，而是**把老键的最后一个合法读点收进迁移函数，读完即删**：

- `store.forget(keys)`：删设置项并返回真正删掉的键（用于审计）。
- `emby_proxy.Config` 不再读 `emby_proxy_*`；只在 `_migrate_legacy()` 里读一次端口/开关，
  上游与 Key **仅在和服务连接不同时**才带进规则（避免又造一份隐形配置），随后删除老键。
- 规则已存在的安装（本项目现场就是这样）走 `_forget_legacy()`：规则一条不动，只清老键。
- `housekeeping.purge_dead_config()`：清 `media_info_extract` / `media_info_inject_minutes`，
  随日常维护启动跑一次，删了什么写操作记录。

### 守约束的回归

新增两条测试，属于"防回归"而不是"测功能"：

- `test_purge_dead_config_removes_only_dead_keys`：只删死键，`emby_url`/`emby_key`/`sync_config` 一个不碰，且幂等；
- `test_no_module_reads_dead_settings`：**静态扫源码**——`strimkeep/` 下任何文件都不许出现
  `get('media_info_extract')` 这类读取；`get('emby_proxy_` 只允许在 `emby_proxy.py` 中出现 4 次，
  且必须全部落在 `_migrate_legacy` 里。以后再有人加回读取点，门禁会直接红。

线上验收（0.9.83）：`settings` 表里 `emby_proxy_port`、`media_info_extract`、
`media_info_inject_minutes` 均已消失；`emby_proxy_servers` 规则原样保留，反代端口照常监听。

顺带修了门禁自身的一个坑：`scripts/verify.sh` 不传镜像时用
`grep -m1 '^ *image:' docker-compose.yml` 取镜像，而 0.9.79 的发布模板里**第一行 image 是 emby 侧车**，
于是门禁拿 `emby/embyserver:latest` 跑 pytest → 退出码 127（容器里没有 `python`）。
现在按名字挑 `strimkeep` 那一行，挑不到就明确报错让人显式传参。

本次全量门禁：12 组全绿，`全部通过：strimkeep:0.9.83`，其中「反代与媒体信息」27 项。
线上复核：匿名 `GET /emby/Items` → 401，带 Key 的 PlaybackInfo → 200 且直链仍注入。

## 0.9.81 — 2026-11-11（功能债第二批：三表对账）

`output_registry` / `owned_outputs` / `share_strms` 描述两套归属，写入点散在 `outputs.py`、
`sync.py`、`share_service.py`；少写一笔不会报错，但症状很难认：

- `owned_outputs` 是 `output_registry.files` 的展开，同步清理按它判断"这文件是不是我的"。
  **归属行在、文件没了** → 以后同名文件落地会被当成自己的误删；**归属没了、文件还在** → 野文件。
- `share_strms` 是另一套归属（分享 STRM 不写 `owned_outputs`）。同一路径被两套认领时，
  同步清理和分享撤销会互相拆台。

新增 `strimkeep/output_reconcile.py`，六类不一致，**只修能证明的**：

| 类型 | 判据 | 处理 |
|---|---|---|
| `ghost_ownership` | 归属行在、文件不存在 | 删归属行 |
| `orphan_ownership` | 归属行的受管文件不在台账 | 删归属行 |
| `missing_owned` | 台账列了、归属行没有 | 文件在 → 按台账指纹补建 |
| `stale_in_registry` | 台账列的文件已不在磁盘 | 从 `files` 摘掉，其余字段保留 |
| `path_conflict` | 同路径被两套归属认领 | **只报告**，交人工 |
| `share_missing` | 分享 STRM 文件不存在 | 置 `revoked=1`（不删行） |

- 新写入的行有 1 小时宽限（避免和正在进行的写入抢）；
- 动手前把将删/将改的内容导出到 `<数据目录>/cleanup/reconcile-<时间>.json`；
- 入口：`GET /api/v1/strm/reconcile`、`POST /api/v1/strm/reconcile?dry_run=1`、
  命令行 `scripts/reconcile_outputs.py [--db --root --apply]`；
- 自动跑：`housekeeping_loop` 里每 24 小时一次（启动即跑首轮），只在真有改动时记操作记录。

线上实测（0.9.82，真实库）：

```
台账 1025 / 归属 1124 / 分享登记 154
六类问题全部 0 → 一致
GET /strm/reconcile 耗时 0.06s
```

也就是说前几轮的手工清理之后三表已经收敛；这套东西现在的价值是**防止以后再漂**，而不是补历史。

- 回归：`tests/test_output_reconcile.py` 3 项（六类都能报出、只修可证明项且写备份、
  1 小时宽限保护新行），已进门禁组。
- 写测试时踩到一次自己的坑：fixture 里新行落在宽限内 → "过期"类测不出来；宽限是必要的，
  所以改 fixture 时间戳而不是取消宽限。

## 0.9.80 — 2026-11-11（功能债第一批：同步跳过原因可见 + 操作记录自动裁剪）

按上一轮盘点的功能债，先做两件收益最高、风险最低的。

### 1. 同步「按生成规则跳过」现在能看出**是什么、为什么**

原来只写数量（"按生成规则跳过 1 个文件"），现场根本没法判断。现在 `skip_reason()` 的结论也一起记：

```
按生成规则跳过 1 个文件（00000 (2021) - 1080p...LPCM 2.0.m2ts：未列入 STRM 生成扩展名）。
```

- `sync_filter_names` 增加 `reason` 字段（最多记 10 条，消息里列前 3 条）；
- 原因来自 `strm_options.skip_reason()`：未列入生成扩展名 / 小于生成阈值 N MB。

### 2. 操作记录（events）自动裁剪

新增 `strimkeep/housekeeping.py`：

- 两条线，只删最老的：超过 `events_keep_days`（默认 90 天）、剩余超过 `events_keep_rows`
  （默认 20000 行）时再按 id 从旧到新删到上限；0 表示该条不裁剪。
- 启动先跑一次，之后每 6 小时一次（`housekeeping_loop`），裁剪结果写一条操作记录。
- 接口：`GET /api/v1/system/events/stats`、`POST /api/v1/system/events/prune?dry_run=1`。

线上实测（0.9.80）：

```
裁剪前统计 : rows 13569 / keep_days 90 / keep_rows 20000
dry_run    : by_age 0 / by_count 0（都在阈值内，不删）
实际裁剪   : 0 条，remaining 13569
```

- 回归：`tests/test_housekeeping.py` 2 项（按时间+行数裁剪、dry-run 不写库、阈值 0 时全保留），
  已加入门禁组「反代与媒体信息」。

## 0.9.79 — 2026-11-10（发布前收尾：去掉写死的环境、补门禁、修一个提权漏洞）

这版不新增功能，只把"为了现场方便"留下的东西清理成可发布状态，并修掉一个 **安全漏洞**。

### 安全：反代把管理员 API Key 借给了匿名请求

`emby_proxy.py` 原来对**任何**没带 token 的请求都补 `x-emby-token: <Emby API Key>`。实测：

```
curl "http://<host>:9001/emby/Items?Recursive=true"
→ 200，返回全库 12228 条记录（TotalRecordCount: 12228）
```

Emby 的 API Key 等同管理员：读全库、调管理接口。等于把这个公网端口的匿名请求全部提权。
改为**只对 PlaybackInfo 补 Key**（我们做直链注入才需要读那个响应），其余请求原样转发。
补了回归用例 `test_proxy_only_injects_key_for_playback_info`（13 项通过）。

### 可发布化：不再假设用户的路径/密钥

- `emby_media_inject.py`：`DEFAULT_DB` 由 `/emby-data/library.db` 改为**空**；未配置时
  接口返回明确提示（`configured: false` + hint），**不再猜路径**。
- 新增 `emby_strm_mount(store)`：Emby 侧看到 STRM 的挂载根，顺序为
  设置项 → `STRIMKEEP_EMBY_STRM_MOUNT` → 控制台 `emby_strm_path` → `/media`，
  取代原来写死的 `emby_root='/media'`。
- `docker-compose.yml` 改成发布模板：路径、口令、指标令牌全部 `${...}` 走 `.env`；
  新增 `.env.example`；镜像 tag 仍与 `__init__.__version__` 保持一致（测试会校验）。
- 前端占位符去掉私有内网地址（`192.0.2.10` 全部替换为 `example.com` / 「你的NAS地址」）。

### 门禁

- `scripts/verify.sh` 新增一组「反代与媒体信息」：`test_emby_proxy`、`test_media_inject`、
  `test_orphan_registry` —— 这三块之前不在门禁里，等于没有回归保护。
- compose 里口令改 `${ADMIN_PASSWORD:-changeme}`，`test_compose_has_no_hardcoded_password`
  这条长期红灯随之可以变绿。

## 2026-11-10 现场：找出并清理「按生成规则跳过」的那个文件

「全量同步一直显示跳过 1 个文件」查明了：消息只有数量、没有名字，现场根本看不出是哪个文件。

- 改动（0.9.77）：算完 `sync_filter_count` 之后，把被跳过文件的 `id/name/size/parent_id` 记进
  `config['sync_filter_names']`（最多 10 个），并把前 3 个名字写进任务消息。
- 跑了一次全量同步（任务 #920），消息变成：
  `按生成规则跳过 1 个文件（00000 (2021) - 1080p.SDR.H.264.8-bit.24fps.LPCM 2.0.m2ts）。`
- 原因：`.m2ts` 不在「视频扩展名」列表里，所以每轮都不生成 STRM（不是错误）。
- 处置：按用户要求把该文件移入 115 回收站（`cloud.recycle`，可恢复）：
  `00000 (2021) {tmdb-886864}/00000 (2021) - 1080p...LPCM 2.0.m2ts`，22176.9 MB，返回 `{'state': True}`。
  它所在的目录现在只有这一个文件，删完即空，下一轮同步不会再报「跳过 1 个」。

## 0.9.76 — 2026-11-10（去掉重复的"任务完成再补一次"）

0.9.72 之后媒体信息有三条写入路径：入库即写、任务完成再补、cron 兜底。后两条对同一批条目
是重复劳动（入库那一刻已经写过了），是我把"兜底"写复杂了。

- `inject_loop()` 只保留 **cron** 一条：到点扫一次"漏写的"（写库瞬间失败、旧条目、库被重算）。
- 文案同步改成"只用来补漏写的：新条目入库时已经即时写过"。
- 现在的分工只有三条，互不重复：
  1. 新条目 → 提取完 sidecar 立刻写库（追更）；
  2. cron（默认 `0 4 * * *`）→ 扫一遍补漏；
  3. 手动「写入媒体信息」按钮。

## 2026-11-10 现场清理：孤儿记录（台账 + 分享登记）

`STRM 重建预览` 一直显示 `missing 156`。查明是**台账里记着、磁盘上没有**的孤儿行，来源是
删条目时 Emby 顺手删掉了对应 `.strm`（分享来源的更是同步也不会再生成），台账不监听外部删除。

- 新增 `scripts/cleanup_orphans.py`：先按"文件确实不存在"筛，导出整行到
  `data/cleanup/orphans-<时间>.json`，再删；默认预览，`--apply` 才写库。
- 本次执行：台账 1181 → **1025**（删 156 条），分享登记 190 → **154**（删 36 条孤儿）。
- 清理后：重建预览 `rewritten 0 / unchanged 1025 / missing 0 / failed 0`；
  Emby 1140 条 / 有视频流 1140；媒体信息缺口 0；反代 9001 仍正常注入直链。
- 回滚：把 `data/cleanup/orphans-20261110-013003.json` 里的行重新插回 `output_registry` /
  `share_strms` 即可（含全部原字段）。

## 0.9.74 — 2026-11-10（文案：不再对自己的 Emby 叫"上游"）

- 反代卡片按钮「连一次上游」→ **「测试连通性」**；提示语「上游就是…」→「后端就是上面填的
  『Emby 服务连接』」；端口说明里的「上游地址与 Key 用上面那份」→「地址与 Key 用上面的
  『Emby 服务连接』」。
- 后端回报同步改口径：「还没有配置 Emby 服务连接：请先在上面填写地址与 API Key。」
  「Emby 返回 HTTP %d」「连不上 Emby：…」「反代无法连接 Emby，请检查 Emby 服务地址。」
- 已核对打包产物：`测试连通性 / 兜底补写 / Emby 服务连接` 命中，`连一次上游` 不再出现。

## 0.9.73 — 2026-11-10（兜底改成 Cron，与同步/扫描同一套）

上一版用「每 N 分钟」是错的口径：项目里同步/扫描本来就用 `croniter` + 时区，兜底也该一样。

- 兜底改读 `schedule_config`：`media_info_enabled` + `media_info_cron`（默认 `0 4 * * *`），
  时区沿用 `schedule_config.timezone`；`inject_loop()` 每分钟比对"最近一次该触发的 cron 时间"，
  只在该触发后跑一次（不会重复跑）。
- 写入接口 `PUT /api/v1/emby/media-info/config` 改为接收 `cron_enabled` 与 `cron`，
  5 段表达式用 `croniter.is_valid` 校验，不合法直接 422。
- 设置页那颗「兜底补写」从分钟数换成「启用 + Cron 表达式」，并注明与同步/扫描同一套时区。
- 另一条触发保留：**任一同步/整理任务完成就补一次**（与你的每日/每周同步天然对齐）。
- 线上设置：`media_info_enabled=true`、`media_info_cron=0 4 * * *`（每天 04:00 兜底一次）。

## 0.9.72 — 2026-11-10（追更=入库即写；兜底挂在任务完成，不再需要单独的定时开关）

按"入库就写"的要求把链路收紧，并去掉上一版多出来的重复开关：

- **追更（入库即写）**：`native_media_info.write_sidecar()` 写完 sidecar 立刻调用
  `inject_sidecar()` 把这一条写进 Emby 的 library.db（已有视频流则跳过）。所以只要
  「同步设置 → 媒体信息提取」是开的，新剧集入库 → ffprobe → sidecar → 立刻出现在 Emby，
  不再等任何定时任务。
- **兜底**：`inject_loop()` 现在两条触发——① 任一同步/整理任务状态变成 completed 就补一次
  （对齐你每天/每周的同步节奏）；② 设置项 `media_info_inject_minutes` 到点也补一次
  （0 = 只靠第 ① 条；1440 = 每天、10080 = 每周）。
- **去掉重复开关**：删掉上一版新增的 `media_info_extract`。是否提取统一由既有的
  `sync_config.emby_media_info` 决定（`native_media_info.tick()` 本来就看它），设置页那颗
  开关改成只读展示并注明来源，避免同一件事两个开关。
- 设置页：Emby 卡片里「新入库自动提取媒体信息（追更）」只读展示；「兜底补写（分钟）」可填
  0 / 1440 / 10080，保存走 `PUT /api/v1/emby/media-info/config`。

线上状态（0.9.72）：

```
extract=true（sync_config.emby_media_info）
inject_minutes=1440（每天兜底一次）
STRM 条目 1140 / 有视频流 1140 / 只有字幕 0 / 外挂字幕行 208（未被覆盖）
```

- 验证：`tests/test_media_inject.py` 4 项（新增「即时写入 + 已有视频流跳过 + sidecar 缺失」）。

## 0.9.70 — 2026-11-10（职责拆开：同步只写 STRM/NFO，媒体信息走自动提取 + 定时写入）

按"追更模式"的思路把三件事拆开，各自独立开关：

| 环节 | 谁来做 | 什么时候 |
| --- | --- | --- |
| STRM / NFO | 同步与整理 | 全量或定时同步时 |
| 媒体信息提取（ffprobe → sidecar） | 入库交付时自动（可关） | 新条目入库那一刻（追更） |
| 媒体信息写进 Emby | 定时任务 / 手动按钮 | 每 N 分钟，或点「写入媒体信息」 |

- `emby.py` 的入库交付处加开关 `media_info_extract`（默认开）：关掉后同步/整理**只写 STRM 与
  NFO**，不再对每条新条目发起 ffprobe 探测。
- 新增 `inject_loop()`（`media_info_api.py`）：每 60 秒检查设置项 `media_info_inject_minutes`，
  到点就把缺媒体信息的 sidecar 写进 Emby；只补缺的、幂等，失败只记一条操作记录。
  lifespan 里启动并在退出时取消。
- 新增接口 `GET/PUT /api/v1/emby/media-info/config`（`extract`、`inject_minutes`）。
- 设置页「Emby」卡片新增：「新入库自动提取媒体信息（追更）」开关、「定时写入 Emby（分钟，0 = 不定时）」+
  「保存媒体信息设置」。线上已设成 10 分钟。

**顺带补齐 0.9.68 的漏网条目**：判据从「一条流都没有」改成「没有视频流（StreamType=2）」，
补写时保留 `IsExternal=1` 的外挂字幕行（先取出、写完补回），避免补视频信息把字幕弄丢。

线上结果（Emby 4.10.2）：

```
第一批（0.9.68）：candidates 1028 → injected 1028，streams 7903，chapters 10618
第二批（0.9.70）：candidates  105 → injected  105，streams 1199，chapters 1083
现在：STRM 条目 1140，有视频流 1140，只有字幕 0，外挂字幕行 208（未被覆盖）
设置：extract=true，inject_minutes=10
```

- 验证：`tests/test_media_inject.py` 3 项通过（含「只有外挂字幕的条目」一例）。

## 0.9.69 — 2026-11-10（补齐「只有外挂字幕」的 105 条，且不丢字幕）

0.9.68 的判据是「一条流都没有才补」，漏掉了只登记外挂字幕的条目：现场 1140 条里有
**105 条**只有一条 `3:ass` / `3:sup`，没有视频和音频信息，被「已有就跳过」挡在外面。

- 判据改为**没有视频流（StreamType=2）才补**；补过的条目一定有视频流，所以仍然幂等。
- 补写时**保留原有的外挂行**（`IsExternal=1` 指向磁盘上的真实 .ass/.sup 文件），
  先取出、写完再按顺序补回去，避免"补了视频信息却把字幕弄丢"。
- 回归：`tests/test_media_inject.py` 增了「只有外挂字幕的条目」一例（补完 2 条流 + 字幕仍在），
  幂等与 dry-run 断言同步收紧。



现场问题：1140 个影片/剧集里 **1028 条在 Emby 里没有媒体信息**（MediaStreams=0）。

先把「能不能让 Emby 自己补」全部试掉（Emby 4.10.2）：

| 尝试 | 结果 |
| --- | --- |
| `POST /Items/{id}/Refresh?MetadataRefreshMode=Default` | 204，`MediaStreams` 仍 0 |
| `MetadataRefreshMode=FullRefresh` | 204，仍 0 |
| `POST /Library/Media/Updated`（告知文件变更） | 204，仍 0 |
| `touch` STRM + 全库扫描，等 120 秒 | 仍 0 |
| `.nfo` 写 `<fileinfo><streamdetails>`（真实宽高/码率/时长） | 刷新后仍 0 |

对照：用 Emby 的 UA 对同一个 STRM 地址跑 ffprobe，能正常读出 `hevc 3840×1604 + eac3` —— 取流链路没问题，是 **Emby 不重算 STRM**。参考开源实现（StrmAssistant + 它的外部提取器）确认：这一环由**服务端插件**直接写库完成。

于是用同样的原理从外部写库（实测可行）：

- 新增 `strimkeep/emby_media_inject.py`：读本项目已有的 sidecar（字段本身就是 Emby 的
  MediaStream 形状）→ 写 `MediaStreams2` / `Chapters3` → 补 `MediaItems` 的宽高与总码率。
  Emby 的 StreamType 按线上库核对：1=Audio、2=Video、3=Subtitle、4=EmbeddedImage。
- 新增 `strimkeep/media_info_api.py`：`GET /api/v1/emby/media-info/status`（缺多少条）、
  `POST /api/v1/emby/media-info/inject?dry_run=&limit=`（整库先备份再逐条补，按 ItemId 先删后插，幂等）。
- docker-compose 给 strimkeep 挂 `…/emby/data:/emby-data`（库路径默认 `/emby-data/library.db`，
  可用设置项 `emby_library_db` 或环境变量覆盖）。
- 设置页「Emby」卡片新增按钮「写入媒体信息」，返回写入条数。

**线上结果（Emby 4.10.2，运行中直接写，无需重启）**

```
写之前：Emby 条目 1140，缺媒体信息 1028
写入  ：candidates 1028 / injected 1028 / streams 7903 / chapters 10618 / failed 0 / no_sidecar 0
备份  ：/emby-data/library.db.bak-20261110-004922
写之后：Emby 条目 1140，缺媒体信息 0
```

- 验证：`tests/test_media_inject.py` 3 项通过（路径反推、字段与 StreamType 映射、批量补齐与幂等、
  dry-run 不写库、备份落盘）。
- 已知取舍：这是外部写库，Emby 若日后重算或升级改表结构，可能需要再点一次「写入媒体信息」
  （幂等，且有整库备份）。

## 0.9.66 — 2026-11-10（反代改成多台服务器，形态照上游那张卡片页）

上游的反代不是一个开关，而是一组**服务器卡片**：每台独立的地址、Key、端口，卡片上直接带
启停、测试、记录、删除。这里按这个结构重做（布局参考，文案是我们自己的说法，没有照抄上游文字）：

- 后端：`emby_proxy.py` 拆成 `ProxyServer`（一台服务器：独立端口、独立连接池、独立启停）
  与 `EmbyProxyManager`（规则落库 `settings.emby_proxy_servers`、增删、启停、测试、取链记录）。
  老库只有单台配置时自动迁移成一条规则；地址与 Key 留空即沿用「Emby 服务连接」。
- 新增 `emby_proxy_api.py`：`GET /emby-proxy`、`POST /emby-proxy/servers`、
  `PUT /emby-proxy/servers/{id}`、`POST .../enable|disable`、`POST .../test`、
  `GET .../plays`、`DELETE /emby-proxy/servers/{id}`；端口冲突返回 409 并说明被哪台占用。
- 新增 `EmbyProxyServers.vue`，位置仍挂在「系统设置 → 服务连接 → Emby」卡片内（不单开一张卡）：
  每台一张小卡，状态徽标（运行中／已启用但没有监听／已停用）+ 保存、启用/停用、取链记录、
  连一次上游、移除；取链记录用弹窗表格展示。
- 取链记录落库 `emby_proxy_plays`（时间、客户端、请求路径、补了几条直链、结果），只留最近 2000 条。
- 验证：`tests/test_emby_proxy.py` 11 项通过（含老配置迁移、单台覆盖服务连接、换端口重启、
  POST 请求体带 Content-Length 到达上游）。

**部署时抓到并修掉一个致命问题**：0.9.66 第一次上线后整个应用卡在 `Waiting for application
startup`。原因不是新模块逻辑，而是端口绑定失败时 uvicorn 在后台任务里 `sys.exit(3)`，
`SystemExit` 会从 `asyncio.Task` 里重新抛出，把**整个事件循环**带走。改为先自己 `bind`
端口（失败就只让这一台不监听，写一条记录），再把已绑好的 socket 交给 uvicorn，并给后台
任务挂 done 回调取走异常。补了回归测试「端口被占用只能这一台起不来，事件循环仍可用」
（第 12 项），旧代码在这条用例下会直接崩。

## 0.9.65 — 2026-11-09（修反代两处转发缺陷：POST 体与流式响应）

现场反馈「局域网 9001 连不上」。检查结论：端口本身是通的（同一局域网另一台机器
`http://192.0.2.10:9001/` 得 200），真正断在 Emby 的握手请求上：

```
直连 8100  POST /emby/Users/AuthenticateByName  → 401 Invalid username or password（正确）
经反代 9001 同一个请求                            → 400 Value cannot be null. (Parameter 'name')
```

两个缺陷，都是转发写法的问题：

- **请求体被按分块转发**：`request.stream()` 交给 httpx 后没有 Content-Length，上游 Emby
  解析不了 JSON 体，登录必然失败（于是播放器显示"连不上"）。改为把 API 请求体读全再转发
  （带 Content-Length），只有超过 16MB 的大上传才退回流式。
- **响应流被提前关闭**：`httpx.AsyncClient` 建在请求作用域里，`async with` 退出就把连接关了，
  流式响应体会被截断（`incomplete chunked read`）。改为实例级长连接客户端，`start()`
  建立、`stop()/restart()` 关闭，单个请求只关自己的上游响应流。

**0.9.65 追加：不重复配置**

设置页本来就有「Emby 服务连接」（`emby_url` / `emby_key`），反代却又让人填一遍上游地址
和 API Key，属于重复设计。现改为**反代直接沿用服务连接**：

- `Config` 解析顺序：环境变量 → 显式 `emby_proxy_*`（特殊部署才用）→ `emby_url` / `emby_key`。
- 设置页的反代卡片只剩「启用」开关与「监听端口」，上游一行只读显示服务连接里的地址。
- 改了「Emby 服务地址 / API Key」保存后，反代同样立即重启，换到新的上游。
- 验证：`tests/test_emby_proxy.py` 覆盖「沿用服务连接 / 显式覆盖优先 / 环境变量优先 /
  两者都没配则不监听」四种情况，9 项全过。

## 0.9.64 — 2026-11-09（反代进设置页：显示、可改端口、保存即生效）

0.9.63 只做了后端：设置项在 API 里有，前端设置页看不到，改端口还得重启容器。两处补齐：

- 「系统设置 → 服务连接」新增「Emby 反代（播放加速）」卡片：启用开关、上游 Emby 地址、
  上游 API Key（留空保留原值）、监听端口（1–65535 任意填）。
- 保存后立即生效：`EmbyProxy.restart()` 先停旧监听再按新配置起；关掉开关即停止监听。
  端口被占用或无权绑定时不再静默失败——启动改为等 `server.started`，失败写一条操作记录。
- 说明文字直接写在卡片里：外网使用需在 frp 里加同名端口的 tcp 映射；播放器填
  `http://服务器地址:端口`，账号密码与上游 Emby 相同。
- 验证：`tests/test_emby_proxy.py` 增到 8 项（新增「端口 19001 → 19002 重启后旧端口关闭、
  新端口监听、stop 后释放」）；前端改动走 `docker compose up -d --build` 重新构建。

## 0.9.63 — 2026-11-09（新增 Emby 反代：PlaybackInfo 注入 115 直链）

**先对齐上游，再写代码。** 之前的结论都是猜的，这次先做黑盒取证，把上游 TgtoDrive
（容器 `TgtoDrive`，host 网络，Web 12366）播放快的原因测出来：

| 取证手段 | 结果 |
| --- | --- |
| `docker logs TgtoDrive` | `PlaybackInfo 参数规范化：已强制 EnableDirectPlay/EnableDirectStream=true`、`PlaybackInfo 处理成功: direct=1, local_upstream=0` |
| `db/embydata/emby_proxy_config.json` | `proxy_type=emby`、`host=http://192.0.2.10:8096`、`api_key=…`、`listen_port=8097` |
| `ss -ltnp` | 8097 是 TgtoDrive 的 python 进程，8096 是上游 Emby：反代与 Emby 是两个监听 |
| 同一 item 两份 PlaybackInfo 逐字段 diff（直连 8096 vs 经反代 8097） | 结构差异只有 **一个**：反代多出 `MediaSources[0].DirectStreamUrl = https://cdnfhnfile.115cdn.net/…`；Path / Protocol / SupportsDirectPlay 一字未改 |

结论：上游的播放加速 = **Emby 反代**。客户端连反代端口，反代带 API Key 转发接口，
并在 PlaybackInfo 里补上预解析好的 115 CDN 直链；客户端据此直接向 CDN 取流，
视频不经过 Emby、不经过反代、也不经过 frp 隧道。**上游 Emby 一行都不用改**，
STRM 里写内网还是公网地址也不再影响速度——所以调 `playback_base`／给播放端口加
TLS 都是错的方向（那两件事已在 0.9.63 之前回滚）。

改动：

- 新增 `strimkeep/emby_proxy.py`：独立端口监听（默认 9001），`/emby/*` 原样转发到上游，
  仅对 `.../Items/{id}/PlaybackInfo` 的响应注入 `MediaSources[*].DirectStreamUrl`；
  身份取自本项目生成的 `/play115/{pickcode}/…` 与 `/play115/share/{sid}/…`，
  取链复用 `cloud.url()`（按 `(账号, 文件, UA)` 缓存与并发合并，与 `/play115` 同一条通道）。
- 取链失败、源已是别人的地址、分享已撤销：一律保持上游响应原样，客户端照常回落到
  Emby 转发，不会因为反代把可播的变成不可播。
- 反代只转发 API 流量（KB 级），视频字节一个都不代理。
- 设置项 `emby_proxy_enabled / emby_proxy_upstream / emby_proxy_api_key / emby_proxy_port`
  （也可用 `STRIMKEEP_EMBY_PROXY*` 环境变量）；未填上游地址时不监听端口。
- 验证：新增 `tests/test_emby_proxy.py` 7 项通过（地址身份解析、只加一个字段、
  取链失败保持原样、已有直链不覆盖、分享行解析、撤销分享、配置回退与环境变量优先级）；
  `test_app / test_iteration1 / test_iteration2` 45 项通过（`test_compose_has_no_hardcoded_password`
  失败与本次改动无关，compose 里的密码是既有状态）。

**部署与实测（NAS，2026-11-09 23:2x）**

- 镜像 `strimkeep:0.9.63`，容器 healthy；反代监听 `0.0.0.0:9001`，frp 新增
  `fnos-emby-proxy`（remotePort 9001），公网自测 `http://95.181.190.234:9001/` 200 / 0.7 s。
- 设置：`emby_proxy_enabled=true`、`emby_proxy_upstream=http://127.0.0.1:8100`、
  `emby_proxy_api_key`（该 Emby 的 API Key）、`emby_proxy_port=9001`。
- 经反代取 PlaybackInfo（UA=SenPlayer/6.3.0）：
  - `#24755`（分享库）→ `DirectStreamUrl=https://cdnfhnfile.115cdn.net/6aec5fa8…`；
  - `#25228`（网盘库，走公网 9001）→ `DirectStreamUrl=https://cdnfhnfile.115cdn.net/6aeeacff…`。
- 客户端视角实测拿到直链后的取流：`206 Partial Content`，20 MB / 1.1 s = **17.8 MB/s**
  （同一部片经 Emby 转发的隧道上限是 4 MB/s）。
- 用法：播放器里把服务器地址指向反代端口（公网 `http://skp.xclogc.top:9001`，
  在家可用 `http://192.0.2.10:9001`），账号密码与那台 Emby 相同；
  不再是 `:8100`／`:443` 的直连入口。原入口保留，随时能切回，反代出问题时客户端
  会自动回落到 Emby 转发（只是慢）。

## 0.9.61 — 2026-11-09（回滚「UA 不进缓存键」：115 CDN 直链是绑 UA 的）

**现场现象**：Emby 播放报 HTTP 500。链条是 Emby 自己的日志给的：

```
21:18:23 HttpClient: GET http://192.0.2.10:9000/play115/d0ynysx0d0tadxlds/…
21:18:23 HttpClient: Http response 403 from http://192.0.2.10:9000/play115/…
21:18:23 VideoService: Response 500 to … GET /emby/videos/25481/stream.mkv
```

即：我们 302 过去的 115 CDN 返回 **403**，Emby 把它折成 **500** 给客户端。我们自己的
`/play115` 始终是 302，日志里一条 5xx 都没有——所以问题在 CDN 那一跳。

**定位实验**（同一条被缓存下来的直链，只换求取的 UA）：

| 用哪个 UA 去取这条链 | 结果 |
| --- | --- |
| Infuse/7.8（签发时就是它） | **206** |
| Emby/4.8.0.0 | 403 |
| Lavf/57.28.100 | 403 |
| 空 UA | 403 |

结论：**115 的 CDN 地址绑定签发那次请求的 UA**。0.9.59 依据 115driver 里
「分享下载返回的 CDN 地址不绑 UA」的注释，把 UA 从缓存键里去掉了——但那条注释只对
**分享下载**成立，普通 `download_url` 不成立。于是 Emby 探测（Lavf）拿到的链接被
Emby 播放端复用，CDN 403 → Emby 500。**我的判断错了，这条实测把它证伪了。**

- 改动：缓存键恢复 `(账号, 文件, UA)`；0.9.59 的另一个收益（TTL 由直链 `t` 推导、
  命中率指标）**保留**——那部分与 UA 无关。
- 测试同步改回并加了"为什么"：`test_link_cache_is_isolated_by_user_agent`
  （两个 UA 各取一次链、同一 UA 仍复用）；`test_flow058` 恢复「换 UA 必须重取」。
- 教训：`115driver` 的注释是**针对分享接口**写的，不能平移到普通下载；涉及
  "能不能共用"的判断，一律用同一条链接换 UA 实测，别用别处的注释推论。


## 0.9.60 — 2026-11-09（P2 根治：任务状态机收成单一入口）

审计 A1 的根治。之前 `organize_jobs` 有 **9 个写入方**、`organize_items` 有 **6 个**，
各自拼 SQL、各自决定能写哪一步——0.9.47「未完成当完成」能发生，不是因为某一行写错，
而是**没有任何地方能拒绝一次错误的写入**。

- **新增 `strimkeep/jobs.py`（唯一的写入层）**：
  - 状态词表 `JOB_STATES` / `ITEM_STATES`：写未知状态（`completd` 这种拼错）直接抛 `JobStateError`，
    以前它会静默入库，报表按已知词统计就永远看不见；
  - 迁移表 `JOB_TRANSITIONS` / `ITEM_TRANSITIONS`：非终态之间只能走表里的路；终态可以收口，
    但离开终态只能显式重开；
  - `finish()` 强制终态 + **自动收口**：进终态时把还停在 `discovered` / `planned` 的条目落
    `skipped` 并写清原因（`Store.settle_terminal_job` 的实现在这里，方法名保留）；
  - `create/update/update_item/replace_manifest/copy_manifest/prune_manifest/insert_item/
    skip_job_items/skip_reconciled/skip_recovered/note_superseded/set_item_plan/...` 收口所有写入形态。
- **迁移 9 个模块、共 33 处直写**：main / sync / share_service / tasks / transfer / offline_api /
  notifications / upgrade / store。`store.py` 只留建表、保留期清理与退役模块清理（维护动作）。
- **新指标 `strimkeep_job_rejected`**：被迁移表拒绝的次数。长期为 0 才说明没人绕路。
- **守门测试 `tests/test_jobs_repo.py`（10 项）**：源码扫描断言"除 jobs.py / store.py 外没有任何
  模块直接 UPDATE/INSERT/DELETE 这两张表"，加上状态词、迁移、收口、人工结项、重启恢复等语义。
  已加入门禁「核心流水线」组。
- **门禁揪出 4 个真回归（都已按真实链路修正，而不是放宽到"什么都允许"）**：
  1. `partial → running`：本地输出补齐（repair_only）与人工重跑会重跑同一个任务 —— 终态回 queued/running 合法；
  2. `paused → waiting_login`：登录失效时会从暂停转等待登录；
  3. `planned → pending`：合并集/未识别分流会把已定计划的条目退回"待处理"；
  4. `create(config=...)` 对已经是 JSON 字符串的 config 又编码了一次（双重编码），
     下游 `json.loads(row['config']).get(...)` 直接炸 —— 现在字符串原样入库；
  5. **原子性**：仓储层每条写各自提交，把「建任务 + 存分享密钥 + 认领频道」拆成了三次提交，
     认领失败时任务行留在库里（`test_channel_job_and_claim_commit_together` 抓到）。
     现在 `Jobs._atomic()` 会并入 `store.transaction()` 开启的显式事务；
     分享入队三步走同一个事务，失败一起回滚。
- 验证：`test_jobs_repo`（10）+ `test_organize`/`test_deep_delete`（53）+ `test_transfer`/`test_upgrade`（67）
  全绿，随后跑全量门禁 11 组。


## 0.9.59 — 2026-11-09（直链缓存：按直链自己的有效期存，探测与播放共用一份）

先按"参考开源项目"的要求查了一圈同类实现，再动自己的代码：

| 项目 | 115 直链有没有缓存 |
| --- | --- |
| AList / OpenList 的 115 驱动（`Link()`） | **没有**。每次播放都 `DownloadWithUA` 打 115，只有 `rate.Limiter` 限速（`LimitRate`） |
| SheltonZhu/115driver（上面两家共用的 SDK） | **没有**。`DownloadWithUA` 就是一次请求 |
| MediaWarp（Emby/Jellyfin 的 STRM 302 中间件） | 只缓存**图片和字幕**；README 里"利用 Redis 做数据缓存"被标记为"需求不大，放弃" |
| 115-Station / faststrm / SmartStrm 等 STRM 生成器 | 生成 STRM + 302，未见直链缓存 |

结论：**我们本来就有缓存，比主流实现做得细**。但对照参数就发现方向错了——

- 现场抓 4 条真实 302 的 `Location`：115 直链的 `t` 参数是**过期时间戳**，有效期 **92~297 小时（4~12 天）**；
- 而我们把它们只存 **15 秒**（探测 UA 60 秒）。等于把一个能用四天的地址每 15 秒重取一次：既慢，又白烧配额。

改动：

- **TTL 交给直链自己决定**。新增 `link_expiry()` / `link_ttl()`：解析 `t`，取「剩余 − 5 分钟安全边距」，地板 15 秒（读不到 `t` 时的保守回退）、上限 6 小时（兼顾换账号／文件被撤这类我们看不见的失效）。
- **缓存键从「账号 + 文件 + UA」收紧成「账号 + 文件」**。115 直链不绑定请求 UA（115driver 源码注释与现场实测都确认），按 UA 分开存等于 Emby 探测（Lavf）与播放器开播各打一次接口——同一部片、同一分钟、两次 115 请求，纯浪费。
- **容量 256 → 2048**：每条缓存只是一个 URL 字符串，没必要为省内存挤掉热条目。
- **命中率进 `/metrics`**：`strimkeep_play_cache{result="hit|miss"}`，用来直接回答"开播还需不需要等 115 接口"。
- 验证：`test_play85.py` 重写两条把旧契约钉死的用例（原本要求"按 UA 分缓存 / 探测 60 秒、播放 15 秒"），新增"探测与播放共用一份""读不到 `t` 退回 15 秒"；`test_flow058.py` 的`test_playback_link_reuse_is_short_lived_and_isolated_by_session_and_ua` 同样钉着旧契约（改 UA 必须重取），改写为"按账号隔离 + 换 UA 共用 + 无 `t` 时 16 秒后过期"；`test_ops0839.py` 新增"命中率出现在 /metrics，云客户端缺席时不报错"。门禁首轮的失败正是这三条旧用例拦下来的——契约改了，用例必须跟着改，不能让它继续"通过"。


## 0.9.58 — 2026-11-09（分享库播放补上指标、入口限流与超时兜底）

0.9.57 修完「302 慢在排队」之后顺手核对两条产线的播放入口，发现分享库**少了网盘侧早就有的三件套**——而现场 1145 条 STRM 里有 82 条走分享库，反馈「播放慢」时这一半完全没有数据可看。

- **指标**：`/play115/share/...` 以前只写一条 `play` 事件，`strimkeep_play_total` / `strimkeep_play_latency_ms` 里看不到任何分享流量。现在 ok / error / timeout 三种结果与耗时都入桶（`record_play`）。
- **入口限流**：网盘侧每个入口 60 次/分钟（`app.state.play_limits`），分享侧一道闸都没有——一个反复重连的播放器就能把分享取链刷穿、白烧 115 配额。现在共用同一份 `play_limits`，键用 `share:<sid>`，超限直接 429 且不打 115。
- **超时兜底**：网盘侧有 `asyncio.timeout(35)`，分享侧只有底层 15 秒读超时、且超时只留一条事件。现在同样 35 秒封顶，超时记 `timeout` 并返回 504。
- 验证：`test_app.py` 新增「分享取链入指标 + 失败也入桶 + 第 61 次 429 且没打到 115」；该文件此前**根本不在门禁清单里**（所有端点测试都没有守卫），这版一并纳入「核心流水线」组（15 项 12.28 秒）。


## 0.9.57 — 2026-11-09（播放取链不再全局串行："302 播放慢"的真因）

现场反馈「302 播放加载慢」。先量再改，结论是**服务器出 302 本身不慢，慢在排队**。

- **实测三段，只有一段慢**：
  - StrimKeep 出 302 的 TTFB 0.20–0.44 秒；`play` 事件里 268 条取链耗时中位 **0.19 秒**、p90 0.34 秒、最大 2.30 秒；
  - 跟随 302 到 115 CDN：首字节 0.29–0.58 秒，下行 10–12 MB/s（NAS 出口上限），seek 到中段同样 0.4 秒；
  - **并发 6 个请求**：单条涨到 0.31–1.33 秒，6 个总耗时 **1.37 秒** —— 等于逐个串行。
- **根因**：`cloud115.play_gate` 是一把全局 `asyncio.Lock()`，把「50ms 发车间隔」和「请求本身」扣在同一把锁里，于是第 N 个请求必须等前 N-1 个跑完：`N ×（50ms + 单次往返）`。播放器开播时探测、正片、断线重连会同时撞上来，观感就是"卡一下才开"。
- **改法（只动调度，不动业务）**：
  - `play_gate` 由 `Lock` 换成 `asyncio.Semaphore(PLAY_CONCURRENCY = 4)`；
  - 新增 `reserve_play_slot()`：在单独的 `play_timetable` 锁里原子「抢发车时刻」并把下一次推后 `PLAY_SPACING`，请求本身在锁外并发跑；
  - `PLAY_SPACING` 仍是 50ms（≤20 次/秒），对 115 的**启速压力与改前完全一致**，只是不再把请求串起来；
  - 不变：同 (账号, pickcode, UA) 的并发合并、直链缓存 15 秒（探测 UA 60 秒）、超时与 WAF 判定。
- **验证**：`test_play85.py` 新增两项——「不同文件并发取链不被串行」（断言同时在飞 ≥2、4 个 0.2 秒请求 <0.6 秒；用 `git stash` 退回旧实现跑过，必失败）与「并发放开后 50ms 间隔仍然生效」，全部 8 项通过；`docs/operations/RATE_LIMITS.md` 同步改写。上线后复测并发 TTFB。

## 0.9.56 — 2026-11-09（P1：长请求转后台、跳过的删除明细只留 2 天）

审计清单里 P1 的两项。都只改"什么时候做"和"留多久"，不动业务判定。

- **分享存活检测转后台**。实测 82 个分享串行取链要 **41.4 秒**（`perf` 事件里那条 `POST /api/v1/share/check 41.4 秒`），以前这段时长直接压在 HTTP 请求上、页面假死。现在统一走 `Organizer.start_share_check(wait)`：
  - 手动触发（`POST /api/v1/share/check`）最多等 **8 秒**：小库照旧同步拿结果，大库立刻返回 `{'pending': True}`，跑完照旧写 `share_check` 事件与 `meta.share_check_last`；
  - 定时巡检（每天 03:00）用 `wait=0`，完全后台，不再占着调度循环；
  - 同一时刻只允许一次（不重入），异常仍写 `share_check` 事件。
- **`deep_delete_queue` 的 `skipped` 只留 2 天**。现场 11-04 的一次深度删除爆发留下 **12,129 行 / 6.8MB**，按 7 天规则要等满一周才清。`done`/`queued` 的保留期不变。
- 顺带查清了两件"看起来能省其实不能"的事，避免误操作：
  - 库 93MB 里 **freelist 只有 0.7MB**，所以 **VACUUM 毫无收益**，没做；
  - `organize_items` 占 55.5MB，其中 `plan` 23.4MB + `original` 一大块，都是业务历史（重整理/升级要用），**没有安全的瘦身空间**，没动。
- 验证：`test_share_schedule`（新增"后台执行且不阻塞调用方"，并把两条既有用例改成让出一拍）与 `test_iteration2`（新增"skipped 超 2 天清理"）等 70 项通过。


## 0.9.55 — 2026-11-09（P0：先止住事件循环阻塞、让阻塞有栈可查、别让噪音埋掉告警）

架构/链路审计的第一批修复，只动"纯收益、不改业务逻辑"的三处。

- **补两枚索引（实测 2351× / 200×）**。`organize_items` 的唯一键是 `(job_id,file_id)`，所以 `WHERE file_id=?` 用不上索引——生产库副本实测 **25.36ms/次**，补 `organize_items(file_id)` 后 0.01ms；`organize_jobs` 原来一个索引都没有，`WHERE state=?` 2.23ms → 0.01ms。而分享产线正在 per-file 循环里查 `file_id`，缺这一个索引就能把事件循环堵成秒级（现场「事件循环被阻塞」65 次，中位 1.28 秒、最大 18.2 秒）。
- **卡顿栈落盘**。看门狗以前只把线程栈写 stderr，也就是只进 `docker logs`——容器每次升级重建就清空，所以 65 次阻塞一次栈都没留住。现在同时写 `<data>/logs/stall-YYYYMMDD.log`，由 `Store._prune_logs` 一起轮转 7 天。
- **事件表按 kind 封顶**。`events` 打满 2 万上限时，organize 7343 + progress 3536 + deep_delete 1949 稳定占掉一半名额，真正的告警被全局 FIFO 冲走（0.9.47 前的媒体信息空窗就是这么被埋的）。现在 `EVENT_KIND_CAPS` 单独封顶（progress 800 / organize 4000 / deep_delete 1200），**明细不丢**——`Store.event()` 本来就同时写 /data/logs 的按天日志文件。
- 未改：`deep_delete_queue` 现存 12129 行是 11-04 的一次爆发，`queue_days=7` 的规则会在 11-11 前自然清掉，不需要额外动作。
- 验证：`test_iteration1`（新增"热查询必须走索引"含 EXPLAIN QUERY PLAN 断言）/ `test_iteration2`（新增"高频事件按 kind 封顶、告警不被挤掉"）/ `test_ops0821`（新增"卡顿栈落盘且追加"）+ 其余相关共 128 项通过。


## 0.9.54 — 2026-11-08（账本契约：身份写在文件里，登记表退位成索引）

这版是"账本是不是依赖债"的正式回答。结论：**账本整体去不掉，但能从 5 张登记表砍到 3 张，并且让以后加功能不必再兼容账本**——方法是把"查表断言归属"换成"从文件推导身份"。

- **契约文档** `docs/LEDGER-CONTRACT.md`：两条契约——①身份写在文件里；②数据库只存不可推导的状态（凭据/令牌/元数据/发布意图），路径只是缓存。附新增功能 checklist 与反模式清单。
- **`strimkeep/identity.py`**：`parse_url` / `parse_file` / `matches`，把三种地址形态（`/play/<pid>`、`/play115/<pickcode>/<fid>/<sha1>/<size>/<name>`、`/play115/share/<sid>/`）统一成身份。现场 1078 条 STRM 全部可解析。
- **守护测试** `tests/test_ledger_contract.py`：所有生成入口（`play115_url` / `share_play_url`）的产物必须可解析；且**改名不改变归属**——这就是"改名不必走账本"的依据。
- **退场两张纯派生表**（旧库启动时一次性 `DROP`，`meta.schema_pruned_0954` 记标记）：
  - `output_stats`：只是 STRM 的 size/mtime 摘要缓存，STRM 只有几百字节，缓存省不下什么却要求每次搬文件同步；`sync.inspect_output` 现在直接算摘要。
  - `media_info_outputs`：sidecar 自带 `version` + `playback_id`，`write_sidecar`/`purge_orphan_sidecars` 改成**只看内容**判归属。代价是"内容里没有自家字段的 sidecar"不再被视为我们的（`{}` 那种会被保留），这与契约一致：判不出来就不动。
- **明确降级**：`owned_outputs` 与各表的 `relative_path`/`strm_path` 记为"可重建索引/路径缓存"，漂移由巡检追平，不再是每个写入方必须维护的第二真相。
- 验证：`test_native_media_info` / `test_iteration1` / `test_iteration2` / `test_metadata_files` / `test_notice_sidecars` / `test_ops0837` / `test_sync_sweep` 共 79 项 + 契约与体检 8 项通过。


## 0.9.53 — 2026-11-08（孤儿侧车清理：规则、脚本、以及一次真实清理）

0.9.52 的基线里 `orphan_sidecar = 128`，这版把它做成可操作的：同一套判定背后多了一个清单函数和一个默认 dry-run 的脚本，然后按这份清单真清了一次。

- `ledger_audit.orphans()`：与 `audit()` **共用**同一套判定（`_scan` / `orphan_stem`），只多返回清单与三个字段 `owned / age_days / deletable`。这样不会出现"指标说 128、脚本删 130"。
- `scripts/purge_orphan_sidecars.py`：默认只出清单，`--apply` 才删；三道保险——只删**本项目生成**的侧车、只删超过保护窗口的、逐条打印。
- 判据踩过一次坑：一开始用 `<tmdbid>` 判"我们的 nfo"，结果 75 个 nfo 全被判"归属不明"，可删 0 条。翻真实文件才发现本项目写的是 `<uniqueid type="tmdb" default="true">`。改成认 `<uniqueid` / `<tmdbid>` / `playback_id` / `"version"` 之后才正常。
- 真实现场清理（窗口用 3 天；脚本默认 7 天，3 天是这次显式传的，仍能挡住正在跑的整理任务）：**删除 117 个文件 = 70 个 .nfo + 47 个 .ass，清理 7 个空目录，0 失败**；剩 11 条在窗口内（6 个 .sup + 5 个 nfo），留给下次。顺手清掉 `meta.ledger_audit_last` 让体检立刻重算，`orphan_sidecar` 从 128 降到 11。
- 验证：`test_ledger_audit` 4 项（新增"保护窗口与归属判定"用例，并断言 `orphans()` 条数与 `audit()` 一致）+ `test_ops0820` 共 14 项通过。


## 0.9.52 — 2026-11-08（体检排期改成按「上次体检时间」，并给出真实基线）

0.9.51 的修法没救回现场：0.9.50 已经把 `ledger_audit_due` 写进 settings（那版只排期不跑），0.9.51 启动时看到"还没到点"就继续跳过，`/metrics` 仍然空等 6 小时。这次把排期规则换掉——**不再存 due 键，直接看 `ledger_audit_last.at`**：上次体检超过 6 小时就跑，所以首次启动、升级、宕机恢复都自然补跑；失败只等 10 分钟再试，不刷日志。顺手去掉了那个多余的 `settings.ledger_audit_due`。

首次体检的真实基线（现场 1158 条受管 STRM）：

- `stale_active = 1`：就那一条 `organized_files` 的历史行，文件早就不在了；
- `unregistered = 0`：两个库根下 1078 个 STRM **全部**有登记行认领；
- `orphan_sidecar = 128`：75 个 `.nfo` + 47 个 `.ass` + 6 个 `.sup`，全是换源/洗版留下的同名不同 stem 残留（「伟大的长征」49 条、「贝茨旅馆」47 条、「走马上任」14 条……），确认不是误报——目录级侧车与字幕语言后缀都已排除。

也就是说：**账本本身是健康的，脏的是换源残留**。这版只把这个事实变成数字，没有清理任何一个文件。验证：`test_ledger_audit` / `test_ops0820` / `test_ops0821` / `test_iteration1` 共 36 项通过。


## 0.9.51 — 2026-11-08（体检基线别等 6 小时）

0.9.50 上线后自查发现一个瑕疵：`ledger_audit_tick` 在没有排期时只是"排期 + return"，于是**首次启动 / 刚升级要空等 6 小时**才出第一个数字——`/metrics` 在升级后是空的，正好把"想立刻确认基线"的需求挡在门外。改成「没有排期就立刻跑一次，再排下一次」，并顺手去掉了那句没信息量的"已排期"事件。验证：`test_ledger_audit` / `test_ops0820` / `test_iteration1` 共 28 项通过。


## 0.9.50 — 2026-11-08（账本体检：让「索引与文件不同步」自己说话）

这轮没有改任何业务路径，只加了一个**只读**体检。起因是复盘时有人问「账本是不是成了依赖债」——查完发现真实情况比想象的健康，但**没有任何指标能证明这件事**，而 0.9.1~0.9.47 那 70 条媒体信息空窗恰恰就是"索引静默失真、系统不报错"的典型。

- 新增 `backend/strimkeep/ledger_audit.py`：只用文件自带的身份反向核对登记表，返回三个数——
  `stale_active`（登记为有效但文件已不在：playbacks.revoked=0 / share_strms.revoked=0 / output_registry.active=1）、
  `unregistered`（库根下存在却没有登记行认领的 STRM）、
  `orphan_sidecar`（只剩 nfo/字幕/media-info.json、没有对应 STRM 或视频的孤儿）。
  **不判断谁对谁错、不删文件、不修表**：一条悬空登记可能是"文件被外部删了"（该收敛），也可能是"文件搬家了"（该改路径），只有结合文件里的身份才能区分，那是后续 reconcile 的事。
- `organizer.ledger_audit_tick()` 每 6 小时在工作线程里跑一次，结果写 `meta.ledger_audit_last`，有偏差才写一条事件；`/metrics` 暴露 `strimkeep_ledger_drift{kind=...}` 与 `strimkeep_ledger_audit_timestamp`。
- 顺带修正一个排查结论：一度以为 `output_registry` 有 156 行漂移（13.5%），实际那把 `active=0` 的**历史行**也算进去了；只看 `active=1` 是 997 行里 996 行健康、1 行真没了。**没有需要批量清理的脏数据**，所以这版没有动任何数据。
- 库根白名单同时接受「影视媒体库 / 媒体库 / 分享影视库」：现场叫「媒体库」，`fsops.LIBRARY_NAMES` 里的历史名是「影视媒体库」，只写一个会漏统计。
- 验证：`test_ledger_audit` 3 项（含"待整理目录不算未登记""目录级侧车不算孤儿""字幕语言后缀算有主"）+ `test_ops0822` / 核心流水线共 47 项通过。


## 0.9.49 — 2026-11-08（把「媒体信息缺口」变成看得见的东西）

0.9.48 修完了 Emby 那一半，但复盘时最贵的一条不是探测逻辑，而是**没人知道**：70 条 STRM 在 Emby 里空了 24 小时，期间所有报表、通知、页面都说「媒体信息已保存」。这个版本只做三件事，都是围绕「下次再出事，能第一时间看见」。

- **`/metrics` 加 `strimkeep_media_info_emby{state}`**（pending / gave_up / confirmed 三个值）。以前只有 organize_jobs、deep_delete_queue 这类有状态的表进指标，媒体信息一个都没有；现在哪一半缺、缺多少，Prometheus 直接能看到，配一条 `strimkeep_media_info_emby{state="pending"} > 20` 的告警就够。
- **`report()` 每 6 小时汇总一次缺口**：`Emby 端仍有 N 条待补齐、M 条已停止追踪（受管 STRM 共 K 条）`。有缺口才写，没缺口就安静，不需要额外配置；写进日志中心，Telegram 已有的通知通道照旧能收到。
- **同名条目在另一个库已有视频流时不再白追**：实测那 7 条「活链但 Emby 永远 0 流」的根因不是链接，而是 `分享影视库` 和 `媒体库` 各存了一份同内容 STRM（如 2911 ↔ 12762），Emby 把两条当同一部片——对它 `POST /Items/{id}/Refresh` 只花 **2 毫秒**就 Complete，压根不跑 ffprobe（日志实证）。补扫现在会用一次全库条目表认出这种孪生关系，直接标 `confirmed` 并留一条事件，不再让它白跑 6 次退避。
- **`update.sh` 不再因缺文件整批中止**：从开发仓库直接跑 `update.sh` 时没有发布包才有的 `SHA256SUMS`，0.9.48 上线就栽在这一行（且发生在覆盖 compose 之前，所以只是没更新成）。现在缺文件跳过并提示，开发仓库也能一条命令走完。
- 验证：`test_native_media_info` 22 项、`test_media_sweep` 6 项、`test_ops0822` 12 项全绿（`test_ops0822` 新增两条断言，空库也必须输出 0 值，避免「没数据等于没告警」）。


## 0.9.48 — 2026-11-08（媒体信息不再只做一半：Emby 端重新有人追）

0.9.1 把媒体信息改成「本地优先」：整理阶段已经 ffprobe 过，就写 sidecar 直接收工，不再等 Emby 去拉 115 直链。收益是真的——4K 源不再撑满 8 次、约 25 分钟的重试窗口，也不再刷出上百条失败。但这一刀切得太狠，把 Emby 那一半也一起砍了。

现象：Emby 里 11-07 之后新入库的条目，媒体信息整块空白（分辨率、码率、音轨、字幕都不显示）。库里 1077 条 STRM 条目，`MediaStreams` 为 0 的 70 条，全部落在 11-07 10:15（0.9.1 部署）之后；同样 70 条正好等于 `source='ffprobe'` 的 sidecar 集合——本地落过盘的，Emby 就一条流都没有。

根因有三层，缺一层都不会成灾：

- **`tick()` 短路了唯一的触发点**。`PlaybackInfo` 是全项目唯一能让 Emby 去解析 STRM 背后 115 直链的调用（grep 全仓只此一处）。0.9.1 把「本地探测有值」放在它前面并直接 `return`，Emby 从此没被请求过探测。`POST /Library/Media/Updated` 只会让它重读元数据，不会替我们探流。
- **补扫把最后一点补救也关掉了**。0.9.16 之前「sidecar 在、Emby 没有视频流」还会每 5 分钟重新入队（顺带给 Emby 一次机会）；改成「有 done 行就跳过」之后，连这点机会也没了。
- **存量补齐也只看本地文件**。`sync.backfill_sidecars()` 的 `missing_media` 判据是「本地没有 sidecar」，于是也不会再标记 `output_changed` 去通知 Emby。

修法是把两个关注点拆开记账，而不是把本地优先退回去：

- `native_media_info_queue` 新增 `emby_state` / `emby_attempts` / `emby_next_due`。`state` 只表示「本项目自己的媒体信息」，本地 ffprobe 有值仍是立刻 `done`；`emby_state` 表示「Emby 端确认有视频流了没」。
- 新增 `probe_emby()`：每轮处理一条 `emby_state='pending'` 的行，先回读 `/Items`（Emby 可能自己已经探好），没有就踢一次 `PlaybackInfo` 再回读。拿到视频流 → 用 Emby 的值覆盖 sidecar 并置 `confirmed`；拿不到 → 按 10 分钟起翻倍、封顶 1 小时退避，6 次后 `gave_up`，**不改 `state`**，所以本地媒体信息永远不受影响。
- `_sweep_files()` 补扫改成：Emby 已有视频流 → 顺手把存量行标 `confirmed`（一次查完整张表的 `relative → emby_state`，不再对全库逐个查库/UPDATE）；Emby 没有视频流 → 交给 `probe_emby()` 的记账继续追，**不新建队列行、也不重置它的退避**（0.9.16 之前正是重新入队才堆出 234 组重复；退避被补扫每 5 分钟重置一次也等于没有退避）。
- 存量自愈：升级后 1172 条 done 行默认 `pending`，第一次补扫就把 Emby 已有流的那 1100 条标成 `confirmed`，剩下的 70 条进入 `probe_emby` 的重推队列，无需手工修数据。
- 验证：`test_native_media_info` 21 项 + `test_media_sweep` 6 项全绿。新增三组用例——本地落盘后 `emby_state` 必须仍是 `pending` 且 `probe_emby()` 真踢了 `PlaybackInfo`；退避 6 次后 `gave_up` 而 `state` 仍是 `done`；补扫不建行、不重置退避，Emby 后来探出来能自愈成 `confirmed`。


## 0.9.47 — 2026-11-08（把「没做成」说成「做完了」的五处一起修掉）

0.9.45 发布时门禁挂在 `test_iteration1.py::test_offline_state_failure_beats_percent_complete` 上，当时判定为既有失败就带 `SKIP_VERIFY` 上线了。这轮把门禁真正修绿：从 10 个失败降到 0，全量 294 项通过。

### 1. 离线三态：`'未完成'` 里含 `'完成'`

```python
if any(word in status for word in ('完成', '成功')): return 'done'   # ← '未完成' 命中
```

中文状态是子串匹配，`'未完成'`、`'未成功'` 都会先撞上肯定词。离线任务还在下载（40%）却被判 `done`，`check()` 直接跳过「下载进行中」分支去认领产物，拿不完整的东西往下走。

现在判定顺序改成：失败 → 明确的中间态（未完成/未成功/未开始/等待/排队/暂停/进行中/下载中）→ 百分比 100 → 状态文字完成 → **有进度就算下载中**。

最后一条是新加的：状态文字认不出来但已经有 35% 进度时，以前报 `unknown`，`check()` 每轮都退回「稍后重试」；进度本身就是还在下载的证据。

### 2. 洗版回收未确认，任务却报「已完成」

`organizer.failed_item` 的 `review` 分支（"本文件后续步骤未完成"）直接 `update_item(state='review')`，**不写 plan**。任务收尾的 `blocking` 只认 `plan['failure']` 和 `warnings`，于是看到 `counts['review']=1` 却当正常结果 —— 因为 0.9.x 把「未识别」改成了正常结果口径，洗版失败被一起吞了。

后台发布队列那层（`后台 STRM 生成未完成`）同样只置 review、不写 plan，`RECYCLE_REVIEW_REQUIRED` 冒上来就被这样吞掉。

改法：给"后续步骤未完成"一个独立强阻塞键 `blocks_job`，收尾时走 `_blocks_job(job_id)` 直接查库（这条路径的条目不在收尾的 `plans` 列表里）。和「识别不了、放进未识别目录」的正常结果彻底分开。

现场后果：洗版回收没核实，新文件已入库、旧文件还在盘上，任务显示「已完成」—— 用户会以为洗好了。

### 3. 重跑成功后，上次的失败标记还在

`plan` 是合并写的，条目重跑成功、state 变 `done` 之后，`failure` / `blocks_job` 仍留在 plan 里，`any(p.get('failure') for p in plans)` 照样成立 → 任务永远挂在「部分未完成」。

改成只让**还没结束**的条目阻塞：

```sql
SELECT plan FROM organize_items WHERE job_id=? AND state NOT IN ('done','skipped','recycled')
```

现场例子就是「本地导出失败 → 重跑成功」：`test_local_export_failure_recovery_never_repeats_cloud_move`。

### 4. 测试写法过时：`localtime` 被 patch 成 0 参数

`store._append_log` 调的是 `time.localtime(now)`，测试却把 `time.localtime` 换成 `lambda:`（无参）→ `TypeError`。改成 `lambda *args:`。

### 5. 未识别的口径统一：配没配未识别目录不该有两种说法

同一件事（`无法识别.mkv`）在两种配置下结果不一样：

- 没配未识别目录 → 条目原位保留，`state='skipped'` → 任务「已完成」
- 配了未识别目录 → 条目移进去，`state='pending'` → 任务「部分未完成」

`test_flow058.py` 的两个参数化 case 期望一致（都是 partial），其中 `[]` 一直是红的。

现在统一成「还有活要干」：原位保留的分支也打 `blocks_job`，`_blocks_job` 覆盖 `state IN ('review','skipped')`。

更关键的是，这样**不用去猜「识别不出」和「probe 失败」的区别**：两者都走 `quarantine`、都是 `plan.pending`，靠 `plan` 字段根本分不开。统一算 partial 更保守 —— 宁可多报未完成，不可漏报；人工处理完之后由 `settle_jobs` 结项（"待办条目已全部结束，任务自动结项"），不会一直挂着。

试过反过来（`plan.pending` 的不阻塞）会打破 `test_probe_failure_quarantines_original_and_associated_subtitle` 和 `test_existing_episode_is_never_replaced` —— 那两个是真失败，必须报 partial。

### 结果

| 范围 | 之前 | 现在 |
|---|---|---|
| 全量 `test_in_container.sh` | 10 failed / 284 passed | **294 passed** |
| `test_upgrade.py` | 7 failed | 40 passed |
| `test_organize.py` | 1 failed | 30 passed |
| `test_recycle_readback.py` | 1 failed | 通过（同根因顺带修好） |
| `test_iteration1.py` | 1 failed | 15 passed |
| `test_cover_service.py` | 1 failed | 2 passed |
| `test_flow058.py` | 1 failed | 16 passed |
| `test_tv_year_policy.py` | 1 failed | 11 passed |

## 0.9.46 — 2026-11-08（删了重加不再重复；频道可以就地改名）

### 问题：去重账本按"频道名字"记账

```sql
channel_posts(name, message_id, ... PRIMARY KEY(name, message_id));
channel_state(name PRIMARY KEY, last_message_id, ...);
```

账本的主键是用户输入的字符串。频道从公开转私密后用户名被释放，用户只能删掉旧条目、
从会话列表重加数字 ID —— 名字一换，账本对不上，新条目当"首次接入"重扫一页。

各落盘方式的实际后果：

| 落盘方式 | 会重复吗 | 原因 |
|---|---|---|
| 转存 transfer | 不会 | `channel_shares.share_code` 是全局主键，同一分享码被 `INSERT OR IGNORE` 拦住 |
| 磁力 / 电驴 | **会** | `_offline()` 只靠 `channel_posts` 认领，没有全局账本 → 重新提交到 115 离线 |
| STRM | 重写同名文件 | 文件名由资源名决定，覆盖而非新增，但会重复调接口 |

预览页 `t.me/s/` 拿不到频道数字 ID（只有 `data-post="telegram/123"`），所以没法用
channel_id 做全局身份 —— 只有会话模式有 `entity.id`。改用两种模式都能拿到的标题。

### `_adopt_orphan`：抓到标题后接上旧条目的跟进位置

`refresh()` 拿到标题后，找 `channel_state` 里"配置里已经没有、标题相同、抓过东西"的旧行：

- 搬 `last_message_id` 和 `posts`，接 `channel_posts` / `channel_backfill` 的账本
- 搬完删掉旧行，免得下一轮再认一次
- 只在新闻条目还没抓过东西时触发；旧行还在配置里就不抢（那是两份配置，交给判重）
- 事件日志留一句"已接上原来的跟进位置（消息 x）"

### `POST /api/v1/channels/rename`：就地改名

```json
{"old": "ltxjasna", "new": "-1004419698318"}
```

- 只改 `name`，落点、正则、分流规则全部保留
- `channel_state` / `channel_backfill` / `channel_posts` / `channel_share_attempts` /
  `channel_shares` 的 `name` 一起 UPDATE，改名后不重扫旧帖
- 新名字是数字 ID 时自动把 `source` 改成 `session`（不改的话下次保存会被校验拦下）
- 重名 409、未知频道 404、非法名字 422

界面上频道卡片名字旁多了「改名」按钮。

### 顺带修掉：离线状态判定被中文子串带偏

`_state()` 原来先判「完成 / 成功」，再判「未完成 / 未开始」。中文是子串匹配：
`未完成` 里含 `完成`、`未成功` 里含 `成功` —— 还在下载的任务被判成已完成，`check()`
直接跳到产物认领，拿不完整的东西往下走。

顺序改成：失败 → 明确的中间态（未完成 / 未开始 / 等待 / 排队 / 暂停 / 进行中 / 下载中）
→ 百分比 100 → 状态文字完成 → 有进度算下载中 → `unknown`。

`tests/test_small_fixes.py::test_offline_done_accepts_percent_forms_and_rejects_midway_states`
从基线失败转为通过。

### 测试

- `tests/test_channel_rename.py`（4 项）：保规则搬游标、重名 409、未知 404、非法名字 422
- `tests/test_channel_monitor.py` 加 2 项：删了重加接上游标和账本；旧条目还在配置里时不抢

定向 50 项全部通过。

## 0.9.45 — 2026-11-07（会话能读群组和机器人；机器人按钮里的链接也要收）

用户会话早就能读私密频道，但三处没打通，等于"能力有、用不上"。

### 1. 机器人把链接放在按钮里，正文是空的

`fetch()` 只遍历 `message.entities`（正文里的裸链接和文本超链接），**完全没碰 `reply_markup`**。资源机器人几乎都是 `[4K] [1080p]` 这种按钮形态，正文为空 → 被 `if not text and not links: continue` 整条跳过 → 加了机器人什么都不来。

补上按钮 URL，并按出现顺序去重（正文和按钮常指向同一个链接）：

```python
markup = getattr(message, 'reply_markup', None)
for markup_row in (getattr(markup, 'rows', None) or []):
    for button in (getattr(markup_row, 'buttons', None) or []):
        url = getattr(button, 'url', None)
        if url:
            links.append(url)
links = list(dict.fromkeys(links))
```

### 2. 会话列表只列频道

`dialogs()` 硬过滤 `is_channel`，群组（supergroup）和机器人根本不在列表里，用户只能手填 `-100…` ID。

现在列三类，带 `kind` 字段：`channel` / `group` / `bot`，按这个顺序排。**普通联系人仍然不列**（不在监控范围，列出来只是噪音）。

### 3. 会话源读不到消息时报「没有公开预览」

这句文案是 preview 和 session 共用的，加群组或机器人的人看到会完全懵。按 source 分开：

```
session  → 会话里没有可读消息：机器人可能把链接放在按钮里，或你没有该群组的读取权限
preview  → 没有公开预览：频道不存在、未公开，或已被限制
```

### 4. 判重只认配置里还在的频道

`channel_state` 里的行不会随配置删除而消失（**故意的**：`last_message_id` 是游标，误删恢复后要能续上）。但它们会参与判重 —— 一条已经从配置里删掉的旧行如果 title 相同，会被当成正本，把用户新加的那条标成重复。

现在判重先过一遍配置里的频道名，不在配置里的行跳过判重（数据留着）。

### 测试

- `tests/test_telegram_session_read.py`（4 项）：按钮链接、正文与按钮重复去重、只有按钮没文字的消息、列表只列频道/群组/机器人。
- `tests/test_channel_duplicate.py`（7 项）：新增「从配置里删掉的行不当正本」。

## 0.9.44 — 2026-11-07（判重选错正本：会把唯一能用的那条跳过）

0.9.43 的判重按 `added` 排序，先加的当正本。真实场景里这正好是反的：

```
ltxjasna          added 更早 · 公开期加的 · 转私密后一直 HTTP 302 · state=error
-1004419698318    added 更晚 · 会话方式加的 · 唯一能正常抓的
```

两条 title 相同（旧的 title 是公开期抓到的，302 分支不写 title，所以留住了），于是判重生效 —— 但把 `ltxjasna` 当正本，**跳过唯一能用的数字 ID 条目**，频道直接停摆。

### 修法

正本按「能正常抓取」优先，其次才看加入时间：

```sql
ORDER BY CASE WHEN state='error' THEN 1 ELSE 0 END, added
```

现在 error 的那条被标 `duplicate_of` 并跳过（顺带省掉每轮一次必然 302 的请求），能用的那条当正本。

### 顺带把成因写进注释

频道原本公开（有用户名），改成私密后用户名失效、`t.me/s/<用户名>` 变成 302，只剩数字 ID。用户只能从「用户会话」列表重新加一条，而 `dialogs()` 对私密频道只给 `id`，于是配置里出现两条指向同一个频道。

### 测试

`tests/test_channel_duplicate.py::test_erroring_channel_never_becomes_the_owner`：error 条目绝不能当正本。

## 0.9.43 — 2026-11-07（频道判重：同一个频道的用户名 / 数字 ID 只抓一个）

现场：`ltxjasna`（用户名）和 `-1004419698318`（数字 ID）其实是同一个频道，两条都在配置里，每轮抓两次。`ltxjasna` 已经删了，但添加时没有任何拦截，同一个频道换个写法加进来就会再来一次。

按抓到的 `title` 判重：先加的保留，后加的标 `channel_state.duplicate_of`，抓取循环跳过它。

### 范围要说清楚

- **首轮两条都会抓一次**：`title` 只有抓完才知道，没法在抓之前认出来。第二轮起才只抓正本。
- **不会产生重复产物**：`channel_shares.share_code` 是主键，`INSERT OR IGNORE`，同一个分享码第二次进来 `rowcount=0`，不算新条目，也不发通知。
- 所以这个功能省的是**每轮多打一次 t.me 游客页请求** —— 游客页有频率限制（`test_requests_to_t_me_are_spaced_out` 就是为它写的），多一倍请求量是实打实的风险。

### 两个细节

- **通知只发一次**：判重每轮都会重算，但通知只在判定变化时发 —— 每轮都发会刷屏，和 0.9.15 修掉的 368 条通知重放是同一个坑。
- **正本被禁用时由另一条接管**：正本不在启用列表里就不跳过，否则这个频道谁都不抓了。

### 两个候选功能被否掉

同一轮还做了「外部 STRM 忽略 / 纳入」和「只清台账悬空」，对着现场数据一验，都不该留：

| 功能 | 现场 | 结论 |
| --- | --- | --- |
| 外部 STRM 忽略 | 外部 STRM **0** 个 | 没有对象；而且忽略列表会把真问题永久藏起来 |
| 只清台账悬空 | 悬空 **0** 条 | 「清理残次品」本来就包含撤销悬空登记，等于一个按钮做子集 |

两个功能的代码已撤（`phase5_api.py` / `Sync.vue` 回到 0.9.42 状态），不进这一版。

### 测试

- `tests/test_channel_duplicate.py`（5 项）：同名标重复、不同名不动、无 title 不动、**通知只发一次**、改名后标记撤销。
- `tests/test_channel_monitor.py::test_duplicate_channel_is_skipped_after_titles_are_known`：首轮各抓一次 → 第二轮只抓正本（请求只打到 chanA）→ 正本禁用后由重复那条接管。

## 0.9.42 — 2026-11-07（修台账悬空：未识别 STRM 移走后要撤销登记）

全链路深度排查（孤儿数据 / 时间异常 / 路径长度 / 空目录 / DB 完整性 / 台账一致性 / sidecar 身份 / 洗版残留）发现的唯一硬伤：

```
磁盘 1063 | playbacks 996 | share_strms 71 | 合计 1067
台账有磁盘无: 4   ← 分享STRM未识别/05、06、27、28.mkv.strm
```

### 根因

`_to_unident()` 把未识别的 STRM 移进 `分享STRM未识别` 时，只更新了 `share_strms.strm_path`，**没有撤销登记**（`revoked` 仍是 0）。于是台账里留下一条「活跃」记录指向未识别目录；用户在未识别目录删掉文件后，这条记录就永远悬空。

### 修法

```python
store.db.execute('UPDATE share_strms SET strm_path=?, revoked=1 WHERE id=?', ...)
```

移进未识别目录即视为「入口失效」：撤销登记，之后用户改名移回待整理会生成新的登记。

### 数据清理

4 条悬空记录 → `revoked=1`。现在 `share_strms` = 活跃 **67** + 撤销 18，**台账与磁盘完全一致**。

### 其余检查全部通过

| 检查 | 结果 |
| --- | --- |
| 孤儿 items / organized_files | 0 |
| 重复 relative_path / playback_id / strm_path | 0 |
| 未来时间戳 / 负 size / submitted_at=0 | 0 |
| 路径 >240 字节 / 文件名 >200 字节 / 控制字符 | 0 |
| 临时文件（.tmp/.part/!qB） | 0 |
| `PRAGMA integrity_check` | ok |
| sidecar 身份一致性 | 核对 1063，不一致 **0** |
| archived 残留 / quarantine 未终态 | 0 / 0 |
| emby_deliveries 未完成 | 0（archived 12 + verified 105） |

验证：`test_phase2_share_pipeline` + `test_review_not_blocking` + `test_close_recovered` + `test_app` 共 37 项通过。

## 0.9.41 — 2026-11-07（分享库对齐网盘库：未识别不算任务失败）

0.9.39 只改了网盘库的任务结束判定，**分享库有自己的判定**，没跟着改：

```
engine.update_job(job_id, state='partial' if unident_count else 'completed', ...)
```

只要这一批有未识别的 STRM，分享库任务就变 `partial`，等每小时 `settle_jobs()` 才结项——中间一直显示「部分未完成」。

### 三处对齐

| 位置 | 原来 | 现在 |
| --- | --- | --- |
| 分享库任务结束判定 | `partial if unident_count` | **`completed`**（通知失败有独立 `notify_state`，不拖累任务） |
| 未识别 STRM 条目（移动成功） | `review · 未识别，已转待确认目录` | **`skipped`**（终态，不进「待确认」计数） |
| 未识别 STRM 条目（移动失败） | `review · 具体原因` | **`skipped`**（文件原位保留，下次扫描会重试） |

`unident_count` 是循环里累加的计数，不受条目状态影响，所以 message 里的「未识别 N」照常准确。

### 两个库现在同口径

| 你的操作 | 网盘库 | 分享库 |
| --- | --- | --- |
| 未识别文件放着不管 | 任务 completed，不标红 | 任务 completed，不标红 |
| 去未识别目录改名 | 移回待整理后重新扫描 | STRM 改名后移回 `分享STRM待整理` 重新扫描 |
| 去未识别目录删除 | 对账发现文件不在 → 账本闭环 | 条目已 `skipped`，不影响任何地方 |

### 顺带修的测试

`test_phase2_share_pipeline` 两项跟着新口径更新：产物 ID 取 `delete_file_id`；响应丢失场景改用另一个链接（同一链接现在会被「已在离线任务中」直接拒绝）。

验证：`test_phase2_share_pipeline` + `test_review_not_blocking` + `test_close_recovered` + `test_app` 共 37 项通过。

## 0.9.40 — 2026-11-07（任务一结束就对账，删了未识别文件账本立刻闭环）

用户问「那我去未识别删除了呢」。原来 `reconcile_ledger()` 只处理 `updated` 超过 **24 小时**的任务，刚结束的任务要等一天才被对账。

改成：**任务已 `completed`/`cancelled` 的立刻对账**，还在跑的任务保持 24 小时窗口（避免和正在处理的文件抢）。

```sql
WHERE i.state IN ('review','pending')
  AND (j.state IN ('completed','cancelled') OR j.updated < now-86400)
```

### 效果

| 你做的事 | 系统反应 |
| --- | --- |
| 在未识别目录删掉文件 | 每小时对账时 `cloud.file()` 查不到 → 账本标「源文件已不在网盘」并闭环 |
| 把文件移回待整理 | 对账发现 `parent_id` 是扫描根 → 旧账本闭环，下次扫描重新入队 |
| 什么都不做 | 任务已是 `completed`，不标红；未识别条目留着自己决定 |

任务状态本来就已是完成，对账只是把「未识别 N」这条尾巴收掉。

验证：`test_review_not_blocking`（4 项）+ `test_reconcile_ledger` + `test_close_recovered` 共 **16 项通过**。

## 0.9.39 — 2026-11-07（未识别不再让任务变「部分未完成」）

用户的原话：「一出错就要人工干预，干预完了成功了还要报错，这对吗」。查下来是任务状态判定把**未识别（review）当成了失败**，导致：

| 现象 | 原因 |
| --- | --- |
| 任务永远显示「部分未完成」 | 结束判定里 `counts.get('review')` 也算失败 |
| 手动点「结项」后还提示未完成 | 结项只改任务状态，条目仍是 `review`，任务中心的「未识别 N 个」还挂着 |
| 后续批次补齐了旧任务也不闭环 | `close_superseded_batches` 和 `settle_jobs` 都把 `review` 当未完成 |

### 核心口径

**未识别是正常结果，不是失败。** 文件已经放进未识别目录，等人工决定要不要处理；它不该让整个任务挂着「部分未完成」。

### 四处改动

| 位置 | 改动 |
| --- | --- |
| 任务结束判定 | `blocking = pending / archived / warnings / 通知失败`，`review` 不再参与 |
| 人工结项（任务中心按钮） | 顺带把 `review`/`pending` 条目落成 `skipped · 已人工结项`，计数归零 |
| `close_superseded_batches()` | 旧批次闭环时 `review` 不阻塞 |
| `settle_jobs()` | 自动结项时 `review` 不阻塞 |

### 效果

- 有文件识别不了 → 任务正常 `completed`，message 里写明「未识别 N」，不再标红
- 手动结项 → 条目一起落终态，任务中心立刻干净
- 后续批次把旧批次的文件补齐 → 旧任务自动闭环（不再需要人工点结项）

验证：新增 `tests/test_review_not_blocking.py` 2 项；与 `test_close_recovered` + `test_console060` 合计 **23 项通过**。

## 0.9.38 — 2026-11-07（旧批次补齐后不再显示「待处理 N 个视频」）

用户说「兜底 7 集完成了，为啥还提示部分文件未完成」。查下来 job 647 已经是 `completed`、7 条都是 `已由后续批次补齐`、非终态 0——状态是对的，但 message 还留着旧计数：

```
已整理 1 个视频；待处理 7 个视频。已由任务 651 全部补齐。
                    ^^^^^^^^^^^^ 这句是当时的计数，看着像还没做完
```

`close_superseded_batches()` 原来只是把「已由任务 X 全部补齐」追加在后面，前半段的旧计数原样保留。

### 改动

补齐时用 `re.sub(r'[；，]?待处理 \d+ 个视频', '', message)` 把旧计数去掉，再追加结项说明。

数据清理：**2 个任务**的 message 已修正——

| 任务 | 修正后 |
| --- | --- |
| job 647 | `已整理 1 个视频。已由任务 651 全部补齐。` |
| job 263 | `已由任务 265 全部补齐。` |

另外确认：现在显示「部分未完成」的是 **job 638**（2 条 `review`，`27.mkv` / `28.mkv` 文件名只有集号），那是真未识别，不是文案问题。

验证：`test_close_recovered` + `test_multilang_identify` 共 13 项通过。

## 0.9.37 — 2026-11-07（产物认领改成按文件 ID 直查：115 的保存路径字段不可信）

用户说「47 分那个离线我查了，落盘成功了」。顺着查，找到真正的坑：

```json
// 115 离线任务 #203（98T@SSNI-953_LTX.mp4）
{"file_id": "3510832216693277696",        // 声称保存到 porn 目录
 "delete_file_id": "3556951310982973024",
 "wp_path_id": "3510832216693277696",
 "status_text": "下载成功", "display_status": "finished", "move": 1}

// 用产物 ID 直接查文件
{"id": "3556951310982973024", "name": "SSNI-953.mp4",
 "parent_id": "3503430221283985095",   // ← 实际落在别的目录！
 "size": 10038557652}
```

**115 报的 `file_id` / `wp_path_id` 和真实落盘目录不一致**。原来的认领逻辑是「列出 `target_cid` 目录，找新增项里 ID 匹配的」——文件不在那个目录，自然一个都找不到，于是全被判成「产物不在目标目录」。

### 修法

| 位置 | 原来 | 现在 |
| --- | --- | --- |
| 产物认领 | 列 `target_cid` 目录，和 `before_ids` 做差集再比 ID | **直接 `cloud.file(产物 ID)` 查文件本身**，文件在哪都认得出 |
| 整理任务的源目录 | 固定用 `row['target_cid']` | 用产物实际的 `parent_id`（多个产物不同目录时退回 target_cid） |

顺带删掉不再使用的 `before` 差集变量（`before_ids` 字段保留兼容）。

### 影响

之前所有「115 说完成、目录里找不到」的提交，其实多半是**文件在别的目录**。修复后它们会正常认领，然后按扫描范围决定「整理」还是「只落盘」。

验证：`test_offline_retry` + `test_offline_duplicate` 共 **13 项通过**。

## 0.9.36 — 2026-11-07（115 说完成就发完成回执，产物不在也发）

用户问「47 分的频道离线没收到完成回执，是打断还是故障」。查数据：`#203` 15:47:14 提交，15:53:04 变成 `completed`，message 是「115 显示下载完成，但产物不在目标目录」——**是 0.9.30 故意静默的分支，不是故障**。

但顺着查下去发现这不是偶发：

| 数据 | 值 |
| --- | --- |
| 115 离线任务列表 | **20 个任务，全部 `done`**（含 `98T@SSNI-953_LTX.mp4`、十三种爱情 E01-E08、Sunshine E01-E06…） |
| `porn / Av转存待整理` 目录 | **还是原来那 11 个老文件，一个都没多** |

也就是说「115 说完成、目标目录里找不到产物」是常态，不是个别情况。既然如此，静默结项就错了——用户看不到任何反馈，反而以为系统坏了。

### 改动

| 位置 | 改动 |
| --- | --- |
| `offline_completed(target, count=None)` | `count=None` 表示产物不在目录，输出 `📄 产物  不在目标目录` |
| `check()` 的「115 完成但产物不在」分支 | 结项的同时**照发完成回执**，并记一条 event |

回执长这样：

```
✅ 离线下载 · 已完成
📂 落盘  porn / Av转存待整理
📄 产物  不在目标目录
```

现在离线的回执只有两种，覆盖所有结局：**✅ 已完成**（含产物不在）、**⏳ 超时**（115 明确在下载且超过 10 分钟）。

验证：`test_offline_retry` + `test_offline_duplicate` 共 **13 项通过**。

## 0.9.35 — 2026-11-07（频道通知的目录名不再显示成数字）

0.9.26 为了让文案跟随目录改名，把频道通知改成「传目录 ID，渲染前用 `directory_label(cid, fallback)` 解析」。但 `directory_label` 只查 `organize_config.directory_labels`，那里只有**待整理 / 媒体库 / 未识别**三个目录——频道监控用的 `porn / Av转存待整理` 根本不在里面，于是查不到就退回 fallback，而 fallback 当时也传的 cid，结果文案变成了 `📂 保存至  3510832216693277696`。

### 修法

| 位置 | 改动 |
| --- | --- |
| `channel_monitor._notify()` | 去掉内部的 `directory_label` 解析（它拿到的已经是最终文本） |
| 三处调用 | 改成 `directory_label(rule['offline_cid'], rule['offline_label'])`：**cid 用来查最新名字，label 作为兜底**，永远不会退成数字 |
| `telegram_bot` 转存回执 | 同理，`label` 等于 cid 时按空处理，让模板显示「已配置目录」 |

### 优先级

1. `organize_config.directory_labels[cid]`（整理相关的三个目录，跟随改名）
2. 频道规则里保存的 `offline_label` / `share_label`（用户选目录时的名字）
3. 「已配置目录」

验证：`test_app` + `test_cards062` + `test_sync_notice` + `test_offline_retry` 共 **43 项通过**。

## 0.9.34 — 2026-11-07（TMDB 没登记这一集，也照常归档）

现场：`十三种爱情` 实际播了 8 集，TMDB 里只登记了 **1 集**（`tv/341065`，`number_of_episodes: 1`）。E01 成功，E02~E08 单集详情 404 → 全部转人工。用户问得很对：**不能因为 TMDB 数据没更新就不整理。**

关键事实：命名里的「第N集」来自 `episode_label(hint)`，**本来就不读 TMDB 的集名**（代码注释里写着「集号只由 episode_label 给出」）。所以缺集时完全可以照常归档。

### 三层改动

| 位置 | 改动 |
| --- | --- |
| `tmdb()` | 404 改抛 `CloudError('TMDB_NOT_FOUND')`，让调用方自己决定怎么处理 |
| `tmdb_episode()` | 单集 404 → **返回 `None`**（不再是异常）；季接口 404 也算「这季没有」，并缓存 60 秒避免每集都撞 |
| `build_plan()` | `episode is None` → 用 `{season, episode, name:'', air_date:''}` 兜底，并在 `plan['warnings']` 里记一条「TMDB 未登记 S01E02，集号按文件名归档」 |

作品详情 404 仍然转人工（`identify()` 里 `TMDB_NOT_FOUND` → `NeedsReview`），只是不再停整批。

### 结果

| 情况 | 行为 |
| --- | --- |
| 作品能识别、TMDB 有这一集 | 正常（集名/播出日期都用 TMDB 的） |
| 作品能识别、TMDB 没这一集 | **照常归档**，命名用文件名的集号，warning 里记一笔 |
| 作品本身查不到（404） | 该条转人工，任务继续跑 |
| TMDB 服务故障（429/5xx/超时） | 停 + 自动重排（不变） |

验证：新增 `tests/test_tmdb_missing_episode.py` 2 项；与离线、多语言识别、`test_app` 合计 **37 项通过**。

## 0.9.33 — 2026-11-07（TMDB 404 不再停整批 + 离线重复提交直接拒绝）

### 1. TMDB 404 把整批整理停掉了（job 647）

`tmdb()` 里所有 HTTP 错误都抛 `TMDB_UNAVAILABLE`，而这个 code 在 per-item 的 re-raise 列表里 → 一条片查不到就让整个任务 `halt(needs_review)`。job 647 就是这么停在「TMDB 请求失败（HTTP 404）」。

| 状态码 | 原来 | 现在 |
| --- | --- | --- |
| **404** | `TMDB_UNAVAILABLE` → 整批停 | **`NeedsReview`** → 该条转人工核查，任务继续 |
| 429 / 5xx / 超时 / 网络 | `TMDB_UNAVAILABLE` → 停 + 自动重排 | 不变 |

404 是「TMDB 里没这个条目」，属于单条识别问题；只有真正的服务故障才该停。

### 2. 离线重复提交没有任何提示

同一个链接再次提交时，原来只是照常插一条记录、交给 115 回「任务已存在」再转核查——用户看不到「你已经提交过了」。

现在 `submit()` 先按 `_link_key`（磁力/电驴用 hash）比对**非终态**的提交：

| 情况 | 结果 |
| --- | --- |
| 同一链接已在 `submitting/submitted/needs_review` | 直接 `409 · 该链接已在离线任务中（#196），不会重复提交。` |
| 上一次已 `completed/expired` | 允许重新提交（失败想重试是正常需求） |

和转存的「先查再提交」保持一致。

验证：新增 `tests/test_offline_duplicate.py` 2 项；与 `test_offline_retry` 合计 **13 项通过**。

## 0.9.32 — 2026-11-07（入库记录：标签靠右、每类 10 条、删掉多余按钮）

| 改动 | 说明 |
| --- | --- |
| 类型标签挪到右侧 | 行结构改为 `[#id][标题/时间][转存|离线][状态]`，左边的彩色块太抢眼 |
| 每类各取 10 条 | 转存 10 + 离线 10，合并后按时间倒序（原来合并后只留 5 条） |
| 删掉「放弃 7 天前的待核查」 | 0.9.19 起 72 小时未认领产物会自动 `expired`，这个手动按钮已经没有可处理的东西，留着只会让人问「什么意思」 |

验证：前端 `vue-tsc --noEmit && vite build` 通过。

## 0.9.31 — 2026-11-07（资源入库「最近记录」合并成一条时间线）

三个界面问题，根子是两个独立列表：

| 问题 | 原因 | 修法 |
| --- | --- | --- |
| 最新的离线记录排在转存下面 | 模板里两个 `v-for`：先渲染 `jobs.slice(0,3)` 再渲染 `submissions.slice(0,3)`，离线永远在后 | 合成一个 `recentRows`，按 `updated` / `submitted_at` **倒序取前 5 条** |
| 「离#196」和「#632」文字没对齐 | 前缀长度不同，`.history-id` 又是 `flex:none` 随内容宽 | id 统一成 `#id` 并给 `min-width:52px` + 右对齐 |
| 记录看不出是转存还是离线 | 只靠 `离#` 前缀暗示 | 前面加固定 30px 的类型标签：<br>`转存`（绿）/ `离线`（蓝） |

现在的行结构：`[类型 30px][#id 52px][标题 + 时间/来源/状态][状态标签]`，四列固定，换行也不歪。

验证：前端 `vue-tsc --noEmit && vite build` 通过。

## 0.9.30 — 2026-11-07（离线只保留「完成」和「超时」两条提示）

现场口径：10 分钟后看结果——成功就不管，没成功就自己去 115 删任务换源。所以系统只需要在他「没看」的时候提醒一次超时，其余情况一律静默。

| 情况 | 0.9.29 | 0.9.30 |
| --- | --- | --- |
| 下载完成、产物正常 | ✅ 完成回执 | ✅ 完成回执（不变） |
| 10 分钟还在下载 | ⏳ 超时回执 | ⏳ 超时回执（不变） |
| 115 说完成、产物不在目录 | ⚠️ 产物不在目录回执 | **静默结项**，不发提示（完成后被删是他自己干的，他知道） |
| 115 里找不到任务（被删） | 6 小时后 `expired` | **去掉这个判定**，交给 72 小时兜底（删任务的人自己知道） |

`offline_missing_product()` 模板随之删除，`TASK_GONE_SECONDS` 常量删除。

现在系统对离线只发两种消息：**✅ 已完成**、**⏳ 超时**。

验证：`tests/test_offline_retry.py` 11 项通过。

## 0.9.29 — 2026-11-07（离线「完成但没有产物」「任务被删」如实结项）

0.9.28 修好产物 ID 之后，露出最后一层：**115 明确说下载成功，但产物文件根本不在目标目录**。

现场核对（#196，提交 `98T@IPZ-377_LTX.mp4`）：

| 项 | 值 |
| --- | --- |
| 115 任务状态 | `percentDone 100` / `display_status finished` / `下载成功` |
| 产物 ID（`delete_file_id`） | `3556901390494205376` |
| 目标目录提交前 | 11 项 |
| 目标目录现在 | **还是 11 项，产物不在里面** |

也就是说系统在等一个永远不会出现的文件，同时又被 10 分钟规则误报成「超时」。

### 两处如实结项

| 情况 | 判定 | 结果 |
| --- | --- | --- |
| 115 说完成、产物不在目录 | 提交超过 **5 分钟**（给落盘留延迟） | → `completed`「115 显示下载完成，但产物不在目标目录」+ 一条 `⚠️ 离线下载 · 产物不在目录` 回执 |
| 115 里匹配不到该任务 | 提交超过 **6 小时**（任务被删了） | → `expired`「115 离线任务已不存在（可能已被删除）」 |

两条都不猜、不报超时，只陈述 115 的事实。

### 现在「超时」的唯一依据

115 明确回「下载中 / 等待 / 暂停」**且**本轮核查确认过**且**持续 10 分钟。少任何一条都不报。

验证：`tests/test_offline_retry.py` 11 项通过。

## 0.9.28 — 2026-11-07（修离线产物认领：115 的 file_id 是目录，不是产物）

用户报「频道离线明明成功了却报超时/一直卡着」。查 115 原始返回，找到根因：

```json
{"name": "98T@IPZ-377_LTX.mp4", "percentDone": 100, "status_text": "下载成功",
 "display_status": "finished",
 "file_id": "3510832216693277696",        ← 这是「保存到的目录」
 "delete_file_id": "3556901390494205376", ← 这才是产物文件 ID
 "wp_path_id": "3510832216693277696",
 "del_path": "www.98T.la@IPZ-377-LTX.mp4"}
```

`_tasks()` 一直把 `file_id`（目录 ID）当产物 ID，`check()` 就拿目录 ID 去目录里找同名文件——**永远找不到**，所有离线任务都卡在「等待通过文件 ID 确认本次下载产物」，随后被 10 分钟规则误报成超时。

### 三处修复

| 位置 | 改动 |
| --- | --- |
| `_tasks()` | `file_id` 改用 **`delete_file_id`**（产物）；另存 `dir_id`（保存目录）与 `display`（`display_status`） |
| `_state()` | 完成判定加 `display_status == 'finished'`；**认不出的状态返回 `unknown`**，不再默认 `running` |
| `check()` | `unknown` → 只写「115 未返回明确状态，稍后重试」，**不写「下载进行中」**，因此不会触发超时回执 |

现在「超时」回执的唯一依据是：115 明确回「下载中/等待/暂停」且持续 10 分钟。没有事实依据的一律不报。

验证：`tests/test_offline_retry.py` **11 项**，新增「产物 ID 取 delete_file_id」「只有真实完成证据才算 done」「认不出的状态是 unknown」。

## 0.9.27 — 2026-11-07（修离线超时误报）

现场：`#196` 14:08 提交、14:18 就报「超过 10 分钟没下完」，可它其实**下载早就完成了**——message 是「等待通过文件 ID 确认本次下载产物」。用户刚说频道离线成功，却收到超时回执。

### 根因（两处都错）

1. `tick()` 每轮只核查 **5 条**（`ORDER BY last_checked,id LIMIT 5`），而 `report_stalled()` 对**所有** `submitted` 且超 10 分钟的一律回执——包括这一轮根本没轮到核查的。刚提交的排到队尾，10 分钟没被查过就直接判超时。
2. 判定只看「本地状态是 submitted + 超过 10 分钟」，不看 `check()` 的真实结论。「下载完成但产物未认领」「任务已被删匹配不到」这两种都不是「下不动」，却同样被报成超时。

### 修复

`report_stalled(now, ids)` 只报同时满足三条的：

| 条件 | 为什么 |
| --- | --- |
| `id IN ids`（本轮 `tick()` 真的核查过） | 没核查过的不能猜 |
| `message LIKE '%下载进行中%'`（115 明确回任务在跑） | 产物未确认、任务被删都不算超时 |
| `submitted_at < now-10min` 且 `stalled_at IS NULL` | 真卡住、且没报过 |

`tick()` 现在把本轮 `check()` 过的 id 传给 `report_stalled()`。

数据清理：**196 条**误报标记 `stalled_at` 全部清空，让新逻辑重新判断。

验证：`tests/test_offline_retry.py` **9 项**，新增「没轮到核查的不报超时」「任务被删不报超时」。

## 0.9.26 — 2026-11-07（所有通知里的目录名改成实时解析）

问题：通知里的目录名一直用「提交/配置时的快照」，用户换了目标目录或改了目录名之后，文案还是旧名字——存进媒体库却显示「待整理」，看着像程序出错。

统一入口：`Store.directory_label(cid, fallback)` —— 按当前 `organize_config.directory_labels[cid]` 解析，查不到才用快照。

| 文案点 | 原来 | 现在 |
| --- | --- | --- |
| 离线完成/超时回执 | 提交时的 `target_label` 快照 | `directory_label(target_cid, 快照)` |
| 转存完成通知 `transfer_completed` | 写死 `115 网盘 / ` + 快照 | 去掉写死前缀；`notifications` 渲染前按 `target_cid` 解析 |
| 频道通知 `channel_notice`（离线/转存/待核查） | 频道规则里的 `*_label` 快照 | 调用处改传 `*_cid`，`_notify` 里解析 |
| 机器人转存回执 `transfer_received` | `details.target_label` 快照 | `directory_label(dest, 快照)` |

顺带修掉：`transfer_completed` 里也有写死的 `115 网盘 / `，而标签本身已含前缀，会显示成「115 网盘 / 115 网盘 / 媒体库」。

验证：`tests/test_offline_retry.py` + `test_sync_notice` + `test_cards062` + `test_console060` 共 **43 项通过**；新增用例断言「目录改名后回执跟着变」且「115 网盘 只出现一次」。

## 0.9.25 — 2026-11-07（回执里的目录名改成动态，修掉重复前缀）

0.9.24 的模板里写死了 `'📂 落盘  115 网盘 / ' + target`，而 `target` 本身已经是「115 网盘 / 待整理」这种带前缀的目录标签——实际会显示成「115 网盘 / 115 网盘 / 待整理」。

两处修复：

| 问题 | 修法 |
| --- | --- |
| 写死前缀导致重复 | 模板里去掉 `115 网盘 / `，只输出标签本身；`channel_notice()` 的 offline / offline_unknown / transfer 三个分支同样有这个问题，一并去掉 |
| 目录改名后回执还是旧名 | 新增 `OfflinePostProcess._label(cid, fallback)`：**优先取当前 `organize_config.directory_labels[cid]`**，没有配置才用提交时的 `target_label` 快照 |

所以目录改了名（或换了目录），回执里的名字跟着变，不是写死的。

验证：`tests/test_offline_retry.py` 7 项，新增「目录改名后回执跟着变」+「`115 网盘` 只出现一次」。

## 0.9.24 — 2026-11-07（离线回执改成模板格式：成功 + 超时两条）

0.9.23 只是把长文案删短，格式还是「一堆句子」。改成和 `channel_notice` 同一套「emoji + 两列短标签」模板，在 `message_templates.py` 里新增两个函数，离线模块直接调用。

**成功回执**（`offline_completed`，一条提交一条）：

```
✅ 离线下载 · 已完成
📂 落盘  115 网盘 / 待整理
📄 产物  3 个
🎬 整理  任务 #636 已入队
```

只落盘不入队时，最后一行是 `📄 状态  只落盘，不自动整理`。

**超时回执**（`offline_stalled`，一条提交一条）：

```
⏳ 离线下载 · 超时
📂 目标  115 网盘 / 待整理
📄 文件  The.Silent.Storm.S01E02.mkv
👉 到 115 离线列表删掉换源
```

| 改动 | 原来 | 现在 |
| --- | --- | --- |
| 组织方式 | 多条卡住任务汇总成一条 | **一条提交一条回执**（能直接对上 115 列表里的任务） |
| 格式 | 自由排版 | 模板函数，与频道/转存通知同风格 |
| 时间 | 显示「已等 868 分钟」 | **不显示**，超时本身就是信息 |
| 解释 | 两三句 | 无 |

`_enqueue_organize()` 现在返回 `jid`，成功回执才能带上「任务 #636 已入队」。每次 `tick()` 最多发 5 条超时回执，剩下的下一轮。

验证：`tests/test_offline_retry.py` 6 项，文案断言改成匹配模板字段。

## 0.9.23 — 2026-11-07（离线回执文案精简）⚠️ 已被 0.9.24 的模板回执取代，勿参考本条文案

原来那条回执八行、字比信息多。改成：

```
⏳ 3 个离线任务卡住了

• The.Silent.Storm.S01E02.2160p.WEB-DL.H.265.AAC.mkv · 14 小时
• 98T@WAAA-693_LTX.mp4 · 13 小时
• CEMD-906_LTX.mp4 · 6 小时

去 115 离线列表删掉换源。
```

| 改动 | 原来 | 现在 |
| --- | --- | --- |
| 标题 | 「超过 10 分钟没下完」 | 「卡住了」 |
| 每条 | 「已等 868 分钟 · 下载进行中（0/1）」 | 「14 小时」 |
| 结尾 | 两行解释 + 建议 | 一句「去 115 离线列表删掉换源。」 |

时长按量级显示（`_duration()`）：≥1 天用天、≥1 小时用小时、否则用分钟。

验证：`tests/test_offline_retry.py` 6 项，文案断言改成精确比对（标题与正文全文）。

## 0.9.22 — 2026-11-07（离线回执阈值改成 10 分钟，每个任务只报一次）

现场口径：正常离线 **5 分钟**就下完，最多 10 分钟；超过这个时间基本就是下不动了。0.9.21 用的 6 小时/12 小时太迟钝，等于没有回执。

| 项 | 0.9.21 | 0.9.22 |
| --- | --- | --- |
| 判定卡住 | 6 小时 | **10 分钟** |
| 回执频率 | 每 12 小时一次 | **每个任务只报一次**（`offline_submissions.stalled_at` 标记） |
| 单位 | 小时 | **分钟** |

回执文案：

```
⏳ 2 个离线任务超过 10 分钟没下完

• Movie.S01E02.2160p.mkv · 已等 14 分钟 · 下载进行中（0/1）
• 98T@SOE-992-U_LTX.mp4 · 已等 11 分钟

正常 5-10 分钟就能下完，这些基本是下不动了。
到 115 离线列表删掉换别的源即可，系统不会重复提交。
```

改「每个任务只报一次」而不是固定间隔：卡住的任务就该由人去 115 处理，重复提醒没有意义；新卡住的任务会立刻进下一条回执。

验证：`tests/test_offline_retry.py` 6 项（含 14 分钟触发一次、同一任务不重复、5 分钟内不打扰）。

## 0.9.21 — 2026-11-07（离线任务卡住时的回执）⚠️ 6 小时/12 小时的阈值已由 0.9.22 改为 10 分钟，以 0.9.22 为准

现场痛点：离线任务一直不完成时，系统既不报错也不推进——`check()` 只是把 message 写成「下载进行中（X/Y）」，用户看不到，也就不知道是卡住了还是在慢慢下，更没法决定去 115 换源。

新增 `OfflinePostProcess.report_stalled()`，在 `tick()` 每轮末尾调用：

| 规则 | 值 |
| --- | --- |
| 判定卡住 | `state='submitted'` 且提交超过 **6 小时** |
| 回执频率 | 每 **12 小时**最多一条（`meta.offline_stall_report_at` 节流） |
| 内容 | 总数 + 前 8 条：文件名（从 ed2k `|file|` 或磁力 `dn=` 提取）· 已等多久 · 下载进度 |
| 结尾 | 「建议到 115 离线列表核查：下不动的删掉换别的源即可，系统不会重复提交。」 |

为什么不做 0 字节防护：17506 条整理记录里 `size==0` 和 `<1MB` 都是 **0 条**，115 不会留半成品，加了也永远不命中。

验证：`tests/test_offline_retry.py` 扩到 6 项（原 4 项 + 卡住回执只发一次、新提交不触发回执）。

## 0.9.20 — 2026-11-07（离线任务列表清空后的收尾，只有数据清理）

用户在 115 里清空了离线任务列表，剩下的 56 条待核查提交（42 `submitted` + 14 `needs_review`）永远认领不到产物。

**先抽查再结项**：

| 目录 | 目录内文件 | 待核查提交 | 命中产物 |
| --- | --- | --- | --- |
| `3552820403619497086`（115 网盘 / 待整理） | 0 | 4 | 0 |
| `3510832216693277696`（porn / Av转存待整理） | 11 | 52 | 0 |

结论：**56 条的产物一个都没落盘**，不存在「已下载好但没认领」的遗漏。

处理：56 条 → `expired · 115 离线任务列表已清空，本批产物未落盘，自动结项。`

`offline_submissions` 现在 = `completed 29` + `expired 166`，**非终态 0**。

顺带：首次清理时有 4 条因为和 `tick()` 并发被写回 `submitted`，补清一次；媒体信息队列 7 条 `failed`（STRM 与 sidecar 现在都在，纯历史失败记录）一并删除，队列只剩 `done 1151`。

## 0.9.19 — 2026-11-07（离线提交超期自动结项）

0.9.18 把 99 条「115 请求未完成」放回 `submitted` 重试后暴露了下一层问题：这些离线任务早已结束，产物永远认领不到，核查只会把它们又推回 `needs_review`——现场 166 条待核查里 **110 条超过 72 小时**。

新增 `EXPIRE_SECONDS = 3 天`：`tick()` 每轮先把「超过 72 小时还没认领产物」的 `submitted/needs_review/submitting` → `expired · 离线提交超过 72 小时未认领产物，自动结项。`，然后才核查剩下的。

数据清理：**110 条**当场结项。`offline_submissions` 现在 = `expired 110` + `submitted 47` + `completed 29` + `needs_review 9`（真需要人工的只剩 9 条「无法唯一确认产物」）。

验证：`tests/test_offline_retry.py` 4 项（临时故障重试、连续 6 次转人工、超期结项、新鲜提交不结项）。

## 0.9.18 — 2026-11-07（离线提交核查重试 + 积压清理）

全链路排查结论：**数据面全绿**（磁盘 STRM 1049 = `playbacks` 981 + `share_strms` 68，台账缺失 0、野条目 0，sidecar 1047/1049，孤儿 0，`transfer-secret` 残留 0）。积压集中在离线提交。

### 修复：核查失败按次数退避

`OfflinePostProcess.tick()` 原来对任何 `CloudError` 都直接 `needs_review`，现场 **99 条「核查未完成：115 请求未完成」**一次就判死、再没人碰。

改成 `_retry_later()`：临时错误保持 `submitted` 继续核查，`attempts` 累加，连续 6 次才转人工。`offline_submissions` 加 `attempts` 列（`ALTER TABLE ... DEFAULT 0`，幂等）。

### 修复：重复提交转核查

115 返回「任务已存在」说明链接早就提交过，原来被写成 `needs_review`（现场 **28 条**）。现在识别这个文本 → 转 `submitted` 去核查产物。

### 数据清理（只动台账/队列，不动任何文件）

| 对象 | 处理 |
| --- | --- |
| 99 条「115 请求未完成」 | → `submitted`，按新策略重试 |
| 28 条「任务已存在」 | → `submitted`，转核查 |
| 422 条 `share_strms(revoked=1)` | 先归档进 `share_revocations`（此前该表为空，撤销历史其实没留），再删除 |
| 1 行 `channel_state` 残留（`ltxjasna`） | 删除，剩 2 行与配置一致 |
| 27 条 `share_recovery_intents` | 全是 `done` 的历史记录，删除 |
| 46 条 `strm_changes`（job 291 `removed`） | 对应 STRM 已确认全部删除，变更记录过期，删除 |

清理后：`offline_submissions` = `submitted 157` + `completed 29` + `needs_review 9`（真需要人工的只剩 9 条「无法唯一确认产物」）。

### 验证

新增 `tests/test_offline_retry.py` 2 项：临时故障保持重试、连续 6 次才转人工。

## 0.9.17 — 2026-11-07（修复 0.9.12 回滚时误删的 `<details>`）

**我自己的锅**：0.9.12 撤「重新识别」时用了「删掉所有包含 `can_reidentify` 的行」，而 `<details v-if="…||job.can_reidentify)" class="task-extra">` 这一整行都含这个词——开始标签被删掉，`<summary>更多操作</summary>` 和三个按钮直接裸露在卡片里。

现场表现：任务卡片下方出现一堆「更多操作」但点不开（没有 `<details>` 就没有折叠行为），未扫到任何文件的任务也显示「重新生成 STRM 与字幕」。

修复：

| 改动 | 说明 |
| --- | --- |
| 补回 `<details v-if="…">` 开始标签 + `</details>` | 折叠恢复正常 |
| 「重新生成 STRM 与字幕」加 `v-if="job.counts.done||job.counts.archived"` | 没有已归档输出的任务不再显示这个按钮 |

**教训**：批量按行过滤删代码，必须回头逐行确认删掉的是不是完整语句；标签类文件（`.vue`）尤其不能用「包含关键字就删整行」。

## 0.9.16 — 2026-11-07（媒体信息补扫不再空转）

现场现象：每 5 分钟一条「媒体信息补扫：为 15 条缺失 sidecar 的受管 STRM 入队」，但本地真正缺 sidecar 的只有 2 条（`分享STRM未识别/05`、`06`）。

### 根因

`native_media_info._sweep_files()` 的跳过条件是「有 sidecar **且** Emby 条目里有视频流」。sidecar 已经在、但 Emby 还没解析出视频流时，两条都不成立 → 每轮重新入队 → `enqueue` 只对 `queued/running` 判重，`done` 不判 → 同一个文件被反复插入。

现场数据：队列 2609 行，其中 **234 组重复**；**1226 行的 STRM 文件早已不存在**（历史任务/已撤销分享的残留）。

### 修复

| 位置 | 改动 |
| --- | --- |
| `_sweep_files()` | `has_sidecar` 且队列里已有该文件的 `done` 记录 → 跳过；补过一次就不再补。sidecar 被删后仍会重新入队 |
| `store._prune()` | 补扫队列每个 `target` 只留最新一行，并封顶 3000 行 |

### 数据清理（只删队列行，不动文件、不动 STRM）

`native_media_info_queue` **2609 → 1149**：去重 234 行、删除「STRM 已不存在」的 1226 行。剩 `done 1142` + `failed 7`（后者是历史失败记录，对应 STRM 与 sidecar 现在都在）。

### 顺带核对（全绿）

| 检查 | 结果 |
| --- | --- |
| 磁盘 STRM | 1040 |
| `share_strms`(revoked=0) + `playbacks`(revoked=0) | 68 + 972 = **1040** |
| 台账有、磁盘没有的 STRM | **0** |
| 磁盘有、台账没登记的 STRM | **0** |
| 缺 sidecar 的 STRM | 2（都在 `分享STRM未识别`，未登记，补扫本来就不该碰） |

### 验证

新增 `tests/test_media_sweep.py` 4 项：sidecar 在且补过不再入队、sidecar 在但没补过补一次、sidecar 缺失照常入队、已在队列中跳过。

## 0.9.15 — 2026-11-07（账本自动对账 + 任务自动结项）

现场问题：用户在 115 里手动删除 / 移动 / 重命名 / 移回待整理之后，账本一直挂着，必须人工来点完结；任务条目都结束了还显示「部分未完成」。这不是操作问题，是账本只会记账、不会对账。

### `reconcile_ledger()`：每小时自动对账

只读 115 文件语义接口 + 只改账本状态。**不移动文件、不删除文件、不碰扫描范围。** 判定只认 `file_id`，所以手动重命名不影响。

| 判定 | 依据 | 结果 |
| --- | --- | --- |
| 文件已归档 | `file_id ∈ organized_files`（纯本地，不打 115） | 闭环 |
| 文件已入库 | `file_id ∈ share_strms WHERE revoked=0` | 闭环 |
| 分享已失效 | `file_id ∈ share_strms WHERE revoked=1` | 闭环 |
| 文件没了 | `cloud.file()` 抛 `FILE_NOT_FOUND` 或 `parent_id=0` | 闭环 |
| 已人工移回待整理 | `parent_id ∈ organize_config.source_cids` | 旧账本闭环，让它重新入队 |
| 文件在别处 | 以上都不是 | **不动**，等人工判断 |

安全阀：连续 3 次异常（限流/网络）立刻停手，本轮不动任何条目；只处理 `updated` 超过 24 小时的任务，正在跑的不碰；原始失败原因保留在 message 里。

### `settle_jobs()`：没活干的任务自己结项

`partial` / `needs_review` 的任务，如果 `organize_items` 里没有任何 `done/skipped/recycled` 以外的状态，且 `notify_state` 不是 `failed`/`unknown`，就自动 → `completed · 已自动结项`。现场 10 个历史任务（job 2/3/8/55/56/74/81/92/105/183）会自己消失。

### 同版修复：结项不再补发历史入库通知

现场事故：把 `job 218` 从 `partial` 改成 `completed` 之后，它 11-03 那 129 个视频对应的 **368 条「🎉 新片入库」通知被集中补发**（`send()` 每条间隔 1.1 秒，刷了约六分钟）。

原因：`notifications._job()` 在任务进入终态时调 `media_report()`，而它会为该任务**所有** `done` 条目重建 `media:{job}:...` 通知。结项只是把账本收口，不是「这批刚入库」。

| 位置 | 改动 |
| --- | --- |
| `notifications._job()` | `stage` 是 `已人工结项` / `已自动结项` / `账本已闭环` → `notify_state` 置 `not_needed` 并 return，**不调 media_report** |
| `organizer.settle_jobs()` | 结项时一并写 `notify_state='not_needed'` |
| `main.py` 人工结项 | stage 改成 `已人工结项`，并写 `notify_state='not_needed'` |

### 验证

新增 `tests/test_reconcile_ledger.py` 9 项、`tests/test_settle_notify.py` 3 项；加上 `test_close_recovered` + `test_console060` + `test_sync_notice` + `test_app` 共 **52 项通过**。

## 0.9.14 — 2026-11-07（只有数据清理，没有代码改动）

### 最后 39 条账本闭环：未完成归零

对 39 条逐条向 115 核实（`cloud.file(file_id)`），结论一致：

| 证据 | 结果 |
| --- | --- |
| 文件详情 `parent_id` | 全部为 `0`（115 对已脱离所有目录的文件才这样返回） |
| 网盘根目录（cid=0） | 只有 6 个文件夹，不含这些文件 |
| 回收站 `recyclebin_list` | 0 条 |

→ 39 条 → `skipped · 源文件已不在网盘`，涉及任务 24 / 63 / 216 / 218 / 221 结项。**未完成条目 39 → 0，partial 任务 14 → 10。**

### 还剩 10 个 partial 任务（条目都是 0，纯状态残留）

| 任务 | 挂着的理由 |
| --- | --- |
| job 2 / 3 / 8 / 92 | 「扫描期间网盘内容发生变化，已跳过失效清理；请重新同步」 |
| job 55 / 56 / 74 / 81 / 105 | 「1 个目录读取失败，已保留待下次清理」 |
| job 183 | 「已跳过 5 个视频」 |

这些任务的 `organize_items` 全部是终态，**没有任何待办**，只是任务状态没自愈。

### 设计缺陷（这次挨骂的正题）

账本只会「记账」，不会「对账」。用户在 115 里手动删除/移动/重命名/移回待整理之后，系统三种情况都处理不了：

| 用户动作 | 现状 | 应该有 |
| --- | --- | --- |
| 手动删除文件 | 账本一直挂着，要人来点完结 | 维护任务发现 `parent_id=0` / `FILE_NOT_FOUND` → 自己闭环 |
| 手动移回待整理 | 旧账本还挂着，还会被当成未完成 | 旧账本闭环，让文件重新入队 |
| 手动重命名 | 账本记的旧名字对不上 | 按 `file_id` 对账，不看名字 |

**正确做法是每小时自动对账（账本 vs 115 vs 库），不是让人来汇报。** 本次清理用的判定逻辑（`parent_id=0`、`file_id ∈ share_strms/organized_files`）已经用 114 条真实数据验证过，直接写进维护任务即可，不需要碰扫描范围、不需要移动文件。

## 0.9.13 — 2026-11-07（只有数据清理，没有代码改动）

### 僵尸账本清理：114 → 39

114 条未完成账本里，**75 条的文件早就处理完了**，账本行没跟着走：

| 判定 | 条数 | 依据 |
| --- | --- | --- |
| 文件已入库 | 2 | `file_id` 命中 `share_strms WHERE revoked=0` |
| 分享已失效 | 73 | `file_id` 命中 `share_strms WHERE revoked=1`（分享取消/失效，文件永远进不来） |

处理：这 75 条 → `skipped · 账本已闭环`，message 追加「文件已入库/分享已失效，本条目不再跟踪」；涉及任务无剩余未完成时 → `completed · 账本已闭环`。partial 任务 24 → 14。**没有移动或删除任何文件。**

### 剩下的 39 条（真需要人看）

| 任务 | 条数 | 原因 |
| --- | --- | --- |
| job 24 | 11 | `Hua Deng Qi` 年份 2026，TMDB 候选 0 条 |
| job 63 | 21 | `My Little One.2019.S05E01~E21`，TMDB 请求失败；文件既不在原目录也不在未识别目录 |
| job 218 | 4 | 音乐类电影，无法可靠判断是否演唱会 |
| job 221 | 2 | `古惑仔：江湖大风暴` 年份 1996，候选 0 条 |
| job 216 | 1 | 输出目标已有未确认归属或已修改的本地文件 |

### 纠正一处误判：频道并没有重复抓取

`channel_state` 有 3 行（`ltxjasna`、`QukanMovie`、`-1004419698318`），但**配置里只有 2 条**（前端显示的就是 2 条）。监控循环遍历的是 `config()['channels']`，不是 `channel_state`，所以 `ltxjasna` 那一行**只是残留状态，不会被抓**——证据：它最后抓取停在 11-06 14:06、最后帖子 11-05 20:36，之后再无更新。

残留原因：把频道从用户名改成数字 ID 时，`channel_state.name` 没跟着改（`channel_monitor.delete()` 会删，但直接改配置里的 name 不会）。属于脏数据，不影响抓取。0.9.12 日志里「两条都在跑、重复转存」的说法是错的，以本条为准。

### 账本 vs 文件是两套口径

- **文件**：网盘库未识别目录 10 个（已人工收尾）、分享库未识别目录 2 个，其余都已入库或已撤销。
- **账本**：`organize_items` 里 review/pending 的行，代表「某次整理没走完」，不等于文件还在原处。以后看到数字先查这条：

```sql
-- 未完成账本里，有多少其实文件已入库 / 分享已失效
SELECT COUNT(*) FROM organize_items i WHERE i.state IN ('review','pending')
  AND (i.file_id IN (SELECT file_id FROM share_strms)
       OR i.file_id IN (SELECT file_id FROM organized_files));
```

## 0.9.8~0.9.12 变更索引（排查用，先看这里）

### 一句话结论

- **0.9.8~0.9.11 引入的「重新识别未识别文件」是一个越权后门，0.9.12 已整体删除。** 它为了扫「未识别目录」而在 `job_scan_scope()` 里放行了一个不在 `source_cids` 里的目录。任何以「让整理扫到扫描范围外的目录」为手段的功能，一律不做。
- 保留下来的两个纯账本维护（不碰文件、不碰扫描范围）：`close_recovered_items()`、`close_superseded_share_batches()`。

### 扫描范围的唯一权威

| 位置 | 作用 |
| --- | --- |
| `organizer.allowed_scan_cids(cids)` | 唯一判定：等于扫描根或是其后代，用 115 真实父子链，读不到链一律不放行 |
| `organizer.scan_scope_allows(cids)` | 上面那个的布尔包装 |
| `transfer.py:191` | 转存完成后要不要自动整理，过这道闸 |
| `offline_api.py:357` | 离线/转存产物要不要自动整理，过这道闸 |
| `organizer.run_job` 复校 | 执行前再核一次 `config['source_cids']`，不在范围就 `cancelled · 扫描目录已移出范围` |

**没有第四种入口。** 以后再看到整理任务扫了范围外的目录，先查 `organize_jobs.origin`：

```sql
-- 谁派发的这个任务、扫了什么
SELECT id, state, json_extract(config,'$.origin'), json_extract(config,'$.source_cids'), message
FROM organize_jobs ORDER BY id DESC LIMIT 20;
```

`origin` 取值：`offline-scan:<cid>`（离线/转存产物）、`schedule`（定时扫描）、`web`/`telegram`/`monitor`（分享库）、`null`（手动「开始整理」）。

### 0.9.12 的人工收尾（改的是数据，不是代码）

| 对象 | 动作 |
| --- | --- |
| `job 408` 10 条 pending | → `skipped`「已人工移出，不再跟踪」，任务 → `completed` |
| `job 409` 8 条 pending | 同上 |
| `job 627` | 越权扫描任务，→ `cancelled · 越权扫描，已作废` |
| porn 目录 10 个文件 | 用户自己移回 `3510832216693277696`（porn / Av转存待整理），代码没有移动过任何文件 |

### 保留下来的账本维护（每小时跑，只读 + 只改状态）

| 方法 | 判据 | 不会做什么 |
| --- | --- | --- |
| `close_recovered_items()` | 更晚的任务里有**完全相同的原始文件名**且已 `done` | 不模糊匹配、不动通知失败的任务 |
| `close_superseded_share_batches()` | 分享库按「作品+季+集」匹配到后续成功的批次 | 不碰文件 |

### 怎么查这批改动的痕迹

```sql
-- 账本被谁闭环的
SELECT id, job_id, state, step, message FROM organize_items
WHERE step IN ('已由后续批次补齐','已人工移出，不再跟踪') ORDER BY id DESC LIMIT 20;

-- 任务结项原因
SELECT id, state, stage, message FROM organize_jobs
WHERE stage IN ('已人工结项','已由后续批次补齐','越权扫描，已作废') ORDER BY id DESC;
```

### 回滚点

| 版本 | commit | 说明 |
| --- | --- | --- |
| 0.9.7 | `733321e` | 越权后门引入前的最后干净状态（STRM 体检 + 残次品清理） |
| 0.9.9 | `54f9f68` | 引入后门（0.9.8~0.9.11 全部压在这一个提交里） |
| 0.9.12 | `3207eb9` | 后门下线，回到 0.9.7 的扫描范围语义 |

需要退回干净状态时：`git checkout 733321e -- backend/strimkeep/organizer.py backend/strimkeep/main.py frontend/src/Tasks.vue`，再重新刷版本号。

## 0.9.12 — 2026-11-07

- **撤回 0.9.8~0.9.11 的「重新识别未识别文件」**。它必须让扫描范围放行「未识别目录」才能工作，等于给整理开了一个绕过 `source_cids` 的后门——现场 job 627 就是这样扫了 10 个不该碰的文件。功能、按钮、`can_reidentify` 标记、`job_scan_scope()` 放行、对应测试全部下线，扫描范围恢复「只认扫描根及其后代」。
- 现场复盘（0.8.42 之前的越权）：频道监控「Jasna LTX 频道」`share_mode=transfer` 把分享转存到 `3510832216693277696`（porn / Av转存待整理），转存完成后自动派发整理，源直接取产物目录，**不经过 `source_cids`**，于是 11-05 job 408/409 把 10 个成人文件当影视扫了一遍、全进了未识别目录。0.8.42（11-06）的「所有整理入口都要过扫描范围闸」已经堵住这条路，11-07 的转存没有再产生整理任务。
- 顺带确认：同一个 Jasna 频道被加了两次（`ltxjasna` 用户名 + `-1004419698318` 数字 ID），两条都在跑，重复抓取/重复转存。待处理。
- 验证：`test_console060` + `test_close_recovered` + `test_multilang_identify` = 31 项通过。
- **人工收尾（只改数据，没动任何文件）**：`job 408` 10 条、`job 409` 8 条 pending → `skipped · 已人工移出，不再跟踪`，两个任务 → `completed · 已人工结项`；越权任务 `job 627` → `cancelled · 越权扫描，已作废`。那 10 个文件由用户自己移回 `3510832216693277696`（porn / Av转存待整理）。
- 排查索引见文件顶部「0.9.8~0.9.12 变更索引」。


## 0.9.11 — 2026-11-07  ⚠️ 本版新增的扫描范围放行已随 0.9.12 全部删除，勿再参考

- 0.9.10 放行了扫描范围后，重扫任务又撞上第二条校验：「待整理、归档、待处理目录不能相同」——因为 `source_cids` 就是 `pending_cid`。改为重扫任务把 `pending_cid` 清空、另存 `retry_source_cid`：认不出的文件**原位保留**（不再搬一次），扫描范围照旧只给这一个目录开口子。
- 验证：`test_retry_identify_scope`（4 项）+ `test_close_recovered` + `test_console060` + `test_app` = 40 项通过；控制台用例补了 `pending_cid` 清空与 `retry_source_cid` 的断言。


## 0.9.10 — 2026-11-07  ⚠️ 本版新增的 `job_scan_scope()` 已随 0.9.12 删除，勿再参考

- 0.9.8 的「重新识别未识别文件」在线上第一次点就被判死：任务执行前的扫描范围复校只认扫描根，而它把本任务的 `pending_cid`（未识别目录）当源，于是直接 `cancelled · 扫描目录已移出范围`（job 625）。新增 `job_scan_scope()`：允许目录 + **本任务自己的未识别目录**，只给这一个目录开口子，别的目录照旧复校。
- 顺带验证播放链路：网盘库与分享库各取一条 STRM 做真实探测，均 `302` 跳 115 CDN 并支持 Range，播放地址链路正常。
- 新增 `tests/test_retry_identify_scope.py` 3 项（未识别目录放行、范围外仍拒绝、扫描根行为不变）。


## 0.9.9 — 2026-11-07

- 231 条未完成条目里 **99 条是历史残留**：同一文件名（如未识别目录里的 `08.mp4`）在更晚的批次里已经成功归档，旧条目却一直挂着「未识别」。新增 `close_recovered_items()`：每小时维护时按「更晚的任务 + 完全相同的原始文件名」自动闭环，并把因此清空的 partial 任务结项；Emby 通知失败的任务不自动结项，不做模糊匹配。原始失败原因保留在 message 里可追溯。
- 剩下 132 条是真实未识别（文件名只有集号 39、无检索线索 34、TMDB 候选不足等），保留人工处理。~~网盘库可用 0.9.8 的「重新识别未识别文件」按原配置重扫未识别目录~~ —— 该功能已于 0.9.12 下线（它靠越权放行扫描范围才能工作），不要再找这个按钮。
- 验证：新增 `tests/test_close_recovered.py` 3 项（同名闭环、更早的成功不闭环更晚的条目、通知失败不结项）；`test_multilang_identify` + `test_console060` 合计 32 项通过。


## 0.9.8 — 2026-11-07  ⚠️ 本版新增的「重新识别未识别文件」已于 0.9.12 整体下线

- 深挖发现 **231 条「未识别 / 待处理」积压**（review 151 + pending 80），全部集中在 11-02~11-05：TMDB 请求失败被当成识别失败转进未识别目录（21）、英文发布名在中文搜索下相似度为 0（42）、音频偏好缺失直接拒收（19）、文件名只有集号（39）。这些路径现在的代码已经分别改成「任务级可重试」「换语言重搜」「容错命名」，但**旧条目没有恢复路径**——文件已经被搬到未识别目录，重跑原任务碰不到它们。
- 新增「重新识别未识别文件」（任务中心 → 更多操作 → 按钮；`POST /api/v1/organize/jobs/{id}/retry-identify`）：按该任务的同一套配置把「未识别目录」当成源再扫一遍。能认出来的正常归档，认不出的仍留在原处，不会来回搬。`job_public` 增加 `can_reidentify` 标记。
- 顺带核对（全绿）：1037 个 STRM 100% 用配置的播放地址、0 个重复播放地址、sidecar 与 STRM 播放身份 0 错配、分享库 66 条活跃 STRM 全部有探测数据（只有未识别目录那 2 条没有）。
- 验证：`test_console060` 19 项（含「重新识别未识别目录」用例）。


## 0.9.7 — 2026-11-07

- 新增「清理残次品」：`POST /api/v1/strm/audit/cleanup` + 体检卡片上的按钮。删两类东西——内容不是 `/play115/` 的外部 STRM（另一条产线/旧工具在路径变更时留下的残次品），以及台账里文件已经不在的登记（`playbacks` / `share_strms` 置撤销）。**本项目格式的 STRM 一个都不删**，哪怕没登记（那多半是台账丢了，删了就真没了）。
- 双库独立补了明确用例 `test_sidecar_is_written_for_both_libraries`：同一个视频在网盘库和分享库各有一条 STRM 时，两边各自写自己的 `-media-info.json`，一边有不能代替另一边（后续「双库去重」依赖这个前提）。
- 验证：`test_console060`（含清理用例）/ `test_native_media_info`（含双库用例）38 项通过。


## 0.9.6 — 2026-11-07

- **STRM 体检**（P1-A / P1-C）：新增 `GET /api/v1/strm/audit`，并在「STRM 生成」页加了「STRM 体检」卡片。一次只读扫描分三类：本项目登记、非本项目（另一条 STRM 产线写的，含旧 `/play/share/<id>/…` 格式）、台账有但文件不在。现场：1036 个 STRM 里 7 个是非本项目的（`/play/share/afc31f29…/一生所爱_(2026)_…` 这类下划线命名），3 条台账悬空（`分享STRM未识别/50.mkv.strm`、`51.mkv.strm`、媒体库一个已删条目）。
- **历史未确认投递归档**（P1-B）：超过 24 小时仍是 `unconfirmed` / `partial` 的 `emby_deliveries`，在每小时维护里归档成 `archived`，任务中心不再把它当当前状态展示。核对窗口本身只有 30 分钟，放一天足够宽容；现场 12 条全部是 11-01~11-04 库路径还没配好时留下的。
- P1-D（全量同步「扫描期间网盘变化就跳过清理」）核对后确认当前代码已经改成「继续核对、逐条 live 确认」，3 条 partial 全是 11-01~11-03 的历史，不需要再改。
- 验证：`test_console060` 17 项（新增「体检分类」「超期归档」两个用例）；`test_emby_delivery_states` / `test_ops0820` / `test_app` / `test_organize` / `test_sync_notice` 65 项通过。


## 0.9.4 — 2026-11-07

- 孤儿媒体信息 JSON 再补一半：0.9.3 只清「登记在 `media_info_outputs` 里」的，现场还剩 46 个早期没登记的老文件。它们不只是垃圾——同名新文件会被它们挡住（`write_sidecar` 判定「已存在且归属无法确认」），媒体信息永远写不进去。现在没登记但带本项目字段（`version` / `playback_id`）的孤儿也一起清，别人的 JSON（结构不同）不动。
- 验证：`test_native_media_info` 19 项通过（孤儿用例扩到「登记 / 未登记但属本项目 / 别人的」三种）。
- 现场：部署后孤儿从 58 → 46 → 归零（每小时一次的清扫）。


## 0.9.3 — 2026-11-07

全链路排查（识别 → 扫描 → 重命名 → 洗版 → 移动 → 删除 → 生成 STRM → 取链 → 路径映射 → Emby 通知 → 播放）后的第一批修复：

- **媒体信息 JSON 孤儿**（现场 58 个）：洗版替换/清理只删 STRM，`<stem>-media-info.json` 留着，既占空间，还可能被同名新文件误当成「已有媒体信息」。现在 `outputs.unlink_recorded()` 的非 purge 分支也会删掉自己那份 sidecar 和 `media_info_outputs` 登记；另加每小时一次的 `purge_orphan_sidecars()`，清掉「STRM 已不存在」的 sidecar（只动登记在 `media_info_outputs` 里的，不碰别人的文件）。
- 现场核对：磁盘 1034 个 STRM / 台账 1030 条 / 7 个孤儿 STRM 全是 `/play/share/<sid>/<下划线文件名>` 格式（另一条 STRM 产线写的，不是本项目生成）；台账有但文件没了 3 条（`分享STRM未识别/50.mkv.strm`、`51.mkv.strm`、媒体库一个已删条目）。
- 排查结论（后续排期见报告）：Emby 重复条目告警 107 条（第三方产线重复入库）、Emby 投递未确认 11 条 + 部分 1 条、全量同步「扫描期间网盘变化，已跳过失效清理」3 次、播放取链 182 成功 / 9 失败（全部是已取消的分享）。
- 验证：`test_native_media_info` 19 项（含新增的「STRM 删除带走 sidecar」「只清孤儿 sidecar」两个用例）；`test_deep_delete` / `test_share_wash_alignment` / `test_phase2_share_pipeline` / `test_sync_cleanup_revision` / `test_recycle_readback` / `test_organize` 96 项通过。


## 0.9.2 — 2026-11-07

- Emby 重复条目治本：同一个播放入口在 Emby 里对应多个条目时（另一条 STRM 产线重复入库），媒体信息补扫以前直接跳过，这些文件就永远补不上媒体信息。现在先按条目路径收敛；路径完全相同的真重复稳定取 Id 最小的那个并留一次痕；路径各不相同的（可能来自另一个库）仍然不猜。
- 日志能下载了：新增 `GET /api/v1/logs/export`（按当前筛选导出最近 5000 条，纯文本附件），实时日志页加了「下载日志」按钮。
- 日志有文件了：`store.event()` 除了写 events 表，同时落一份 `data/logs/strimkeep-YYYYMMDD.log`（按天，保留 7 天）。以前 `data/logs` 一直是空目录，容器 stdout 之外没有任何留存。
- 验证：`test_native_media_info` / `test_console060` 32 项；`test_organize` / `test_phase2_share_pipeline` / `test_app` / `test_sync_notice` / `test_cards062` / `test_strm_tree` / `test_deep_delete` 109 项通过。


## 0.9.1 — 2026-11-07

- 媒体信息改成「本地优先」：`tick()` 先看本项目整理阶段自己 ffprobe 过的结果（分享库 `share_strms.media`，网盘库 `organized_files.metadata.media`），有就写 sidecar 直接完成，不再等 Emby 去拉 115 直链。原来必须先让 Emby 探测出视频流再回读确认，4K 源常常撑不过 8 次重试（约 25 分钟）后判失败——这次 295 条失败主要就是卡在这一步。
- `share_probe_item()` 扩成 `probe_item()`，同时支持分享库和网盘库；sidecar 的 `source` 从 `share_ffprobe` 改成 `ffprobe`。
- 没有本地探测结果时才回落到 Emby（PlaybackInfo + 回读），这条兜底路径保留并有用例覆盖。
- 用户自己的 sidecar 依旧不覆盖，只是不再为它反复重试。
- 验证：`test_native_media_info` 16 项；`test_emby_delivery_states` / `test_emby061` / `test_emby_prewarm` / `test_emby_scan_fallback` / `test_phase2_share_pipeline` / `test_share_wash_alignment` 51 项通过。


## 0.9.0 — 2026-11-07

- 全量同步消息去掉「附属文件或非视频 146 个」这半句：附属文件本来就不生成 STRM，每天报一次只是噪音。现在只有真的按生成规则挡掉了视频才提示「按生成规则跳过 N 个视频（不生成 STRM）」，N=0 时整行不出现。
- 版本号从这一版起按 0.9.x 走（0.8.x 收尾在 0.8.100）。
- 验证：`test_sync_notice`（含差额文案用例）/ `test_strm_tree` / `test_cards062` 26 项通过。


## 0.8.100 — 2026-11-07

- 目录同步消息里那行差额说明再拆细：`sync_scope_lines()` 用 `sync_filter_count` 把「真被规则挡掉的视频」和「附属文件/非视频」分开，写成「附属文件或非视频 146 个、按生成规则跳过 1 个视频（不生成 STRM）」，不再把两类混在一个「跳过」里。
- 现场复跑一次真实全量同步（job 613）确认：963 个视频全部「输出未变更」，STRM 更新 0、清理 0、耗时 12 秒；消息里已经没有「独立附属文件下载：0 个（跳过 47 个）」和「附属文件处理耗时」这两行，只剩「本次无变化：全部沿用已有 STRM，未重写任何文件」。
- 验证：`test_sync_notice`（含新增的差额文案用例）/ `test_strm_tree` / `test_cards062` 26 项通过。


## 0.8.99 — 2026-11-07

- 「跳过 47 个」不是失败。全量同步的附属文件统计把「本地已有同名文件、不重复下载」也算进 skipped，于是每天 03:00 的目录消息都写「独立附属文件下载：0 个（跳过 47 个）」。现场那 47 个是《贝茨旅馆》S05 的 `.ass` 字幕，本地早就存在。现在单独计 `existing`，文案改成「附属文件：新增 N 个 · 已存在 M 个」，只有新增 / 超限 / 失败时才显示这一行，纯「已存在」的日子不再出现。
- 目录消息补两行口径说明：本次没有任何变化时写「本次无变化：全部沿用已有 STRM，未重写任何文件」；`扫描文件` 与 `核对视频` 的差额写成「非视频或按规则跳过：N 个（不生成 STRM）」，不再让人猜 1110 → 963 少了什么。
- 事件日志同步改口：「附属文件新增 X 个，已存在保留 Y 个，超限或未写入跳过 Z 个，失败 W 个」。
- 现场核对：全量同步 job 583 的 963 个视频全部「输出未变更，已复用核对结果」，STRM 更新 0、清理 0、失败 0 —— 这条线本身是健康的，问题只在措辞。
- 验证：`test_sync_notice` / `test_sync_sweep` / `test_sync_orphans` / `test_sync_stale` 19 项通过。


## 0.8.98 — 2026-11-07

- 媒体信息队列收尾：STRM 被洗版替换或撤销时，`outputs.unlink_recorded()` 顺手删掉它的 `native_media_info_queue` 行，不再留下对着旧路径的重试记录。
- `NativeMediaInfo.tick()` 对「STRM 已变化」类失败直接判失败，不再按 8 次退避重试约 25 分钟：`STRM 播放身份已变化` / `分享 STRM 身份或路径已变化` / `STRM 文件不存在或路径不安全` 三种都是重试也不会变对的情况。
- 现场（0.8.97 上线后）：`POST /api/v1/share/check` 跑完 108 秒，120 个失效分享 STRM 已清理、60 个正常、0 个无法判定；`/play115/share/<sid>/<文件名>` 由 404 变为 302（活分享）/ 422「分享已取消」（死分享）；媒体信息失败行从 295 降到 69，剩余项重新排队后继续消化。


## 0.8.97 — 2026-11-07

- STRM 明细只列「新增」和「洗版替换」，其余（重写、重新发布、地址换了但集没变）一条都不列。`strm_detail_paths()` 改成按 `strm_created` / `share_wash_replaced`（或 `wash.decision=replace`）分两组：分享库在发布时判定「目标路径既没有文件、也没有任何历史登记」，网盘库在写 STRM 时判定「磁盘上没有该文件、`organized_files` 也没有同路径登记」，两条产线同一口径。
- 明细标题不再叫「新增或更新」：拆成「🆕 新增 STRM」和「🔄 洗版替换」两条消息；汇总行也从「STRM 更新：N 个」改成「新增 STRM：A 个 / 洗版替换：B 个 / 清理 STRM：C 个文件」。纯新增批次照常逐条列，混合批次只列新增和洗版那几集。
- 修分享库播放 404（现场媒体信息一直不同步的根因）：分享 STRM 里写的是 `/play115/share/<sid>/<文件名>`，路由却只注册了 `/play115/share/<sid>`，带文件名段的请求全部 404。Emby 拉不到视频流，媒体信息提取就一直失败。补上 `{name:path}` 路由，旧的裸 `/play115/share/<sid>` 保留。
- 存活检测认「分享已取消」（errno 4100010，`CLOUD_REJECTED`）：以前只认 `SHARE_NOT_FOUND`，死分享永远留着——STRM 一直 404、媒体信息重试 8 次判失败、日志里只有「暂时无法判定」。现在明确失效就撤销该 STRM，并清掉它对应的媒体信息队列行。
- 媒体信息重试失败时把原因写进事件（`<异常类型>: <消息前 80 字>`），不再只有「提取未确认」。
- 现场：295 条媒体信息失败集中在 伟大的长征（124）/ 尚公主（53）/ 璀璨之城（37）/ 嫁金钗（27）等，`/play115/share/<sid>/<文件名>` 实测 404，`/play115/share/<sid>` 取链返回「分享已取消」。不是改分享库路径导致的全库同步；是路由缺段 + 分享已取消两件事叠加。
- 验证：`test_strm_tree` / `test_cards062` / `test_phase2_share_pipeline` / `test_concurrent_pipeline` 55 项通过；`test_app` 14 项通过（旧用例的 `/play/share/` 断言同步改成 `/play115/share/`，并覆盖带文件名段的 302）。


## 0.8.96 — 2026-11-07

- 洗版批次的 STRM 明细只列真正被替换的集。此前把本组所有 `done` 条目的 `.strm` 全列出来：现场任务 589 只洗版 2 集，却发了 5 条共 47 个文件的明细，和后面对应的海报卡片完全对不上号。新增 `strm_detail_paths()`：存在洗版替换（`share_wash_replaced` 或 `wash.decision=replace`）时明细只列这些集，其余在汇总里报「同批其他 STRM：N 个（明细见任务中心）」；没有洗版替换的批次行为不变。
- 卡片「保留已有」按区间显示：`episode_text()` 把集号压成 `E01-E26`，有断层写成 `E01-E25、E27`，不再一集一段被 100 字截断（尚公主 26 集只剩 `E01…E25…`，后面的集数看不见）。
- 读不出集号的跳过项只报数量。`S01E16-1080P.mkv` 这类缺作品名的名字 `parse_name()` 会抛 `NeedsReview`，以前一律计进「本批另有 N 项未入库」，把主动保留的重复副本说成入库失败。现在先按状态归到「保留已有 / 洗版处理 / 其他跳过 / 待核查」，读不出集号的写「18 项（无集号）」；只有真的归不到本季的才提示查看任务明细。
- 任务中心同样改口：`taskPresentation` 的「另有 N 个文件因已有更好版本或同名条目未入库」改成「按洗版规则保留已有（已有更好版本、同名条目或不符合整理规则），原文件未改动」。
- 顺手修掉一个从 0.8.8x 起就失败的旧用例：`test_share_wash_requires_evidence_and_same_episode` 造旧条目时写的是已删除的 `/play/share/{sid}` 地址，`share_play_path_matches()` 只认 `/play115/share/{sid}/…`，归属校验失败 → 旧条目不撤销，用例一直红。改成新路径后该文件 16 项全过。
- 验证：新增 `test_cards062.py` 3 项（区间压缩、断层、无集号计数）、`test_strm_tree.py` 3 项（`strm_detail_paths` 洗版 / 非洗版 / `wash.decision`）、前端 `tests/taskPresentation.test.ts` 1 项。`test_cards062` / `test_strm_tree` / `test_notice_sidecars` / `test_message_templates` / `test_tg_notification_flow` / `test_telegram_error_redaction` / `test_concurrent_pipeline` / `test_phase2_share_pipeline` 全绿；前端 21 项全过。


## 0.8.95 — 2026-11-07

- 修 0.8.94 引入的降级误判：`public_policy()` 判断「是否用过已移除设置」时写成了 `raw.get('season_layout') != 'standard'`。配置里没有这个键时 `None != 'standard'` 恒为真，于是任何不显式传 `season_layout` 的策略都被当成「用过已移除设置」，`mode` 被静默降级成 `pending` —— 用户看到的现象会是「明明选了允许洗版，保存后变成转待处理」。改成 `raw.get('season_layout', 'standard')`。
- 前端每次保存都带 `season_layout`，所以界面上没暴露；但旧任务快照、脚本调用、任何不传该字段的客户端都会中招。
- 验证：线上 0.8.95 实测 PUT 一个不含 `season_layout` 的 `mode=upgrade` 配置，返回仍是 `upgrade`；恢复原配置后 `mode=upgrade, dynamic_groups=off`。76 个后端测试全过。


## 0.8.94 — 2026-11-07

- 多版本共存只留「杜比 / 非杜比」一个维度：分辨率（8K / 4K / 低于 4K）、帧率（120+ / 60+ / 低于 60）、以及「杜比 / HDR / SDR」三档全部移除。洗版页「多版本共存」从三张并排卡片收成一张，下拉只剩「不分组，一起比较」和「杜比 / 非杜比」。
- 后端 `group_key()` 只剩动态范围分组：`DoVi` / `NonDoVi` / `default`。原来多维分组用 `.` 拼成 `Below4K.DoVi.Below60` 这类复合组名，现在不会再出现。
- `UpgradePolicy` 的 `resolution_groups` / `fps_groups` / `unknown_fps_low` / `season_layout` 保留为兼容字段（旧任务快照仍能反序列化），模型层统一归零；`dynamic_groups='three'` 读入后收敛为 `off`。
- 因为不再有帧率分组，「未知帧率可以显示在低档共存组，但不足以授权洗版淘汰」这条拒绝分支删掉了；未知动态范围仍走 `UnknownDoVi` 保护路径。
- `PUT /api/v1/upgrade/config` 不再因为提交这些旧字段报 422，只校验九项规则完整性。
- 测试：`test_upgrade.py` 的多维分组用例改成杜比 / 非杜比（`test_dovi_and_non_dovi_coexist_without_recycling`），`test_quality_settings.py` 的 422 断言改成「旧字段静默收敛」，前端 `upgradeEditor.test.ts` 改成断言不再上传这些字段。76 后端 + 4 前端全绿。
- 部署：NAS 容器 0.8.94 ready；`Upgrade-*.js` 里「按分辨率分组」「120+ / 60+ / 低于 60」「杜比 / HDR / SDR」均为 0 次。


## 0.8.93 — 2026-11-07

- 去掉重复的洗版总闸：影视整理「清理与洗版开关」里的「允许使用洗版规则」删掉了（卡片改名「清理开关」）。洗版是否启用只看「洗版与多版本 → 冲突处理」选的是「转待处理 / 直接跳过 / 允许洗版」——原来两个开关叠加，关掉总闸会让冲突处理的「允许洗版」静默失效。
- 后端同步删掉 `wash_enabled` 的两处判断（原来会强制把 mode 改成 skip），现在完全由 `upgrade_policy.mode` 决定；`organize_options.wash_enabled` 字段保留兼容旧配置，但不再参与决策。
- 删掉「只读检查回收站接口」按钮及其死代码（`checkRecycle` / `recycleCheck`）。
- 洗版说明文案改短：「同片同集且内容 SHA1、精确字节数一致时，先归并并同步字幕，再回收新副本，保留归档原版。」→「同片同集按规则比较；内容完全一致时归并字幕并回收新副本。」
- 验证：容器 0.8.93 ready；`Organize-*.js` 无「允许使用洗版规则」，`Upgrade-*.js` 无「只读检查回收站接口」。
- 部署：NAS 的 StrimKeep 容器运行 0.8.93，0.8.92 镜像保留可回滚。


## 0.8.92 — 2026-11-07

- 孤儿记录核对的时间可以配了：分享 STRM 页「分享监控」多了一行「台账核对」，cron 表达式（默认 `0 5 * * *`，每天凌晨 5 点）。保存后立刻重算下次执行时间（`share_prune_due` 清零），不用等旧时间到点。
- 洗版页「多版本共存」重排：三行 label + 控件的横条改成三张并排小卡片（分辨率 / 动态范围 / 帧率），标题左、开关或下拉右，下面一行灰字说明分组规则。窄屏自动折成一列。
- 验证：容器 0.8.92 ready；`/share/settings` 返回 `share_prune_cron: "0 5 * * *"`；`ShareStrm-*.js` 含「台账核对」，`Upgrade-*.js` 里 `coexist-heading` 已全部换成 `coexist-grid`。
- 部署：NAS 的 StrimKeep 容器运行 0.8.92，0.8.91 镜像保留可回滚。


## 0.8.91 — 2026-11-07

- STRM 管理页「同步范围与失效清理」卡片底部的五条行为说明（改名/移动/云端删除/移出范围/撤销入口）是纯文档，没有任何操作，删掉了。
- 验证：容器 0.8.91 ready。
- 部署：NAS 的 StrimKeep 容器运行 0.8.91，0.8.90 镜像保留可回滚。


## 0.8.90 — 2026-11-07

- 媒体总览去掉「未读通知」统计项：站内消息入口早就没了，那个数字只会涨到几千，没有任何入口能把它清零。`notifications` 表本身保留——它还承担 Telegram 投递状态与海报卡片原位编辑的对账。
- 分享 STRM 台账核对改成 cron：默认每天凌晨 5 点（`share_prune_cron`，无效时回退默认），不再每小时跑。
- 验证：容器 0.8.90 ready；`Overview-*.js` 里「未读通知」0 次。
- 部署：NAS 的 StrimKeep 容器运行 0.8.90，0.8.88 镜像保留可回滚。


## 0.8.88 — 2026-11-07

- 解决「文件删了、台账还在」：新增 `prune_missing_share_strms()`，把 STRM 文件已不存在的记录标成 `revoked=1`（不物理删除，仍可追溯）。
- 两个入口：
  - 手动：`POST /api/v1/share/strms/prune`，分享 STRM 页「管理」区多了「清理孤儿记录」按钮（带二次确认）；
  - 自动：`Organizer.share_prune_tick()` 每小时核对一次，只清理创建超过 24 小时的记录。
- 防护：一级目录本身不存在时直接放弃本轮——挂载抖动不能当成「文件被删」，否则会把整库台账误标撤销。
- 实测：清理前手动核对过 144 条孤儿（用户手动删了「分享STRM未识别」下的文件），端点再跑一次返回 `{"removed":0}`，说明台账已干净。
- 验证：容器 0.8.88 ready；`ShareStrm-*.js` 含「清理孤儿记录」按钮。
- 部署：NAS 的 StrimKeep 容器运行 0.8.88，0.8.87 镜像保留可回滚。


## 0.8.87 — 2026-11-07

- 按用户要求**删掉全部旧播放路径兼容**，只保留新结构 `/play115/share/{sid}/{name}`：
  - `share_api.py`：移除 `/play/share/{sid}` 与 `/play/share/115/{sid}` 两条路由；
  - `main.py`：移除 `/play/{item_id}/{slug}` 里 `item_id == 'share'` 的转交分支；
  - `share_service.share_play_path_matches()`：只认 `/play115/share/`；
  - `emby.py`：路径匹配正则只认 `/play115/share/`。
- 迁移范围扩到全部 `share_strms`（原来只扫 `分享影视库/`，漏了 `分享STRM未识别/` 下的 2 条）。实测：277 条记录里 133 条存在的 STRM 全部是 `/play115/share/`，旧格式 0 条；其余 144 条是文件早已不存在的历史记录。
- 验证：`/play115/share/x` → 「分享播放链接无效或已撤销」；`/play/share/x`、`/play/share/115/x` → 「播放入口不存在或已撤销」（已落到网盘播放路由，说明兼容分支确实没了）。
- 部署：NAS 的 StrimKeep 容器运行 0.8.87，0.8.86 镜像保留可回滚。


## 0.8.85 — 2026-11-07

- 分享库播放地址改成 **`/play115/share/{sid}`**（0.8.84 是 `/play/share/115/{sid}`）：存储商前缀放最前面，和网盘侧的 `/play115/{pickcode}/...` 统一。以后接别的网盘就是 `/play115/...` 与 `/playxxx/share/...`，一眼能看出走的是哪家。
- 兼容三种写法：`/play115/share/{sid}`（新）、`/play/share/115/{sid}`（0.8.84）、`/play/share/{sid}`（更早）。三条路由都注册，Emby 路径匹配正则同时认。
- 迁移函数同时处理两种旧前缀，启动时把存量 STRM 统一换成新前缀；实测 `分享影视库/剧集/.../嫁金钗...strm` 内容已变成 `http://192.0.2.10:9000/play115/share/fff83271.../嫁金钗...`。
- 测试：8 个文件 80 个用例全过（生成断言改成新前缀，旧前缀作为输入的用例仍通过）。
- 部署：NAS 的 StrimKeep 容器运行 0.8.85，0.8.84 镜像保留可回滚。


## 0.8.84 — 2026-11-07

- 分享库播放地址加上存储商段：`/play/share/{sid}` → **`/play/share/115/{sid}`**，和网盘侧的 `/play115/...` 对齐。以后接别的网盘就是 `/play/share/alist/{sid}`，不用再猜这条地址是哪家的。
- **向后兼容**：旧路径 `/play/share/{sid}` 的路由保留，已生成的 STRM 不改也能播；Emby 路径匹配正则同时认两种写法。
- **存量自动迁移**：启动时 `migrate_share_play_paths()` 把分享影视库里已生成的 STRM 从旧前缀换成新前缀，幂等（没有旧前缀就返回 0），改过的文件登记 `share_url_refresh_pending` 等 Emby 刷新。
- 实测：迁移后 `分享影视库/剧集/.../嫁金钗...strm` 内容为 `http://192.0.2.10:9000/play/share/115/fff83271.../嫁金钗...`；`分享STRM未识别/` 下的暂存文件不在迁移范围。
- 测试：`test_share_flat_generation` / `test_share_explicit_regenerate` / `test_share_wash_alignment` / `test_phase2_share_pipeline` / `test_phase1_foundation` / `test_native_media_info` / `test_app` / `test_ops0821` / `test_emby_show_episode_verify` 全过（生成断言改成新前缀，旧路径作为输入的用例保持通过）。
- 部署：NAS 的 StrimKeep 容器运行 0.8.84，0.8.83 镜像保留可回滚。


## 0.8.83 — 2026-11-07

- 修频道监控里私密频道显示成「—」的问题：`get_entity(-100…)` 对个别目标返回的对象不带名字，`fetch` 的 title 兜底链拿不到值就一直空着，页面只能显示占位符。现在 title 为空时按 ID 扫一遍会话列表补名字。实测：`-1004419698318` 的 title 从空变成「Jasna LTX 频道」（和 ltxjasna 是同一个频道——它转私密后预览页 302，用户手动用 ID 加了进来）。
- 手动触发一轮频道检查确认 `channel_state.title` 已被真名覆盖。
- 验证：容器 0.8.83 ready；`channel_state` 三条记录的 title 都是真名。
- 部署：NAS 的 StrimKeep 容器运行 0.8.83，0.8.82 镜像保留可回滚。


## 0.8.82 — 2026-11-06

- 修「点读取我加入的频道没反应」：`dialogs()` 现在只返回频道、不带 `kind` 字段（0.8.80 回滚时一起回去了），而前端还留着 0.8.77 那句 `filter(x=>x.kind==='channel')`——结果把所有频道都滤掉，列表永远是空的。去掉这个过滤。
- 按钮补反馈：点击时转圈（`:loading`），成功提示「读到 N 个频道」，会话里没有频道提示去 Telegram 加，失败直接把接口错误显示出来（原来 `catch` 里静默清空，看起来就是没反应）。
- 验证：容器 0.8.82 ready；`Channels-*.js` 里不再有 `kind==="channel"`，含「读到」「读取频道失败」提示。
- 部署：NAS 的 StrimKeep 容器运行 0.8.82，0.8.81 镜像保留可回滚。


## 0.8.81 — 2026-11-06

- `frontend/src/Channels.vue` 回滚到 0.8.54（`751240c`）的版本，只回滚这一个板块，后端与其它页面不动。
- 0.8.54 那次「删掉重复的 Telegram 面板」把「读取我加入的频道 → 列表里挑 → 添加」这一整套选择器一起删了，只留下手填输入框。现在按 0.8.53 的模板恢复：
  - 已登录时显示「读取我加入的频道」按钮；
  - 读到的频道用搜索框 + 卡片列表展示（标题、公开/私密、ID），点「添加」直接加进规则，`source` 自动设为 `session`（私密频道只能走会话）；
  - 已添加的显示「已添加」并禁用，不会重复加。
- 保留 0.8.77 那行 `kind==='channel'` 过滤：`dialogs()` 现在返回频道/群组/机器人/用户四类，不过滤的话列表里会混进机器人。
- 验证：容器 0.8.81 ready；`Channels-*.js` 含「读取我加入的频道」与 `tg-list` 样式。
- 部署：NAS 的 StrimKeep 容器运行 0.8.81，0.8.80 镜像保留可回滚。


## 0.8.80 — 2026-11-06

- **回滚「会话监控」独立模块**（0.8.75 引入）：侧边栏入口、`/session` 路由、`SessionMonitor.vue`、`session_watch.py` 与后端挂载全部移除。频道监控的「用户会话」来源本来就能监控机器人/群组，独立一份既重复登录页，又把频道添加流程拆散了。
- 频道监控恢复原状：下拉只列频道（`kind==="channel"`），私密频道照常能选、能加；机器人等类型不再混进来。
- 修「频道名变成 -100… ID」：`telegram_session.fetch()` 拿不到 `entity.title` 时原来回退成 `name`（也就是 ID），这个 ID 被当成频道名写进 `channel_state.title` 后永远换不回真名。现在依次退到 first_name+last_name、username，都没有才留空，让调用方保留上一次的真实标题。
- 分享 STRM 洗版面板保持在卡片内（沿用 0.8.79 的结构，点收起会一起收），只补一条样式：`.inline-rules :deep(.quality-toolbar){position:static}` —— 内嵌时工具栏原来带 `position:sticky` 和负上边距，会盖住上面的说明文字。
- 验证：容器 0.8.80 ready；镜像里已无 `SessionMonitor-*.js`；`Channels-*.js` 含频道过滤；`ShareStrm-*.css` 含 toolbar 覆盖。
- 部署：NAS 的 StrimKeep 容器运行 0.8.80，0.8.79 镜像保留可回滚。


## 0.8.79 — 2026-11-06

- 修 0.8.77 引入的一处回归：分享 STRM 设置页的洗版规则面板被挪到 `SettingCard` 外面，用 `v-if="wash_enabled"` 独立控制，导致「洗版与多版本」卡片点「收起」也收不掉规则列表。现在挪回卡片内部，随卡片一起收起。
- 影视整理页的同类结构本来就是对的（`inline-rules` 在卡片内），未改动。
- 验证：前端在镜像内编译通过（`vue-tsc`）。
- 部署：NAS 的 StrimKeep 容器运行 0.8.79，健康检查 ready；0.8.78 镜像保留可回滚。


## 0.8.78 — 2026-11-06

- 洗版规则默认全部收起：`Upgrade.vue` 加载后不再把「已启用」的规则自动展开。此前一进页面就把分辨率的 6 项设置铺开，手机端要滑很久才能看到下面的规则。
- 分享设置的「套用影视整理」改成真正生效的「跟随影视整理」：直接调 0.8.77 新增的 `POST /api/v1/share/profile/follow`，删除 `share_profile` 让分享 STRM 永久沿用影视整理；已跟随时按钮显示「已跟随影视整理」并禁用。此前它只是把当前值复制进表单，必须再点「保存规则」才生效，点了看起来像没保存。
- 验证：前端在镜像内编译通过（`vue-tsc`）；`test_share_profile` / `test_share_wash_alignment` 共 9 项通过。
- 部署：NAS 的 StrimKeep 容器运行 0.8.78，健康检查 ready；0.8.77 镜像保留可回滚。


## 0.8.77 — 2026-11-06

- 分享 STRM 支持「跟随影视整理」：分享设置页新增开关——开启时删除 `share_profile`（永远沿用影视整理的目录 / 命名 / 洗版），关闭时保留当前独立配置。新增 `POST /api/v1/share/profile/follow`，`GET /share/profile` 返回 `follow` 字段。
- 分享设置的洗版规则改为随「允许使用洗版规则」开关就地展开，去掉单独的「编辑 / 收起洗版规则」按钮（`ShareProfile.vue` 移除 `rulesOpen` / `toggleRules`）。
- 日志分级补一条：`核对超时后只刷新命中的目标库` 归为 info，不再因为含「超时」被误判成错误。
- UI 打磨：分享 STRM 菜单图标换成 `Share`；`Intake` / `Logs` / `Organize` / `Tasks` / `Upgrade` / `Channels` / `SessionMonitor` / `TransferDirectories` / `ExtensionEditor` 的文案与间距统一。
- 验证：`test_share_profile` / `test_share_wash_alignment` 共 9 项通过；前端在镜像内编译通过（`vue-tsc`）。
- 部署：NAS 的 StrimKeep 容器运行 0.8.77，健康检查 ready；0.8.76 镜像保留可回滚。


## 0.8.77 — 2026-11-07

- 修 0.8.75 的两个回归：
  1. **频道监控没法添加私密频道了**：`dialogs()` 放开类型后，频道页的下拉混进了机器人/群组/用户。现在频道页只取 `kind==="channel"`，恢复原样；机器人等只在会话监控页出现。
  2. **会话监控的「从会话里选」体验差**：原来是两次弹窗（先输关键字、再回编号），现在改成正常的对话框——可搜索的列表，点一下就填入，四类目标都带类型标签。
- 验证：容器 0.8.77 ready；`SessionMonitor-*.js` 含对话框选择，`Channels-*.js` 含 `kind==="channel"` 过滤。
- 部署：NAS 的 StrimKeep 容器运行 0.8.77，0.8.76 镜像保留可回滚。


## 0.8.76 — 2026-11-07

- 会话监控去掉「只记录」动作：只剩「转存」和「分享 STRM（免转存）」。试探性观察用不上，留着只是让下拉变长。
- 修一个隐患：**关掉再开启会把关掉期间的消息全部补处理**。现在 `PATCH enabled=true` 会把 `last_message_id` 清零，走「首次接入」分支，从下一条新消息开始，不补历史。
- 「启用」开关本来就有（表格每行一个），关掉后该目标不再出现在轮询里，也不读消息。
- 验证：容器 0.8.76 ready；`/session/watch` 的 actions 只剩 transfer / strm。
- 部署：NAS 的 StrimKeep 容器运行 0.8.76，0.8.75 镜像保留可回滚。


## 0.8.75 — 2026-11-07

- 新增独立模块「会话监控」（侧边栏媒体组，路由 `/session`）：用 Telegram 用户会话盯住**任意**对话的新消息，不再只限频道。
  - 监控目标可以手动填 `@用户名` 或数字 ID，也可以「从会话里选」（先按关键字筛，再回编号），候选项现在包含频道 / 群组 / 机器人 / 用户四类。
  - 每个目标单独选动作：转存（分享链接走转存，磁力/电驴/直链走离线）、分享 STRM（免转存）、只记录（只写日志不动网盘）。
  - 首次接入只记游标，从「下一条新消息」开始处理——不会把历史消息一次性全部入库。
  - 页面上直接可完成会话登录（api_id/api_hash/手机号 → 验证码 → 两步验证），登录状态与频道监控共用同一份 StringSession。
- 后端 `session_watch.py`：新表 `session_watch` / `session_watch_log`，`SessionWatch.tick()` 挂在调度循环里；新增 `GET|POST /api/v1/session/watch`、`PATCH|DELETE /api/v1/session/watch/{id}`。新增目标时会先试读一次，读不到立刻撤销并报错，避免写错用户名在后台每轮刷错误。
- `telegram_session.dialogs()` 放开类型过滤，返回 `kind`（channel/group/bot/user），机器人对话终于能选到了。
- 「转发到我的机器人」在实现上等价于把消息里的链接直接交给转存管线：效果和自己发给机器人一样，但不需要机器人参与，也不占一次往返。
- 验证：容器 0.8.75 ready；`/session/watch` 实测返回 `{items: [], actions: {transfer, strm, record}, log: []}`；频道监控、机器人通知相关测试全过。
- 部署：NAS 的 StrimKeep 容器运行 0.8.75，0.8.74 镜像保留可回滚。


## 0.8.74 — 2026-11-07

- 删掉 `/dirs`（查看目录 ID）：保存目录都在网页里配好了，机器人里再查 ID 用不上；帮助里那句「/offline 目录ID 链接 可指定保存目录」也是假的——代码从来没解析过这个参数，一并删掉。
- 新增两个指令：
  - `/tasks` — 最近 8 个任务（编号 · 类型 · 状态），比 `/status` 只给计数更实用。
  - `/sign` — 115 签到，直接复用 `cloud.sign()`。
- 机器人指令可以在网页里开关了：系统设置 → Telegram 通知 → 「机器人指令」，每个指令一个开关，保存到 `bot_commands_config`（只存关掉的那些，将来新增指令默认可用）。关掉的指令在私聊里会回一句「指令已关闭」。
- 新增 `GET|PUT /api/v1/bot/commands`；菜单 `setMyCommands` 升到 v4（去掉 dirs、加上 tasks 和 sign）。
- 验证：容器 0.8.74 ready；`/bot/commands` 实测 GET 返回 8 个指令、PUT 保存后开关生效（测完已全部恢复为开启）。
- 部署：NAS 的 StrimKeep 容器运行 0.8.74，0.8.73 镜像保留可回滚。


## 0.8.73 — 2026-11-07

- 清掉所有通知里的「后续」行：转存回执、频道通知（转存 / STRM / 离线）、转存完成、机器人分享 STRM 回执，一律不再追一句「转存完成后等待手动或定时整理」这类套话。
- 回收站通知去掉「📤 来源  手动清空 / 定时清空」：谁触发的用户自己清楚。`cloud_recycle_notice()` 相应去掉 `manual` 参数。
- 保留的仍是有信息量的行：转存完成里的整理任务号改成 `📋 整理任务  #9`（原来是「⏳ 后续  整理任务 #9 已入队」），离线提交的 `⏳ 状态  已提交 115，下载结果待确认` 保留，频道的扫描配置警告保留。
- 修 0.8.72 的一处误删：当时按「📌 后续」整行过滤，把离线分支那行同时带 `⏳ 状态  已提交 115，下载结果待确认` 的内容一起删掉了，现在补回来（`test_offline_submission_is_not_reported_as_completed` 依赖它）。
- 测试：`test_message_templates.py` 的两处断言跟着改（`定时整理` → `转存任务`、`已入队` → `整理任务`），连同 `test_cards062.py`、`test_tg_notification_flow.py` 全过。
- 验证：容器 0.8.73 ready。
- 部署：NAS 的 StrimKeep 容器运行 0.8.73，0.8.72 镜像保留可回滚。


## 0.8.72 — 2026-11-07

- 去掉签到与回收站通知里的「📌 后续」那一行：标题已经写明结论（已签到/今天已签/已清空），再补一句「明天再签即可」「删除的文件不可恢复」是废话。
- 现在的文案：

  ```
  ☑️ 115 签到 · 已完成
  📅 日期  2026-11-07
  🔥 连续  7 天
  ```

  ```
  ☑️ 115 签到 · 今天已签
  📅 日期  2026-11-07
  ```

  ```
  🧹 115 回收站 · 已清空
  🗑 清理  12 个文件
  🕒 时间  11/07 04:00
  📤 来源  手动清空
  ```

- 验证：镜像内直接调用模板函数确认输出；容器 0.8.72 ready。
- 部署：NAS 的 StrimKeep 容器运行 0.8.72，0.8.71 镜像保留可回滚。


## 0.8.71 — 2026-11-07

- 签到与回收站的 Telegram 文案改成与其它通知一致的样式（emoji + 两空格对齐 + 空行 + 「📌 后续」），不再是一句话重复标题。
- 新增 `message_templates.cloud_sign_notice()` / `cloud_recycle_notice()`，手动与定时触发共用：

  ```
  ☑️ 115 签到 · 已完成
  📅 日期  2026-11-06
  🔥 连续  7 天

  📌 后续  明天可再次签到
  ```

  ```
  🧹 115 回收站 · 已清空
  🗑 清理  12 个文件
  🕒 时间  11/07 04:00
  📤 来源  手动清空

  📌 后续  回收站已空，删除的文件不可恢复
  ```

  已签到时标题变「· 今天已签」、后续变「明天再签即可」；回收站本来为空时后续写「回收站本来就是空的」。日期/时间按设置里的时区格式化。
- 验证：容器 0.8.71 ready；实测签到后 `notifications` 表记录为 `☑️ 115 签到 · 今天已签` + `📅 日期 2026-11-06`，`telegram_state='sent'`。
- 顺带清理：NAS 上那个字面名为 `*` 的空目录又出现了（`cd /vol1/1000/*/StrimKeep-dev` 未展开时误建），已再次删除。
- 部署：NAS 的 StrimKeep 容器运行 0.8.71，0.8.70 镜像保留可回滚。


## 0.8.70 — 2026-11-07

- **修签到调错接口**：`user_sign` 是「个性签名」设置（返回 HTML 页面，所以页面上一堆乱码），真正的每日签到是 `user_points_sign`（查询是否已签）+ `user_points_sign_post`（签到）。现在先查状态，已签到直接回「今天已经签到过了」，不再抛乱码。实测返回 `{"signed":false,"message":"今天已经签到过了。"}`。
- 安全密钥改成密码输入框（`type=password` + 可切换显示），不再明文。
- 提示文字加 `word-break`/`overflow-wrap`，异常文本不会撑破卡片边框。
- 新增 TG 通知分类 `cloud`（115 签到与回收站），默认开启；系统设置 → Telegram 通知里可单独关。签到、清空回收站的手动与定时触发都会推送：
  - 签到：`✅ 115 签到完成` / `ℹ️ 115 签到`（已签到时），带连续天数（115 有返回时）。
  - 清空：`🧹 115 回收站已清空`，手动触发时带清空前的条目数。
- 验证：容器 0.8.70 ready；签到端点实测正常；`/cloud/recycle` 返回 `{"available":true,"info":{"count":0}}`。
- 部署：NAS 的 StrimKeep 容器运行 0.8.70，0.8.69 镜像保留可回滚。


## 0.8.68 — 2026-11-07

- 115 账号页新增「签到与回收站」板块：
  - 每日签到：手动「立即签到」+ 定时签到（Cron，默认 `0 9 * * *`）。重复签到按「今天已经签到过了」正常返回，不当失败。
  - 回收站：显示当前条目数、上次清空时间；手动「立即清空回收站」（二次确认）+ 定时清空（Cron，默认 `0 4 * * 0`）。
- 后端新增 `POST /cloud/sign`、`GET /cloud/recycle`、`POST /cloud/recycle/clear`、`GET|PUT /cloud/maintenance`；`cloud115` 新增 `sign()` / `recycle_info()` / `recycle_clear(password)`。
- 回收站条数只读第一页拿 `count`（`recyclebin_list`），不拉全量清单；`count` 用既有的 `exact_number` 解析，兼容 115 返回字符串。
- 清空走 `recyclebin_clean`：115 要求安全密钥（账号安全里的 6 位数字），页面上可填，默认 `000000`。
- 调度：`organizer.maintenance_tick()` 挂在 `schedule_loop` 里，签到与清空各自按 Cron 独立执行、互不影响；失败只写事件日志，不打断其它调度。改设置时把 `cloud_sign_due` / `cloud_recycle_due` 清零，避免旧的 due 先跑一次。
- 验证：容器 0.8.68 ready；`Account-*.js` 含「签到与回收站」「安全密钥」「立即签到」；`test_console060` / `test_ops0840` / `test_ops0841` 全过。
- 部署：NAS 的 StrimKeep 容器运行 0.8.68，0.8.67 镜像保留可回滚。


## 0.8.65 — 2026-11-07

- 影视整理页的 11 张设置卡片全部改为**默认收起**（整理执行 / 扫描来源与归档目录 / 未识别目录 / 需要处理的文件类型 / 重命名策略 / 忽略策略 / 分类策略 / 清理与洗版开关 / 洗版与多版本 / STRM 与 Emby 通知 / 整理联动），不再一进页面就是一大片平铺。
- 与 0.8.64 的分享 STRM 保持一致：点标题展开，再点收起。
- 验证：容器 0.8.65 ready；`Organize-*.js` 里 `"default-open":!1` 出现 11 次、`!0` 0 次。
- 部署：NAS 的 StrimKeep 容器运行 0.8.65，0.8.64 镜像保留可回滚。


## 0.8.64 — 2026-11-07

- 分享 STRM 页去掉右上角的「整理设置 / 返回分享」整体切换按钮：设置不再藏在一个按钮后面整页切换，而是常驻页面底部。
- 整理规则的 5 张卡片恢复**默认折叠**（文件筛选 / 重命名与媒体信息 / 分类 / 洗版与多版本 / STRM 与 Emby 通知），需要时再点开，避免一进页面就是几十行平铺。0.8.62 把它们改成全展开是理解错了需求，这里改回来。
- 页面顺序：解析分享链接 → 分享内容（选片生成 STRM）→ 分享内容（管理已生成的 STRM）→ 整理规则 + 分享监控。
- 验证：容器 0.8.64 ready；`ShareStrm-*.js` 里「返回分享」0 次、5 个卡片都是 `"default-open":!1`；页面说明里出现「页面底部」。
- 部署：NAS 的 StrimKeep 容器运行 0.8.64，0.8.63 镜像保留可回滚。


## 0.8.63 — 2026-11-07

- 扩展名编辑器增删更直接：常用格式全部列出并做成开关，点一下添加、再点一下移除（已选的显示 `✓` 并高亮），不用再去点小叉；标题栏加「清空」按钮。影视整理、分享 STRM、STRM 同步三处共用这个组件，同时生效。
- 去掉 ISO 的硬编码，不再按后缀写死例外：
  - `organizer.py`：主流程探测不再排除 `.iso`；预取探测也不再跳过 `.iso`。
  - `share_service.py`：分享整理探测不再排除 `.iso`（该流程本来就有「探测失败退回文件名」的兜底）。
  - `media.py`：命名时是否禁用音频规则，改为看有没有音轨信息，而不是看 `.iso` 后缀。
- 行为不变的部分：整理主流程里探测失败仍按既有规则转待处理（`test_probe_failure_quarantines_original_and_associated_subtitle` 保持原样）——这一条对所有格式一致，没有为 ISO 单开分支。
- 文案：影视整理的扩展名说明改成「需要处理哪种格式就把它加进列表（例如 ISO 原盘），和其他视频走同一条流程；探测读不出来时自动按文件名解析规格」；「提取媒体信息完善命名」的说明去掉「ISO 始终跳过探测」。
- 测试：`test_iso.py` 的第一个用例改为断言「加进扩展名后照常探测」（`media_origin == 'probed'`、发出 1 次 ffprobe），文件头说明同步改写；`test_organize.py` / `test_probe_prefetch.py` / `test_archive.py` / `test_unrecognized063.py` / 识别相关用例全部通过。
- 验证：容器 0.8.63 ready；`Organize-*.js` 里「ISO 始终跳过」0 次、「需要处理哪种格式」1 次；`ExtensionEditor-*.js` 含「清空」与 `aria-pressed`。
- 部署：NAS 的 StrimKeep 容器运行 0.8.63，0.8.61 镜像保留可回滚。


## 0.8.61 — 2026-11-07

- **纠正 0.8.60 的错误改动**：0.8.60 误把海报卡片（`media`）一起下线了。海报卡片必须保留——它自带入库状态，`drain_media()` 会把卡片上的「⏳ 待核实」原位编辑成「✅ 已入库」，这本来就是「不需要再单独开 Emby 入库检查通知」的原因。
- 同时恢复 `metadata`：它不是死开关，控制 STRM 汇总里「🧩 文件：…」那行统计（`notifications.py` 的 `file_line`）。
- 现在只下线 `task`（普通整理任务完成回执）：默认一直关着、从来没人开过，页面不再提供开关，`RETIRED_CATEGORIES=('task',)` 强制关闭防止旧配置残留的 `true` 把它打开。
- 通知分类恢复为 7 项：入库海报与媒体信息 / 明细里的 .strm/.nfo/.json 统计 / 转存结果 / STRM 同步汇总 / STRM 文件明细 / 失败与登录异常 / Emby 入库检查结果。
- 测试：`tests/test_cards062.py` 回到原版（`test_actual_notification_payload_uses_card_and_respects_disabled_details` 断言发出 `sendPhoto`），连同 `test_console060.py`、`test_alignment_0713.py` 全过。
- 验证：容器 0.8.61 ready；`Settings-*.js` 里七个分类各命中 1 次，「普通任务结束消息」0 次。
- 部署：NAS 的 StrimKeep 容器运行 0.8.61，0.8.60 镜像保留可回滚。


## 0.8.60 — 2026-11-07

- 下线三个通知分类：`media`（入库海报与媒体信息）、`metadata`（明细统计）、`task`（普通任务结束消息）。系统设置的「选择要发送的消息」从 8 项减到 5 项：转存结果 / STRM 同步汇总 / STRM 文件明细 / 失败与登录异常 / Emby 入库检查结果。
- 为什么下线 `media`：海报已经由 `metadata_files.py` 把 `poster.jpg` / `fanart.jpg` 写进 STRM 目录、`cover_service.py` 补图，Emby 重扫自然展示，不需要再推 Telegram 卡片。
- 为什么下线 `task`：它是普通整理任务成功完成的回执（`notifications.py` 里 job 结束的 else 分支），但 `CATEGORY_DEFAULTS` 里一直是 `False`，所以从来没发过，页面上的开关等于摆设。
- 为什么下线 `metadata`：`category_for()` 从不产出这个分类，也没有任何 `publish(category='metadata')`，是完全没人用的死开关。
- 实现方式：新增 `RETIRED_CATEGORIES=('media','metadata','task')`，`publish()` 组装分类后强制置 `False`。不改数据库：通知分类是 Fernet 加密存在 `settings` 表里的，直接改库有风险，而且旧配置里残留的 `true` 会被这里的强制关闭覆盖掉。
- 关键约束：`media` 的 `publish` 调用**必须保留** —— `drain_media()` 靠 `notifications` 里那行（状态 `disabled`）判断任务已处理，才会删掉 `media_notice_outbox`；把调用删了会让 outbox 每轮重查、永不清理。
- 测试：`test_cards062.py` 的海报卡片用例改为 `test_media_card_notice_is_retired_but_outbox_drains`，断言不再发 `sendPhoto`、通知行状态为 `disabled`。
- 验证：容器 0.8.60 ready；`Settings-*.js` 里「入库海报与媒体信息」「普通任务结束消息」「明细里的 .strm」均为 0 次，「Emby 入库检查结果」「STRM 同步汇总」各 1 次；容器内 `RETIRED_CATEGORIES` 与默认值确认生效。
- 部署：NAS 的 StrimKeep 容器运行 0.8.60，0.8.59 镜像保留可回滚。


## 0.8.59 — 2026-11-07

- 「Telegram 接收链接」（启用转存机器人、允许操作的 TG 用户数字 ID、连接状态）从「资源入库」页搬到「系统设置 → Telegram 通知」，Telegram 相关配置集中在一处；资源入库页只留一句指引。
- `transfer.bot_enabled` / `allowed_user_id` 仍留在资源入库页的配置对象里透传：`PUT /transfer/config` 是整份替换，保存「落盘后处理」时必须原样带回，否则会把机器人开关写回 false。
- 系统设置新增「保存接收设置」：先 GET `/transfer/config` 取全量，只覆盖这两个字段再 PUT，避免把转存目录等其它配置写空。
- 删除 NAS 上误建的空目录 `/vol1/1000/*/StrimKeep-dev`（目录名字面量就是星号，0 个文件），它让 `cd /vol1/1000/*/StrimKeep-dev` 报 `too many arguments`。
- 验证：容器 0.8.59 ready；前端产物里「Telegram 接收链接」只出现在 `Settings-*.js`，`Intake-*.js` 里 0 次（`启用转存机器人` 同样 0 次）。
- 部署：NAS 的 StrimKeep 容器运行 0.8.59，0.8.58 镜像保留可回滚。


## 0.8.58 — 2026-11-07

- 「落盘后处理」从 2×2 改成 **3×3**，列与「保存目录」对齐：115 分享 / 磁力·直链 / ED2K，行仍为 Web / 机器人。磁力与 ED2K 从此各自开关，直链跟随磁力。
- 后端新增 `magnet_auto_organize` / `ed2k_auto_organize` / `bot_magnet_auto_organize` / `bot_ed2k_auto_organize`（`bool | None`），`None` 时在 `directory_config` 里回退到旧的 `auto_organize` / `bot_auto_organize`，老配置不会因升级变行为。新增 `organize_flag_for(cfg, kind, origin)` 统一取值。
- 三个入口接新开关：`POST /offline/submit`（单类型）、`POST /intake`（按磁力/ED2K/直链分组）、Telegram 机器人离线提交。频道监控仍固定 `organize=False`（走规则里的 `organize_after`）。
- 资源入库的「最近记录」里，转存任务也显示时间（`fmtSub(job.updated)`），与离线提交的 `submitted_at` 对齐；此前只有任务编号。
- 系统设置 → Telegram 通知页的底注修正：接收链接与白名单已在「资源入库」页的「Telegram 接收链接」，不再指向已不存在的「115 账号配置 → 链接转存」。通知页与接收链接不重复：前者管推送，后者管提交。
- 修复 `tests/test_tg_notification_flow.py` 的遗留断言：`setup_bot` 未设 `bot_auto_organize`，而 0.8.47 已把默认改成「只落盘」，断言 `organize is True` 必然失败（已用 `git stash` 在 0.8.57 代码上复现，确认不是本版引入）。现显式设 `bot_auto_organize=True` 验证回退链，另新增 `test_bot_per_kind_organize_switches` 验证磁力/ED2K 独立开关优先于总开关。
- 停用兜底扫描后，`tests/test_ops0840.py`（3 个兜底扫描用例）与 `tests/test_ops0841.py`（扫描节奏用例）
  改为断言「已停用」：`offline_scan_tick()` 不访问云盘、不写 `offline-scan:*` 任务、`scan_minutes` 不再驱动扫描。
- 验证：verify 分组全过 —— 核心流水线 / 归档与写入 / 同步与巡检 / 识别与命名 / 身份与上下文 /
  未识别与字幕清理 / 通知卡片（修复后）/ 媒体信息 / 离线与探索 / 自检与计数。
- 部署：NAS 的 StrimKeep 容器运行 0.8.58，`/health/ready` ready；前端产物确认含 3×3 表格（`Intake-*.css` 含 post-table）。


## 0.8.57 — 2026-11-07

- 停用离线目录兜底扫描（`organizer.offline_scan_tick` 直接返回）：第一阶段约定「不得使用离线兜底逻辑跨目录整理」，离线落盘后是否整理只由「落盘后立即整理」开关或扫描模块的手动/定时扫描决定。原实现保留在 git 历史里。
- 删掉资源入库页的「离线目录扫描节奏」设置（兜底扫描已停用，该设置不再有意义）。
- 「落盘后处理」从四行竖排开关改成 **2×2 表格**（入口 × 类型）：Web / 机器人两行，分享链接转存 / 磁力·ED2K·直链两列。此前四个开关竖排，看不出哪个管哪个。
- 验证：容器 0.8.57 healthy；前端产物确认包含新样式（`Intake-*.css` 含 post-table）。
- 部署：NAS 的 StrimKeep 容器运行 0.8.57，健康检查 ready；0.8.56 镜像保留可回滚。


## 0.8.56 — 2026-11-07

- 资源入库页去掉顶部四个目录按钮（转存 / 直链 / 磁力 / ED2K）：下方「保存目录」聚合表本来就有 Web / 机器人两行、三列类型，点「编辑」就能改，不需要顶部重复一份。
- 「保存目录」聚合表隐藏「Telegram 频道」行：频道的落盘目录只在频道监控页配置，且分享免转存模式没有 115 落盘（只生成本地 STRM），在这里显示「免转存 STRM + 未设置目录」是误导。
- 验证：前端在镜像内重新编译；容器 0.8.56 healthy。
- 部署：NAS 的 StrimKeep 容器运行 0.8.56，健康检查 ready；0.8.55 镜像保留可回滚。


## 0.8.55 — 2026-11-07

- 资源入库页合并成一个输入框：粘贴任意链接（115 分享 / magnet / ed2k / http 直链，可混贴），后端 `POST /api/v1/intake` 自动按类型拆分——分享走转存、磁力/ED2K/直链走离线，各自落到对应目录并应用关键词分流。此前是两个 tab 分开提交，且 `/offline/jobs` 明确拒绝混合类型。
- 结果逐项返回：`{share:[...], offline:[...], errors:[...]}`，某一类失败不影响其它类。
- 目录设置收敛为四个按钮（转存 / 直链 / 磁力 / ED2K），点哪个改哪个。
- 验证：容器 0.8.55 healthy；未做真实提交测试。
- 部署：NAS 的 StrimKeep 容器运行 0.8.55，健康检查 ready；0.8.54 镜像保留可回滚。


## 0.8.54 — 2026-11-07

- 频道新增「首次回溯」页数配置（1/3/5/10 页，约 20~200 条），公开与私密频道都生效：首次接入按该页数往回翻，之后仍按游标增量。此前首次固定只读 1 页（约 20 条），私密频道积压的历史分享只能处理最近 20 条。
  - `ChannelRule` 加 `history_pages`（1-20，默认 1）；`refresh()` 首次分支按它决定翻页次数，`MAX_PAGES` 仍限制单轮上限。
- 修 `Channels.vue` 里两个 Telegram 会话面板重复的问题（dialog 版与 inline 版并存），保留 dialog 版。
- 验证：容器 0.8.54 healthy。
- 部署：NAS 的 StrimKeep 容器运行 0.8.54，健康检查 ready；0.8.53 镜像保留可回滚。


## 0.8.53 — 2026-11-07

- 用户会话读取补上隐藏超链接：Telegram 帖子里「显示文字 + 隐藏 URL」的链接（`MessageEntityTextUrl`）不在 `message.message` 里，只读文本会漏掉 115 分享。现在同时从 `entities` 提取 `MessageEntityTextUrl` 与 `MessageEntityUrl`，塞进 `links`，`consume()` 本来就把 text + links 合并后统一解析，下游无需改动。
- 验证：容器 0.8.53 healthy。
- 部署：NAS 的 StrimKeep 容器运行 0.8.53，健康检查 ready；0.8.52 镜像保留可回滚。


## 0.8.52 — 2026-11-07

- 修私密频道读取报 `Cannot find any entity corresponding to "-100…"`：
  1. `str(name).isdigit()` 对带负号的频道 id 返回 False，于是把整个字符串当成 username 去查。现在去掉符号再判断并转 `int`。
  2. 容器重启后 Telethon 实体缓存为空，直接 `get_entity(int)` 会 `ValueError`。现在捕获后先遍历一次 `iter_dialogs()` 补上 access_hash，再取一次。
- 验证：容器 0.8.52 healthy；私密频道需现场点「立即检查一轮」复核。
- 部署：NAS 的 StrimKeep 容器运行 0.8.52，健康检查 ready；0.8.51 镜像保留可回滚。


## 0.8.51 — 2026-11-07

- 修「添加到监控」保存时报「输入格式不正确」：`normalize_channel` 只认公开频道用户名，私密频道的数字 id（`-100…`）在 `strict=True` 下被拒。现在允许数字 id；同时 `ChannelRule` 增加反向校验——`source='preview'` 时数字 id 仍拒绝，避免拿预览页去读没有用户名的频道。
- 私密频道列表改成紧凑网格（`auto-fill minmax(300px,1fr)`，宽屏 3–4 列、移动端单列、最高 420px 滚动），加搜索框（按标题或 id 过滤）与「已添加」置灰。此前几十个频道以大卡片单列排列，一屏只能看几个。
- 验证：`test_channel_monitor` + `test_destination_matrix` 共 38 项通过（预期字典补 `source` 字段）。
- 部署：NAS 的 StrimKeep 容器运行 0.8.51，健康检查 ready；0.8.50 镜像保留可回滚。


## 0.8.50 — 2026-11-06

- 新增「Telegram 用户会话」读取来源，用于私密频道：
  - `telegram_session.py`（Telethon 1.45）：`send_code` / `verify` / `password` / `logout` / `dialogs` / `fetch`。只读（`get_entity` / `get_messages` / `iter_dialogs`），不发送、不删除、不改频道。
  - `StringSession` 与 `api_id/api_hash/phone` 都用现有 Fernet 加密存 settings；登录中的临时客户端只在内存，进程重启后需重新走验证码。
  - 频道规则新增 `source: preview | session`；`refresh()` 按来源分派，`session` 走 MTProto，产出结构与预览页一致，去重 / 解析 / 落盘 / 扫描授权全部复用。
  - 新增 6 个端点：`GET /telegram/session`、`POST /telegram/session/{code,verify,password,logout}`、`GET /telegram/session/dialogs`。
  - 前端频道页新增「Telegram 用户会话」面板（登录 / 退出 / 已加入频道预览）与三步登录弹窗（api 凭据 → 验证码 → 二步密码）；每条频道增加「读取来源」选择。
- 依赖：新增 `telethon==1.45.0`（+ `pyaes` / `pyasn1` / `rsa`），镜像 720MB → 735MB。
- 验证：镜像重建后容器 healthy，`/health/ready` 返回 0.8.50。**未加自动化测试**——Telethon 需要真实网络与账号，打桩测试待补。
- 部署：NAS 的 StrimKeep 容器运行 0.8.50，健康检查 ready；0.8.49 镜像保留可回滚。


## 0.8.50 — 2026-11-07

- 新增「私密频道 · Telegram 用户会话」：用个人账号（MTProto / Telethon）读取私密频道，补上公开预览页读不到的场景（`t.me/s/xxx` 返回 302 的频道就是这类）。
  - 后端新增 `telegram_session.py`：三步登录（api_id/api_hash + 手机号 → 验证码 → 二步密码）；StringSession 与 api 凭据都用现有 Fernet 加密存 settings；登录中的临时客户端只在内存，重启需重新验证。
  - 新增 6 个端点：`GET /api/v1/telegram/session`、`POST .../code`、`.../verify`、`.../password`、`.../logout`、`GET .../dialogs`。
  - 频道规则新增 `source: 'preview' | 'session'`；`refresh()` 按来源分派，会话读取产出与预览页完全相同的结构，下游去重、解析、落盘、扫描授权全部复用。
  - 只读：仅使用 `get_entity` / `get_messages` / `iter_dialogs`，不发送、不删除、不改频道权限。
- 频道页补上会话面板：未登录时填 api_id/api_hash/手机号走三步验证；登录后可「读取我加入的频道」，一键加入监控（`source=session`）。私密频道没有 username，用数字 id 读取。
- 依赖新增 `telethon==1.45.0`（+ pyaes / pyasn1 / rsa），镜像 720MB → 735MB。
- 验证：容器 0.8.50 healthy；`/api/v1/telegram/session` 等端点返回 401（存在、需登录）。**未做真实 Telegram 登录测试**（需要你自己的 api_id/api_hash 与验证码）。
- 部署：NAS 的 StrimKeep 容器运行 0.8.50，健康检查 ready；0.8.49 镜像保留可回滚。


## 0.8.49 — 2026-11-06

- 「链接转存」与「离线下载」合并为单页「资源入库」：顶部标签切换「分享链接（115 转存）」和「磁力 / ED2K / 直链（离线下载）」，两个入口在同一页；侧边栏只保留「资源入库」，`/offline` 重定向到 `/transfer`。
- 「落盘后处理」四组开关并列：Web 转存 / 机器人转存 / Web 离线 / 机器人离线，**默认全部关闭**（只落盘，由「影视整理」的手动/定时扫描决定）。
- 目录配置集中在一页：转存目录沿用 `TransferDirectories`；直链 / 磁力 / ED2K 三个目录用 `DirectoryPicker` 就地选择；离线目录的扫描节奏（0/5/10/30 分钟）也放在这里。
- 记录区合并显示最近转存任务与离线提交，保留「放弃 7 天前的待核查」。
- 后端接口未改动（`/transfer/*` 与 `/offline/*` 保持原样），只合并前端信息架构；`Transfer.vue` / `Offline.vue` 保留未删，便于回滚。
- 验证：前端在镜像内重新编译通过（`vue-tsc` 无错）。
- 部署：NAS 的 StrimKeep 容器运行 0.8.49，健康检查 ready；0.8.48 镜像保留可回滚。


## 0.8.48 — 2026-11-06

- 离线提交新增「放弃超期待核查」：`POST /api/v1/offline/submissions/expire?days=7` 把超过 7 天仍未确认产物的 `needs_review` 记为 `expired`（新状态），离线页「提交记录」加了对应按钮。现场 125 条历史待核查（全部 ≤ 11-05，0.8.40 之前）可一键清掉，不再长期挂在列表里。
- 修 `test_destination_matrix.py::test_bot_mixed_links_use_independent_destinations`：测试桩 `Offline.submit()` 缺 `organize` 与 `origin` 参数，实际调用带这两个关键字导致 TypeError 被吞、断言为空。补齐后该用例通过——是测试桩过期，不是产品缺陷。
- 验证：`test_destination_matrix.py` 6 项通过；频道相关组 42 项仍全绿。
- 部署：NAS 的 StrimKeep 容器运行 0.8.48，健康检查 ready；0.8.47 镜像保留可回滚。


## 0.8.47 — 2026-11-06

- 转存 / 离线 / 频道改为三套独立配置，各自决定「落盘后是否立即整理」，并把 Web 与 Telegram 机器人分开：
  - 转存：`auto_organize`（Web）/ `bot_auto_organize`（机器人），目录同样分开（`target_cid` / `bot_target_cid`）。
  - 离线：`auto_organize`（Web）/ `bot_auto_organize`（机器人）。
  - 频道：每条规则新增 `organize_after`，控制转存与磁力/ED2K 落盘后是否立即入队。
- 所有「落盘后立即整理」默认关闭（只落盘，交给「影视整理」的手动/定时扫描），修掉此前「Web 转存默认 False、机器人离线默认 True」的不一致。
- 「立即整理」仍受扫描授权约束：目标目录不在待整理范围内时只落盘。
- 验证：`test_channel_monitor` / `test_destination_matrix` / `test_channel_skip_offline` 共 39 项通过（`test_bot_mixed_links_use_independent_destinations` 为既有失败，干净基线同样失败）。
- 部署：NAS 的 StrimKeep 容器运行 0.8.47，健康检查 ready；0.8.46 镜像保留可回滚。


## 0.8.46 — 2026-11-06

- 「115 网盘落盘」只保留一个目标目录：分享转存、磁力、ED2K 统一落到这个目录，删掉前端单独的「磁力落盘」「ED2K 落盘」两个选择器。后端 `normalize_rule` 在模式 A 下把磁力/ED2K 目录默认取转存目录（显式配置过的旧值仍然保留）。
- 与文档「三种资源统一使用用户配置的 115 落盘目标目录」对齐。
- 验证：`test_channel_monitor` / `test_destination_matrix` 共 37 项通过；`test_bot_mixed_links_use_independent_destinations` 仍是既有失败（干净基线同样失败）。
- 部署：NAS 的 StrimKeep 容器运行 0.8.46，健康检查 ready；0.8.45 镜像保留可回滚。


## 0.8.45 — 2026-11-06

- 「分享 STRM 落盘」模式下隐藏磁力 / ED2K 落盘区块。后端在 strm 模式下本来就会清空 `magnet_cid` / `ed2k_cid`（只有 115 网盘落盘才收），前端却仍显示两个目录选择器，选完保存即丢失。现在两端口径一致。
- 验证：前端在镜像内重新编译；`test_channel_monitor` / `test_destination_matrix` 结果与 0.8.44 一致（37 项通过，1 项既有失败）。
- 部署：NAS 的 StrimKeep 容器运行 0.8.45，健康检查 ready；0.8.44 镜像保留可回滚。


## 0.8.44 — 2026-11-06

- 频道落盘模式收敛为两种：**115 网盘落盘**（115 分享转存 / 磁力 / ED2K，共用目标目录与关键词分流）与**分享 STRM 落盘**（免转存）。去掉第三种「不处理分享」选项。
- 后端同步收敛：`share_mode` 只接受 `strm` / `transfer`；旧配置的 `none` 与 legacy `mode='offline'` 一律归一为 `transfer`（转存目录为空即不处理分享，行为等价）。
- 校验放宽：「115 网盘落盘」只要分享转存 / 磁力 / ED2K 三者配置了任意一个即可保存，不再强制要求分享转存目录。
- 验证：`test_channel_monitor` / `test_destination_matrix` 共 38 项通过；`test_bot_mixed_links_use_independent_destinations` 在干净 0.8.43 基线上同样失败，属既有问题。
- 部署：NAS 的 StrimKeep 容器运行 0.8.44，健康检查 ready；0.8.43 镜像保留可回滚。


## 0.8.43 — 2026-11-06

- 磁力 / ED2K 接入频道关键词分流：命中 `routes` 时用分流目录；未命中仍用各自配置的目录（`magnet_cid` / `ed2k_cid`）。此前只有 115 分享转存走分流，磁力与电驴固定用单一目录。
- 分流只认「命中」，未命中不回退到统一离线目录——保持「分享 STRM 模式跳过同帖磁力链接」的既有行为。
- 验证：`test_channel_monitor.py` 32 项通过（新增 2 项：磁力命中分流目录、ED2K 共用同一分流规则）。
- 部署：NAS 的 StrimKeep 容器运行 0.8.43，健康检查 ready；0.8.42 镜像保留可回滚。


## 0.8.42 — 2026-11-06

- 扫描范围唯一授权：能否整理只看目标目录是否在「待整理目录」（`source_cids`）范围内，落盘本身永远成功。新增 `allowed_scan_cids()` / `scan_scope_allows()`，用 115 真实父子链（`cloud.directory`）判断，不做字符串前缀比较；读不到链一律不授权。
- 离线完成（`offline_api`）：目标目录不在扫描范围时只落盘，不再改写 `source_cids` 强建整理任务；「只落盘」分支不再写额外事件。
- 离线兜底轮询（`organizer.offline_scan_tick`）：只轮询授权范围内的目录，未授权目录静默跳过。
- 转存（`transfer`）：目标不在「待整理根」列表里不再报错导致转存失败；建 `organize_after` 任务前校验 `dest` 在授权范围。
- 任务恢复（`run_job`）：进入整理前按当前配置复校，目录已移出范围的整批取消并留一条说明；未配置 `organize_config` 时跳过复校（不影响测试与一次性任务）。
- 频道三种落盘方式、关键词分流、分享 STRM 链路均未改动。
- 验证：`test_organize` / `test_channel_monitor` / `test_share_profile` / `test_share_flat_generation` 共 67 项通过；新增 `test_scan_scope_authority.py` 4 项通过。`test_transfer.py` 在容器内 20 项后挂起——干净 0.8.41 基线同样挂起，属既有问题，非本次引入。
- 部署：NAS 的 StrimKeep 容器运行 0.8.42，健康检查 ready；0.8.41 镜像保留可回滚。


## 0.8.41 — 2026-11-04

- 问题一：`meta.metrics_token` 配了却仍然 404。根因是 `metrics_token()` 用 `store.meta()` 原样读，而 `store.set_meta()` 写的是 JSON，取到的是带引号的 `"abc"`，与请求里的 `abc` 永不相等。改为「env → meta（先 JSON 解，解不动按裸串）」，两种写法都认，并 `strip()` 首尾空白。
- 问题二：0.8.40 的离线兜底扫描节奏只有隐藏 meta 键，界面无入口。`OfflineConfig` 新增 `scan_minutes`（默认 5、0 关闭、上限 1440），走既有 `PUT /api/v1/offline/config`；离线页「直链与通用目录」面板加「整理节奏」下拉（关闭/5/10/30 分钟）并计入该面板未保存提示；`offline_scan_tick()` 主口径改配置，`meta.offline_scan_minutes` 保留为运维压过。
- 验证：新增 `test_ops0841.py` 3 项；受影响回归 24 项通过；完整门禁 `verify.sh`：`全部通过`（655 项通过 / 1 项跳过，11 组全绿）。
- 已知边界：节奏只影响兜底扫描，不影响 115 下载与整理写节流；下拉仅 4 档，其它值走 API。
## 0.8.40 — 2026-11-04

- 问题（现场两条）：①Telegram 机器人提交 ed2k/磁力离线下载，实时日志没有记录；②只有转存会立即整理，手动提交的磁力/ed2k 不自动整理，5 分钟「待整理」扫描也扫不到。
- 根因：①`submit()` 本身不记事件，唯一一条 `store.event('offline', …)` 在 Web 路由里，机器人直连 `engine.offline.submit()` 所以没日志；②`check()` 的认领链有三道门——每条链接唯一匹配离线任务（磁力按 btih、**电驴只按 URL 包含**）、再用任务 `file_id` 在目录增量里唯一确认产物，115 规范化 ed2k 链接后基本必然失败，失败即 `needs_review`，永远走不到 `_enqueue_organize()`；③`share_scan_tick()` 只扫本地 `待整理` 里的分享 STRM，不扫 115 离线目录，`organize` 定时扫描只看 `source_cids`。
- 改动：`submit(..., origin='web')` 提交成功即写 `offline` 事件（登记号/来源/链接数/目录/是否自动整理），删掉 Web 路由的重复日志，机器人传 `origin='bot'`；新增 `_link_key()` 让**电驴按 32 位 ed2k hash** 认领（磁力/电驴同一条路径，其余仍 URL 包含）；新增 `Organizer.offline_scan_tick()` + `offline_new_videos()`：每 `offline_scan_minutes`（默认 5、0 关闭）轮询一个离线目录（磁力/电驴/直链/机器人目录去重），`fresh_children()` 取实时清单，只挑「是视频且 `file_id` 不在 `organized_files`」的条目入队 `origin=offline-scan:<cid>`（`cleanup_disabled=True`，同源 queued/running 则跳过），直接视频为 0 时每轮最多再看一个子目录；同一批文件按 `目录+排序 file_id` 签名去重（签名不变不重排，否则整理残留会让扫描每 5 分钟死循环）；`submit()` 成功后写 `meta.offline_scan_next_cid`，让兜底扫描下一轮先看刚提交的目录（否则轮询要等好几轮）；`verify.sh` 自检组加 `test_ops0840.py`。
- 验证：新增 `test_ops0840.py` 6 项（含「提交失败也留痕」「刚提交的目录优先扫」）；受影响离线/整理回归 42 项通过；完整门禁 `verify.sh` `全部通过`（652 项通过 / 1 项跳过，11 组全绿）。
- 已知边界：`offline_scan_minutes` 未在 UI 暴露；兜底只回答「目录里有未整理视频」，不看链接归属（归属仍由整理任务核对）；电驴 hash 取链接 MD4，与云端 `info_hash` 不符时仍落 `needs_review`。
## 0.8.39 — 2026-11-04

- 目标：把可观测性补到「能配告警」的程度，并修掉单值超时隐患。
- 改动：播放延迟改 histogram（`strimkeep_play_latency_ms_bucket{le=50…5000,+Inf}` + `_sum`/`_count`，仅成功取链入桶），删除客户端 `_percentile()` 与 p50/p95 gauge；新增 `strimkeep_deep_delete_queue{state}`、`strimkeep_tg_retry_pending`、`strimkeep_share_check{result}`+`_timestamp`、`strimkeep_db_wal_bytes`；`share_service.check_alive()` 结果落 `meta.share_check_last` 供指标读取；`network.py` 引入 `httpx.Timeout(connect=5,read=15,write=15,pool=5)` 并统一 4 个客户端，`cloud115` 播放取链改 `read=10`；新增 `docs/operations/ALERTING.md`（指标表 + 9 条 PromQL 告警 + 症状→指标排障顺序）。
- 明确不做：事件表 FTS5——`events` 被 prune 限制在 20000 行/90 天，`LIKE` 已是毫秒级；容器内 FTS5（SQLite 3.40.1）可用，留待保留量上到十万级再评估。
- 依据：Prometheus histogram/summary 取舍、SRE Book 第 6 章四黄金信号与按症状告警。
- 验证：新增 `test_ops0839.py` 4 项（直方图累计桶与 +Inf、失败不入桶、三个 gauge、`check_alive` 落 meta、四段超时常量）；受影响回归 52 项通过；完整门禁 `verify.sh` `全部通过`（646 项通过 / 1 项跳过，11 组全绿）。
- 已知边界：桶为毫秒制上限 5000ms（超出落 +Inf）；进程内计数重启清零；`share_check*` 仅在真跑过一次检测后出现。

## 0.8.38 — 2026-11-04

- 目标：0.8.37 功能 bug 清完后，先还三笔工程债（砍旁路 / 补错误率分子分母 / 备份入库）。
- 改动：
  - `deep_delete.apply()` 托管分支只留预演，删除真删体、`pending` 与 `confirm_recycled()` 调用，并**删除 `confirm_recycled()`**——托管真删唯一入口只剩 `guarded_managed_delete()`（队列 + 回收确认），不再留第二条可被误用的删除路径。
  - `share_service.generate()` 写库失败的回滚删除改走 `purge_strm(..., store=store)`，与「删 STRM 必登记」全局约定一致。
  - `metrics.py` 新增 `record_request()` 与 `strimkeep_http_requests_total{method,route,status}`（label 只用路由模板，避免 `/play115/<pickcode>` 造成无限基数）；`main.py` 中间件响应后记一次，错误率从此有分母。
  - 新增 `scripts/backup.sh`（`sqlite3 .backup`，无 CLI 退回 Python `backup()`；先 `.part` 自检再改名；连带备份 `config/master.key`；按份数轮转）与 `docs/operations/BACKUP_RESTORE.md`（含恢复步骤与「WAL 模式不要 cp state.db」）。
- 依据：SRE Book 第 6 章四黄金信号、Prometheus histogram/计数器实践、SQLite WAL 官方在线备份说明。
- 验证：新增 `test_ops0838.py` 2 项（路由模板计数、备份产出可打开的 SQLite 且无 `.part`）；受影响回归 63 项通过；完整门禁 `verify.sh` `全部通过`（642 项通过 / 1 项跳过，11 组全绿）。
- 已知边界：`/metrics` 仍默认关闭（需 token）；请求计数进程内累计、重启清零；备份默认保留 14 份且不含 `strm/`（派生数据可重建）。

## 0.8.37 — 2026-11-04

- 全局排查发现「删 STRM 留侧车/空目录」还有三处：`sync.sweep()` 撤销输出清理、`sync.sweep_orphans()`、`outputs.retire()`；另有死代码 `sync.cleanup_orphans()`（定义了从未调用、且接线顺序会误删）和 `sweep()` 把删除失败也计为已清理。
- 改动：`outputs.unlink_recorded(..., purge=)` 新增 purge 模式（走 `purge_strm` 清 STRM+同名 nfo/mediainfo/字幕+空目录，删不掉即抛错）；`sweep()` 撤销清理改走 `purge_strm` 且只把真的不在了的路径记为已清理/停用台账；`sweep_orphans()` 改 `unlink_recorded(purge=True)`，删不掉计入 kept；`retire()` 对 `.strm` 用 purge 模式；**删除死代码 `cleanup_orphans`**。
- 另修：离线下载单次上限 20 → 100（后端 `OFFLINE_MAX_LINKS` + `ensure_link_count()`、Telegram 机器人截断与提示、前端提示文案统一），超限仍 422、不静默截断。
- 验证：新增 `test_ops0837.py` 5 项；受影响回归（sync_sweep/sync_orphans/sync_stale(_extra)/ops0830/offline_throttle/sweep_offline/channel_skip_offline/upgrade/transfer）116 项通过；完整门禁 `verify.sh` `全部通过`（640 项通过 / 1 项跳过，11 组全绿）。
- 已知边界：侧车按同名 stem 与 `-media-info.json` 关联；目录级侧车仅在目录无视频时清；离线上限只拦提交入口，115 配额仍以云端为准。

## 0.8.36 — 2026-11-04

- 问题：0.8.35 联动清理了本地 STRM + 专属侧车 + 本地空目录，但**115 上源文件的空目录没删**——删掉一部电影/一季最后一集后，网盘上对应的 `片名 {tmdb-x}/Season 1/` 空目录一直留着。
- 改动：新增 `deep_delete.prune_cloud_empty_chain()`：删完源文件、确认进回收站后，从文件父目录开始自底向上回收 115 空目录；每层先 `children` 判空（非空即停），再用 `recycle_empty_directory`（自身再核位置+空目录）删除，遇同步根/归档根即停；`guarded_managed_delete` 在「本地目录已清空或本地 STRM 本就不在」时调用它，结果写进返回消息，失败只记提示。根集合取自 `sync_config.source_cids` 与 `organize_config.source_cids`/`archive_cid`，越界不删。
- 验证：`test_deep_delete.py` 新增 2 项（清空后回收云端目录链且遇同步根停、云端还有内容时不回收）；完整门禁 `verify.sh` `全部通过`（**635 项通过 / 1 项跳过，11 组全绿**）。
- 已知边界：只在本地目录已清空时尝试云端回收；只在同步/归档根之下回收；失败不重试、不回滚已确认的源文件删除。

## 0.8.35 — 2026-11-04

- 问题：0.8.34 深度删除只删 STRM（受管另删云端源文件），故意不碰侧车；导致删单集后该集 nfo / -media-info.json 残留，删整剧/单季后 poster.jpg / tvshow.nfo 和空目录一直堆着，Emby 还可能把「只剩 nfo 没视频」的死条目再显示出来。
- 改动：`fsops.purge_strm()` 增加专属侧车清理——先删同名 `<stem>.*`（nfo/srt/ass）与 `<stem>-media-info.json`，目录里再无视频时才清目录级侧车（tvshow.nfo/poster/fanart）并自下而上删空目录（库根不动）；`deep_delete.guarded_managed_delete`、`deep_delete.apply` 分享分支、`share_service.check_alive`、`share_api.revoke` 四处删 STRM 全部改走 `purge_strm`，实现「删 STRM 联动清其专属侧车」。单集只删单集、不碰同季其它集；网盘库与分享库仍独立。
- 验证：`test_ops0830.py` 新增 2 项（单集范围、末集清目录级侧车）；受影响回归 `test_deep_delete`/`test_share_check_safety` 40 项通过；完整门禁 `verify.sh` `全部通过`（633 项通过 / 1 项跳过，11 组全绿）。
- 已知边界：目录级侧车只在目录无任何视频时才清；只清 SIDECAR_EXT 列出的扩展名。

## 0.8.34 — 2026-11-04

- 事故：库根迁移后 Emby 反复报「条目已移除」，0.8.32 之前的深度删除对**只有 TMDB 号、没有路径**的扫描删除事件按 `{tmdb-<id>}` 前缀把**整部作品**全部命中，一次命中把分享库「嫁金钗 (2026)」S01E01–E27 共 27 个分享 STRM 撤销并删本地文件（批次 `1793772724.330`，真删），属误删。
- 根因：① TMDB 号是作品不是删除范围；② 真删无归属核实 + 回收确认；③ 分享存活检测撤销后连侧车一起删且不核归属；④ 内部删除登记尽力而为、失败静默。
- 改动（fail-closed 全线收紧）：`deep_delete.match_targets()` 无路径 TMDB-only 事件不再匹配、目录事件须整条路径含 `{tmdb-<id>}` 且季名精确；`apply()` 拒绝托管真删；新增 `guarded_managed_delete()`（核实 output_registry/owned_outputs/本地摘要/网盘身份 → 一次性提交回收并落盘意图 → 回读回收站 + live_file_exists 确认 → 才删本地清台账）；`run_row()` 执行前重查 dry_run 开关、台账变化、drop_internal，`drain` 用 publication_gate 串行；`main` webhook 无路径事件直接拒绝；`internal_delete` fail-closed（事务落盘、窗口 7 天、上限 10 万）；`fsops.purge_strm`/`outputs.unlink_recorded`/`sync` 孤儿清理/`library_roots` 改名回滚都要求登记成功否则保留；`share_service.check_alive` 只有 SHARE_NOT_FOUND 且 `_verified_share_strm` 核实归属才撤销、不删侧车；`generate(allow_restore=)` 已存在不自动重建；分享整理改 `item_by_share` 按 organize_items.id 关联避免 UNIQUE(job_id,file_id) 冲突；`share_api.revoke` 撤销前核归属 + 登记；`emby` TV 分集验证改分页 `/Shows/<id>/Episodes`；`cleanup` 补元数据快照与候选重试。
- 恢复：新增 `recover_share_batch.py`（预演优先、只恢复已核实批次 `1793772724.330` 的 27 条、逐条核对身份/路径/占用、`share_recovery_intents` 落盘），**已在生产执行**，27 条 `state=done`、分享记录 revoked=0、STRM 已重建。
- 验证：新增 6 个测试文件 + 重写 `test_deep_delete` 真删用例 + 修 `test_native_media_info` TV 分集 mock；完整门禁 `verify.sh` `全部通过`（**631 项通过 / 1 项跳过，11 组全绿**）。
- 已知边界：恢复白名单只含已核实批次；托管真删强制走队列，旧脚本直接 `apply(dry_run=False)` 会抛错；`_verified_share_strm` 只认路径 + 内嵌分享 ID 一致的普通 STRM。

## 0.8.33 — 2026-11-04

- 补齐三块体验缺口：① 深度删除一批全部结束后发一条 TG 汇总（成功/跳过/失败 + 前 3 条样本 + 总数），预演不发避免刷屏；② 历史面板从平铺改按「作品 · 季」折叠（剧集按 Season 折叠、电影/分享单片一组），每批返回 `groups` + `rollup`（作品/季/文件/组数）；③ 附属文件同步统计补 `failed` 与扩展名 `types`，同步结果卡片改「下载 N 个（跳过 M 个）（失败 K 个）」。
- 改动：`deep_delete.py` 新增 `_group_items()`/`notify_batch()`、`history()` 返回 `groups`+`rollup`；`sync.download_metadata()` 统计加 `failed`/`types`、结束事件补失败数；`notifications.py` 目录卡片一行报下载/跳过/失败（`.get()` 兼容旧 `sync_metadata_stats`）；`DeepDeleteHistory.vue` 新增分组类型与折叠 UI；新增 `tests/test_ops0833.py` 4 项；`verify.sh` 「自检与计数」组加入 0833。
- 顺延：`library_roots` 的 `--prune-legacy`/`--prune-dead` 加 `--notify` 本次未做——深度删除自身的真删汇总已覆盖「收尾通知」，本地 prune 仍走日志与面板，避免两套口径打架。
- 验证：新增 `tests/test_ops0833.py` 4 项通过；完整门禁 `verify.sh strimkeep-dev:0.8.33` `全部通过：strimkeep-dev:0.8.33`（618 项通过 / 1 项跳过，11 组全绿）。

## 0.8.32 — 2026-11-04

- 事故（测试库，当天）：0.8.31 的 `--prune-legacy` 删掉 395 个旧命名副本后，**61 个云端源文件（约 610 GB）被删除**（在 115 回收站）。链路：程序删本地 STRM → Emby 报「Episode deleted」→ 深度删除按台账命中 → 删云端源文件 → 新命名那份 STRM 变成死链。根因是**深度删除分不清「用户删的」和「程序删的」**；库根迁移又让 Emby 把整棵旧树当成消失，一次排了 12007 条待删（当时是真删模式）。同一条链路也解释了更早（11-03 ~ 11-04 05:3x）尚公主/嫁金钗/伟大的长征/Dark Matter 的云端文件为何消失。
- 处置：先把 `deep_delete_dry_run` 改回 True，并把仍在排队的 12007 条全部置为 `skipped`（不会再删云端）。
- 改动：新增 `internal_delete`（程序删本地 STRM 前把路径记进 `meta.internal_deletes`，30 分钟窗、上限 3000）；`fsops.purge_strm(store=)`、`outputs.unlink_recorded()`、`sync.sweep()` 撤销清理三处删除入口都先登记；新增 `deep_delete.drop_internal()` 并在 webhook 入队前过滤（程序删的那些只记事件、不删云端）；`prune_legacy()` 在深删是真删模式时**拒绝执行**、执行前批量登记；`apply_rename()` 改名前登记所有旧路径。
- 验证：新增 `tests/test_ops0832.py` 5 项通过（登记窗口/过期、只剔程序删除、purge 自动登记、真删模式整体拒绝、改名登记）；受影响回归 12 文件 135 项通过；完整门禁 `verify.sh strimkeep-dev:0.8.32` `全部通过：strimkeep-dev:0.8.32`（614 项通过 / 1 项跳过，12 组全绿）。
- 部署：`strimkeep:0.8.32`；线上深删保持演练；12007 条历史排队已作废（需要时可人工重建）。
- 已知边界：登记窗口 30 分钟（Emby 通知正常在秒级）；用户手工在文件系统删 STRM 仍会走深删；用户主动在 Emby 删条目时深删语义不变。

## 0.8.31 — 2026-11-04

- 问题：0.8.30 迁到「镜像 115 目录名」之后，现场还留着 395 个**旧命名副本**（同一云端文件在本地有两份 `.strm`：旧版把 TMDB 分集标题写进文件名 `S01E01.初潮`，现版本用 `S01E01.第1集`），另有 47 个「只有旧命名」的孤本。追查发现同步本该清掉它们，但清理链路有三处窟窿：① `sweep_orphans()` 的判定写死 `playback_base + '/play/'` 前缀，而 0.7.12 起 STRM 写的是 `/play115/<pickcode>/<fid>/<sha1>/<size>/<name>`，一个都不匹配 → 全部按「保留」，等于 play115 切换后清理就失效了；② 语义正确的 `cleanup_orphans()` 只有定义、没有任何调用（死代码，且 expected 只含本次任务条目却扫整个同步根，直接接上会误删）；③ `outputs.retire()` 只处理 `output_registry.files` 登记过的路径且要求归属，旧命名这批从没进登记表 → 天然跳过。
- 改动：`sync.sweep_orphans()` 改为**按文件身份**判定——旧形态 `/play/<token>` 照旧按播放身份是否撤销处理，新形态从地址里取 `file_id` 查 `output_registry`，同一云端文件已被登记到别的路径且那份仍存在 → 删，否则只报数（新增 `_duplicate_output()`）；`library_roots` 新增只读 `legacy_report()`、`prune_legacy(apply=False)`、`restore_legacy()` 与 CLI `--prune-legacy` / `--restore` / `--backup-dir`（默认演练，清单里保存每个 STRM 原文，删除走 `fsops.purge_strm`，绝不 rmtree）；新增 `docs` 说明与 `tests/test_ops0831.py`。
- 验证：新增 `tests/test_ops0831.py` 4 项通过（play115 副本删除且正式输出保留、未登记只报数、旧形态照旧清、prune 的 dry-run/apply/restore 全链路）；受影响回归 7 文件 86 项通过；完整门禁 `verify.sh strimkeep-dev:0.8.31` `全部通过：strimkeep-dev:0.8.31`（609 项通过 / 1 项跳过，12 组全绿）。
- 部署与数据修复：`strimkeep:0.8.31`；`--prune-legacy` 先演练后执行，删掉 395 个旧命名副本（清单 188 KB 可 --restore 回滚；清理后媒体库 1070 个 STRM，覆盖 1104/1104）（保留 47 个只有旧命名的），清单落在数据目录可用 `--restore` 回滚；Emby 重扫后重复条目消失。
- 已知边界：只清「库根下、台账未登记、且同一集已有有效登记」的副本；整部只有旧命名的作品不动（要先让云端重新整理/同步接管）。

## 0.8.30 — 2026-11-04

- 问题（现场）：115 上的归档目录与 STRM 同步根都叫「媒体库」，而配置 `library_dir_name = 影视媒体库`（0.6.3 时代的默认名）。磁盘上因此并存两棵树：`strm/媒体库` 530 个 STRM（1892 文件，最后写入 11-01）、`strm/影视媒体库` 只有 12 个 STRM（当天两个任务）；台账 978 行**全部**是 `影视媒体库/...`，其中只有 12 行在磁盘上存在（换前缀后也只有 42 行命中），924 行指向不存在的文件；Emby 三个库根都挂在 `/media/影视媒体库/*`，老库整体不在任何库里。根因：库根名被当成自由配置，而整理/同步会按配置重算 `relative_path` 并直接写台账、却不移动已有文件，两边无声分叉。
- 改动：`organize_options` 新增 `cloud_dir_name()` / `library_root()`，`library_dir_name` 默认改为空（= 镜像 115 目录名；显式填写仍优先，兜底 `影视媒体库`）；`organizer`（批量准备用配置标签、`fill_directory` 用扫描到的真实路径）、`sync.relative()`、`tasks`（同步任务带上 `directory_labels`）、`main` 的 `library_prefix` 全部走同一个 `library_root()`；新增 `library_roots`（只读三方对照、可回滚的库根改名、启动自检、CLI）；`fsops` 与 `cleanup.py` 的空目录清理都不再依赖名字白名单（`/strm` 一级目录永远保留；洗版后的空目录跟随当前整理配置解析出的库根）；新增 `docs/operations/LIBRARY_PATHS.md`。
- 顺带修复：`test_merged_episode_copy.py` 两个断言自 0.8.28 起就是红的（未识别分流已改为「来源根目录或已有成功兄弟时逐个转移文件」），按新语义修正并注明原因——0.8.29 发版时门禁并非全绿。
- 验证：新增 `tests/test_ops0830.py` 11 项通过（镜像取名、显式覆盖、转义 JSON 路径改写、冲突整体拒绝、改名/回滚、自检去重、空目录边界）；受影响回归 17 文件 185 项通过；完整门禁 `verify.sh strimkeep-dev:0.8.30` 全部通过：strimkeep-dev:0.8.30（605 项通过 / 1 项跳过，12 组全绿）。
- 部署与数据修复：`strimkeep:0.8.30`；停服务备份数据库与旧库根 → `library_roots --state` → `--rename 影视媒体库 媒体库 --apply`（30 个文件、978 行台账）→ `--set-config ''`（以后跟随 115 目录名）→ 起服务；Emby 三个库根需人工改到 `/media/媒体库/*`。
- 已知未修：924 行历史台账原本就指向不存在的文件，改名只把前缀改对，补齐要一次全量 STRM 同步；显式写死旧库根名仍会与网盘不一致（只有启动报警）；分享库根 `分享影视库` 仍是固定名字。

## 0.8.29 — 2026-11-04

- 修复已匹配作品因跨地区合拍或缺少地区信息转未识别：保留已确认作品身份并分类到“其他”；关闭地区分类时亦不再被地区阻断。确定地区和音乐类型安全规则维持不变。
- Telegram 轮询、消息处理、频道及通知异常在写事件或回复前脱敏；HTTP 错误仅保留状态或类型。历史日志未修改，已暴露凭据应在 Telegram 侧撤销并更换。
- 验证：地区分类与整理 71 项通过；Telegram 脱敏、通知与分类回归 23 项通过、1 项跳过。任务 #263 的五集未主动移动。

## 0.8.28 — 2026-11-04

- 网盘未识别先按本次完整清单与现场核对：完整失败且能确认的作品目录保留，部分失败只转失败视频和绑定字幕；分类目录、其他作品、未纳入的视频和空目录不拖走。作品身份或状态不确定时按单文件保守处理。
- 分享 STRM 待整理取消分享码包装，保留原作品目录；保存原始来源目录和布局版本以区分同名分享，旧数据继续兼容，不主动迁移历史文件。
- 分享未识别在同一作品本批结果确定后分流：完整失败且作品目录范围可确认时保留作品目录，部分失败或证据不足时逐文件平铺；不搬源目录及其中其他文件，同名禁止覆盖。中断恢复按不完整证据保守处理。
- 验证：网盘分流与清理、分享目录/同名/中断、通知和分享调度等分组回归通过；未针对历史文件执行迁移。

## 0.8.27 — 2026-11-04

- 分享 STRM 未识别逐条转移时去掉生成的分享码包装目录，保留分享内作品与季目录，方便整季识别失败时按原剧名查找。
- 相同路径文件名自动追加稳定身份后缀、无覆盖保留；两个候选都冲突时保留待整理原文件并延迟重复重试。兼容旧布局和冲突记录。
- 按用户要求不迁移、不重试历史未识别文件；只对未来处理生效。
- 验证：分享整季未识别、同名文件、双重冲突、目录识别及洗版回归 40 项通过。

## 0.8.26 — 2026-11-04

- 修复定时分享存活检测调用不存在的实例属性，覆盖间隔、Cron 及异常留痕；检测仍按配置执行。
- 分享整理清理空目录转到工作线程，取消时等待操作完成，保持原目录边界与恢复流程。
- 分享文件只有集号时，允许从对应分享的作品目录采用带年份或 TMDB ID 的线索；排除分享码与分类目录，保留冲突和合并集的人工核查。历史未识别资源未自动移动或重试。
- Infuse/Kodi 使用普通播放的 15 秒直链缓存，明确探测客户端保留原 60 秒；修复已有播放缓存测试冲突。
- 验证：调度、清理取消、播放器缓存、分享目录识别及洗版回归共 69 项通过。

## 0.8.25 — 2026-11-04

- 修复日志将“失败 0”等零计数误判异常；真实错误仍按现有规则显示，历史日志无需迁移。
- 事件循环监控按 60 个采样写快照；历史受管记录的查询转到独立线程，归属核验与发布锁不变。
- 离线提交复用 115 写入节流、冷却与请求账本，维持 proapi 调用和结果不明不自动重发的契约。
- 影视探索剧集计数缓存增加容量上限、过期清理及 Emby 连接身份隔离；修正掉线告警示例。
- 验证：前端测试 20 项通过；同步、事件循环、缓存与离线提交相关用例分组串行通过；未发起真实网盘离线提交。

## 0.8.24 — 2026-11-04

- 排查全量任务 #232：183.4 秒中，115 递归目录扫描约 1.84 秒；51 个已有字幕因旧台账缺少字幕指纹而重复取链下载。
- 整理成功下载字幕后，仅在本地内容与云端摘要和大小一致时登记身份；旧台账仅在新鲜扫描、归属、已登记 SHA256、本地 SHA256 及云端 SHA1 和大小一致时复用，否则仍下载核验。
- 字幕匹配回退限定同目录，移除全量清单中重复匹配；不跳过全量扫描与失效清理校验。
- 验证：整理、字幕归属和同步相关回归 78 项通过；并发运行大组测试曾退出 137，分组串行通过。

## 0.8.23 — 2026-11-04

- 未识别按文件所在的最小作品目录分流，保留已提交的旧移动意图；目录仍含其他视频或子目录时原位保留，避免误搬整个电影分类或跳过的 ISO。
- 同目录电影字幕在标题一致且年份无冲突时按唯一候选匹配；不确定或多个版本不猜测。
- 保存本次扫描未匹配字幕的身份快照；仅在相关视频已处理、目录无其他资源且字幕身份复核一致时移入回收站，并持久化意图和核对回收记录，响应丢失不重复提交。
- 显式转存批次把已扫描空子目录纳入清理候选；根目录、未识别资源、跳过的 ISO、后到文件及其他来源目录均保留。任务中心文案与清理计数同步更新。
- 用户已清空原未识别目录，本版只修复后续流程，不按历史任务记录修改或删除现有网盘文件。
- 验证：分流、字幕、目录清理、并发变更、回收意图和恢复等回归 87 项通过；0.8.22 既有播放缓存用例在原镜像中同样失败，与本次改动无关。

## 0.8.22 — 2026-11-04

- 问题：0.8.21 只回答了「事件循环被堵多久」，还缺四件事：没有可采集的指标（接不了看板/告警）；请求慢在哪没有 trace（日志串不起来）；播放成功率只有一条条 success 日志；115 的冷却/WAF 只在内存里，重启即清零（`RATE_LIMITS.md §5` 的未覆盖项）。另外通知/Emby/115 的退避是固定间隔，同一批任务会在同一秒一起重试。
- 改动：新增 `metrics.py`（`Metrics` + `/metrics`，Prometheus 0.0.4；令牌来自 `STRIMKEEP_METRICS_TOKEN` 或 `meta.metrics_token`，未配置时 404，常量时间比较）；中间件生成/透传 `X-Request-Id` 并对 ≥1 秒的非流式请求写一条带 rid 的 `perf` 事件；两个播放 handler 在 `finally` 记录取链结果与耗时；`Cloud115` 冷却加 ±15% 抖动并把 `throttle_scale`/冷却/WAF 写入 `meta.cloud_runtime`、启动时按墙钟接回；通知退避 ±15%、Emby 核对重试 ±20% 抖动；新增 `docs/operations/METRICS.md`。
- 修复（门禁当场抓到的两个 0.8.21 崩溃）：
  - `start_stall_watchdog()` 的线程永不退出（每个 app 生命周期留一个），测试进程里积累十几个后并发调用 `faulthandler.dump_traceback()` → 退出时 SIGSEGV（退出码 139）。现在心跳协程被取消时置 `Event` 让线程退出，并用全局非阻塞锁 `_DUMP_LOCK` 串行化转储。
  - `Store.close()` 关闭**所有**线程的连接（0.8.21 改成一线程一连接后就没改这里）：事件循环一侧关库时，正在 `prune_in_thread()` 的工作线程连接被抽掉 → sqlite3 在 C 层 SIGSEGV（栈顶 `store.py:446 _prune`）。现在只关本线程与已退出线程的连接，登记表改为 `(thread_id, conn)`。
- 验证：新增 `tests/test_ops0822.py` 11 项通过（指标鉴权/口径、rid、慢请求不误报、播放计数、退避抖动、冷却与 WAF 跨重启、看门狗线程不泄漏、`close()` 不抽活线程连接）；受影响回归 11 文件 164 项通过；完整门禁 `verify.sh strimkeep-dev:0.8.22` 414 项通过 / 1 跳过（9 组全绿）。
- 部署：`strimkeep:0.8.22`；`/health/ready` 返回 0.8.22；未带令牌 `/metrics` 404，带令牌返回指标；响应头出现 `X-Request-Id`；线上库写入 `meta.metrics_token`。
- 已知未修：指标是拉取式的，没有内置看板/告警；`events` 仍无 `level`/`fields` 列（慢请求与播放日志靠 rid + 中文句子）；`offline_add` 未纳入全局节流；剩余同步 SQL 按 `loop_lag`（p50 5.4ms / p95 11ms）暂不紧急；`loop_lag` 快照在样本满 300 后每秒写一次 meta（0.8.21 的 `len % 60` 恒真），0.8.23 改成按时间写。

## 0.8.21 — 2026-11-04

- 问题：0.8.20 的教训（`to_thread` + 共享 sqlite 连接 = SIGSEGV）当时只是绕过；任务列表、实时任务流、日志、总览聚合仍在事件循环上跑同步 SQL；而且没有「事件循环到底被阻塞多久」的度量，只能靠猜。
- 改动：`Store.db` 改成按线程懒创建的属性（`_connect()` 统一设 WAL/busy_timeout/synchronous，`close()` 收拢所有连接），新增 `run_in_thread()`；`/api/v1/tasks`、`/tasks/events`（`stamp()`+`build()`）、`/api/v1/logs`、`/logs/events`、`/dashboard` 的 `organized_files` 聚合都进 `asyncio.to_thread`；新增 `loop_lag_monitor()`（p50/p95/max 写 `meta.loop_lag`，单次 ≥500ms 记事件）与 `start_stall_watchdog()`（循环卡住 1 秒就 dump 全部线程栈到 stderr）；总览 `system.loop_lag`；Sync 页「重写」改成**先预览再执行**并新增「回滚上次重写」。
- 修复（度量当场发现）：`native_media_info.sweep_missing()` 是「每个 STRM × 每个 Emby 条目」的双层循环且整段跑在事件循环上 → **每次启动阻塞 16 秒**（看门狗抓到栈 loop → sweep_missing → matches）。改为 `EmbyDelivery.play_ids()` + `pid → 条目` 索引 + 索引与文件遍历进线程，并保留「无播放入口条目按路径后缀匹配」的旧语义。
- 验证：新增 `tests/test_ops0821.py` 6 项通过；受影响回归 9 文件 124 项通过；补扫改造 22 项通过；完整门禁 `verify.sh strimkeep-dev:0.8.21` 9 组全部通过（403 项通过、1 skipped，约 9 分钟）。
- 部署与现场对比：`strimkeep:0.8.21`，`/health/ready` 返回 0.8.21，`meta.loop_lag` 有数；同一台机器两次重启对比 —— 修复前 `max_ms` 16408 / 16152，修复后启动 2.3 秒内通过健康检查、看门狗无告警；前端产物含「回滚上次重写」与 `dry_run=1`。
- 已知未修：剩余同步 SQL（按 `loop_lag` 实测数据挑下一批）；`emby_deliveries.payload` 未拆行式表（理由见发布说明）；探索页模块级缓存没有容量上限。


## 0.8.20 — 2026-11-04

- 问题：任务中心每 2 秒把 30 个任务整个重建（6 条批量查询 + 序列化），且每条任务都要 `json.loads` 整段 Emby payload 才能显示「已确认 N/M」；`POST /strm/rebuild` 同步遍历 892 行（读文件→比字节→写文件→2 次 UPDATE）全压在事件循环上；每小时的 `prune` 也是。
- 改动：`/tasks/events` 增加 `stamp()`（一条 SQL 的 4 个子查询）做变更检测，签名不变只发心跳；`emby_deliveries` 新增 `total`/`confirmed` 摘列（老库用 SQLite JSON 函数一次性回填，`save()` 写 payload 时同步维护），`job_public` 优先读列、两列均为 0 时回退解析；新增 `rebuild.py`（预览/执行/快照/回滚，文件 I/O 走 `to_thread`，台账一次事务写回），`/strm/rebuild` 支持 `?dry_run=1`，新增 `/strm/rebuild/status` 与 `/strm/rebuild/rollback`；`Store` 打开即 `busy_timeout=5000` + `synchronous=NORMAL`；`maintain()` 改 async 并在线程里跑维护清理。
- 修复（0.8.18 遗留）：`/strm/rebuild` 对 `play115_candidates()` 返回的 bytes 再 `.encode()`，身份齐全的行必然 500；重建写出的地址缺结尾换行，与 `write_strm()`/`sync` 的规范形态不一致。两处一并修正。
- 踩坑并修正：`asyncio.to_thread(store.prune)` 在容器内直接 SIGSEGV——sqlite3 连接不能跨线程共用，改为 `Store.prune_in_thread()`（线程内独立连接，WAL + busy_timeout），并把结论写进发布说明与用例。
- 验证：新增 `tests/test_ops0820.py` 10 项通过；受影响回归 8 文件 102 项通过（首轮 SIGSEGV 修复后重跑）；完整门禁 `verify.sh strimkeep-dev:0.8.20` 9 组全部通过（397 项通过、1 skipped，组内约 3.7 分钟）。
- 部署与现场复核：`strimkeep-dev:0.8.20` → `strimkeep:0.8.20`，`/health/ready` 返回 `0.8.20`；容器内只读重算 949 行台账：`rewrite=0 unchanged=879 missing=70 failed=0`（重建在现网是幂等的）；`emby_deliveries` 53 行摘列与 payload 比对 0 不一致；`/strm/rebuild/status`、`/rollback` 未登录 401；部署后 30 分钟 0 条 error 事件。
- 现场发现：台账里有 70 条记录的本地 STRM 已不存在（历史删除或未落地）。重建只统计不创建，补齐应走「STRM 同步」（有归属与来源校验）。
- 已知未修：其余约 450 处同步 SQL 仍在事件循环里（需要「一线程一连接」的单写队列，单独一版做）；`emby_deliveries.payload` 仍是整段 JSON（`targets` 未拆行式表）；页面还没有 dry-run/回滚按钮。


## 0.8.19 — 2026-11-04

- 问题：现场 294 条 `organize_items` 停在 `discovered`（所属任务早已结束，却持续影响任务中心「未处理/未识别」统计）；21 条 `transfer-secret:*` 永不释放；`/api/v1/tasks` 与 `/tasks/events` 对每条任务各跑 2~3 条统计（30 个任务时每 2 秒约 62 次全表扫描）；Emby 媒体库路径漂移只能等 30 分钟核对超时才暴露。
- 改动：`Store.settle_terminal_job()` / `reconcile_terminal_items()`（终态收口 + 启动自检，幂等）；`Store.prune()` 增加「已结束任务的转存密钥」与「`transfer-update` 保留最近 200 条」；`Organizer.update_job()` 在 `completed/partial/cancelled` 统一调用收口；新增 `job_item_stats()` 批量统计并接入 `/api/v1/tasks` 与 `/api/v1/tasks/events`；`EmbyDelivery.coverage_selfcheck()` / `coverage_loop()`（启动 60 秒后首次、之后每 6 小时、每天最多一条结论）；`scripts/verify.sh` 修正过期注释并新增「自检与计数」组。
- 契约：任务进终态时，「还没开始处理」的条目（`discovered`/`planned`）归一为 `skipped`（step/message = 任务结束未处理），已出结果的条目不动；`test_phase5` 的「退休任务零改动」断言按新契约更新（仍要求云端零改动、条目其余字段不变）。无 schema 变更，回滚不需要迁移。
- 验证：新增 `tests/test_ops0819.py` 10 项通过（容器内 9.19s）；受影响回归 `test_ops065`/`test_iteration1`/`test_app`/`test_phase5`（37 项）与 Emby 六文件（48 项）通过；完整门禁 `sh scripts/verify.sh strimkeep-dev:0.8.19` 9 组全部通过（387 项通过、1 skipped，含组间间隔约 11 分钟）。
- 部署：`strimkeep-dev:0.8.19` → `strimkeep:0.8.19`；线上 compose 先备份再改 tag，`/health/ready` 返回 `0.8.19`；启动后按现场数据产生「启动自检」与「媒体库路径自检」事件（健康数据上不产生）。
- 已知未修：`/tasks/events` 仍每 2 秒全量重建（本版只消除 N+1，单轮约 6 次查询）、`emby_deliveries.payload` 未拆行式表、`POST /strm/rebuild` 仍无 dry-run 与后台进度。


## 0.8.18 — 2026-11-03

- 修复长任务中已经由 Emby 独立核实入库的作品海报一直显示待核实：每张海报按自身关联的全部条目核对，确认后原位更新；未确认、正在发送的新路径及跨集不完整的海报继续等待。
- 任务仍在运行时保留通知队列；最终未确认的条目仍按原有逻辑报告，重试与重启恢复继续生效，不新增第二条海报。
- 验证：进行中任务、部分确认、跨集完整性、旧消息状态、重试和 Emby 交付回归通过；未打断线上整理任务。

## 0.8.17 — 2026-11-03

- 对照 Emby STRM 文档和同类项目复核失效清理：保留现有自动清理配置；仅处理完整扫描缺失、云端实时核实、账号/所有权/内容摘要一致的受管输出，手工修改、身份不明和扫描后新登记的输出保留。
- 历史受管记录在扫描边界前导入，解决首次完整同步无法清理已确认失效旧条目的旧测试；扫描后并发登记保护继续生效。旧版手动失效清理接口也加固当前账号、归属、内容与云端双重核实。
- STRM 页面说明开关只处理本地输出、不删除 115 源文件；同步通知增加复用和重新处理数量，补充操作文档及公开资料。
- 参考 Emby、p115client、115-station：事件增量与完整对账是后续方向；本版不声称已实现事件增量，也不更改既有定时与清理配置。
- 验证：全量同步、旧记录、失效清理、并发保护、人工修改、归属冲突和通知回归通过；未对线上媒体库执行试删。

## 0.8.16 — 2026-11-03

- 修正历史分享 STRM 可读地址的扩展名：只有真实的视频扩展名才保留，从原整理任务核对来源；自动修复 0.8.15 将音轨标记 5.1 误当扩展名的地址，并重新通知 Emby。

## 0.8.15 — 2026-11-03

- 全量同步在最近完整清单与文件指纹、目标路径、归属和 STRM 内容均一致时，复用整理阶段记录的简略目录路径，并安全补全台账；变动和不确定情况仍逐项核查，清理保护不变。
- 分享 STRM 播放地址带可读文件名，旧地址与新地址均可播放和参与 Emby 身份匹配；启动时仅处理可核实归属的旧分享影视库 STRM，并向 Emby 提交变更路径，失败保留待重试记录。
- Telegram 海报画质对明确含 REMUX 的版本显示 REMUX；其他格式保持原样。
- 验证：同步复用与清理、分享洗版与历史地址、Emby 匹配及海报格式回归；新增测试覆盖改名、指纹改变、本地文件被修改时不能复用。此前的旧输出首次导入清理测试在 0.8.14 基线同样失败，未放宽扫描期间新登记输出的保护。

## 0.8.14 — 2026-11-03

- 影视探索保留原有单选排序，支持上映／播出排序与地区、类型、年份组合；使用 TMDB 日期范围近似上映状态，趋势榜和名称搜索不支持的组合明确提示。
- 修复筛选地区时自动切回热门、切换排序时清除地区的问题；失败时清除旧结果避免误读。
- 历史“内部操作失败”记录为 KeyError，时间与上一版分享任务明细故障吻合，原记录缺少接口信息无法完全确认；新增不包含请求参数的接口模板记录以便后续排查。
- 验证：探索接口组合筛选和原有角标、分页回归通过；前端随运行镜像构建。

## 0.8.13 — 2026-11-03

- 修复分享整理任务明细缺少网盘 parent_id 时返回 500；分享生成的 NFO 和海报写入结果记录到任务，供明细与通知统计。
- Emby 核对时按条目 ID 去重，避免同一个单集从多个父剧集入口返回而被误判为多个版本；真正的不同 ID 仍不自动探测。
- 媒体信息补扫改为完整分页并检查 Emby 视频流，即使已有本地 JSON 也能补查 Emby；保留非本项目的媒体信息文件，重新排队时不重复提交仍在运行的任务。
- 海报统一两字字段，区分本批入库、保留已有、洗版替换与待核查；STRM 汇总显示本批 NFO 是否写入及实际数量，并兼容旧海报的 Emby 状态原地更新。
- 《尚公主》第 24 集通过 Emby PlaybackInfo 核实并触发补探，回读已显示视频及音频流；未修改其他文件。
- 验证：分享任务详情、Emby 核对与分页补扫、媒体信息、NFO、洗版海报及通知回归 99 项通过、1 项因隔离环境没有前端构建产物跳过；未发送真实 Telegram 测试消息。

## 0.8.12 — 2026-11-03

- 复核线上 0.8.11：英文发布名 Love in Disguise 已由后续任务 #185 成功识别为《我的有罪之人》（TMDB 303745），五集归档及 STRM 完成，Emby 已确认入库。
- 加固历史任务自动结项：只处理同账号、同类型、更早且仍为部分完成的任务；保留原始失败原因，其他未补齐条目仍存在时不结项，不覆盖已完成或较新的任务。
- STRM 失效清理记录完整扫描开始时刻，并跳过此时之后新登记的输出；缺少时间证据时停止清理并提示重新同步，仍保留逐条云端存续核对。
- 验证：多语言识别、候选年份、历史任务及 STRM 同步清理回归 57 项通过；线上原有任务和文件未手动改动。

## 0.8.11 — 2026-11-03

- 分享监控的「存活检测」支持两种执行方式：固定间隔（小时，锚在 03:00）或 Cron 表达式（5 段，按服务器时区）。此前只有「每 N 小时」一个数字，想按 03:00 以外的时间或只在周末跑就没有入口。
- 后端 `ShareSettingsInput` 新增 `share_check_mode` / `share_check_cron`，Cron 模式用 `croniter.is_valid` 校验 5 段表达式；表达式缺失时退回间隔模式，不把整块功能静默关掉。`organizer.share_check_tick` 按模式取下次执行时间，新增 `next_share_check_due` / `next_cron_due`；间隔模式的 03:00 锚点行为保持不变。
- 前端分享监控卡片加「间隔 / Cron」切换，Cron 模式显示表达式输入与「分 时 日 月 周」提示，说明文案随模式变化。
- 验证：`tests/test_share_schedule.py` 8 项通过（新增 3 项：Cron 下次时间、Cron 在小时=0 时仍执行、间隔模式仍按 0 关闭）。
- 部署：NAS 的 StrimKeep 容器运行 0.8.11，健康检查 ready；0.8.10 镜像保留可回滚。


## 0.8.10 — 2026-11-03

- STRM 失效清理恢复执行。以前扫描结束后只要账号级 `sync_revision` 与本批保存的 `manifest_revision` 不一致，就直接跳过失效清理并提示「扫描期间网盘内容发生变化，已跳过失效清理」。而账号级 revision 只要有任何远端改动（转存、整理、动别的目录）就会变，本批写过输出也会 `invalidate()`，所以这条判断几乎总成立——现场 #92 / #8 / #3 都是这个现象，失效 STRM 长期不清理。
- 现在 revision 不一致只写一条事件留痕，仍然继续清理。安全性由 `cleanup()` 自己保证：清单里有的跳过、扫描开始后才登记的跳过、`live_file_exists` 确认文件真没了才删；文件仍在但脱离范围会抛 `UncertainState` 中止整轮清理。
- 验证：新增 `tests/test_sync_cleanup_revision.py` 2 项（revision 中途变化时仍清理 1 个失效输出并留痕、留痕事件必写），并入「同步与巡检」门禁组；`test_sync_stale` / `test_sync_sweep` / `test_sync_orphans` 共 14 项无回归。
- 部署：NAS 的 StrimKeep 容器运行 0.8.10，健康检查 ready；0.8.9 镜像保留可回滚。


## 0.8.9 — 2026-11-03

- 「过滤」口径修正：0.8.8 只看 `scan_filter.excluded`（扫描阶段的扩展名/体积/关键词过滤），现场全是 0；真正的过滤发生在整理阶段，记在条目 step 上——`已有同集或同名条目，保留已有` 共 91 条（#145 42、#159 25、#40 24）。`job_public` 新增 `filtered_items` 按 step 统计，`filtered = filtered_items + excluded`，前端只要 `filtered > 0` 就归「过滤」。
- 「小于 100MB 不整理」（`min_video_size_mb=100`、`recycle_small_videos=True`）属于扫描阶段，落在 `scan_filter.excluded`，同样计入「过滤」。
- 提示文案改为按 `scan_filter.reasons` 的真实原因显示（「低于最小视频大小 100 MB，已回收（3 个）」等），不再把体积过滤说成「未列入扩展名」；整理阶段跳过则提示「另有 N 个文件因已有更好版本或同名条目未入库」。
- `Task` 类型补 `unidentified` / `filtered` / `filtered_items`，`scan_filter` 补 `reasons`——第一版漏了类型，被 `vue-tsc --noEmit` 拦住后补齐。
- 验证：生产库按新口径统计 `#145=42 / #159=25 / #40=24`，合计 91；`vue-tsc --noEmit` 与 `vite build` 通过；冒烟 `/health/ready` 返回 0.8.9、首页 200。
- 部署：NAS 的 StrimKeep 容器运行 0.8.9，健康检查 ready；0.8.8 镜像与 compose 备份保留可回滚。

## 0.8.8 — 2026-11-03

- 任务中心「深度删除」从分类栏中间移到最后一个 tab：它是 `DeepDeleteHistory` 组件，却被当成分类 `el-tab-pane` 插在「全部」和「进行中」之间，看着像挡住了进行中。
- 「未识别」分类修复：原来用 `counts.pending > 0` 判定，而 `pending` 只是中间态、跑完就变 `done`/`skipped`，所以该栏永远为 0。`job_public` 新增 `unidentified`（按 `organize_items.step LIKE '%未识别%'` 统计），前端改用它，`pending` 仅作兜底。
- 「过滤」分类修复：原条件 `!job.total && scan_filter.scanned` 要求一个文件都没进队列，实际几乎不成立。改为 `!counts.done && scan_filter.excluded > 0`——有文件被扩展名/关键词/体积挡掉且这批没有成功入库。
- 新增 `POST /api/v1/playbacks/cleanup`：清理没被 `output_registry.active=1` 引用的播放身份，默认只删 `revoked=1`（现场 65 条），`include_active=1` 才连未撤销孤儿（另外 682 条）一起删。入口在 STRM 生成页的播放地址卡片。只删表记录，不动本地 STRM 与云端文件——STRM 走 `/play115/{pickcode}/...`，取链不查这张表。
- 验证：`vue-tsc --noEmit` 与 `vite build` 通过；镜像内确认 `unidentified`、`playbacks/cleanup` 与前端「清理未被引用的播放身份」均存在；冒烟 `/health/ready` 返回 0.8.8、首页 200。
- 部署：NAS 的 StrimKeep 容器运行 0.8.8，健康检查 ready；0.8.7 镜像与 compose 备份保留可回滚。
- 已知未修（详见 `docs/quality/known-issues.md`）：`strm_sync` 失效清理仍因账号级 `sync_revision` 被跳过；裸集号文件仍不做父目录兜底。

## 0.8.7 — 2026-11-03

- 新增任务「结项」动作：`POST /api/v1/organize/jobs/{id}/close` 把 `partial`/`needs_review`/`cancelled` 的任务置为 `completed`，message 追加「（已人工结项，不再重试）」。只改任务状态与 message，不动条目——点进详情仍能看到当时失败原因。此前 19 条「部分完成」（organize 10、share_organize 6、strm_sync 3）在界面上没有任何出口。
- 任务卡片接上后端早已具备的两个动作：「重试空目录清理」（`retry-cleanup`）与「重新核查目录残留」（`recheck-cleanup`）。此前前端只接了 `retry-output`，这两个 action 从来没被调用过。
- `job_public` 补 `can_recheck_cleanup`：已结束 + 有清单 + 条目状态正常 + config 里有 `empty_cleanup.entries`，与后端 409 校验条件一致。
- 「更多操作」的展开条件放宽为 `done||archived||can_retry_cleanup||can_recheck_cleanup`，否则没有已归档任务时按钮不可见。
- 删除误放的 `backend/strimkeep/test_sync_stale.py`（正主在 `backend/tests/`）。
- 验证：镜像内确认 `can_recheck_cleanup`、`已人工结项`、`结项`、`重试空目录清理` 均存在，`/app/strimkeep/test_sync_stale.py` 不存在；冒烟实例 `/health/ready` 返回 0.8.7、首页 200。
- 部署：NAS 的 StrimKeep 容器运行 0.8.7，健康检查 ready；0.8.6 镜像与 compose 备份保留可回滚。
- 已知未修：`strm_sync` 的失效清理仍会因账号级 `sync_revision` 在扫描期间变化而跳过（`sync.py`），失效 STRM 至今未被真正清理过；裸集号文件（`27.mkv`）仍不做父目录兜底。

## 0.8.6 — 2026-11-03

- PC 端「影视探索」每页 40 个改为 45 个。页面是 9 列网格，40 = 4×9+4，每页最后一行只剩 4 个，看着像加载不全；45 = 5×9 正好填满。移动端（宽度 <640）仍为 30。
- 入库/整理卡片的「来源」收成两种写法：免转存的分享 STRM 标「115网盘分享」，分享链接转存与网盘原有文件统一标「115网盘」。此前分三种（`115 分享 STRM（免转存）` / `115 分享转存` / `115 网盘`），而转存后的文件本来就在自己网盘里，和网盘原有文件不该有两种说法。`cards.py` 的 `media_card()` 里 `transferred` 参数保留在签名中，继续供调用方区分批次来源（留档与排查用），不再影响文案。
- 后端 `per_page` 允许 10–60，无需改动；分页、入库角标与资源查找逻辑不变。
- 验证：前端在镜像内重新编译，`/health/ready` 返回 0.8.6；`test_cards062`、`test_message_templates`、`test_tg_notification_flow` 共 16 项通过（1 项跳过），三个文件已纳入门禁新增的「通知卡片」组；`test_cards062` 的整卡快照与区分语义两处断言同步更新。
- 部署：NAS 的 StrimKeep 容器运行 0.8.6，健康检查 ready；0.8.5 镜像保留可回滚。


## 0.8.5 — 2026-11-03

- 播放日志：`/play115` 与 `/play/share` 此前成功失败都不留痕，`events` 里 play 类记录始终为 0，播放出问题只能看现象。现在两条通道按入口节流 60 秒，记录成功（UA、pickcode、耗时）、失败（错误码）与超时，写库失败不影响播放；新增 `playaddr.ua_label` 把 UA 截到 32 字符写入日志——115 直链与 UA 绑定，排查第一步就是看 UA。
- 取链缓存：Emby 每探测一集都会打一次 115（实测 18:40 一分钟 14 次 GetPostedPlaybackInfo + 14 次 ffprobe、14 个不同 pickcode）。现在按 UA 分层 TTL：探测类（Lavf/ffprobe/Emby/Plex/Jellyfin/Kodi）60 秒、真实播放 15 秒；同一 (账号, pickcode, UA) 并发只向 115 取一次链（single-flight），扫一季时探测与播放撞在一起也不会成倍请求。
- 播放地址切换：后端 `POST /api/v1/strm/rebuild` 早已具备「按当前 playback_base 重写全部受管 STRM、不读云端」的能力，前端从未调用。STRM 生成页的播放地址卡片新增按钮，换 IP、加域名、内网切公网时一键把存量 STRM 换成新入口，不必整库重扫。
- 验证：新增 `tests/test_play85.py` 5 项（探测 UA 识别、UA 标签、并发合并、TTL 分层、缓存复用），并入「身份与上下文」门禁组。
- 部署：NAS 的 StrimKeep 容器运行 0.8.5，健康检查 ready；0.8.4 镜像保留可回滚。


## 0.8.4 — 2026-11-03

- 任务进入「等待核查」时，还停在「发现文件」的条目一并落到 `review` 并写入失败原因。此前 TMDB 报错或待核查的文件从没进过 `update_item`，任务已经是「等待核查」，点进去每个文件却还是「发现文件」，看起来像任务没启动。
- 修复 `tests/test_identity_real_names.py::test_tmdb_failure_is_reported_not_guessed`：TMDB 返回 HTTP 500 时条目状态由 `discovered` 变为 `review`。
- 验证：`test_identity_real_names.py`（5 项）与 `test_multilang_identify.py`（8 项）全部通过。
- 部署：NAS 的 StrimKeep 容器运行 0.8.4，健康检查 ready；0.8.3 镜像保留可回滚。


## 0.8.3 — 2026-11-03

- 识别按文件名语言选择 TMDB 检索语言：非中日韩文字用 en-US，中日韩用 zh-CN。此前固定 zh-CN，英文发布名只能拿到中文译名候选，标题相似度全为 0，被判「未找到唯一匹配」：现场 5 集 `Love in Disguise.2026`（tmdb 303745 / 나의 유죄인간）三条候选中正确项得分 0.000，换 en-US 后同一候选精确命中 1.550。
- TMDB 缓存 key 补回 `language`，同一关键词的两种语言结果不再共用一条缓存；默认语言沿用原 key，已有缓存继续有效，无需清库。
- 主语言检索无候选达标时改为跨语言回退；原先的 `retry` 用同 query、同 language、同缓存重查，属于空转。
- 候选全部低于阈值时记录阈值、各候选分数与前三名标题，能直接区分「语言错配」还是「确实没有这部作品」。
- 批次收尾把更早批次里同一文件 ID 的失败项改写为「已由任务 N 补齐」，旧批次没有剩余未完成项时结项；此前重跑成功后旧任务永久停在 partial/「已跳过 N 个视频」，任务列表一直显示部分未完成。
- 验证：新增 `tests/test_multilang_identify.py` 8 项通过；发布门禁核心流水线 49 项及归档、同步、识别、媒体、转存各组通过；生产容器内用真实 TMDB 复核 `Love in Disguise` → tmdb 303745 命中。
- 部署：NAS 的 StrimKeep 容器运行 0.8.3，健康检查 ready；0.8.2 镜像与 compose 备份保留，可回滚。
- 已知未修：TMDB HTTP 500 时条目停留 discovered 未上报（未改动的 0.8.2 镜像同样复现，不在门禁清单内）。


## 0.8.2 — 2026-11-03

- 修复剧集候选年份校验时，TMDB 不存在对应单集（HTTP 404）被误判为服务故障并中断整个整理任务的问题；该候选按年份证据不足处理，继续核对其他候选。
- 区分 TMDB HTTP 状态、超时、网络异常和无效响应，真正的服务故障仍保留任务并按原规则重试，不自动猜测作品。
- 验证：识别、年份校验、TMDB 缓存与整理回归测试通过。

## 0.8.1 — 2026-11-03

- 区分频道分享 STRM 待整理、115 网盘转存和磁力/电驴离线下载，按实际生成、排队、提交或待核查状态反馈；修复同帖多类通知互相覆盖和 STRM 数量显示不准确。
- 机器人自动拆分混合消息中的分享、磁力和电驴，分别按配置处理；/share 可将多个分享分别加入分享 STRM 任务。结果按类型独立反馈。
- 重新整理海报与 STRM 明细的来源和信息层次；海报初始显示 Emby 待核实，确认后编辑原 Telegram 消息为已入库，失败时改为未确认，支持重启恢复和临时失败重试。
- 验证：频道、转存、机器人、海报、STRM 明细、Emby 和通知重试的后台回归通过，使用模拟 Telegram API 验证原消息编辑，未向真实聊天发送测试消息。

## 0.8.0 — 2026-11-03

- 高级文件名识别测试默认留空，示例改为输入提示，方便直接粘贴待测文件名。
- 验证：前端类型检查与生产构建通过。

## 0.7.100 — 2026-11-03

- 媒体总览的观看记录将 Emby 返回的 is playing 显示为“开始播放”，保留原有用户及影片信息。
- 识别测试增加 TMDB 人工查询，复用系统设置中的 Key，支持电影和剧集搜索、ID 与目录命名复制；原文件名测试收进高级工具。
- 验证：前端类型检查、构建及 17 项前端测试通过；使用真实 TMDB 搜索结果检查电脑和手机深色布局。

## 0.7.99 — 2026-11-03

- 修复整理与 STRM 等设置页搜索时卡片无法手动收起的问题；更改搜索词后重新展开匹配项，退出搜索时保留原展开状态。
- 任务页显示当前分类已载入的进行中与已结束数量，缩小空状态；分享列表优化手机端长标题，保存状态支持读屏提示。
- 修复生成 Webhook 令牌后其他未保存修改被误判已保存、复制地址失败仍提示成功、Cron 摘要与令牌保存状态显示不准确的问题。
- 频道检查前提示先保存未保存设置；离线目录保存失败不再提示成功，并说明保存会包含其他目录修改。洗版规则页统一蓝色主题。
- 重新布局频道转存目录、关键词分流与磁力/ED2K 落盘，改善离线分流、目录树和手机端长路径显示。
- 手机端影视探索筛选可收起；修复敏感表单被浏览器误填，以及深色输入提示对比度不足的问题。STRM Webhook 设置改为按需展开并参与搜索。
- 影视整理、STRM 与分享整理的扩展名支持批量输入、去重、快捷添加和逐项移除，并提示格式、数量与分类冲突。
- 深度删除历史移至任务中心；Webhook 复制使用完整地址并提供兼容回退和手动复制。
- 验证：前端类型检查、生产构建和 15 项前端测试通过；本地预览检查主要页面的桌面及手机布局、深浅主题、扩展名编辑和筛选折叠，未执行真实转存或删除。

## 0.7.98 — 2026-11-03

- 离线下载页将提交入口前置，显示直链、磁力和 ED2K 目录设置，并明确标识尚未保存的修改。
- 提升深色模式下探索筛选与总览选中项的文字对比度，补充探索切换和频道输入的无障碍标识。
- 验证：前端类型检查、生产构建和 8 项前端测试通过；浏览器界面未实测。

## 0.7.97 — 2026-11-03

- 优化 115 账号页：优先展示登录状态与容量，将手动 Cookie 登录收进可展开区域。
- 优化影视探索的搜索和筛选、分享文件选中反馈，以及手机端转存记录显示；修正日志内嵌任务时重复出现页面标题。
- 调整频道、离线、整理、STRM、任务、日志和设置页面的间距与窄屏布局，保持蓝色与深浅色主题。
- 验证：前端类型检查、生产构建和 8 项前端测试通过；浏览器界面未实测。

## 0.7.96 — 2026-11-03

- 移除 STRM 管理页顶部“重新整理输出”入口及其前端调用，保留现有全量同步和后端接口。
- 延续蓝色工作台，优化总览、任务、网盘、探索、日志和设置等页面的视觉层次与手机布局；保留深浅色模式。
- 验证：前端类型检查、生产构建和 8 项前端测试通过；浏览器界面未实测。

## 0.7.95 — 2026-11-03

- 整理转存页面目录管理，用紧凑表格展示不同入口与链接类型；按入口编辑，频道通过选择器切换。
- 转存设置按需展开，最近任务精简为三条，完整记录从任务中心查看；保留目录独立配置与未保存修改提示。
- 调整分享 STRM 页面层级，将整理设置与内容管理分开展示，精简内容列表和监控设置，保留独立规则和映射入口。
- 验证：前端类型检查和生产构建通过；目录配置专项 6 项通过。浏览器界面尚未实测。

## 0.7.94 — 2026-11-03

- 分享 STRM 页按筛选、命名、分类、洗版和 Emby 分组显示设置，支持复制当前影视整理规则并保存成分享专用配置。
- 分享的新任务使用独立配置快照；已有安装在首次保存独立设置前保留原来的配置行为，运行中任务继续使用原有快照。
- 分享页显示与影视整理共用的 Emby 路径映射；修复分享关闭重命名后仍改名，以及关闭分类后缺少作品目录的问题。
- 验证：分享管线及洗版等回归 50 项、独立配置及映射专项 3 项通过；前端类型检查及构建通过。

## 0.7.93 — 2026-11-03

- 在转存页增加按 Web、机器人和各频道配置的分享转存、磁力和 ED2K 目录，每一行独立保存，支持选择同一网盘目录。
- 机器人分享和离线目录与 Web 分开；频道的磁力和 ED2K 分开处理，免转存不使用网盘转存目录。
- 旧配置按原有实际生效的目录初始化；免转存频道原先跳过的磁力与 ED2K 保持关闭，直链与关键词分流保留。
- 更新离线及频道页面的目录说明；关键词分流兼容保存后的规则格式。
- 验证：目录配置和迁移专项 6 项、兼容性定向 10 项通过；前端类型检查和构建通过。

## 0.7.92 — 2026-11-03

- 清理收尾时先核实已提交回收的源目录是否仍存在，确认不存在则跳过目录路径读取，避免重复检查误报。
- 目录仍存在、已恢复或状态不能确认时记录目录及原因，保留现场且不重复发送删除请求；修正无法确认时的提示。
- 验证：空目录清理、回收核实与文件状态回归测试 69 项通过。

## 0.7.91 — 2026-11-03

- 同批新剧在完整核查无既有作品、且不涉及洗版替换或淘汰时，操作前直接核对目标路径和空目录，减少重复分类目录读取。
- 目标路径变化、目录出现内容或读取失败时停止批量写入；已有作品与涉及洗版的任务保留完整复核。快速路径不重新枚举其他分类，可能漏检并发新增的同作品目录。
- 增加目标变化、目录读取失败和洗版动作不进入快速路径等回归。

## 0.7.90 — 2026-11-03

- 同一批次中同一作品、同一季的整理复用已有资源核查清单，兼容沿用已有目录与文件名。
- 批量操作前重新读取作品和分类目录并比对已记录文件身份；发现新增、移动、改名或内容变化时停止批量写入。
- 增加已存在作品、同集冲突、命名回退、外部变化和洗版等回归测试。

## 0.7.89 — 2026-11-03

- 增加整理准备的作品识别、季集资料、源文件身份、媒体信息、命名、作品目录刷新和洗版判断耗时及 115 读取统计。
- 增加 TMDB 网络请求和缓存命中统计，按任务隔离并覆盖异常及并发场景。
- 保持整理、写入节流和已有资源核查行为不变。

## 0.7.88 — 2026-11-03

- 分享 STRM 媒体信息保存后继续请求 Emby 原生提取，只有回读确认视频流才标记完成；失败按既有退避机制重试。
- 增加 Emby 确认成功与本地备份存在但同步未确认的回归。

## 0.7.87 — 2026-11-02

- 音轨信息不完整时，命名使用可确认的音轨，无可用信息时省略音轨标签，避免阻断影片整理。
- 音频洗版比较使用可确认音轨，无法比较时记录未知并继续其他规则；不猜测未知编码。
- 补全识别方案阶段失败时的耗时统计，增加混合音轨、未知音轨及整理流程回归。

## 0.7.86 — 2026-11-02

- 即时清理后的目录记录到任务，收尾时核对目录确已不存在后跳过，避免整理成功却误报读取失败；无法确认时保留警告。
- 增加目录已删除、仍存在、核查异常及完整整理流程的离线回归。

## 0.7.85 — 2026-11-02

- 同季整理优先复用季详情中的单集数据；季数据缺失、重复、字段不完整或接口暂不可用时回退单集查询，保留季集身份核验。
- 同批新作品在确认没有旧目录时复用归档核查结果，批量操作前重新读取并检查；有旧版本、状态变化或读取失败时保持完整核查或停止处理。
- 增加混合媒体、季数据异常、请求次数、已有作品、目录变化与故障等离线回归。

## 0.7.84 — 2026-11-02

- 离线下载完成后按提交链接匹配云端任务，并通过产物文件 ID 和目标目录核对归属；证据不足时等待或转待核查，不再用目录差集认领其他文件。
- 合并范围和设置一致的待执行离线整理任务，保留运行中任务的后续处理机会；修复离线文件夹产物漏扫内部视频。
- 任务列表显示离线、转存后整理等来源；整理准备日志分别记录识别、已有资源、目标目录及冲突核查耗时。
- 增加旧任务误认领、部分匹配、目录产物、重复批次及整理回归验证。

## 0.7.83 — 2026-11-02

- TMDB 和云端临时故障时保留尚未处理的文件，并按原有退避机制重试，避免误转未识别目录。
- 修正整理收尾阶段提示，区分后台输出、未识别分流和空目录清理。
- 隔离并发任务的 115 请求账本，明确请求分类和业务动作的统计口径。
- 分享媒体信息在 Emby 无视频流时使用已有的实测探测数据，标记来源并保留文件身份和归属核验。
- 增加临时故障恢复、账本并发隔离及分享媒体信息补齐的离线验证。

## 0.7.82 — 2026-11-02

- 媒体信息补扫支持分享 STRM，核对分享记录、播放身份和路径，并跳过身份或 Emby 路径不明确的条目。
- 保留已有媒体信息文件的归属校验，验证分享补扫、撤销、路径冲突以及常规整理回归。

## 0.7.81 — 2026-11-02

- 按文件 ID 和各自目标名称串行批量改名，每批最多 50 个，保留普通写入间隔。
- 批量移动前逐项核实改名结果；状态异常时回到逐项核对流程。
- 使用模拟接口验证混合影片、部分成功、限流、响应丢失及拆批间隔；真实接口待实际验证。

## 0.7.80 — 2026-11-02

- 移除按固定写入次数强制暂停的机制，保留现有读写请求间隔和实际限流后的冷却。
- 修正整理改名日志，明确文件逐项请求，并在任务中显示当前重命名阶段。
- 请求节奏页面移除不再生效的固定暂停选项；旧配置字段保留兼容。
- 使用隔离模拟验证 48 次写入节奏、限流冷却、批量异常核对及任务并发；真实运行仍需观察。

## 0.7.79 — 2026-11-02

- 将整理扫描的共享锁缩小到单个目录读取，允许等待中的转存在目录读取之间执行。
- 转存接收与核查期间仍阻止整理读取半成品；本轮快照之后的新文件由后续整理处理。
- 增加模拟慢扫描、转存核对和目录快照的并发测试；实际网盘耗时仍需观察。

## 0.7.78 — 2026-11-02

- 间隔整理等待本轮完成后再计时，减少整理期间重复的定时扫描；Cron 保留到点合并行为。
- 修复空扫描退避可能缩短用户设置间隔的问题；相同的等待中手动整理请求合并并提示。
- 任务中心区分整理、转存和同步，目录访问有竞争时显示等待阶段。
- 使用隔离环境验证定时、并发、队列合并、等待登录和前端构建；真实网盘行为仍需运行观察。

## 0.7.77 — 2026-11-02

- 修复整理任务跨轮次复用旧目录缓存的问题；每次新扫描从网盘读取来源目录，避免漏掉外部新增文件。
- 扫描完成后不保留本轮目录缓存；原有请求节奏、定时设置与整理队列规则保持不变。
- 验证：模拟同账号目录变化、根目录与子目录的新文件发现，并通过缓存和调度等 58 项测试；实际运行仍需观察。

## 0.7.76 — 2026-11-02

- 按要求撤回 0.7.70 和 0.7.71 的整理去重、暂停保护、整理定时调度及其配套改动，相关业务代码恢复为 0.7.69 的行为；保留 0.7.72～0.7.75 的分享 STRM 与频道监控、转存改动。
- 同步后端、前端、Compose 镜像标签和 README 版本号为 0.7.76。取消跨未完成整理任务的文件占用与清单冲突检查后，旧任务与新任务可能重复处理同一文件；定时整理恢复旧版空扫描退避行为。
- 验证：补丁基于 0.7.75 干净源码检查和应用，Python 语法及版本一致性检查通过；当前环境缺少项目测试依赖，完整测试未运行，尚未在飞牛或真实 115 环境验收。

## 0.7.75 — 2026-11-02

- 频道落盘转存读取到明确已取消的分享时，将任务标记为跳过并记录原因，不再自动重试。
- 读取分享或首次提交时明确返回取消才标记跳过；登录失效、限流和先前转存结果不明确继续按原规则核查。
- 验证：取消分享、首次提交拒绝与已有不确定意图相关测试 6 项通过。

## 0.7.74 — 2026-11-02

- 频道使用免转存 STRM 模式时，检测并跳过磁力和电驴链接，记录跳过数量。
- 同一消息中的 115 分享链接继续处理，单独的离线下载模式保留原有行为。
- 消息处理后正常推进频道游标，避免不支持的链接阻塞后续消息。
- 验证：频道解析、免转存跳过、离线模式和失败重试相关测试共 34 项通过。

## 0.7.73 — 2026-11-02

- 频道免转存监控遇到明确已取消的分享时记录并跳过，后续帖子中的同一分享不再自动请求。
- 单条消息中的单个分享读取失败时，首次尝试后最多重试 3 次；仍失败则保留原因并跳过，避免阻塞频道游标。
- 同一消息内其他有效分享继续处理；重试次数与跳过记录持久化，容器重启后保留。
- 验证：频道监控、失败恢复、取消分享和分享路径相关测试共 45 项通过。

## 0.7.72 — 2026-11-02

- 分享根目录的视频生成待整理 STRM 时直接放入待整理目录，不额外创建分享码文件夹。
- 分享内已有的目录结构继续保留；同名目标使用独立文件名，避免覆盖现有文件或其他分享的 STRM。
- 既有分享 STRM 的路径和登记不做迁移或删除。
- 验证：分享生成、整理、频道和同名保护相关测试 27 项通过。

## 0.7.71 — 2026-11-02

- 间隔模式的定时整理从上一轮结束时开始计时；整理排队、执行、暂停或等待登录期间不新增定时整理任务。
- 使用用户设置的扫描间隔，不再因空扫描自动拉长间隔；Cron 模式按表达式运行，忙时跳过本次触发。
- 保存整理结束时间，避免通知状态变化或容器重启影响计时；手动整理、转存和 STRM 同步保留各自原有入口。
- 整理设置页面说明间隔计时规则。
- 验证：调度、暂停恢复、重复检查和限流等 69 项测试通过；前端类型检查和生产镜像构建通过。

## 0.7.70 — 2026-11-02

- 整理任务的持久文件清单按 115 账号和文件 ID 防止跨未完成任务重复接管；扫描时跳过已被其他任务负责的文件。
- 既有任务清单存在重叠时暂停并提示核查，避免直接重放可能已发生的云端操作；不自动更改历史任务或网盘数据。
- 整理任务被用户暂停时，定时任务不能绕过暂停创建或执行整理；原有定时任务合并逻辑保留。
- 修复错误提示“操作尚未执行完成”被误判为全局限流；明确的频率限制仍保留冷却机制。
- 外部限制后达到重试时间可重新排队，不必等整理队列完全空闲；保留退避和次数限制。
- 验证：跨任务占用、暂停、限流和调度测试 25 项通过；整理及调度回归 34 项通过。前端类型检查及生产构建通过。
- 部署：NAS 的 StrimKeep 容器运行 0.7.70，健康检查 ready；未恢复任务或修改定时整理设置。
- 现场：任务 1 和 2 已有重复清单，任务 1、2、6、7 保持暂停；定时整理由用户关闭，历史重复数据仍需核查。
