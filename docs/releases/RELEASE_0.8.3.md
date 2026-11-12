# StrimKeep 0.8.3

修复识别环节的语言错配，以及重跑成功后旧批次仍显示「部分未完成」的状态残留。不改业务规则，不改数据库结构。

## 现象

现场一批文件 `Love in Disguise.2026.S01E01-E05.1080p.WEB-DL.mkv`（tvN《我的有罪之人 / 나의 유죄인간》，tmdb 303745）整理时全部落到「未识别」：

```
TMDB 未找到唯一匹配：关键词「Love in Disguise」年份 2026，候选 3 条；已停止自动命名。
已尝试：title、year_evidence、retry。
批次结束 → 已跳过 5 个视频。
```

手工把文件名改成剧名并指定 tmdb id 后，重跑成功（`identity_source=context_tmdb_id`）。但任务列表里旧任务仍然停在「部分未完成 / 已跳过 5 个视频」，看起来像新问题没有解决。

## 根因：搜索语言和文件名语言不是同一种

TMDB `search` 的 `results` 只给「当前语言的标题 + 原始语言标题」，不给其它语言的别名。旧代码把 language 写死 `zh-CN`：

| 环节 | 实际值 |
| --- | --- |
| 文件名 query | `Love in Disguise`（英文） |
| 搜索 language | `zh-CN` |
| 候选标题 | `我的有罪之人` / `나의 유죄인간`、`隐瞒之事`、`เดอะซีรีส์ รัก ลวง หลอน` |
| `scored_candidates` 得分 | `0.000` / `0.000` / `0.000` |
| `match_threshold` | `0.50` → 全部淘汰 |
| `pick()` | `None` →「未找到唯一匹配」 |

用真实 TMDB 对同一关键词做两种语言的对比：

| language | 303745（正确） | 254364 | 102351 |
| --- | --- | --- | --- |
| `zh-CN`（旧） | **0.000 淘汰** | 0.000 淘汰 | 0.000 淘汰 |
| `en-US`（新） | **1.550 精确命中** | 0.875（年份证据不符） | 0.457 淘汰 |

三条候选里正确项唯一具有 2026 首播年、popularity 78（另两条 2.1 / 2.8），但**标题语言错了，打分全是 0**。

连带的三个缺陷：

- `tmdb_cache_key()` 把 `language` 从 key 中剔除，同一关键词的两种语言结果共用一条缓存，互相污染。
- 识别失败前那次 `retry` 用同样的 query、同样的 language、同样的缓存再搜一次，等于空转，只多花一次请求。
- `alternative_titles` 补别名那条路救不了本例：303745 的别名是 `伪装恋爱`、`My Guilty Man`，**不含** `Love in Disguise`（后者在 translations 里，不在 alternative_titles）。

## 改了什么

### 按文件名语言检索

`identify()` 先判定 query 的语言：不含中日韩文字走 `en-US`，否则走 `zh-CN`；`search_tmdb()` 接收显式 `language` 并传给 TMDB。

### 跨语言回退

主语言没有候选达到阈值时，用另一种语言再搜一次并重新评分。中文发布名 + TMDB 只有英文名（或反过来）也能命中。原先空转的 `retry` 由这一步取代。

### 缓存按语言隔离

`language` 重新进入 `tmdb_cache_key()`。默认语言（不显式传）的缓存 key 与原来一致，已有缓存继续有效，不需要清库。

### 失败可诊断

候选全部低于阈值时，`pick()` 把 `threshold`、每个候选的真实分数、前三名标题和首播年写进 `tmdb_attempt`，日志里能直接看到「分数不够」还是「确实没有这部作品」，不再只留一句「候选 3 条」。

## 旧批次状态闭环

`run()` 收尾新增 `close_superseded_batches()`：本批已完成的文件，如果还挂在更早批次的 `skipped`/`pending`/`review` 记录上，那些条目改写为「已由任务 N 补齐」，旧批次在没有剩余未完成项时直接结项，message 追加「已由任务 N 全部补齐」。原始失败原因保留，记录不删除。

这样重跑修好之后，旧任务不会永久停在 `partial` /「已跳过 N 个视频」。

## 验证

- 新增 `backend/tests/test_multilang_identify.py`（8 项）：语言选择、跨语言回退、缓存 key 隔离、分数诊断、闭环的三条路径（全补齐 / 仍有剩余 / 已完成不重复改）。
- NAS 发布门禁：核心流水线 49 项通过；归档与写入、同步与巡检、识别与命名、媒体信息、转存与洗版各组通过。
- 生产容器内用真实 TMDB 复核同一批文件：

```
RESULT tmdb_id=303745 name=我的有罪之人 original=나의 유죄인간 first_air=2026-10-19
  search/tv language=en-US candidates=3
    Love in Disguise(2026)#303745 | True Love in Disguise()#254364 | Love, Lie, Haunt(2019)#102351
  tv/303745 evidence=first_air
  tv/254364 evidence=mismatch
```

## 升级

```bash
cd "/path/to/StrimKeep"
docker compose pull 2>/dev/null; docker compose up -d
curl -s http://127.0.0.1:9000/health/ready
```

镜像内构建：

```bash
cd "/path/to/StrimKeep-dev"
docker build -t strimkeep:0.8.3 --build-arg NPM_REGISTRY=https://registry.npmmirror.com .
```

## 回滚

```bash
cd "/path/to/StrimKeep"
sed -i s/strimkeep:0.8.3/strimkeep:0.8.2/ docker-compose.yml
docker compose up -d
```

`docker-compose.yml.before-0.8.3-*` 保留了切换前的文件，`strimkeep:0.8.2` 镜像仍在本地。

## 已知未修

`tests/test_identity_real_names.py::test_tmdb_failure_is_reported_not_guessed`（TMDB 返回 HTTP 500 时条目停在 `discovered`、未落到 `review`）。该失败在未改动的 0.8.2 镜像上同样复现，且不在 `scripts/verify.sh` 门禁清单内，本次未一并处理。
